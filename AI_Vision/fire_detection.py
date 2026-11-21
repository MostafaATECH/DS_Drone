"""Detect fire in a BGR frame and encode the shared JPEG preview."""

import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parent.parent
FIRE_LABEL = "fire"
FIRE_COLOR_BGR = (0, 128, 255)
BOX_THICKNESS = 2
LABEL_FONT = cv2.FONT_HERSHEY_SIMPLEX
LABEL_SCALE = 0.5
LABEL_THICKNESS = 1
LABEL_PADDING = 3


@dataclass(frozen=True)
class VisionSettings:
    """Defaults shared by still-image and camera inference."""

    confidence: float = 0.25
    iou: float = 0.45
    image_size: int = 640
    preview_size: int = 640
    jpeg_quality: int = 60
    device: str = "cpu"


DEFAULT_SETTINGS = VisionSettings()


@dataclass(frozen=True)
class Detection:
    """Recognized label, score, and xyxy coordinates in the source frame."""

    label: str
    confidence: float
    box: tuple[float, float, float, float]


def load_model(model_path):
    """Load a trusted, local YOLO detection checkpoint and read its class mapping."""
    path = Path(model_path).resolve()
    if path.suffix.lower() != ".pt" or not path.is_file():
        raise ValueError(f"Model must be an existing local .pt file: {path}")

    # Keep Ultralytics settings in the repository and use the checkpoint offline.
    config_dir = ROOT / "build" / "ultralytics"
    config_dir.mkdir(parents=True, exist_ok=True)
    os.environ["YOLO_CONFIG_DIR"] = str(config_dir)
    os.environ["YOLO_OFFLINE"] = "true"
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["MPLCONFIGDIR"] = str(ROOT / "build" / "matplotlib")
    try:
        from ultralytics import YOLO

        model = YOLO(str(path))
        if model.task != "detect":
            raise ValueError(f"expected an object detector, got {model.task}")
        names = model.names
        pairs = names.items() if isinstance(names, dict) else enumerate(names)
        return model, {int(class_id): str(label) for class_id, label in pairs}
    except Exception as error:
        raise RuntimeError(f"Could not load model {path}: {error}") from error


def prepare_frame(frame):
    """Check the OpenCV frame and make its BGR pixels contiguous for inference."""
    if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
            or frame.ndim != 3 or frame.shape[2] != 3 or not frame.size):
        raise ValueError("Expected a nonempty 8-bit BGR image")
    return np.ascontiguousarray(frame)


def predict_detections(model, names, frame, settings=DEFAULT_SETTINGS):
    """Run YOLO on the checkpoint's fire class and return source-frame boxes."""
    class_ids = [class_id for class_id, label in names.items()
                 if label.strip().casefold() == FIRE_LABEL]
    if not class_ids:
        return []
    result, = model.predict(frame, classes=class_ids, conf=settings.confidence,
                            iou=settings.iou, imgsz=settings.image_size, device=settings.device,
                            rect=True, augment=False, save=False, save_txt=False,
                            save_crop=False, show=False, verbose=False)
    if result.boxes is None:
        raise ValueError("The selected model did not return detection boxes")
    return [Detection(FIRE_LABEL, confidence, (x1, y1, x2, y2))
            for x1, y1, x2, y2, confidence, class_id in result.boxes.data.cpu().tolist()]


def resize_preview(frame, preview_size):
    """Shrink the longest edge to the preview limit while keeping the aspect ratio."""
    height, width = frame.shape[:2]
    scale = min(1.0, preview_size / max(height, width))
    if scale == 1.0:
        return frame.copy()
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)


def detection_text(detection):
    """Format the same class label and two-decimal confidence for boxes and console output."""
    return f"{detection.label} {detection.confidence:.2f}"


def draw_detections(preview, detections, source_shape):
    """Scale and clip source boxes, then draw them on the resized preview in place."""
    height, width = preview.shape[:2]
    source_h, source_w = source_shape[:2]
    for detection in detections:
        coordinates = [max(0, min(edge - 1, round(value * edge / original)))
                       for value, edge, original in zip(detection.box, (width, height) * 2,
                                                        (source_w, source_h) * 2)]
        left, top, right, bottom = coordinates
        color = FIRE_COLOR_BGR
        cv2.rectangle(preview, (left, top), (right, bottom), color, BOX_THICKNESS)

        label = detection_text(detection)
        (text_w, text_h), baseline = cv2.getTextSize(label, LABEL_FONT, LABEL_SCALE,
                                                     LABEL_THICKNESS)
        label_w = text_w + 2 * LABEL_PADDING
        label_h = text_h + baseline + 2 * LABEL_PADDING
        label_x = max(0, min(left, width - label_w))
        label_y = max(0, top - label_h)
        cv2.rectangle(preview, (label_x, label_y), (label_x + label_w, label_y + label_h),
                      color, cv2.FILLED)
        origin = (label_x + LABEL_PADDING, label_y + LABEL_PADDING + text_h)
        cv2.putText(preview, label, origin, LABEL_FONT, LABEL_SCALE, (0, 0, 0),
                    LABEL_THICKNESS, cv2.LINE_AA)


def encode_preview(frame, detections, settings=DEFAULT_SETTINGS):
    """Resize and annotate a frame, then return the exact preview JPEG bytes."""
    preview = resize_preview(frame, settings.preview_size)
    draw_detections(preview, detections, frame.shape)
    ok, jpeg = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, settings.jpeg_quality])
    if not ok:
        raise RuntimeError("Could not encode the JPEG preview")
    return jpeg.tobytes()


def process_frame(model, names, frame, settings=DEFAULT_SETTINGS):
    """Return detections and shared preview bytes for one photograph or camera frame."""
    frame = prepare_frame(frame)
    detections = predict_detections(model, names, frame, settings)
    return detections, encode_preview(frame, detections, settings)
