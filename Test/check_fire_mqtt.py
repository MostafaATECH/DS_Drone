"""Check the fire topics with synthetic messages or watch one real gateway from PowerShell."""

import argparse
import base64
import json
import sys
import time
from dataclasses import replace
from queue import Empty
from uuid import uuid4

from hardware_data_collection.collect_drone_data import ROOT, load_settings, utc_now
from hardware_data_collection.prepare_fire_reports import FireReports
from MQTT_Communication.publish_mqtt import MqttPublisher
from Test.check_mqtt import make_client, require, subscribe, wait_for_connection
from Test.test_model_image import preview_path, save_preview


class FireMatches:
    """Match incident metadata and preview by topic drone ID and payload event ID."""

    def __init__(self):
        """Keep only a small set of events while waiting for a match."""
        self.events = {}

    def add(self, message, payload):
        """Return a pair once both kinds have arrived, in either order."""
        levels = message.topic.rsplit("/", 2)
        if len(levels) != 3:
            return None
        _, drone_id, kind = levels
        event_id = payload.get("event_id")
        if kind not in ("incident", "preview") or not isinstance(event_id, str) or not event_id:
            return None
        key = (drone_id, event_id)
        parts = self.events.setdefault(key, {})
        parts[kind] = (message, payload)
        if len(self.events) > 16:
            del self.events[next(iter(self.events))]
        if "incident" in parts and "preview" in parts:
            return parts["incident"], parts["preview"]
        return None


def wait_for_match(inbox, max_preview_bytes, timeout=None, *, announce_incidents=False):
    """Print useful status changes and return the first complete incident/preview pair."""
    matches = FireMatches()
    previous_status = None
    deadline = time.monotonic() + timeout if timeout is not None else None
    while deadline is None or time.monotonic() < deadline:
        try:
            message = inbox.get(timeout=0.25)
        except Empty:
            continue

        kind = message.topic.rsplit("/", 1)[-1]
        if kind not in ("status", "incident", "preview"):
            continue
        if kind == "preview" and len(message.payload) > max_preview_bytes:
            continue
        try:
            payload = json.loads(message.payload)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if kind == "status":
            status = (payload.get("online"), payload.get("camera_status"), payload.get("stream_state"))
            if status != previous_status:
                print(f"Status: online={status[0]} camera={status[1]} STM32={status[2]}", flush=True)
                previous_status = status
            continue
        if kind == "incident" and announce_incidents:
            print("Received incident", json.dumps(payload), flush=True)
        pair = matches.add(message, payload)
        if pair is not None:
            return pair
    raise TimeoutError("No matching incident and preview arrived")


