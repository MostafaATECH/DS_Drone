"""Check the offline and shared preview paths with synthetic images and boxes."""

import io
import unittest
from contextlib import redirect_stdout
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from Test import test_model_image


@unittest.skipUnless(find_spec("cv2") and find_spec("numpy"), "Install the optional vision dependencies")
class PreviewTests(unittest.TestCase):
    """Exercise the real render and encode functions with a fake detector."""

    def test_offline_output_matches_shared_preview(self):
        """Save byte-identical JPEG with correctly scaled boxes from the model's own class IDs."""
        import cv2
        import numpy as np
        from AI_Vision import fire_detection as vision

        frame = np.full((800, 1280, 3), 105, dtype=np.uint8)
        boxes = [[200, 120, 800, 500, 0.91, 7]]
        model = Mock()
        result = Mock()
        result.boxes.data.cpu().tolist.return_value = boxes
        model.predict.return_value = [result]
        names = {2: "Smoke", 4: "Other", 7: "Fire"}
        detections = [vision.Detection("fire", 0.91, (200, 120, 800, 500))]
        expected = vision.encode_preview(frame, detections)

        output_dir = (test_model_image.ROOT / "Test" / "output").resolve()
        self.assertTrue(output_dir.is_relative_to(test_model_image.ROOT))
        output_dir.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="synthetic-", dir=output_dir) as folder:
            image = Path(folder) / "synthetic photo.png"
            image.write_bytes(cv2.imencode(".png", frame)[1].tobytes())
            output = Path(folder) / "preview folder" / "preview.jpg"
            console = io.StringIO()
            with patch.object(vision, "load_model", return_value=(model, names)), redirect_stdout(console):
                exit_code = test_model_image.main(["--image", str(image), "--model", "best.pt",
                                                   "--output", str(output)])
            self.assertEqual(exit_code, 0)
            self.assertEqual(output.read_bytes(), expected)

        call = model.predict.call_args
        self.assertEqual(call.kwargs["classes"], [7])
        self.assertEqual(call.kwargs["conf"], vision.DEFAULT_SETTINGS.confidence)
        self.assertEqual(call.kwargs["iou"], vision.DEFAULT_SETTINGS.iou)
        self.assertEqual(call.kwargs["imgsz"], vision.DEFAULT_SETTINGS.image_size)
        self.assertFalse(call.kwargs["save"])
        self.assertIn("fire 0.91", console.getvalue())
        preview = cv2.imdecode(np.frombuffer(expected, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(preview.shape, (400, 640, 3))
        self.assertGreater(int(preview[250, 250, 2]), int(preview[250, 250, 0]) + 40)
        self.assertTrue(np.all(frame == 105))

    def test_no_detections_still_encodes(self):
        """An empty detector result still produces a valid JPEG without enlarging the frame."""
        import cv2
        import numpy as np
        from AI_Vision import fire_detection as vision

        frame = np.zeros((50, 100, 3), dtype=np.uint8)
        model = Mock()
        result = Mock()
        result.boxes.data.cpu().tolist.return_value = []
        model.predict.return_value = [result]
        detections, jpeg = vision.process_frame(model, {5: "fire"}, frame)
        self.assertEqual(detections, [])
        preview = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(preview.shape, frame.shape)
        model.reset_mock()
        self.assertEqual(vision.predict_detections(model, {2: "Smoke"}, frame), [])
        model.predict.assert_not_called()


class OutputPathTests(unittest.TestCase):
    """Check that the still-image CLI protects the input and output paths."""

    def test_rejects_input_and_external_output(self):
        """Reject the source photo, the checkpoint path, and paths outside DS_Drone."""
        root = test_model_image.ROOT
        image = root / "Test" / "photo.jpg"
        for output in (image, root / "best.pt", root.parent / "preview.jpg"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                test_model_image.preview_path(output, image, root / "best.pt")
