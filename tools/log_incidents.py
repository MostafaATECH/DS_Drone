"""Subscribe to fire incidents and append one JSON Lines record per drone and event ID."""

import argparse
import json
import logging
from datetime import datetime
from os import SEEK_END
from pathlib import Path
from uuid import uuid4

import paho.mqtt.client as mqtt

from MQTT_Communication.publish_mqtt import validate_mqtt_settings
from project_config import ROOT, load_config


LOGGER = logging.getLogger(__name__)
OUTPUT_DIR = ROOT / "Test" / "output"
DEFAULT_OUTPUT = OUTPUT_DIR / "incidents.jsonl"


def utc_timestamp(value, field):
    """Check a UTC ISO 8601 timestamp ending in Z and return the original string."""
    if not isinstance(value, str) or value[10:11] != "T" or not value.endswith("Z"):
        raise ValueError(f"{field} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} is not a valid timestamp") from error
    return value


def number_in_range(value, low, high, field):
    """Accept finite JSON numbers within the supplied bounds."""
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(f"{field} must be a number from {low} to {high}")
    return value


def validate_position(position):
    """Check a simulated observer fix, or accept null when no fresh fix exists."""
    if position is None:
        return None
    if not isinstance(position, dict) or position.get("source") != "simulated":
        raise ValueError("position must be null or a simulated fix")
    return {
        "latitude": number_in_range(position.get("latitude"), -90, 90, "position.latitude"),
        "longitude": number_in_range(position.get("longitude"), -180, 180, "position.longitude"),
        "source": "simulated",
        "fix_timestamp": utc_timestamp(position.get("fix_timestamp"), "position.fix_timestamp"),
    }


def parse_incident(topic_root, topic, payload):
    """Validate one MQTT incident and take its drone ID from the topic."""
    prefix = f"{topic_root}/"
    drone_id, _, kind = topic.removeprefix(prefix).partition("/")
    if not topic.startswith(prefix) or not drone_id or kind != "incident":
        raise ValueError("expected an incident topic with one drone ID")
    incident = json.loads(payload.decode("utf-8"))
    if not isinstance(incident, dict):
        raise ValueError("incident payload must be a JSON object")
    event_id = incident.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id must be a nonempty string")
    if incident.get("type") != "fire":
        raise ValueError("type must be fire")
    if "position" not in incident:
        raise ValueError("position is required and may be null")
    return {
        "drone_id": drone_id,
        "event_id": event_id,
        "timestamp": utc_timestamp(incident.get("timestamp"), "timestamp"),
        "type": "fire",
        "confidence": number_in_range(incident.get("confidence"), 0, 1, "confidence"),
        "position": validate_position(incident["position"]),
    }


def read_saved_ids(path):
    """Load saved drone/event pairs and report bad lines without discarding healthy rows."""
    keys = set()
    if not path.exists():
        return keys
    with path.open("rb") as log:
        for number, line in enumerate(log, 1):
            try:
                record = json.loads(line.decode("utf-8"))
                key = (record["drone_id"], record["event_id"])
                if not all(isinstance(value, str) and value.strip() for value in key):
                    raise ValueError("missing drone or event ID")
            except (ValueError, KeyError, TypeError, RecursionError):
                LOGGER.warning("Skipped invalid saved record in %s at line %d", path, number)
                continue
            keys.add(key)
    return keys


class IncidentLog:
    """Maintain the saved ID index for a single writer to a local JSON Lines file."""

    def __init__(self, path):
        """Read IDs from earlier runs before listening for new incidents."""
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.saved_ids = read_saved_ids(path)

    def append(self, record):
        """Append one row and mark it saved only after the file write completes."""
        key = (record["drone_id"], record["event_id"])
        if key in self.saved_ids:
            return False
        line = (json.dumps(record, allow_nan=False) + "\n").encode("utf-8")
        with self.path.open("a+b") as log:
            if log.seek(0, SEEK_END):
                log.seek(-1, SEEK_END)
                if log.read(1) != b"\n":
                    # Keep a partial last row separate from the next valid record.
                    line = b"\n" + line
            log.write(line)
        self.saved_ids.add(key)
        return True


def make_incident_client(settings, incident_log, client_id):
    """Configure a Paho subscriber with a single incident filter and local log callbacks."""
    root = settings["topic_root"]
    topic_filter = f"{root}/+/incident"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                         protocol=mqtt.MQTTv311, clean_session=True)
    if settings["username"]:
        client.username_pw_set(settings["username"], settings["password"])
    if settings["tls"]:
        client.tls_set()
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        """Subscribe again whenever the broker accepts a new connection."""
        if reason_code.is_failure:
            LOGGER.warning("MQTT connection refused: %s", reason_code)
            return
        result, _ = client.subscribe(topic_filter, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.warning("Could not subscribe to %s: %s", topic_filter, result)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        """Report readiness after the broker confirms the subscription."""
        if reason_codes and reason_codes[0].value == 1:
            LOGGER.info("Listening on %s at QoS 1 as %s", topic_filter, client_id)
        else:
            LOGGER.warning("Broker did not grant QoS 1 on %s: %s", topic_filter, reason_codes)

    def on_message(client, userdata, message):
        """Validate and log one incident, leaving the connection alive on bad input."""
        try:
            record = parse_incident(root, message.topic, message.payload)
        except (ValueError, RecursionError) as error:
            LOGGER.warning("Ignored malformed incident on %s: %s", message.topic, error)
            return
        try:
            if incident_log.append(record):
                LOGGER.info("Saved %s %s", record["drone_id"], record["event_id"])
            else:
                LOGGER.info("Skipped duplicate %s %s", record["drone_id"], record["event_id"])
        except OSError as error:
            LOGGER.error("Could not append to %s: %s", incident_log.path, error)

    def on_connect_fail(client, userdata):
        """Report when the broker cannot be reached and Paho will try again."""
        LOGGER.warning("Could not reach MQTT broker %s:%d. Retrying.", settings["host"], settings["port"])

    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    return client


def log_path(value):
    """Keep incident files under the repository's ignored test output directory."""
    path = (ROOT / value).resolve()
    if not path.is_relative_to(OUTPUT_DIR) or path.suffix.lower() != ".jsonl":
        raise ValueError("Output must be a .jsonl file under Test/output")
    return path


def main():
    """Connect the independent incident consumer and run until Ctrl+C."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSONL file under Test/output")
    parser.add_argument("--client-id", help="unique MQTT client ID for this logger")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        settings = load_config()["mqtt"]
        validate_mqtt_settings(settings)
        incident_log = IncidentLog(log_path(args.output))
    except (OSError, ValueError) as error:
        LOGGER.error("Cannot start incident logger: %s", error)
        return 1

    client_id = args.client_id or f"dsdrone-log-{uuid4().hex[:10]}"
    client = make_incident_client(settings, incident_log, client_id)
    LOGGER.info("Writing incidents to %s", incident_log.path)
    try:
        client.connect_async(settings["host"], settings["port"], keepalive=settings["keepalive_s"])
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        LOGGER.info("Incident logger stopped")
    finally:
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
