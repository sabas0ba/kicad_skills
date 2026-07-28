"""Extract text, tables, embedded images and page renders from datasheet PDFs."""

from __future__ import annotations

import csv
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..util import EdaError, ensure_dir, parse_page_range, write_json

MIN_IMAGE_PIXELS = int(os.environ.get("EDA_MIN_IMAGE_PIXELS", "10000"))  # skip logos/rules


def _open_plumber(path: Path):
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency guaranteed in container
        raise EdaError("pdfplumber is not installed") from exc
    if not path.exists():
        raise EdaError(f"no such PDF: {path}")
    return pdfplumber.open(str(path))


def _open_pdfium(path: Path):
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover
        raise EdaError("pypdfium2 is not installed") from exc
    return pdfium.PdfDocument(str(path))


def info(pdf_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Page count, metadata and a per-page text-density map."""
    path = Path(pdf_path)
    with _open_plumber(path) as pdf:
        meta = {k: str(v) for k, v in (pdf.metadata or {}).items()}
        pages = []
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            pages.append(
                {
                    "page": i + 1,
                    "width": round(float(page.width), 1),
                    "height": round(float(page.height), 1),
                    "chars": len(text),
                    "first_line": (text.strip().splitlines() or [""])[0][:120],
                }
            )
    scanned = all(p["chars"] < 50 for p in pages) if pages else False
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "page_count": len(pages),
        "metadata": meta,
        "likely_scanned": scanned,
        "pages": pages,
    }


def extract_text(
    pdf_path: str | os.PathLike[str],
    pages: str | None = None,
    *,
    layout: bool = False,
    ocr: bool = False,
) -> list[dict[str, Any]]:
    """Return per-page text. With ocr=True, empty pages are passed through tesseract."""
    path = Path(pdf_path)
    out: list[dict[str, Any]] = []
    with _open_plumber(path) as pdf:
        indices = parse_page_range(pages, len(pdf.pages))
        for idx in indices:
            page = pdf.pages[idx]
            text = page.extract_text(layout=layout) or ""
            source = "text-layer"
            if ocr and len(text.strip()) < 50:
                ocr_text = _ocr_page(path, idx)
                if ocr_text:
                    text, source = ocr_text, "ocr"
            out.append({"page": idx + 1, "source": source, "text": text})
    return out


def _ocr_page(path: Path, index: int, dpi: int = 300) -> str:
    try:
        import pytesseract  # type: ignore
    except ImportError:
        return ""
    from ..util import which

    if not which("tesseract"):
        return ""
    pdf = _open_pdfium(path)
    try:
        image = pdf[index].render(scale=dpi / 72).to_pil()
        return pytesseract.image_to_string(image)
    finally:
        pdf.close()


def extract_tables(
    pdf_path: str | os.PathLike[str], pages: str | None = None
) -> list[dict[str, Any]]:
    """Extract tables (parameter tables, absolute maximum ratings, ...)."""
    path = Path(pdf_path)
    tables: list[dict[str, Any]] = []
    with _open_plumber(path) as pdf:
        for idx in parse_page_range(pages, len(pdf.pages)):
            page = pdf.pages[idx]
            for t_i, table in enumerate(page.extract_tables()):
                rows = [
                    ["" if c is None else re.sub(r"\s+", " ", c).strip() for c in row]
                    for row in table
                ]
                rows = [r for r in rows if any(c for c in r)]
                if len(rows) < 2:
                    continue
                tables.append({"page": idx + 1, "index": t_i + 1, "rows": rows})
    return tables


def render_pages(
    pdf_path: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    pages: str | None = None,
    *,
    dpi: int = 150,
    max_pages: int = 40,
) -> list[dict[str, Any]]:
    """Rasterise pages to PNG so they can be inspected visually."""
    path = Path(pdf_path)
    out = ensure_dir(out_dir)
    pdf = _open_pdfium(path)
    rendered: list[dict[str, Any]] = []
    try:
        indices = parse_page_range(pages, len(pdf))
        if len(indices) > max_pages:
            raise EdaError(
                f"refusing to render {len(indices)} pages (limit {max_pages}); "
                "narrow it down with --pages"
            )
        for idx in indices:
            image = pdf[idx].render(scale=dpi / 72).to_pil()
            dest = out / f"page-{idx + 1:03d}.png"
            image.save(dest)
            rendered.append(
                {"page": idx + 1, "path": str(dest), "width": image.width, "height": image.height}
            )
    finally:
        pdf.close()
    return rendered


def extract_images(
    pdf_path: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    pages: str | None = None,
    *,
    min_pixels: int = MIN_IMAGE_PIXELS,
) -> list[dict[str, Any]]:
    """Extract embedded raster images (block diagrams, curves, package drawings).

    Vector-only figures have no embedded image; use ``render_pages`` for those.
    """
    import pypdfium2 as pdfium

    path = Path(pdf_path)
    out = ensure_dir(out_dir)
    pdf = _open_pdfium(path)
    found: list[dict[str, Any]] = []
    try:
        for idx in parse_page_range(pages, len(pdf)):
            page = pdf[idx]
            try:
                objects = list(
                    page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,), max_depth=6)
                )
            except Exception:  # pragma: no cover - malformed PDFs
                continue
            for n, obj in enumerate(objects, start=1):
                try:
                    pil = obj.get_bitmap(render=False).to_pil()
                except Exception:  # pragma: no cover
                    continue
                if pil.width * pil.height < min_pixels:
                    continue
                dest = out / f"page-{idx + 1:03d}-img-{n:02d}.png"
                if pil.mode not in ("RGB", "RGBA", "L"):
                    pil = pil.convert("RGB")
                pil.save(dest)
                try:
                    pos = obj.get_pos()
                except Exception:  # pragma: no cover
                    pos = None
                found.append(
                    {
                        "page": idx + 1,
                        "index": n,
                        "path": str(dest),
                        "width": pil.width,
                        "height": pil.height,
                        "bbox": [round(float(v), 1) for v in pos] if pos else None,
                    }
                )
    finally:
        pdf.close()
    return found


SECTION_HINTS = (
    "absolute maximum ratings",
    "recommended operating conditions",
    "electrical characteristics",
    "thermal information",
    "pin configuration",
    "pin functions",
    "typical application",
    "application information",
    "package outline",
    "timing requirements",
    "ordering information",
    "block diagram",
    "layout guidelines",
)


def find(
    pdf_path: str | os.PathLike[str],
    queries: Iterable[str],
    *,
    context: int = 200,
    regex: bool = False,
    max_hits: int = 50,
) -> list[dict[str, Any]]:
    """Locate text in the PDF - the fast way to jump to the relevant page."""
    hits: list[dict[str, Any]] = []
    pattern_list = []
    for q in queries:
        pattern_list.append(re.compile(q if regex else re.escape(q), re.IGNORECASE))
    for page in extract_text(pdf_path):
        text = page["text"]
        flat = re.sub(r"[ \t]+", " ", text)
        for pat in pattern_list:
            for m in pat.finditer(flat):
                start = max(0, m.start() - context // 2)
                hits.append(
                    {
                        "page": page["page"],
                        "query": pat.pattern,
                        "match": m.group(0),
                        "snippet": flat[start : m.end() + context // 2].replace("\n", " "),
                    }
                )
                if len(hits) >= max_hits:
                    return hits
    return hits


def outline(pdf_path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Guess the datasheet's section structure from common heading names."""
    sections: list[dict[str, Any]] = []
    for page in extract_text(pdf_path):
        lower = page["text"].lower()
        for hint in SECTION_HINTS:
            if hint in lower:
                sections.append({"page": page["page"], "section": hint})
    return sections


def parse_all(
    pdf_path: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    pages: str | None = None,
    want_text: bool = True,
    want_tables: bool = True,
    want_images: bool = True,
    want_renders: bool = False,
    dpi: int = 150,
    ocr: bool = False,
) -> dict[str, Any]:
    """One-shot extraction into a directory tree plus an index.json manifest."""
    path = Path(pdf_path)
    out = ensure_dir(out_dir)
    result: dict[str, Any] = {"pdf": str(path), "out_dir": str(out), "info": info(path)}

    if want_text:
        text_dir = ensure_dir(out / "text")
        pages_text = extract_text(path, pages, ocr=ocr)
        joined = []
        for page in pages_text:
            (text_dir / f"page-{page['page']:03d}.txt").write_text(page["text"], encoding="utf-8")
            joined.append(f"\n\n===== page {page['page']} =====\n{page['text']}")
        (out / "full-text.txt").write_text("".join(joined), encoding="utf-8")
        result["text"] = {
            "dir": str(text_dir),
            "full_text": str(out / "full-text.txt"),
            "pages": [
                {"page": p["page"], "chars": len(p["text"]), "source": p["source"]}
                for p in pages_text
            ],
        }

    if want_tables:
        table_dir = ensure_dir(out / "tables")
        tables = extract_tables(path, pages)
        entries = []
        for t in tables:
            dest = table_dir / f"page-{t['page']:03d}-table-{t['index']}.csv"
            with dest.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(t["rows"])
            entries.append(
                {
                    "page": t["page"],
                    "index": t["index"],
                    "path": str(dest),
                    "rows": len(t["rows"]),
                    "header": t["rows"][0][:8],
                }
            )
        result["tables"] = {"dir": str(table_dir), "count": len(entries), "items": entries}

    if want_images:
        result["images"] = {
            "dir": str(out / "images"),
            "items": extract_images(path, out / "images", pages),
        }

    if want_renders:
        result["renders"] = {
            "dir": str(out / "pages"),
            "items": render_pages(path, out / "pages", pages, dpi=dpi),
        }

    result["outline"] = outline(path)
    write_json(out / "index.json", result)
    return result
