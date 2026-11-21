"""Check the serial and GPS gateway with synthetic byte streams and a fake clock."""

import json
import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from MQTT_Communication.read_stm32_serial import FrameParser, Stm32Reader, find_stm32_port
from MQTT_Communication.replay_gps import GpsReplay, load_route
from MQTT_Communication.run_gateway import ROOT, Gateway, load_settings, utc_now


SAMPLE = b"$DS1,M,IDLE,0.12,-0.04,9.81,0.57,1.15,-0.57\r\n"
STAMP = "2026-11-21T10:00:00.000Z"
NEXT_STAMP = "2026-11-21T10:00:10.000Z"
ROUTE = ROOT / "upm_drone_gps_loop.json"
DEFAULT_SETTINGS = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))


def fake_port(device, usb_serial, vid=0x0483, pid=0x374E):
    """Make a synthetic pySerial port description without opening hardware."""
    return SimpleNamespace(device=device, serial_number=usb_serial, vid=vid, pid=pid)


class ParserTests(unittest.TestCase):
    """Exercise split lines, framing, and malformed synthetic data."""

    def test_split_crlf_and_lf(self):
        """Decode a split status and measurement in order, leaving values in wire units."""
        parser = FrameParser()
        self.assertEqual(list(parser.feed(b"debug\r\n$DS1,S,STREAM")), [])
        status, sample = list(parser.feed(b"ING\n" + SAMPLE))
        self.assertEqual(status, "STREAMING")
        self.assertEqual(sample.tracker_state, "IDLE")
        self.assertEqual(sample.accel_m_s2, (0.12, -0.04, 9.81))
        self.assertEqual(sample.gyro_deg_s, (0.57, 1.15, -0.57))

    def test_reject_and_resynchronize(self):
        """Drop malformed or oversized lines without interpreting their suffix as a frame."""
        parser = FrameParser()
        malformed = (SAMPLE.replace(b"0.12", b"NaN") + SAMPLE.replace(b"0.12", b" 1")
                     + SAMPLE.replace(b"IDLE", b"UNKNOWN") + SAMPLE.replace(b",-0.57", b"")
                     + b"$DS1,S,DISCONNECTED\n$DS1,S,ERROR,extra\n\xff\n")
        self.assertEqual(list(parser.feed(malformed + b"X" * 129)), [])
        self.assertEqual(list(parser.feed(b"$DS1,S,STREAMING\n$DS1,S,PAUSED\r\n")), ["PAUSED"])


class GpsTests(unittest.TestCase):
    """Check the supplied route's timing and unavailable-position handling."""

    def test_interval_freeze_expiry_and_wrap(self):
        """Freeze across a long pause, expire the fix, then resume and wrap in order."""
        replay = GpsReplay(ROUTE, 5)
        self.assertEqual((len(replay.positions), replay.interval_s, replay.loop), (120, 1, True))
        replay.update(0, STAMP, active=False)
        self.assertIsNone(replay.latest_position(0))
        replay.update(1, STAMP, active=True)
        first = replay.latest_position(1)
        self.assertEqual(first["latitude"], 40.3899329)
        replay.update(1.5, NEXT_STAMP, active=True)
        self.assertEqual(replay.latest_position(1.5), first)
        replay.update(2, STAMP, active=False)
        self.assertEqual(replay.latest_position(6), first)
        self.assertIsNone(replay.latest_position(7))
        replay.update(10, NEXT_STAMP, active=True)
        self.assertEqual(replay.latest_position(10)["longitude"], -3.6292825)
        self.assertEqual(replay.latest_position(10)["fix_timestamp"], NEXT_STAMP)
        for now in range(11, 129):
            replay.update(now, NEXT_STAMP, active=True)
        self.assertEqual(replay.latest_position(128)["longitude"], -3.6294061)
        replay.update(129, NEXT_STAMP, active=True)
        self.assertEqual(replay.latest_position(129)["longitude"], first["longitude"])

    def test_invalid_routes(self):
        """Reject damaged JSON, empty samples, and an out-of-range coordinate."""
        bad_position = {"latitude": 91, "longitude": 0, "source": "simulated"}
        for content in ("not JSON", '{"samples": []}',
                        json.dumps({"sample_interval_ms": 1000, "loop": True,
                                    "samples": [{"position": bad_position}]})):
            with self.subTest(content=content), patch.object(Path, "read_text", return_value=content):
                with self.assertRaises(ValueError):
                    load_route(ROUTE)


