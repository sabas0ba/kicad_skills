"""PDF extraction is tested against a synthetic datasheet built with reportlab."""

import pytest

from eda_toolkit.datasheet import parse
from eda_toolkit.util import EdaError

reportlab = pytest.importorskip("reportlab")


@pytest.fixture(scope="module")
def fake_datasheet(tmp_path_factory):
    """A two page 'datasheet' with text, a table and an embedded bitmap."""
    from PIL import Image
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    path = tmp_path_factory.mktemp("pdf") / "fake-datasheet.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    _width, height = A4

    c.setFont("Helvetica-Bold", 18)
    c.drawString(60, height - 70, "EDA1234 Low Noise Operational Amplifier")
    c.setFont("Helvetica", 11)
    c.drawString(60, height - 100, "Supply voltage range 2.7 V to 5.5 V")
    c.drawString(60, height - 120, "Absolute Maximum Ratings")
    rows = [("Parameter", "Min", "Max", "Unit"),
            ("Supply voltage VDD", "-0.3", "6.0", "V"),
            ("Input current", "-10", "10", "mA"),
            ("Junction temperature", "-40", "150", "degC")]
    y = height - 150
    for row in rows:
        x = 60
        for cell in row:
            c.drawString(x, y, cell)
            x += 120
        # draw the grid so pdfplumber recognises a table
        c.line(60, y - 4, 60 + 4 * 120, y - 4)
        y -= 20
    for i in range(5):
        c.line(60 + i * 120, height - 146, 60 + i * 120, y + 16)
    c.showPage()

    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, height - 70, "Typical Performance Characteristics")
    image = Image.new("RGB", (240, 180))
    for px in range(240):
        for py in range(180):
            image.putpixel((px, py), (px % 256, py % 256, (px + py) % 256))
    c.drawImage(ImageReader(image), 60, height - 320, width=240, height=180)
    c.showPage()
    c.save()
    return path


def test_info(fake_datasheet):
    data = parse.info(fake_datasheet)
    assert data["page_count"] == 2
    assert data["likely_scanned"] is False
    assert "EDA1234" in data["pages"][0]["first_line"]


def test_extract_text(fake_datasheet):
    pages = parse.extract_text(fake_datasheet)
    assert len(pages) == 2
    assert "Low Noise Operational Amplifier" in pages[0]["text"]
    assert pages[0]["source"] == "text-layer"
    assert "Typical Performance" in pages[1]["text"]


def test_extract_text_page_selection(fake_datasheet):
    pages = parse.extract_text(fake_datasheet, "2")
    assert [p["page"] for p in pages] == [2]


def test_extract_tables(fake_datasheet):
    tables = parse.extract_tables(fake_datasheet, "1")
    assert tables, "the absolute maximum ratings table should be found"
    flat = [cell for row in tables[0]["rows"] for cell in row]
    assert "Supply voltage VDD" in flat
    assert "150" in flat


def test_extract_images(fake_datasheet, tmp_path):
    images = parse.extract_images(fake_datasheet, tmp_path / "img")
    assert len(images) == 1
    assert images[0]["page"] == 2
    assert images[0]["width"] == 240 and images[0]["height"] == 180
    assert (tmp_path / "img").exists()


def test_small_images_are_skipped(fake_datasheet, tmp_path):
    images = parse.extract_images(fake_datasheet, tmp_path / "img", min_pixels=10**9)
    assert images == []


def test_render_pages(fake_datasheet, tmp_path):
    rendered = parse.render_pages(fake_datasheet, tmp_path / "pages", "1", dpi=72)
    assert len(rendered) == 1
    assert rendered[0]["width"] > 500


def test_render_page_limit(fake_datasheet, tmp_path):
    with pytest.raises(EdaError, match="refusing to render"):
        parse.render_pages(fake_datasheet, tmp_path / "pages", max_pages=1)


def test_find(fake_datasheet):
    hits = parse.find(fake_datasheet, ["absolute maximum"])
    assert hits and hits[0]["page"] == 1
    assert "Absolute Maximum Ratings" in hits[0]["snippet"]


def test_find_regex(fake_datasheet):
    hits = parse.find(fake_datasheet, [r"\d+\.\d+ V to \d+\.\d+ V"], regex=True)
    assert hits[0]["match"] == "2.7 V to 5.5 V"


def test_outline(fake_datasheet):
    sections = parse.outline(fake_datasheet)
    assert {"page": 1, "section": "absolute maximum ratings"} in sections


def test_parse_all_writes_a_manifest(fake_datasheet, tmp_path):
    out = tmp_path / "extract"
    result = parse.parse_all(fake_datasheet, out, want_renders=True, dpi=72)
    assert (out / "index.json").exists()
    assert (out / "full-text.txt").exists()
    assert (out / "text" / "page-001.txt").exists()
    assert result["tables"]["count"] >= 1
    assert len(result["images"]["items"]) == 1
    assert len(result["renders"]["items"]) == 2


def test_missing_file(tmp_path):
    with pytest.raises(EdaError):
        parse.info(tmp_path / "nope.pdf")


def test_page_range_parsing():
    from eda_toolkit.util import parse_page_range

    assert parse_page_range("1-3,7", 10) == [0, 1, 2, 6]
    assert parse_page_range(None, 3) == [0, 1, 2]
    assert parse_page_range("2-", 4) == [1, 2, 3]
    assert parse_page_range("5", 3) == []  # clamped to the document
    with pytest.raises(EdaError):
        parse_page_range("3-1", 5)
