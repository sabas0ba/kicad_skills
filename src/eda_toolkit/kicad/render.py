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


def _short(exc: Exception, limit: int = 400) -> str:
    """kicad-cli prints its whole usage text on a bad flag; keep reports readable."""
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return message[:limit] + (" ..." if len(message) > limit else "")


def pdf_to_png(
    pdf_path: str | os.PathLike[str], out_prefix: str | os.PathLike[str], dpi: int = 300
) -> list[str]:
    """Rasterise every page of a PDF next to ``out_prefix``."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    written: list[str] = []
    try:
        for index in range(len(pdf)):
            image = pdf[index].render(scale=dpi / 72).to_pil()
            suffix = "" if len(pdf) == 1 else f"-{index + 1}"
            dest = Path(f"{out_prefix}{suffix}.png")
            ensure_dir(dest.parent)
            image.save(dest)
            written.append(str(dest))
    finally:
        pdf.close()
    return written


def render_board(
    target: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    views: Sequence[str] | None = None,
    dpi: int = 300,
    three_d: bool = True,
    per_layer: bool = False,
) -> dict[str, Any]:
    """Plot the requested 2D views (PNG + SVG) and optional 3D renders."""
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
        "images": [],
        "errors": [],
    }

    for view in wanted:
        if view.startswith("layer:"):
            layer = view.split(":", 1)[1]
            layers, mirror = [layer, "Edge.Cuts"], layer.startswith("B.")
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
            pngs = pdf_to_png(pdf_path, out / name, dpi=dpi)
        except Exception as exc:
            result["errors"].append({"view": view, "error": _short(exc)})
            continue
        for png in pngs:
            result["images"].append(
                {"view": name, "layers": layers, "mirrored": mirror, "path": png, "kind": "plot"}
            )

    if three_d:
        for side, extra in (("top", {}), ("bottom", {}), ("top", {"rotate": "-45,0,45"})):
            label = (
                "3d-top"
                if side == "top" and not extra
                else ("3d-bottom" if side == "bottom" else "3d-iso")
            )
            dest = out / f"{label}.png"
            try:
                kicad_cli.render(board_path, dest, side=side, **extra)
                result["images"].append({"view": label, "path": str(dest), "kind": "3d"})
            except Exception as exc:
                result["errors"].append({"view": label, "error": _short(exc)})

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
    result = {"schematic": str(sch), "pdf": str(pdf_path), "images": pages}
    write_json(out / "images.json", result)
    return result
