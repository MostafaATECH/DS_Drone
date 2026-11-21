"""Publish STM32 telemetry, simulated GPS, and optional fire detections over MQTT."""

import argparse
import json
import logging
import math
import time
from datetime import datetime, timezone

from project_config import ROOT, load_config

from .camera_messages import CameraMessages
from .publish_mqtt import MqttPublisher, validate_mqtt_settings
from .read_stm32_serial import Stm32Reader
from .replay_gps import GpsReplay


TELEMETRY_INTERVAL_S = 1.0
POLL_INTERVAL_S = 0.02


def load_settings(root):
    """Read shared defaults and merge any per-PC JSON settings into their sections."""
    settings = load_config(root)

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


def make_camera(settings, drone_id):
    """Create the camera worker only when the local settings enable it."""
    if settings["enabled"] is False:
        return None

    from AI_Vision.camera_stream import CameraWorker, load_camera_settings

    return CameraWorker(load_camera_settings(settings), drone_id)


def main(argv=None):
    """Run the gateway with optional camera capture until Ctrl+C shuts it down."""
    parser = argparse.ArgumentParser(description="Run one DS Drone gateway.")
    parser.add_argument("--camera", choices=("on", "off"), help="override camera.enabled for this run")
    args = parser.parse_args(argv)
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
    if args.camera is not None:
        settings["camera"]["enabled"] = args.camera == "on"
    camera_status = "DISABLED"
    camera = None
    try:
        camera = make_camera(settings["camera"], settings["mqtt"]["drone_id"])
    except (ImportError, OSError, ValueError, OverflowError) as error:
        camera_status = "ERROR"
        logging.error("Camera ERROR: %s", error)
    if camera_status == "DISABLED" and camera is None:
        logging.info("Camera DISABLED")

    camera_messages = CameraMessages(publisher, replay) if camera is not None else None
    previous_camera_status = None
    previous_status = None
    try:
        publisher.start()
        if camera is not None:
            camera.start()
        while True:
            now = time.monotonic()
            timestamp = utc_now()
            status, telemetry = gateway.poll(now, timestamp)
            measurement_at = gateway.last_reported.received_at if telemetry is not None else None
            if camera is not None:
                snapshot = camera.snapshot()
                camera_status = snapshot.state
                state = (snapshot.state, snapshot.detail)
                if state != previous_camera_status:
                    log = logging.warning if camera_status == "ERROR" else logging.info
                    log("Camera %s: %s", *state)
                    previous_camera_status = state
            publisher.publish_update(now, timestamp, status, telemetry, measurement_at,
                                     camera_status=camera_status)
            if camera is not None:
                camera_messages.publish_updates(time.monotonic(), snapshot.frame, camera.take_incidents())
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
        try:
            publisher.close()
        finally:
            if camera is not None and not camera.stop():
                logging.warning("Camera driver did not return before the shutdown timeout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
