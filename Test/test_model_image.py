"""Run the shared vision pipeline on one local image and save its JPEG preview."""

import argparse
import json
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


ROOT = Path(__file__).resolve().parent.parent
# Running this file directly puts Test on the import path instead of the repository root.
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))


def preview_path(output, image_path, model_path):
    """Require a JPEG path in DS_Drone that cannot overwrite either input."""
    path = output.resolve()
    if not path.is_relative_to(ROOT) or path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise ValueError("Output must be a .jpg or .jpeg path inside DS_Drone")
    for source in (image_path, model_path):
        if path == source.resolve() or (path.exists() and source.exists() and path.samefile(source)):
            raise ValueError(f"Output would overwrite an input: {path}")
    return path


def read_image(path):
    """Decode the image from bytes so local paths can contain spaces or Unicode."""
    import cv2
    import numpy as np

    data = path.read_bytes()
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR) if data else None
    if image is None:
        raise ValueError(f"Could not read image: {path}")
    return image


def save_preview(path, jpeg):
    """Write the encoded bytes and replace any prior preview only after a complete write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix="preview-", suffix=".tmp", delete=False) as temp:
            temporary_path = Path(temp.name)
            temp.write(jpeg)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def show_preview(jpeg):
    """Display the exact saved preview until a key is pressed."""
    import cv2
    import numpy as np

    preview = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    cv2.imshow("Still image preview", preview)
    try:
        cv2.waitKey(0)
    finally:
        cv2.destroyAllWindows()


def main(argv=None):
    """Run still-image inference and report the model labels, detections, and saved file."""
    parser = argparse.ArgumentParser(description="Test a local detector on one photograph.")
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--show", action="store_true", help="also display the saved preview")
    args = parser.parse_args(argv)
    try:
        path = preview_path(args.output, args.image, args.model)
        from AI_Vision.fire_detection import detection_text, load_model, process_frame

        frame = read_image(args.image)
        model, names = load_model(args.model)
        print(f"Model classes: {json.dumps(names, sort_keys=True)}")
        detections, jpeg = process_frame(model, names, frame)
        save_preview(path, jpeg)
        if detections:
            for detection in detections:
                print(detection_text(detection))
        else:
            print("No detections")
        print(f"Saved preview: {path}")
        print("Still-image inference only. Incident confirmation requires successive camera frames.")
        if args.show:
            show_preview(jpeg)
    except ImportError as error:
        print(f"Vision dependency missing: {error}. See the vision install commands in README.md.",
              file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Image test failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
