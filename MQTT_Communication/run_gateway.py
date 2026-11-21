"""Read STM32 telemetry, add simulated GPS, and publish gateway updates over MQTT."""

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from .publish_mqtt import MqttPublisher, validate_mqtt_settings
from .read_stm32_serial import Stm32Reader
from .replay_gps import GpsReplay


ROOT = Path(__file__).resolve().parent.parent
TELEMETRY_INTERVAL_S = 1.0
POLL_INTERVAL_S = 0.02


def load_settings(root):
    """Read shared defaults and merge any per-PC JSON settings into their sections."""
    settings = json.loads((root / "config.example.json").read_text(encoding="utf-8-sig"))
    override_path = root / "config.local.json"
    if override_path.exists():
        overrides = json.loads(override_path.read_text(encoding="utf-8-sig"))
        if not isinstance(overrides, dict):
            raise ValueError("config.local.json must contain an object")
        for section, values in overrides.items():
            if section not in settings or not isinstance(values, dict):
                raise ValueError(f"invalid configuration section: {section}")
            unknown = values.keys() - settings[section].keys()
            if unknown:
                raise ValueError(f"unknown {section} setting: {', '.join(sorted(unknown))}")
            settings[section].update(values)

    for key in ("port", "usb_serial_number"):
        value = settings["serial"][key]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"serial.{key} must be a nonempty string or null")
    baud_rate = settings["serial"]["baud_rate"]
    if type(baud_rate) is not int or baud_rate <= 0:
        raise ValueError("serial.baud_rate must be a positive integer")
    for section, keys in (("serial", ("heartbeat_timeout_s", "retry_interval_s", "measurement_freshness_s")),
                          ("gps", ("freshness_s",))):
        for key in keys:
            value = settings[section][key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{section}.{key} must be a positive number")
    if not isinstance(settings["gps"]["path"], str) or not settings["gps"]["path"].strip():
        raise ValueError("gps.path must be a nonempty string")
    validate_mqtt_settings(settings["mqtt"])
    return settings


def utc_now():
    """Return the current UTC time in ISO 8601 format with a Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_telemetry(measurement, position):
    """Assemble the agreed payload, converting the raw gyro axes from deg/s to rad/s."""
    sample = measurement.sample
    return {
        "timestamp": measurement.timestamp,
        "tracker_state": sample.tracker_state,
        "position": position,
        "accel_m_s2": dict(zip("xyz", sample.accel_m_s2)),
        "gyro_rad_s": dict(zip("xyz", map(math.radians, sample.gyro_deg_s))),
    }


class Gateway:
    """Combine the serial and GPS inputs into status and telemetry updates."""

    def __init__(self, reader, replay):
        """Start with no previous telemetry. A missing replay keeps position unavailable."""
        self.reader = reader
        self.replay = replay
        self.last_reported = None
        self.next_report_at = 0.0

    def poll(self, now, timestamp):
        """Return current board status and, when due, one fresh telemetry payload."""
        self.reader.poll(now, timestamp)
        measurement = self.reader.fresh_measurement(now)
        status = self.reader.status(now)
        position = None
        if self.replay is not None:
            self.replay.update(now, timestamp, active=measurement is not None)
            position = self.replay.latest_position(now)

        telemetry = None
        if measurement is None:
            self.next_report_at = now
        elif measurement is not self.last_reported and now >= self.next_report_at:
            telemetry = build_telemetry(measurement, position)
            self.last_reported = measurement
            self.next_report_at = now + TELEMETRY_INTERVAL_S
        return status, telemetry


def main():
    """Run serial polling and MQTT publishing until Ctrl+C closes both connections."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        settings = load_settings(ROOT)
    except (OSError, ValueError, OverflowError) as error:
        logging.error("Gateway configuration: %s", error)
        return 1

    route_path = ROOT / settings["gps"]["path"]
    try:
        replay = GpsReplay(route_path, settings["gps"]["freshness_s"])
    except (OSError, ValueError, OverflowError, RecursionError) as error:
        logging.warning("GPS replay unavailable (%s): %s", route_path, error)
        replay = None

    reader = Stm32Reader(settings["serial"])
    gateway = Gateway(reader, replay)
    publisher = MqttPublisher(settings["mqtt"])
    previous_status = None
    try:
        publisher.start()
        while True:
            now = time.monotonic()
            timestamp = utc_now()
            status, telemetry = gateway.poll(now, timestamp)
            measurement_at = gateway.last_reported.received_at if telemetry is not None else None
            publisher.publish_update(now, timestamp, status, telemetry, measurement_at)
            local_status = {"port": reader.port_name, **status}
            if local_status != previous_status:
                print("STM32", json.dumps(local_status), flush=True)
                previous_status = local_status
            if telemetry is not None:
                print("Telemetry", json.dumps(telemetry), flush=True)
            time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print("Gateway stopped.")
    finally:
        reader.close()
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