class GatewayTests(unittest.TestCase):
    """Drive the real gateway state machine with a simulated serial connection."""

    def test_port_selection(self):
        """Select the intended identification or explicit override, never guess among devices."""
        other = fake_port("COM4", "other", vid=0x0403)
        board = fake_port("COM3", "stm32")
        second = fake_port("COM8", "different-stlink")
        self.assertIsNone(find_stm32_port([other]))
        self.assertIsNone(find_stm32_port([board, second]))
        self.assertIs(find_stm32_port([board, other]), board)
        self.assertIs(find_stm32_port([board, second], usb_serial_number="stm32"), board)
        self.assertIs(find_stm32_port([other], port_name="com4"), other)

    def test_pause_timeout_and_reopen(self):
        """Discard old readings on pause and USB loss, then follow the same board to a new COM port."""
        reader = Stm32Reader(DEFAULT_SETTINGS["serial"])
        gateway = Gateway(reader, GpsReplay(ROUTE, 5))
        original = Mock()
        original.read.side_effect = [b"STM32 debug\n", b"$DS1,S,CALIBRATING\n" + SAMPLE,
                                     b"$DS1,S,STREAMING\n", SAMPLE, b"$DS1,S,PAUSED\n" + SAMPLE,
                                     b"$DS1,S,PAUSED\r\n", b"debug\n$DS1,S,incomplete",
                                     OSError("synthetic USB removal")]
        reopened = Mock()
        reopened.read.side_effect = [b"STREAMING\n", SAMPLE, b"$DS1,S,STREAMING\n"]
        old_port = fake_port("COM3", "stm32")
        new_port = fake_port("COM9", "stm32")
        second = fake_port("COM4", "other")
        with (patch("MQTT_Communication.read_stm32_serial.list_ports.comports",
                    side_effect=[[old_port], [new_port, second]]),
              patch("MQTT_Communication.read_stm32_serial.serial.Serial",
                    side_effect=[original, reopened]) as open_serial):
            self.assertEqual(gateway.poll(0, STAMP)[0]["stream_state"], "DISCONNECTED")
            self.assertEqual(gateway.poll(0.1, STAMP)[0]["stream_state"], "CALIBRATING")
            status, telemetry = gateway.poll(0.2, STAMP)
            self.assertEqual(status["stream_state"], "STREAMING")
            self.assertFalse(status["streaming"])
            self.assertIsNone(telemetry)
            _, telemetry = gateway.poll(0.3, STAMP)
            self.assertEqual(telemetry["position"]["source"], "simulated")
            self.assertAlmostEqual(telemetry["gyro_rad_s"]["x"], math.radians(0.57))
            self.assertEqual(telemetry["accel_m_s2"]["z"], 9.81)
            status, telemetry = gateway.poll(0.5, STAMP)
            self.assertEqual(status["stream_state"], "PAUSED")
            self.assertIsNone(telemetry)
            self.assertTrue(gateway.poll(1.5, STAMP)[0]["stm_connected"])
            self.assertTrue(gateway.poll(2, STAMP)[0]["stm_connected"])
            with self.assertLogs(level="WARNING"):
                self.assertFalse(gateway.poll(2.1, STAMP)[0]["stm_connected"])
            self.assertIsNone(gateway.poll(4, NEXT_STAMP)[1])
            self.assertEqual(open_serial.call_count, 1)
            self.assertFalse(gateway.poll(4.2, NEXT_STAMP)[0]["stm_connected"])
            original.close.assert_called_once()
            reopened.reset_input_buffer.assert_called_once()
            self.assertEqual(reader.port_name, "COM9")
            status, telemetry = gateway.poll(4.3, NEXT_STAMP)
            self.assertTrue(status["streaming"])
            self.assertEqual(telemetry["position"]["longitude"], -3.6292825)
            self.assertEqual(telemetry["timestamp"], NEXT_STAMP)
            self.assertIsNone(gateway.poll(5.4, NEXT_STAMP)[1])
            with self.assertLogs(level="WARNING"):
                self.assertEqual(gateway.poll(10.5, NEXT_STAMP)[0]["stream_state"], "DISCONNECTED")
            reopened.close.assert_called_once()

    def test_b1_resume_requires_new_measurement(self):
        """Keep GPS still while paused and emit the next fix only after a new measurement."""
        reader = Stm32Reader(DEFAULT_SETTINGS["serial"])
        gateway = Gateway(reader, GpsReplay(ROUTE, 5))
        link = Mock()
        link.read.side_effect = [SAMPLE, b"$DS1,S,PAUSED\n", b"$DS1,S,STREAMING\n",
                                 SAMPLE, b"$DS1,S,STREAMING\n", b"$DS1,S,STREAMING\n"]
        with (patch("MQTT_Communication.read_stm32_serial.list_ports.comports",
                    return_value=[fake_port("COM3", "stm32")]),
              patch("MQTT_Communication.read_stm32_serial.serial.Serial", return_value=link)):
            self.assertEqual(gateway.poll(0, STAMP)[1]["position"]["longitude"], -3.6293443)
            self.assertIsNone(gateway.poll(0.1, STAMP)[1])
            self.assertIsNone(gateway.poll(0.2, STAMP)[1])
            self.assertEqual(gateway.poll(0.3, STAMP)[1]["position"]["longitude"], -3.6292825)
            self.assertIsNone(gateway.poll(4, NEXT_STAMP)[1])
            status, telemetry = gateway.poll(5.5, NEXT_STAMP)
            self.assertTrue(status["stm_connected"])
            self.assertFalse(status["streaming"])
            self.assertIsNone(telemetry)

    def test_local_settings_override(self):
        """Apply one local serial setting while keeping the other shared defaults."""
        with (patch.object(Path, "exists", return_value=True),
              patch.object(Path, "read_text", side_effect=[json.dumps(DEFAULT_SETTINGS),
                                                          '{"serial":{"port":"COM9"}}'])):
            settings = load_settings(ROOT)
        self.assertEqual(settings["serial"], DEFAULT_SETTINGS["serial"] | {"port": "COM9"})
        self.assertEqual(settings["gps"], DEFAULT_SETTINGS["gps"])

    def test_missing_gps_keeps_imu_telemetry(self):
        """Confirm missing GPS yields JSON null and the timestamp is valid UTC."""
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                load_route(ROUTE)
        with patch.object(Stm32Reader, "poll"):
            reader = Stm32Reader(DEFAULT_SETTINGS["serial"])
            now_stamp = utc_now()
            self.assertRegex(now_stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
            sample, = list(FrameParser().feed(SAMPLE))
            reader._accept(sample, 0, now_stamp)
            _, telemetry = Gateway(reader, replay=None).poll(0, now_stamp)
            self.assertIsNone(telemetry["position"])
            self.assertEqual(telemetry["timestamp"], now_stamp)
