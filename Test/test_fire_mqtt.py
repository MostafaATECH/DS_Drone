"""Exercise fire payloads, matching, and retries with synthetic inputs."""

import base64
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import paho.mqtt.client as mqtt

from AI_Vision.camera_stream import CameraFrame
from AI_Vision.fire_episodes import FireIncident
from MQTT_Communication.camera_messages import CameraMessages
from MQTT_Communication.publish_mqtt import MqttPublisher
from MQTT_Communication.replay_gps import GpsReplay
from MQTT_Communication.run_gateway import ROOT, make_camera
from Test.check_fire_mqtt import FireMatches


SETTINGS = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))["mqtt"]
STAMP = "2026-11-21T10:00:00.000Z"
LATER_STAMP = "2026-11-21T10:00:01.000Z"
BOARD = {"stm_connected": False, "streaming": False,
         "stream_state": "DISCONNECTED", "imu_status": "UNAVAILABLE"}
INCIDENT = FireIncident("synthetic-fire-1", STAMP, 0.82, 10.5)


class IncidentTests(unittest.TestCase):
    """Check that incident metadata stays consistent across delays and outages."""

    def test_fix_at_confirmation_and_same_payload_on_retry(self):
        """Keep the fix from frame capture even when newer fixes arrive before retry."""
        replay = GpsReplay(ROOT / "upm_drone_gps_loop.json", freshness_s=5)
        replay.update(10, STAMP, active=True)
        initial_fix = replay.latest_position(10)
        replay.update(11, LATER_STAMP, active=True)
        publisher = Mock()
        publisher.publish_incident.side_effect = [False, True]
        updates = CameraMessages(publisher, replay)
        updates.publish_updates(12, None, (INCIDENT,))
        updates.publish_updates(12.5, None, ())
        replay.update(13, LATER_STAMP, active=True)
        updates.publish_updates(13, None, ())
        first, retry = [call.args[1] for call in publisher.publish_incident.call_args_list]
        self.assertEqual(first, {"event_id": INCIDENT.event_id, "timestamp": STAMP, "type": "fire",
                                 "confidence": 0.82, "position": initial_fix})
        self.assertEqual(first, retry)
        self.assertIsNot(first["position"], initial_fix)
        self.assertEqual(len(updates.pending), 0)

    def test_stale_or_unavailable_position_is_null(self):
        """Send an incident when the board is absent or the last simulated fix is stale."""
        replay = GpsReplay(ROOT / "upm_drone_gps_loop.json", freshness_s=5)
        replay.update(1, STAMP, active=True)
        replay.update(2, STAMP, active=False)
        for gps in (None, replay):
            with self.subTest(gps=gps):
                publisher = Mock()
                updates = CameraMessages(publisher, gps)
                updates.publish_updates(11, None, (INCIDENT,))
                self.assertIsNone(publisher.publish_incident.call_args.args[1]["position"])

    def test_incident_skips_fix_with_later_utc_timestamp(self):
        """Choose the preceding fix when the gateway's UTC clock read was delayed."""
        replay = GpsReplay(ROOT / "upm_drone_gps_loop.json", freshness_s=5)
        replay.update(9, "2026-11-21T09:59:59.001Z", active=True)
        earlier_fix = replay.latest_position(9)
        replay.update(10, "2026-11-21T10:00:00.001Z", active=True)
        publisher = Mock()
        CameraMessages(publisher, replay).publish_updates(11, None, (INCIDENT,))
        self.assertEqual(publisher.publish_incident.call_args.args[1]["position"], earlier_fix)

    def test_disabled_camera_skips_optional_import(self):
        """Do not import the camera module for a camera-disabled gateway."""
        with patch("builtins.__import__", side_effect=AssertionError("camera import was attempted")):
            self.assertIsNone(make_camera({"enabled": False}, "synthetic"))


