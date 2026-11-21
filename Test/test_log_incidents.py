"""Check validation and restart deduplication with synthetic incidents."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import paho.mqtt.client as mqtt

from MQTT_Communication.log_incidents import IncidentLog, OUTPUT_DIR, log_path, make_incident_client, parse_incident
from project_config import ROOT


SETTINGS = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))["mqtt"]
TOPIC = f"{SETTINGS['topic_root']}/drone01/incident"
STAMP = "2026-11-21T10:00:00.000Z"
EVENT = {"event_id": "synthetic-fire-example", "timestamp": STAMP, "type": "fire",
         "confidence": 0.8, "position": None}


class ValidationTests(unittest.TestCase):
    """Exercise the fire schema and source drone identification."""

    def test_valid_position_and_topic_drone(self):
        """Preserve the simulated fix and take the drone ID from the topic alone."""
        position = {"latitude": 40.39, "longitude": -3.63, "source": "simulated", "fix_timestamp": STAMP}
        payload = json.dumps({**EVENT, "position": position, "drone_id": "payload-id"}).encode()
        record = parse_incident(SETTINGS["topic_root"], TOPIC, payload)
        self.assertEqual(record, {**EVENT, "drone_id": "drone01", "position": position})

    def test_invalid_payloads(self):
        """Reject invalid timestamps, types, confidence, and observer fixes."""
        for payload in (b"\xff", b"{", b"[]", json.dumps({k: v for k, v in EVENT.items() if k != "position"}).encode()):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_incident(SETTINGS["topic_root"], TOPIC, payload)
        for changes in ({"timestamp": "2026-11-21"}, {"type": "smoke"}, {"event_id": " "},
                        {"confidence": True}, {"confidence": float("nan")}, {"confidence": 1.1},
                        {"position": {"latitude": 91, "source": "simulated"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_incident(SETTINGS["topic_root"], TOPIC, json.dumps({**EVENT, **changes}).encode())

    def test_wrong_topic(self):
        """Reject messages for another root, a missing drone, or the wrong topic level."""
        for topic in (TOPIC + "/other", TOPIC.replace("/drone01/", "//"), "other/drone01/incident"):
            with self.subTest(topic=topic), self.assertRaises(ValueError):
                parse_incident(SETTINGS["topic_root"], topic, json.dumps(EVENT).encode())

    def test_output_location(self):
        """Keep runtime JSONL files in the ignored project output directory."""
        self.assertEqual(log_path("Test/output/other.jsonl"), OUTPUT_DIR / "other.jsonl")
        for path in (ROOT.parent / "outside.jsonl", "root.jsonl", "Test/output/not-a-jsonl.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                log_path(path)


class IncidentLogTests(unittest.TestCase):
    """Exercise real JSONL writes and the Paho callbacks without opening sockets."""

    def setUp(self):
        """Use a temporary log under the project's ignored test output directory."""
        directory = OUTPUT_DIR.resolve()
        self.assertTrue(directory.is_relative_to(ROOT))
        directory.mkdir(parents=True, exist_ok=True)
        folder = self.enterContext(TemporaryDirectory(prefix="synthetic-log-", dir=directory))
        self.path = Path(folder) / "incidents.jsonl"

    def test_repeat_different_drone_and_restart(self):
        """Deduplicate one drone and preserve another drone's incident with the same ID."""
        first = {**EVENT, "drone_id": "drone01"}
        second = {**EVENT, "drone_id": "drone02"}
        log = IncidentLog(self.path)
        self.assertTrue(log.append(first))
        self.assertFalse(log.append(first))
        self.assertTrue(log.append(second))
        restarted = IncidentLog(self.path)
        self.assertFalse(restarted.append(first))
        self.assertFalse(restarted.append(second))
        new = {**first, "event_id": "synthetic-next"}
        self.assertTrue(restarted.append(new))
        records = [json.loads(line) for line in self.path.read_bytes().splitlines()]
        self.assertEqual(records, [first, second, new])

    def test_partial_line_is_preserved_and_separated(self):
        """Keep complete IDs when startup encounters a partial JSONL row."""
        first = {**EVENT, "drone_id": "drone01"}
        IncidentLog(self.path).append(first)
        with self.path.open("ab") as log:
            log.write(b'{"broken":')
        with self.assertLogs("MQTT_Communication.log_incidents", level="WARNING"):
            restarted = IncidentLog(self.path)
        self.assertFalse(restarted.append(first))
        second = {**first, "event_id": "synthetic-next"}
        self.assertTrue(restarted.append(second))
        lines = self.path.read_bytes().splitlines()
        self.assertEqual(lines[1], b'{"broken":')
        self.assertEqual(json.loads(lines[2]), second)

    def test_callback_subscribes_and_keeps_processing(self):
        """Request QoS 1 and persist a valid incident after rejecting malformed input."""
        with patch("MQTT_Communication.log_incidents.mqtt.Client", return_value=Mock()):
            client = make_incident_client(SETTINGS, IncidentLog(self.path), "synthetic-logger")
        client.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 1)
        client.on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
        client.subscribe.assert_called_once_with(f"{SETTINGS['topic_root']}/+/incident", qos=1)
        with self.assertLogs("MQTT_Communication.log_incidents", level="WARNING") as warnings:
            client.on_message(client, None, SimpleNamespace(topic=TOPIC, payload=b"not JSON"))
        self.assertIn("Ignored malformed incident", warnings.output[0])
        client.on_message(client, None, SimpleNamespace(topic=TOPIC, payload=json.dumps(EVENT).encode()))
        self.assertEqual(json.loads(self.path.read_bytes()), {**EVENT, "drone_id": "drone01"})
