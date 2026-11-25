"""Exercise the incident logger on HiveMQ using an isolated synthetic namespace."""

import json
import logging
import sys
import time
from collections import Counter
from contextlib import contextmanager
from threading import Event
from uuid import uuid4

from MQTT_Communication.publish_mqtt import MqttPublisher, drone_topic, validate_mqtt_settings
from project_config import load_config
from Test.check_mqtt import TIMEOUT, require, start_client, wait_for_connection
from tools.log_incidents import IncidentLog, log_path, make_incident_client


@contextmanager
def running_logger(settings, path, client_id):
    """Start a fresh logger and wait for its real QoS 1 subscription acknowledgement."""
    client = make_incident_client(settings, IncidentLog(path), client_id)
    ready = Event()
    log_subscription = client.on_subscribe

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        """Signal the fixture sender when the broker grants the filter."""
        log_subscription(client, userdata, mid, reason_codes, properties)
        if reason_codes and reason_codes[0].value == 1:
            ready.set()

    client.on_subscribe = on_subscribe
    try:
        start_client(client, settings)
        require(ready.wait(TIMEOUT), "incident subscriber did not receive a QoS 1 SUBACK")
        yield
    finally:
        client.disconnect()
        client.loop_stop()


def send_fixture(publisher, topic, payload):
    """Wait for the broker's acknowledgement of one non-retained synthetic message."""
    info = publisher.client.publish(topic, payload, qos=1, retain=False)
    info.wait_for_publish(TIMEOUT)
    require(info.is_published(), f"broker did not acknowledge {topic}")


def wait_for_rows(path, expected):
    """Wait for the receiver to save its final marker and compare all stored keys."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        try:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        except (FileNotFoundError, json.JSONDecodeError):
            rows = []
        keys = Counter((row["drone_id"], row["event_id"]) for row in rows)
        if expected.keys() <= keys.keys():
            require(keys == expected, "stored keys include unexpected rows or duplicates")
            return
        time.sleep(0.05)
    raise TimeoutError(f"the logger did not save the expected records in {path}")


def main():
    """Publish repeated synthetic IDs, restart the logger, and check the same JSONL file."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    settings = load_config()["mqtt"]
    validate_mqtt_settings(settings)
    token = uuid4().hex[:8]
    root = f"{settings['topic_root']}/test-{token}"
    settings = {**settings, "topic_root": root}
    path = log_path(f"Test/output/incident_logger_test_{token}.jsonl")
    event = {"event_id": f"synthetic-fire-{token}", "timestamp": "2026-11-21T10:00:00Z",
             "type": "fire", "confidence": 0.9, "position": None}
    fixture = json.dumps(event)
    first = drone_topic(root, "synthetic01", "incident")
    second = drone_topic(root, "synthetic02", "incident")
    expected = Counter({("synthetic01", event["event_id"]): 1, ("synthetic02", event["event_id"]): 1})
    marker = {**event, "event_id": f"synthetic-marker-{token}"}
    publisher = MqttPublisher({**settings, "client_id": f"ds-il-source-{token}"})
    print(f"SYNTHETIC INCIDENT LOGGER TEST: {root}/+/incident", flush=True)
    try:
        publisher.start()
        wait_for_connection(publisher)
        with running_logger(settings, path, f"ds-il-first-{token}"):
            send_fixture(publisher, first, b"invalid synthetic JSON")
            send_fixture(publisher, first, fixture)
            send_fixture(publisher, first, fixture)
            send_fixture(publisher, second, fixture)
            wait_for_rows(path, expected)
        print("PASS: repeated event stored once for each synthetic drone", flush=True)

        with running_logger(settings, path, f"ds-il-next-{token}"):
            send_fixture(publisher, first, fixture)
            send_fixture(publisher, second, fixture)
            send_fixture(publisher, first, json.dumps(marker))
            expected[("synthetic01", marker["event_id"])] = 1
            wait_for_rows(path, expected)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        publisher.client.disconnect()
        publisher.client.loop_stop()
    print(f"PASS: restart kept the original rows and appended only the new event in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
