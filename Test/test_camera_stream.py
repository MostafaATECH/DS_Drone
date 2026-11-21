"""Check camera recovery, episode timing, and shared previews with synthetic frames."""

import json
import time
import unittest
from dataclasses import replace
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import Mock, patch

from AI_Vision import camera_stream
from AI_Vision.fire_episodes import FireEpisodeTracker
from project_config import ROOT


STAMP = "2026-11-21T10:00:00.000Z"


def camera_settings(**overrides):
    """Read the shared camera defaults with a short retry for synthetic tests."""
    values = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))["camera"]
    return camera_stream.load_camera_settings({**values, "retry_s": 0.02, **overrides})


def wait_for(worker, condition):
    """Wait briefly for a camera-thread result without introducing a fixed sleep race."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = worker.snapshot()
        if condition(snapshot):
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"Camera did not reach the expected state: {worker.snapshot().state}")


class EpisodeTests(unittest.TestCase):
    """Exercise timing and interrupted observations without a device or model."""

    def test_stable_id_and_rearm(self):
        """Confirm after three positive frames and rearm after two seconds of observed absence."""
        tracker = FireEpisodeTracker("drone01", confirmation_frames=3, absence_s=2)
        self.assertIsNone(tracker.observe(0, STAMP, 0.7))
        self.assertIsNone(tracker.observe(0.1, STAMP, None))
        self.assertIsNone(tracker.observe(0.2, STAMP, 0.7))
        self.assertIsNone(tracker.observe(0.3, STAMP, 0.8))
        first = tracker.observe(0.4, STAMP, 0.9)
        self.assertTrue(first.event_id.startswith("drone01-fire-"))
        self.assertEqual(first.confidence, 0.9)
        self.assertIsNone(tracker.observe(1, STAMP, None))
        self.assertIsNone(tracker.observe(2, STAMP, 0.7))
        self.assertIs(tracker.active_incident, first)
        tracker.observe(3, STAMP, None)
        tracker.observe(5, STAMP, None)
        self.assertIsNone(tracker.active_incident)
        self.assertIsNone(tracker.observe(6, STAMP, 0.7))
        self.assertIsNone(tracker.observe(6.1, STAMP, 0.8))
        self.assertNotEqual(tracker.observe(6.2, STAMP, 0.9).event_id, first.event_id)

    def test_outage_breaks_streak_without_assuming_absence(self):
        """Reset an unconfirmed streak and retain an active ID through a camera outage."""
        tracker = FireEpisodeTracker("drone01", confirmation_frames=2, absence_s=2)
        tracker.observe(0, STAMP, 0.7)
        tracker.interrupt()
        self.assertIsNone(tracker.observe(1, STAMP, 0.7))
        first = tracker.observe(2, STAMP, 0.8)
        tracker.observe(3, STAMP, None)
        tracker.interrupt()
        self.assertIsNone(tracker.observe(20, STAMP, 0.9))
        self.assertIs(tracker.active_incident, first)

    def test_disabled_does_not_try_to_open(self):
        """Keep camera construction and startup inert when the camera is disabled."""
        worker = camera_stream.CameraWorker(camera_settings(enabled=False), "drone02")
        with patch.object(camera_stream, "open_camera") as open_device:
            worker.start()
            open_device.assert_not_called()
        self.assertEqual(worker.snapshot().state, "DISABLED")
        self.assertTrue(worker.stop())


@unittest.skipUnless(find_spec("cv2") and find_spec("numpy"), "Install the optional vision dependencies")
class CameraWorkerTests(unittest.TestCase):
    """Inject fake capture objects and run the actual camera worker and renderer."""

    def test_usb_id_opens_only_the_matching_device(self):
        """Choose the Logitech index reported by the backend and never fall back to the laptop."""
        settings = camera_settings(enabled=True, source="046d:094c", backend="dshow")
        laptop = SimpleNamespace(index=0, name="Laptop", vid=0x04F2, pid=0xB76F)
        logitech = SimpleNamespace(index=4, name="Brio 100", vid=0x046D, pid=0x094C)
        with patch.object(camera_stream, "list_cameras", return_value=[laptop, logitech]), \
                patch("cv2.VideoCapture") as capture:
            camera_stream.open_camera(settings)
            capture.assert_called_once_with(4, camera_stream.camera_api("dshow"))
        with patch.object(camera_stream, "list_cameras", return_value=[laptop]), \
                patch("cv2.VideoCapture") as capture:
            with self.assertRaises(camera_stream.CameraUnavailable):
                camera_stream.open_camera(settings)
            capture.assert_not_called()

    def test_retry_reuses_model_and_shared_jpeg(self):
        """Retry missing and disconnected cameras, then confirm once using the shared preview bytes."""
        import numpy as np
        from AI_Vision import fire_detection as vision

        image = np.full((720, 1280, 3), 95, dtype=np.uint8)
        detections = [vision.Detection("fire", 0.8, (200, 140, 1000, 500))]
        expected = vision.encode_preview(image, detections)
        failed = Mock()
        failed.read.return_value = (False, None)
        ready = Mock()
        ready.read.return_value = (True, image)
        worker = camera_stream.CameraWorker(camera_settings(enabled=True, confidence=0.4), "drone01")

        with patch.object(camera_stream, "open_camera", side_effect=[
                camera_stream.CameraUnavailable("synthetic missing camera"),
                (failed, "synthetic disconnected camera"), (ready, "synthetic recovered camera")]) as opened, \
                patch.object(vision, "load_model", return_value=(Mock(), {9: "fire"})) as loaded, \
                patch.object(vision, "predict_detections", return_value=detections) as predict:
            worker.start()
            try:
                snapshot = wait_for(worker, lambda entry: entry.frame is not None and entry.frame.event_id)
                self.assertEqual(snapshot.frame.jpeg, expected)
                self.assertEqual(snapshot.state, "AVAILABLE")
                incident, = worker.take_incidents()
                self.assertEqual(incident.event_id, snapshot.frame.event_id)
                self.assertEqual(worker.take_incidents(), ())
                self.assertEqual(predict.call_args.args[3], replace(vision.DEFAULT_SETTINGS, confidence=0.4))
                self.assertEqual(opened.call_count, 3)
                loaded.assert_called_once()
            finally:
                self.assertTrue(worker.stop())
        failed.release.assert_called_once()
        ready.release.assert_called_once()

    def test_model_error_releases_capture(self):
        """Expose model failure as ERROR and release the handle before retrying."""
        from AI_Vision import fire_detection as vision

        capture = Mock()
        worker = camera_stream.CameraWorker(camera_settings(enabled=True, retry_s=5), "drone01")
        with patch.object(camera_stream, "open_camera", return_value=(capture, "synthetic camera")), \
                patch.object(vision, "load_model", side_effect=RuntimeError("synthetic checkpoint failure")):
            worker.start()
            try:
                error = wait_for(worker, lambda entry: entry.state == "ERROR")
                self.assertEqual(error.detail, "synthetic checkpoint failure")
                self.assertIsNone(error.frame)
            finally:
                self.assertTrue(worker.stop())
        capture.release.assert_called_once()
