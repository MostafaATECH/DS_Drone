"""Capture and process the configured camera on a worker thread."""

import logging
import math
import re
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

from project_config import ROOT
from .fire_episodes import FireEpisodeTracker


LOGGER = logging.getLogger(__name__)
USB_ID = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}", re.IGNORECASE)


@dataclass(frozen=True)
class CameraSettings:
    """Settings for opening the device and confirming live fire episodes."""

    enabled: bool
    source: int | str
    backend: str
    model: Path
    confidence: float | None
    confirmation_frames: int
    absence_s: float
    retry_s: float


@dataclass(frozen=True)
class CameraFrame:
    """The latest processed frame and the episode active at capture time."""

    jpeg: bytes
    detections: tuple
    event_id: str | None
    captured_at: str


@dataclass(frozen=True)
class CameraSnapshot:
    """Camera health and the latest preview, if the camera is available."""

    state: str
    detail: str
    frame: CameraFrame | None = None


class CameraUnavailable(Exception):
    """The configured video device did not provide a frame."""


def load_camera_settings(values):
    """Validate the camera section without loading OpenCV or model dependencies."""
    if type(values["enabled"]) is not bool:
        raise ValueError("camera.enabled must be true or false")
    source = values["source"]
    if not (type(source) is int and source >= 0 or isinstance(source, str) and USB_ID.fullmatch(source)):
        raise ValueError("camera.source must be a nonnegative index or a USB ID like 046d:094c")
    if values["backend"] not in ("dshow", "msmf"):
        raise ValueError("camera.backend must be dshow or msmf")
    model = values["model"]
    if not isinstance(model, str) or not model.strip():
        raise ValueError("camera.model must be a checkpoint path")
    confidence = values["confidence"]
    if confidence is not None and (type(confidence) not in (int, float) or not 0 < confidence <= 1):
        raise ValueError("camera.confidence must be null or a number greater than 0 and at most 1")
    frames = values["confirmation_frames"]
    if type(frames) is not int or frames < 1:
        raise ValueError("camera.confirmation_frames must be a positive integer")
    for key in ("absence_s", "retry_s"):
        value = values[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"camera.{key} must be a positive number of seconds")
    return CameraSettings(**{**values, "model": ROOT / model})


def camera_api(backend):
    """Return the OpenCV API used for both enumeration and opening the camera."""
    import cv2

    return cv2.CAP_DSHOW if backend == "dshow" else cv2.CAP_MSMF


def list_cameras(backend):
    """List named devices and indices for the chosen API without capturing frames."""
    from cv2_enumerate_cameras import enumerate_cameras

    return enumerate_cameras(camera_api(backend))


def open_camera(settings):
    """Open the configured index or the unique USB match, using no fallback device."""
    import cv2

    source = settings.source
    if isinstance(source, int):
        index, name = source, f"{settings.backend} index {source}"
    else:
        vid, pid = (int(part, 16) for part in source.split(":"))
        matches = [device for device in list_cameras(settings.backend)
                   if (device.vid, device.pid) == (vid, pid)]
        if not matches:
            raise CameraUnavailable(f"Camera {source} was not found")
        if len(matches) > 1:
            raise CameraUnavailable(f"Multiple cameras match {source}. Choose an index from --list")
        device = matches[0]
        index, name = device.index, f"{device.name} ({settings.backend} index {device.index})"

    capture = cv2.VideoCapture(index, camera_api(settings.backend))
    if not capture.isOpened():
        capture.release()
        raise CameraUnavailable(f"Could not open {name}")
    return capture, name


class CameraWorker:
    """Own the camera and model, exposing a latest-only preview and local incidents."""

    def __init__(self, settings, drone_id):
        """Prepare worker state without opening a camera or importing the vision stack."""
        self.settings = settings
        self.episodes = FireEpisodeTracker(drone_id, settings.confirmation_frames, settings.absence_s)
        self._snapshot = (CameraSnapshot("UNAVAILABLE", "Waiting for camera") if settings.enabled
                          else CameraSnapshot("DISABLED", "Camera disabled in settings"))
        self._lock = Lock()
        self._stop = Event()
        self._thread = None
        self._incidents = deque(maxlen=16)

    def start(self):
        """Start one capture/inference thread when the camera is enabled."""
        if self.settings.enabled and self._thread is None:
            self._thread = Thread(target=self._run, name="camera", daemon=True)
            self._thread.start()

    def stop(self, timeout=5):
        """Signal the worker and wait briefly for it to release its own camera handle."""
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def snapshot(self):
        """Read camera health and the latest preview without waiting for inference."""
        with self._lock:
            return self._snapshot

    def take_incidents(self):
        """Return the pending local incident notifications and clear the buffer."""
        with self._lock:
            incidents = tuple(self._incidents)
            self._incidents.clear()
            return incidents

    def _set_status(self, state, detail):
        """Report an interruption and clear its obsolete preview."""
        self.episodes.interrupt()
        with self._lock:
            self._snapshot = CameraSnapshot(state, detail)

    def _record_frame(self, name, now, timestamp, detections, jpeg):
        """Update episode state and replace the one-slot preview after processing a frame."""
        confidence = max((detection.confidence for detection in detections), default=None)
        incident = self.episodes.observe(now, timestamp, confidence)
        active = self.episodes.active_incident
        frame = CameraFrame(jpeg, tuple(detections), active.event_id if active else None, timestamp)
        overflow = False
        with self._lock:
            self._snapshot = CameraSnapshot("AVAILABLE", name, frame)
            if incident is not None:
                overflow = len(self._incidents) == self._incidents.maxlen
                self._incidents.append(incident)
        if overflow:
            LOGGER.warning("Camera incident buffer full. Dropped the oldest local notification.")

    def _read_frames(self, capture, name, model, names, process_frame, vision_settings):
        """Read and process one frame at a time, with no raw-frame queue."""
        while not self._stop.is_set():
            ok, image = capture.read()
            if not ok or image is None or not image.size:
                raise CameraUnavailable(f"No frames from {name}")
            now = monotonic()
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            detections, jpeg = process_frame(model, names, image, vision_settings)
            if not self._stop.is_set():
                self._record_frame(name, now, timestamp, detections, jpeg)

    def _run(self):
        """Run capture and inference, reopening after failures on the configured interval."""
        loaded = None
        while not self._stop.is_set():
            try:
                capture, name = open_camera(self.settings)
                try:
                    from .fire_detection import DEFAULT_SETTINGS, load_model, process_frame

                    vision_settings = DEFAULT_SETTINGS
                    if self.settings.confidence is not None:
                        vision_settings = replace(vision_settings, confidence=self.settings.confidence)
                    if loaded is None:
                        loaded = load_model(self.settings.model)
                    model, names = loaded
                    self._read_frames(capture, name, model, names, process_frame, vision_settings)
                finally:
                    capture.release()
            except CameraUnavailable as error:
                self._set_status("UNAVAILABLE", str(error))
            except Exception as error:
                loaded = None
                self._set_status("ERROR", str(error))
            self._stop.wait(self.settings.retry_s)
        self._set_status("UNAVAILABLE", "Camera worker stopped")
