import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from nextask.core import sources


def photo(kind="JPEG", size=(3000, 4000)):
    stream = io.BytesIO()
    Image.new("RGB", size, "white").save(stream, format=kind)
    mime = "jpeg" if kind == "JPEG" else "png"
    return f"data:image/{mime};base64," + base64.b64encode(stream.getvalue()).decode()


class LargeGalleryImagesTests(unittest.TestCase):
    def choose(self, data, save):
        decide = Mock(return_value=({"answers": {"photo": {"choice": "usable", "probabilities": {
            "usable": .99, "unusable": .01}}}}, {"source": "remote"}))
        observations = []
        with patch.dict("os.environ", {"NEXTASK_SITE_GENERIC": "1"}):
            result = sources.choose_gallery_image([{"data": data, "url": "https://example.org/label"}],
                zoro=SimpleNamespace(mode="remote", decide=decide),
                photo_options={"usable": "readable", "unusable": "unreadable"}, threshold=.85,
                save_image=save, observe=observations.append)
        return result, decide, observations[0]

    def test_phone_sized_jpeg_and_png_keep_full_resolution_for_ocr(self):
        for kind in ("JPEG", "PNG"):
            with self.subTest(kind=kind):
                original = photo(kind)
                save = Mock(return_value="original-image")
                result, decide, observation = self.choose(original, save)
                self.assertEqual(result, "original-image")
                save.assert_called_once_with(original)
                preview = decide.call_args.args[0]["image"]
                with Image.open(io.BytesIO(base64.b64decode(preview))) as im:
                    self.assertEqual(max(im.size), 448)
                self.assertEqual(observation["selected_index"], 0)

    def test_corrupt_mismatched_and_excessive_images_never_reach_model(self):
        data = photo(size=(10, 10))
        for candidate in ("data:image/jpeg;base64,broken", data.replace("image/jpeg", "image/png")):
            result, decide, observation = self.choose(candidate, Mock())
            self.assertIsNone(result)
            decide.assert_not_called()
            self.assertEqual(observation["candidates"][0]["status"], "invalid_pixels")
        with patch.object(sources, "GALLERY_MAX_PIXELS", 99):
            result, decide, _ = self.choose(data, Mock())
            self.assertIsNone(result)
            decide.assert_not_called()
        with patch.object(sources, "GALLERY_MAX_BYTES", 1):
            result, decide, _ = self.choose(data, Mock())
            self.assertIsNone(result)
            decide.assert_not_called()


if __name__ == "__main__":
    unittest.main()