class PublisherTests(unittest.TestCase):
    """Inspect exact QoS, payload sizes, and status fields using a mocked MQTT client."""

    def setUp(self):
        """Start a synthetic broker connection without opening a socket."""
        factory = self.enterContext(patch("MQTT_Communication.publish_mqtt.mqtt.Client"))
        self.publisher = MqttPublisher({**SETTINGS, "preview_max_bytes": 250})
        self.publisher.connected_at = 10
        self.client = factory.return_value
        self.client.is_connected.return_value = True
        self.client.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)

    def test_camera_health_publishes_without_stm32(self):
        """Report camera health changes independently of measurements and STM32 connection."""
        for now, state in zip((10.0, 10.1, 10.2), ("UNAVAILABLE", "ERROR", "AVAILABLE")):
            self.publisher.publish_update(now, STAMP, BOARD, camera_status=state)
        messages = self.client.publish.call_args_list
        self.assertEqual(len(messages), 3)
        for message, state in zip(messages, ("UNAVAILABLE", "ERROR", "AVAILABLE")):
            self.assertEqual(message.args[0], self.publisher.status_topic)
            self.assertEqual(json.loads(message.args[1]), {"online": True, "timestamp": STAMP,
                                                          **BOARD, "camera_status": state})

    def test_camera_error_does_not_gate_telemetry(self):
        """Publish current telemetry even when the camera reports an error."""
        board = {**BOARD, "stm_connected": True, "streaming": True,
                 "stream_state": "STREAMING", "imu_status": "READY"}
        self.publisher.publish_update(11, STAMP, board, {"synthetic": True}, 11, camera_status="ERROR")
        status, telemetry = self.client.publish.call_args_list
        self.assertEqual(json.loads(status.args[1])["camera_status"], "ERROR")
        self.assertEqual(telemetry.args[0], self.publisher.telemetry_topic)

    def test_preview_rate_size_and_connection(self):
        """Skip inactive, duplicate, oversize, and old-connection frames, without blocking metadata."""
        frame = CameraFrame(b"synthetic-jpeg", ("fire",), INCIDENT.event_id, STAMP, 10.5)
        self.publisher.publish_preview(10.5, replace(frame, event_id=None))
        self.publisher.publish_preview(10.5, replace(frame, detections=()))
        self.client.publish.assert_not_called()
        self.publisher.publish_preview(10.5, frame)
        self.publisher.publish_preview(10.9, replace(frame, captured_monotonic=10.9))
        self.publisher.publish_preview(11.5, frame)
        self.assertEqual(self.client.publish.call_count, 1)
        topic, body = self.client.publish.call_args.args
        self.assertEqual(topic, self.publisher.preview_topic)
        self.assertEqual(self.client.publish.call_args.kwargs, {"qos": 0, "retain": False})
        self.assertEqual(json.loads(body), {"event_id": INCIDENT.event_id, "timestamp": STAMP,
                                           "image_base64": base64.b64encode(frame.jpeg).decode("ascii")})

        with self.assertLogs("MQTT_Communication.publish_mqtt", level="WARNING") as messages:
            # Raw JPEG is under 250 bytes. The Base64 and JSON envelope are over it.
            self.publisher.publish_preview(11.6, replace(frame, captured_monotonic=11.6, jpeg=b"x" * 180))
        self.assertIn("Skipped preview", messages.output[0])
        self.publisher.connected_at = 15
        self.publisher.publish_preview(15.1, replace(frame, captured_monotonic=14.9))
        self.assertEqual(self.client.publish.call_count, 1)
        updates = CameraMessages(self.publisher, replay=None)
        updates.publish_updates(15.1, None, (INCIDENT,))
        self.assertEqual(self.client.publish.call_args.args[0], self.publisher.incident_topic)
        self.assertEqual(self.client.publish.call_args.kwargs, {"qos": 1, "retain": False})
        self.assertEqual(json.loads(self.client.publish.call_args.args[1])["event_id"], INCIDENT.event_id)

    def test_paho_queue_result(self):
        """Leave rejected incidents pending and let Paho retry packets it already queued."""
        self.client.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_QUEUE_SIZE)
        self.assertFalse(self.publisher.publish_incident(11, {"event_id": INCIDENT.event_id}))
        self.client.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_NO_CONN)
        self.assertTrue(self.publisher.publish_incident(12, {"event_id": INCIDENT.event_id}))


class MatchTests(unittest.TestCase):
    """Check that the terminal watcher joins messages in either order."""

    def test_preview_first_is_matched_by_drone(self):
        """Keep a preview until the same drone sends incident metadata for the same ID."""
        matches = FireMatches()
        payload = {"event_id": INCIDENT.event_id}
        preview = SimpleNamespace(topic="synthetic-root/drone01/preview")
        other = SimpleNamespace(topic="synthetic-root/drone02/incident")
        incident = SimpleNamespace(topic="synthetic-root/drone01/incident")
        self.assertIsNone(matches.add(preview, payload))
        self.assertIsNone(matches.add(other, payload))
        self.assertEqual(matches.add(incident, payload), ((incident, payload), (preview, payload)))
