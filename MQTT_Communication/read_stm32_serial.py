"""Read framed IMU data and track the USB connection to the STM32."""

import logging
import math
import re
from dataclasses import dataclass

import serial
from serial.tools import list_ports


LOGGER = logging.getLogger(__name__)
MAX_LINE_BYTES = 128
STATUS_NAMES = {b"CALIBRATING", b"STREAMING", b"PAUSED", b"ERROR"}
TRACKER_NAMES = {b"IDLE", b"FLYING", b"PROBLEM"}
DECIMAL = re.compile(rb"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
STLINK_USB_IDS = (0x0483, 0x374E)
IMU_STATUS = {
    "DISCONNECTED": "UNAVAILABLE",
    "CALIBRATING": "CALIBRATING",
    "STREAMING": "READY",
    "PAUSED": "READY",
    "ERROR": "ERROR",
}


@dataclass(frozen=True)
class ImuSample:
    """Axes in firmware units, acceleration in m/s² and gyro in deg/s."""

    tracker_state: str
    accel_m_s2: tuple
    gyro_deg_s: tuple


@dataclass(frozen=True)
class TimedSample:
    """A parsed sample with the gateway's monotonic and UTC receipt times."""

    sample: ImuSample
    received_at: float
    timestamp: str


def parse_line(line):
    """Read one line without LF. Return a status, an IMU sample, or None."""
    if len(line) > MAX_LINE_BYTES:
        return None
    fields = line.removesuffix(b"\r").split(b",")
    if fields[0] != b"$DS1":
        return None
    if len(fields) == 3 and fields[1] == b"S" and fields[2] in STATUS_NAMES:
        return fields[2].decode("ascii")
    if len(fields) != 9 or fields[1] != b"M" or fields[2] not in TRACKER_NAMES:
        return None
    if any(not DECIMAL.fullmatch(field) for field in fields[3:]):
        return None
    axes = tuple(float(field) for field in fields[3:])
    if not all(math.isfinite(value) for value in axes):
        return None
    return ImuSample(fields[2].decode("ascii"), axes[:3], axes[3:])


class FrameParser:
    """Buffer split lines and drop overlong lines through their next LF."""

    def __init__(self):
        """Start with an empty receive buffer."""
        self.pending = b""
        self.discarding = False

    def feed(self, data):
        """Yield valid frames in order from a chunk of serial bytes."""
        parts = data.split(b"\n")
        for part in parts[:-1]:
            if not self.discarding and len(self.pending) + len(part) <= MAX_LINE_BYTES:
                frame = parse_line(self.pending + part)
                if frame is not None:
                    yield frame
            self.pending = b""
            self.discarding = False

        if len(self.pending) + len(parts[-1]) > MAX_LINE_BYTES:
            self.pending = b""
            self.discarding = True
        if not self.discarding:
            self.pending += parts[-1]


def find_stm32_port(ports, port_name=None, usb_serial_number=None):
    """Select one matching port. Automatic search only accepts the board's ST-LINK USB IDs."""
    if port_name:
        matches = [port for port in ports if port.device.casefold() == port_name.casefold()]
    else:
        matches = [port for port in ports if (port.vid, port.pid) == STLINK_USB_IDS]
    if usb_serial_number:
        matches = [port for port in matches if port.serial_number == usb_serial_number]
    return matches[0] if len(matches) == 1 else None


class Stm32Reader:
    """Poll serial without blocking and keep one fresh measurement and board status."""

    def __init__(self, settings):
        """Remember port settings and start disconnected until a valid frame arrives."""
        self.settings = settings
        self.usb_serial_number = settings["usb_serial_number"]
        self.connection = None
        self.port_name = None
        self.parser = FrameParser()
        self.stream_state = "DISCONNECTED"
        self.measurement = None
        self.last_frame_at = 0.0
        self.retry_at = 0.0

    def close(self):
        """Release the handle and discard all data from this serial session."""
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                LOGGER.debug("Serial handle was lost before it could be closed.")
        self.connection = None
        self.port_name = None
        self.parser = FrameParser()
        self.stream_state = "DISCONNECTED"
        self.measurement = None

    def _open(self, port, now):
        """Open the selected port at 8N1 and discard bytes queued before opening."""
        self.connection = serial.Serial(baudrate=self.settings["baud_rate"], timeout=0)
        self.connection.port = port.device
        self.connection.dtr = False
        self.connection.rts = False
        self.connection.open()
        self.connection.reset_input_buffer()
        self.port_name = port.device
        self.last_frame_at = now
        self.usb_serial_number = self.usb_serial_number or port.serial_number

    def _accept(self, frame, now, timestamp):
        """Apply status frames and record measurements only when streaming is allowed."""
        if isinstance(frame, str):
            self.stream_state = frame
            if frame != "STREAMING":
                self.measurement = None
        elif self.stream_state in ("DISCONNECTED", "STREAMING"):
            # A measurement can be the first frame seen when joining a running board.
            self.stream_state = "STREAMING"
            self.measurement = TimedSample(frame, now, timestamp)
        else:
            return
        self.last_frame_at = now

    def poll(self, now, timestamp):
        """Try a due reconnect, read one batch, and time out an unresponsive board."""
        if self.connection is None and now < self.retry_at:
            return
        try:
            if self.connection is None:
                self.retry_at = now + self.settings["retry_interval_s"]
                port = find_stm32_port(list_ports.comports(), self.settings["port"],
                                       self.usb_serial_number)
                if port is None:
                    return
                self._open(port, now)

            if now - self.last_frame_at >= self.settings["heartbeat_timeout_s"]:
                LOGGER.warning("No valid STM32 frames on %s before timeout.", self.port_name)
            else:
                for frame in self.parser.feed(self.connection.read(4096)):
                    self._accept(frame, now, timestamp)
                return
        except (OSError, ValueError) as error:
            LOGGER.warning("STM32 serial: %s", error)

        self.close()
        self.retry_at = now + self.settings["retry_interval_s"]

    def fresh_measurement(self, now):
        """Return the last valid sample only while it is within the freshness limit."""
        if (self.stream_state == "STREAMING" and self.measurement is not None
                and now - self.measurement.received_at <= self.settings["measurement_freshness_s"]):
            return self.measurement
        return None

    def status(self, now):
        """Return the board fields used by the local gateway and future MQTT status."""
        return {
            "stm_connected": self.stream_state != "DISCONNECTED",
            "streaming": self.fresh_measurement(now) is not None,
            "stream_state": self.stream_state,
            "imu_status": IMU_STATUS[self.stream_state],
        }
