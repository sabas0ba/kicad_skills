import importlib.util
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

SPEC = importlib.util.spec_from_file_location(
    "example_images", Path(__file__).parents[1] / "tools/update_example_images.py"
)
assert SPEC and SPEC.loader
images = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(images)


def test_crop_removes_page_margin_without_rescaling_copper():
    source = Image.new("RGB", (300, 200), "white")
    ImageDraw.Draw(source).rectangle((50, 70, 149, 119), fill="red")
    result = images.crop_page(source)
    assert result.size == (124, 74)
    assert result.getpixel((12, 12)) == (255, 0, 0)
    assert result.getpixel((111, 61)) == (255, 0, 0)
    assert source.size == (300, 200)


def test_empty_render_is_not_accepted_as_evidence():
    with pytest.raises(ValueError, match="empty render"):
        images.crop_page(Image.new("RGB", (100, 100), "white"))