def jpeg_from_preview(payload):
    """Decode a received Base64 preview and check that it is a JPEG byte stream."""
    image = payload.get("image_base64")
    require(isinstance(image, str), "preview image_base64 is missing")
    jpeg = base64.b64decode(image, validate=True)
    require(jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9"), "preview was not a JPEG")
    return jpeg


def watch_gateway(settings, drone_id):
    """Watch one configured drone until a matching preview can be saved locally."""
    token = uuid4().hex[:8]
    root = f"{settings['topic_root']}/{drone_id}"
    observer = make_client(settings, f"ds-firewatch-{token}")
    try:
        inbox = subscribe(observer, settings, f"{root}/#")
        print(f"Watching {root}. Start the gateway, then put your fire photo in view. Ctrl+C quits.", flush=True)
        (_, incident), (_, preview) = wait_for_match(inbox, settings["preview_max_bytes"],
                                                     announce_incidents=True)
        output = preview_path(ROOT / "Test" / "output" / "mqtt_fire_preview.jpg")
        save_preview(output, jpeg_from_preview(preview))
        print(f"Matched {drone_id} {incident['event_id']}")
        print(f"Saved received preview: {output}")
    finally:
        observer.disconnect()
        observer.loop_stop()


def check_synthetic(settings):
    """Check actual HiveMQ delivery using generated test pixels and an isolated topic."""
    import cv2
    import numpy as np

    from AI_Vision.camera_stream import CameraFrame
    from AI_Vision.fire_detection import Detection, encode_preview
    from AI_Vision.fire_episodes import FireIncident

    token = uuid4().hex[:10]
    test_root = f"{settings['topic_root']}/test-{token}"
    print(f"SYNTHETIC FIRE TEST: {test_root}/synthetic/#", flush=True)
    publisher = MqttPublisher({**settings, "topic_root": test_root, "drone_id": "synthetic",
                               "client_id": f"ds-fixture-{token}"})
    observer = make_client(settings, f"ds-firecheck-{token}")
    late_subscriber = make_client(settings, f"ds-latefire-{token}")
    image = np.zeros((100, 240, 3), dtype=np.uint8)
    cv2.putText(image, "SYNTHETIC TEST", (15, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    detection = Detection("fire", 0.9, (20, 25, 200, 54))
    jpeg = encode_preview(image, (detection,))
    incident = FireIncident(f"synthetic-fire-{token}", utc_now(), 0.9, time.monotonic())
    updates = FireReports(publisher, replay=None)
    try:
        inbox = subscribe(observer, settings, f"{test_root}/#")
        # Queue the incident while disconnected, then send it using the same detection ID.
        updates.publish_updates(time.monotonic(), None, (incident,))
        publisher.start()
        wait_for_connection(publisher)
        time.sleep(max(0, updates.retry_at - time.monotonic()))
        now = time.monotonic()
        frame = CameraFrame(jpeg, (detection,), incident.event_id, utc_now(), now)
        updates.publish_updates(now, frame, ())

        (incident_msg, metadata), (preview_msg, preview) = wait_for_match(
            inbox, settings["preview_max_bytes"], timeout=8)
        require(incident_msg.qos == 1 and not incident_msg.retain, "incident QoS or retain flag differed")
        require(preview_msg.qos == 0 and not preview_msg.retain, "preview QoS or retain flag differed")
        require(metadata == {"event_id": incident.event_id, "timestamp": incident.timestamp,
                             "type": "fire", "confidence": 0.9, "position": None}, "incident payload differed")
        require(set(preview) == {"event_id", "timestamp", "image_base64"}
                and preview["timestamp"] == frame.captured_at, "preview payload differed")
        require(jpeg_from_preview(preview) == jpeg, "received image did not round-trip byte-for-byte")
        print("PASS: matched QoS 1 incident and QoS 0 preview by drone ID and event ID")
        print(f"PASS: Base64 decoded to the original {len(jpeg)}-byte synthetic JPEG")
        late_inbox = subscribe(late_subscriber, settings, f"{test_root}/#")
        try:
            unexpected = late_inbox.get(timeout=0.5)
        except Empty:
            print("PASS: a late subscriber received no retained fire messages")
        else:
            raise ValueError(f"unexpected message on {unexpected.topic} for a late subscriber")
    finally:
        # Closing with a normal DISCONNECT keeps synthetic test topics free of retained status.
        publisher.client.disconnect()
        publisher.client.loop_stop()
        for client in (late_subscriber, observer):
            client.disconnect()
            client.loop_stop()


def main():
    """Run a synthetic public-broker check, or watch a selected drone with --watch."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="watch a real gateway without publishing")
    parser.add_argument("--drone-id", help="drone to watch (defaults to the local MQTT drone ID)")
    args = parser.parse_args()
    try:
        settings = load_settings(ROOT)["mqtt"]
        if args.watch:
            drone_id = args.drone_id or settings["drone_id"]
            require(not any(character in drone_id for character in "/+#\0"), "invalid drone ID")
            watch_gateway(settings, drone_id)
        else:
            check_synthetic(settings)
    except KeyboardInterrupt:
        print("Stopped watching.")
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        print(f"Fire MQTT check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
