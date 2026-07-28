import pytest
from PIL import Image

from eda_toolkit.kicad import render
from eda_toolkit.util import EdaError


def make_png(path, size=(400, 300), colour=(200, 30, 30)):
    Image.new("RGB", size, colour).save(path)
    return str(path)


def test_contact_sheet_tiles_every_image(tmp_path):
    images = [(f"layer-{i}", make_png(tmp_path / f"{i}.png")) for i in range(5)]
    sheet = render.contact_sheet(images, tmp_path / "sheet.png", columns=3, cell=100)
    assert sheet.exists()
    with Image.open(sheet) as im:
        # 5 images at 3 columns is 2 rows. The 400x300 tiles scale to 100x75, and a
        # row is only as tall as its content plus the label strip - no dead space.
        assert im.size == (300, 2 * (75 + 28))


def test_contact_sheet_skips_images_that_were_never_written(tmp_path):
    images = [("real", make_png(tmp_path / "a.png")), ("missing", str(tmp_path / "gone.png"))]
    sheet = render.contact_sheet(images, tmp_path / "sheet.png", columns=2, cell=60)
    with Image.open(sheet) as im:
        assert im.size == (60, 45 + 28)  # one column: 400x300 scaled into a 60 px cell


def test_contact_sheet_without_any_image_is_an_error(tmp_path):
    with pytest.raises(EdaError):
        render.contact_sheet([("missing", str(tmp_path / "gone.png"))], tmp_path / "sheet.png")


def test_transparent_images_are_composited_not_pasted_black(tmp_path):
    path = tmp_path / "rgba.png"
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(path)
    sheet = render.contact_sheet([("clear", str(path))], tmp_path / "sheet.png", cell=40)
    with Image.open(sheet) as im:
        assert im.convert("RGB").getpixel((20, 50)) == (255, 255, 255)


def test_kicad_cli_usage_dumps_are_truncated():
    long = RuntimeError("usage: " + "x" * 2000)
    assert len(render._short(long)) < 450
    assert render._short(long).endswith("...")
