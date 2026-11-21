"""List local cameras or check the configured camera, detector, and fire episodes."""

import argparse
import math
import sys
import time
from pathlib import Path

from AI_Vision.camera_stream import CameraWorker, list_cameras, load_camera_settings
from project_config import load_config
from Test.test_model_image import preview_path, save_preview


def print_cameras(backend):
    """Print the camera names and USB identifiers for the selected backend."""
    devices = list_cameras(backend)
    if not devices:
        print(f"No cameras found with {backend}.")
    for device in devices:
        usb = (f"{device.vid:04x}:{device.pid:04x}"
               if device.vid is not None and device.pid is not None else "USB ID unavailable")
        print(f"{backend} index {device.index}: {device.name} ({usb})")


def display_frame(frame):
    """Show the exact shared JPEG in the local preview window."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(frame.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    cv2.imshow("DS Drone - local camera check", image)


def run_check(worker, seconds, show, output):
    """Print camera changes and incidents, optionally showing and saving live JPEGs."""
    previous_status = None
    frame = None
    window_open = False
    if show and worker.settings.enabled:
        import cv2

    deadline = time.monotonic() + seconds if seconds is not None else None
    print("Local camera check. Press Ctrl+C to stop.", flush=True)
    worker.start()
    try:
        while deadline is None or time.monotonic() < deadline:
            snapshot = worker.snapshot()
            status = (snapshot.state, snapshot.detail)
            if status != previous_status:
                print(f"Camera {snapshot.state}: {snapshot.detail}", flush=True)
                previous_status = status
            for incident in worker.take_incidents():
                print(f"Confirmed {incident.type} {incident.event_id} at {incident.timestamp} "
                      f"(confidence {incident.confidence:.2f})", flush=True)
            if snapshot.frame is not None and snapshot.frame is not frame:
                frame = snapshot.frame
                if show:
                    display_frame(frame)
                    window_open = True
            if window_open:
                if snapshot.frame is None:
                    cv2.destroyAllWindows()
                    window_open = False
                elif (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                    break
            if snapshot.state == "DISABLED":
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        if not worker.stop():
            print("Camera driver did not return before the shutdown timeout.", file=sys.stderr)
        if window_open:
            cv2.destroyAllWindows()
    if frame is not None:
        from AI_Vision.fire_detection import detection_text

        detections = ", ".join(map(detection_text, frame.detections)) or "No detections"
        print(f"Last live frame {frame.captured_at}: {detections}")
        if output is not None:
            save_preview(output, frame.jpeg)
            print(f"Saved live preview: {output}")
    elif output is not None:
        print("No live frame was available to save.", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    """Run the standalone check using the project settings and optional overrides."""
    parser = argparse.ArgumentParser(description="Check the local camera and fire detector.")
    parser.add_argument("--list", action="store_true", help="list available devices and exit")
    parser.add_argument("--enable", action="store_true", help="enable the camera for this check")
    parser.add_argument("--source", help="override the camera index or USB vendor:product ID")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds")
    parser.add_argument("--show", action="store_true", help="display the annotated preview")
    parser.add_argument("--output", type=Path, help="save the last live preview as a JPEG in DS_Drone")
    args = parser.parse_args(argv)
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error("--seconds must be a positive number")
    try:
        config = load_config()
        values = config["camera"]
        if args.enable:
            values["enabled"] = True
        if args.source is not None:
            values["source"] = int(args.source) if args.source.isdecimal() else args.source
        settings = load_camera_settings(values)
        if args.list:
            print_cameras(settings.backend)
            return 0
        output = preview_path(args.output, settings.model) if args.output is not None else None
        worker = CameraWorker(settings, config["mqtt"]["drone_id"])
        return run_check(worker, args.seconds, args.show, output)
    except Exception as error:
        print(f"Camera check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
