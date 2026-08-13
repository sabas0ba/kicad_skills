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


def test_black_sheet_is_black_and_keeps_its_labels_legible(tmp_path):
    images = [("layer-F_Cu", make_png(tmp_path / "a.png"))]
    sheet = render.contact_sheet(images, tmp_path / "sheet.png", cell=100, background="black")
    with Image.open(sheet) as im:
        assert im.mode == "RGB"  # opaque, so no alpha channel is carried around
        assert im.getpixel((0, 0)) == (0, 0, 0)
        # The label strip is above the tile; on black it has to be drawn light.
        strip = [im.getpixel((x, 14)) for x in range(8, 92)]
        assert any(sum(pixel) > 3 * 128 for pixel in strip)


def test_transparent_sheet_keeps_the_alpha_channel(tmp_path):
    images = [("layer-F_Cu", make_png(tmp_path / "a.png"))]
    sheet = render.contact_sheet(images, tmp_path / "sheet.png", cell=100, background="transparent")
    with Image.open(sheet) as im:
        assert im.mode == "RGBA"
        assert im.getpixel((0, 0))[3] == 0
        # The tile itself stays opaque - only the backdrop is see-through.
        assert im.getpixel((50, 28 + 37))[3] == 255


def test_transparent_tile_on_a_transparent_sheet_is_not_double_multiplied(tmp_path):
    """alpha_composite, not paste-with-mask: half alpha over nothing stays half."""
    path = tmp_path / "half.png"
    Image.new("RGBA", (40, 40), (255, 0, 0, 128)).save(path)
    sheet = render.contact_sheet(
        [("half", str(path))], tmp_path / "sheet.png", cell=40, background="transparent"
    )
    with Image.open(sheet) as im:
        assert im.getpixel((20, 28 + 20))[3] == 128


def test_unknown_background_is_rejected():
    with pytest.raises(EdaError):
        render.background_rgba("puce")


def plot_like_pdf(path):
    """A red disc on an unpainted page - the shape a KiCad plot has."""
    pytest.importorskip("reportlab")
    from reportlab.lib.colors import Color
    from reportlab.pdfgen import canvas

    page = canvas.Canvas(str(path), pagesize=(200, 200))
    page.setFillColor(Color(1, 0, 0))
    page.circle(100, 100, 60, fill=1, stroke=0)
    page.save()
    return path


@pytest.mark.parametrize(
    ("background", "mode", "corner"),
    [
        ("white", "RGB", (255, 255, 255)),
        ("black", "RGB", (0, 0, 0)),
        ("transparent", "RGBA", (0, 0, 0, 0)),
    ],
)
def test_page_background_is_chosen_while_rasterising(tmp_path, background, mode, corner):
    """KiCad never paints the page, so the fill colour is the whole story."""
    plot_like_pdf(tmp_path / "plot.pdf")
    written = render.pdf_to_png(
        tmp_path / "plot.pdf", tmp_path / "plot", dpi=72, background=background
    )
    with Image.open(written[0]) as im:
        assert im.mode == mode
        assert im.getpixel((2, 2)) == corner
        # The artwork is untouched whatever the backdrop is.
        assert im.getpixel((100, 100))[:3] == (255, 0, 0)


def test_flatten_composites_alpha_onto_the_background(tmp_path):
    path = tmp_path / "3d.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(path)
    render._flatten(path, render.BACKGROUNDS["black"])
    with Image.open(path) as im:
        assert im.mode == "RGB"
        assert im.getpixel((5, 5)) == (0, 0, 0)


def test_flatten_leaves_an_image_without_alpha_alone(tmp_path):
    path = tmp_path / "opaque.png"
    make_png(path, size=(10, 10), colour=(1, 2, 3))
    before = path.read_bytes()
    render._flatten(path, render.BACKGROUNDS["black"])
    assert path.read_bytes() == before


def test_kicad_cli_usage_dumps_are_truncated():
    long = RuntimeError("usage: " + "x" * 2000)
    assert len(render._short(long)) < 450
    assert render._short(long).endswith("...")
