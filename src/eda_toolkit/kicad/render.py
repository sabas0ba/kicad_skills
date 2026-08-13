"""Turn KiCad documents into PNG images so they can be reviewed visually."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..util import EdaError, ensure_dir, write_json
from . import kicad_cli, pcb, schematic

# name -> (layers, mirror)
BOARD_VIEWS: dict[str, tuple[list[str], bool]] = {
    "front": (["F.Cu", "F.Silkscreen", "F.Mask", "Edge.Cuts"], False),
    "back": (["B.Cu", "B.Silkscreen", "B.Mask", "Edge.Cuts"], True),
    "copper-front": (["F.Cu", "Edge.Cuts"], False),
    "copper-back": (["B.Cu", "Edge.Cuts"], True),
    "silk-front": (["F.Silkscreen", "F.Fab", "Edge.Cuts"], False),
    "silk-back": (["B.Silkscreen", "B.Fab", "Edge.Cuts"], True),
    "assembly-front": (["F.Fab", "F.Courtyard", "Edge.Cuts"], False),
    "outline": (["Edge.Cuts", "User.Drawings", "User.Comments"], False),
}

# Backgrounds every image in this module can be written on. KiCad plots onto an
# unpainted PDF page, so the colour is chosen while rasterising rather than keyed
# out afterwards - anti-aliased edges stay clean instead of fringing white.
BACKGROUNDS: dict[str, tuple[int, int, int, int]] = {
    "white": (255, 255, 255, 255),
    "black": (0, 0, 0, 255),
    "transparent": (0, 0, 0, 0),
}


def background_rgba(name: str) -> tuple[int, int, int, int]:
    """Resolve a background name, rejecting typos before anything is plotted."""
    try:
        return BACKGROUNDS[name]
    except KeyError:
        raise EdaError(
            f"unknown background {name!r}: choose one of {', '.join(BACKGROUNDS)}"
        ) from None


def _flatten(path: Path, fill: tuple[int, int, int, int]) -> None:
    """Composite an image with alpha onto an opaque colour, in place."""
    from PIL import Image

    with Image.open(path) as opened:
        if "A" not in opened.mode and opened.mode != "P":
            return
        image = opened.convert("RGBA")
    flat = Image.new("RGB", image.size, fill[:3])
    flat.paste(image, (0, 0), image)
    flat.save(path)


def _label_colour(fill: tuple[int, int, int, int]) -> str:
    """Contact sheet labels have to stay legible on whatever they are drawn on."""
    red, green, blue, alpha = fill
    if alpha == 0:
        return "#808080"  # the backdrop is unknown; mid grey reads on light and dark
    return "#222222" if (0.299 * red + 0.587 * green + 0.114 * blue) > 140 else "#dddddd"


def _short(exc: Exception, limit: int = 400) -> str:
    """kicad-cli prints its whole usage text on a bad flag; keep reports readable."""
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return message[:limit] + (" ..." if len(message) > limit else "")


def pdf_to_png(
    pdf_path: str | os.PathLike[str],
    out_prefix: str | os.PathLike[str],
    dpi: int = 300,
    *,
    background: str = "white",
) -> list[str]:
    """Rasterise every page of a PDF next to ``out_prefix``."""
    import pypdfium2 as pdfium

    fill = background_rgba(background)
    pdf = pdfium.PdfDocument(str(pdf_path))
    written: list[str] = []
    try:
        for index in range(len(pdf)):
            image = pdf[index].render(scale=dpi / 72, fill_color=fill).to_pil()
            suffix = "" if len(pdf) == 1 else f"-{index + 1}"
            dest = Path(f"{out_prefix}{suffix}.png")
            ensure_dir(dest.parent)
            image.save(dest)
            written.append(str(dest))
    finally:
        pdf.close()
    return written


def contact_sheet(
    images: Sequence[tuple[str, str]],
    dest: str | os.PathLike[str],
    *,
    columns: int = 3,
    cell: int = 700,
    background: str = "white",
) -> Path:
    """Tile labelled images into one sheet.

    Twelve separate PNGs is twelve things to open; one sheet is a glance. Used
    for the per-layer plots, where the question is usually "is anything on the
    wrong layer" rather than "what exactly is at 12.7 mm".
    """
    from PIL import Image, ImageDraw

    fill = background_rgba(background)
    entries = [(label, Path(path)) for label, path in images if Path(path).exists()]
    if not entries:
        raise EdaError("no images to tile")

    columns = max(1, min(columns, len(entries)))
    rows = (len(entries) + columns - 1) // columns
    label_height = 28

    # Rows are as tall as their tallest tile: board plots are landscape, so a
    # square grid would leave a third of the sheet empty.
    thumbs = []
    for label, path in entries:
        image = Image.open(path)
        image.thumbnail((cell, cell))
        thumbs.append((label, image))
    row_heights = [
        max(img.height for _, img in thumbs[row * columns : (row + 1) * columns])
        for row in range(rows)
    ]
    row_offsets = []
    offset = 0
    for height in row_heights:
        row_offsets.append(offset)
        offset += height + label_height
    # Tiled in RGBA whatever the background is: pasting a transparent tile with
    # its own alpha as the mask would multiply the alpha twice, which shows up as
    # a darkened halo. alpha_composite blends it properly.
    sheet = Image.new("RGBA", (columns * cell, offset), fill)
    draw = ImageDraw.Draw(sheet)
    label_colour = _label_colour(fill)

    for index, (label, image) in enumerate(thumbs):
        row = index // columns
        x = (index % columns) * cell + (cell - image.width) // 2
        y = row_offsets[row] + label_height + (row_heights[row] - image.height) // 2
        sheet.alpha_composite(image.convert("RGBA"), (x, y))
        draw.text(
            ((index % columns) * cell + 8, row_offsets[row] + 7),
            label,
            fill=label_colour,
        )

    out = Path(dest)
    ensure_dir(out.parent)
    # An opaque background leaves every pixel at alpha 255, so dropping the
    # channel there costs nothing and keeps the usual sheet a plain RGB PNG.
    if fill[3] == 255:
        sheet = sheet.convert("RGB")
    sheet.save(out)
    return out


def render_board(
    target: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    views: Sequence[str] | None = None,
    dpi: int = 300,
    three_d: bool = True,
    per_layer: bool = False,
    glb: bool = False,
    sheet: bool = True,
    background: str = "white",
) -> dict[str, Any]:
    """Plot the requested 2D views, the 3D renders, and tile them into a sheet."""
    fill = background_rgba(background)  # reject a typo before plotting anything
    board_path = pcb.find_board(target)
    out = ensure_dir(out_dir)
    board = pcb.parse(board_path)
    available_layers = set(board.copper_layers)
    for layer in board.layers:
        available_layers.add(layer["name"])
        if layer.get("user_name"):
            available_layers.add(layer["user_name"])

    wanted = (
        list(views) if views else ["front", "back", "copper-front", "copper-back", "silk-front"]
    )
    if per_layer:
        wanted += [f"layer:{name}" for name in board.copper_layers]

    result: dict[str, Any] = {
        "board": str(board_path),
        "out_dir": str(out),
        "background": background,
        "images": [],
        "errors": [],
    }

    for view in wanted:
        if view.startswith("layer:"):
            layer = view.split(":", 1)[1]
            # Edge.Cuts goes on every plot for context - including its own, where
            # naming it twice would just plot the outline over itself.
            layers = [layer] if layer == "Edge.Cuts" else [layer, "Edge.Cuts"]
            mirror = layer.startswith("B.")
            name = f"layer-{layer.replace('.', '_')}"
        else:
            if view not in BOARD_VIEWS:
                result["errors"].append({"view": view, "error": "unknown view"})
                continue
            layers, mirror = BOARD_VIEWS[view]
            name = view
        layers = [layer for layer in layers if layer in available_layers or layer == "Edge.Cuts"]
        if not layers:
            result["errors"].append({"view": view, "error": "no matching layers on this board"})
            continue
        pdf_path = out / f"{name}.pdf"
        try:
            kicad_cli.export_pcb_pdf(board_path, pdf_path, layers, mirror=mirror)
            pngs = pdf_to_png(pdf_path, out / name, dpi=dpi, background=background)
        except Exception as exc:
            result["errors"].append({"view": view, "error": _short(exc)})
            continue
        for png in pngs:
            result["images"].append(
                {"view": name, "layers": layers, "mirrored": mirror, "path": png, "kind": "plot"}
            )

    if three_d:
        # The 3D view has no white to override - it is drawn on the 3D viewer's
        # own themed background. So "white" leaves KiCad to it, and the other two
        # ask for an empty background and fill it here.
        bg_style = "opaque" if background == "white" else "transparent"
        for side, extra in (("top", {}), ("bottom", {}), ("top", {"rotate": "-45,0,45"})):
            label = (
                "3d-top"
                if side == "top" and not extra
                else ("3d-bottom" if side == "bottom" else "3d-iso")
            )
            dest = out / f"{label}.png"
            try:
                kicad_cli.render(board_path, dest, side=side, background=bg_style, **extra)
                if fill[3] == 255 and bg_style == "transparent":
                    _flatten(dest, fill)
                result["images"].append({"view": label, "path": str(dest), "kind": "3d"})
            except Exception as exc:
                result["errors"].append({"view": label, "error": _short(exc)})

    if glb:
        try:
            result["glb"] = str(kicad_cli.export_glb(board_path, out / "board.glb"))
        except Exception as exc:
            result["errors"].append({"view": "glb", "error": _short(exc)})

    if sheet and len(result["images"]) > 1:
        try:
            result["contact_sheet"] = str(
                contact_sheet(
                    [(i["view"], i["path"]) for i in result["images"]],
                    out / "contact-sheet.png",
                    background=background,
                )
            )
        except Exception as exc:
            result["errors"].append({"view": "contact-sheet", "error": _short(exc)})

    write_json(out / "images.json", result)
    return result


def render_schematic(
    target: str | os.PathLike[str], out_dir: str | os.PathLike[str], *, dpi: int = 200
) -> dict[str, Any]:
    """Plot the schematic to PDF and rasterise every sheet."""
    sch = schematic.find_root_schematic(target)
    out = ensure_dir(out_dir)
    pdf_path = out / "schematic.pdf"
    kicad_cli.export_sch_pdf(sch, pdf_path)
    if not pdf_path.exists():
        raise EdaError(f"schematic PDF export failed: {pdf_path}")
    pages = pdf_to_png(pdf_path, out / "sheet", dpi=dpi)
    result: dict[str, Any] = {"schematic": str(sch), "pdf": str(pdf_path), "images": pages}
    if len(pages) > 1:
        try:
            result["contact_sheet"] = str(
                contact_sheet(
                    [(Path(p).stem, p) for p in pages], out / "contact-sheet.png", columns=2
                )
            )
        except Exception as exc:
            result["contact_sheet_error"] = _short(exc)
    write_json(out / "images.json", result)
    return result
