import importlib.util
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

SPEC = importlib.util.spec_from_file_location(
    "example_images", Path(__file__).parents[1] / "tools/example_images.py"
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


def test_a_render_set_that_shrank_leaves_no_stale_pictures(tmp_path, monkeypatch):
    """A board that loses its inner layers loses their pictures with them."""
    design = tmp_path / "fpga-audio"
    (design / "reviewed").mkdir(parents=True)
    pictures = design / "images"
    pictures.mkdir()
    for name in (
        "board-in1-reviewed.jpg",
        "board-in2-reviewed.jpg",
        "board-front-first.jpg",
        "schematic-as-generated.jpg",
    ):
        (pictures / name).write_bytes(b"stale")

    def fake_render(args):
        out = Path(args[args.index("-o") + 1])
        out.mkdir(parents=True, exist_ok=True)
        for name in ("sheet.png", "front.png", "back.png"):
            page = Image.new("RGB", (40, 30), "white")
            ImageDraw.Draw(page).rectangle((5, 5, 20, 20), fill="red")
            page.save(out / name)

    monkeypatch.setattr(images, "render", fake_render)
    written = images.make_images(design, "reviewed", tmp_path / "scratch")
    assert {p.name for p in written} == {
        "schematic-reviewed.jpg",
        "board-front-reviewed.jpg",
        "board-back-reviewed.jpg",
    }
    left = {p.name for p in pictures.iterdir()}
    # the inner-layer pictures are gone; the first edition and the other
    # variant are not this run's business
    assert "board-in1-reviewed.jpg" not in left
    assert "board-in2-reviewed.jpg" not in left
    assert "board-front-first.jpg" in left
    assert "schematic-as-generated.jpg" in left
