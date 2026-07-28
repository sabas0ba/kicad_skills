"""Produce a fabrication and assembly package from a KiCad project.

Everything a board house and an assembly house need, in one directory plus a
zip: Gerbers, an Excellon drill set (with a map and a report), a pick and place
file, the BOM, and optionally a STEP model. A manifest records what was written
and the checks that were run, so the package can be diffed between revisions.
"""

from __future__ import annotations

import os
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..util import EdaError, ensure_dir, write_json
from . import kicad_cli, pcb, schematic

# The layer set a two-sided board needs; extra copper layers are added per board.
BASE_LAYERS = (
    "F.Cu",
    "B.Cu",
    "F.Paste",
    "B.Paste",
    "F.Silkscreen",
    "B.Silkscreen",
    "F.Mask",
    "B.Mask",
    "Edge.Cuts",
)
FAB_LAYERS = ("F.Fab", "B.Fab")


def gerber_layers(board: pcb.Board, *, include_fab: bool = False) -> list[str]:
    """Copper layers of this board plus the usual technical layers."""
    copper = list(board.copper_layers)
    others = [layer for layer in BASE_LAYERS if not layer.endswith(".Cu")]
    layers = copper + others
    if include_fab:
        layers += list(FAB_LAYERS)
    available = {layer["name"] for layer in board.layers}
    available |= {layer.get("user_name", "") for layer in board.layers}
    available |= set(copper)
    return [layer for layer in layers if layer in available or layer == "Edge.Cuts"]


def export_package(
    target: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    include_fab_layers: bool = False,
    pos_format: str = "csv",
    step: bool = False,
    ipc2581: bool = False,
    exclude_dnp: bool = True,
    make_zip: bool = True,
) -> dict[str, Any]:
    """Write the fabrication package. Individual steps may fail independently."""
    board_path = pcb.find_board(target)
    board = pcb.parse(board_path)
    out = ensure_dir(out_dir)
    gerber_dir = ensure_dir(out / "gerbers")

    manifest: dict[str, Any] = {
        "board": str(board_path),
        "out_dir": str(out),
        "board_size_mm": board.size_mm(),
        "layer_count": len(board.copper_layers),
        "steps": [],
        "errors": [],
    }

    def step_run(name: str, func) -> None:
        try:
            produced = func()
        except Exception as exc:
            manifest["errors"].append({"step": name, "error": _short(exc)})
            return
        manifest["steps"].append({"step": name, "output": produced})

    layers = gerber_layers(board, include_fab=include_fab_layers)
    step_run(
        "gerbers",
        lambda: [
            str(p) for p in _sorted_files(kicad_cli.export_gerbers(board_path, gerber_dir, layers))
        ],
    )
    step_run(
        "drill",
        lambda: [str(p) for p in _sorted_files(kicad_cli.export_drill(board_path, gerber_dir))],
    )
    step_run(
        "position",
        lambda: str(
            kicad_cli.export_pos(
                board_path,
                out / f"{board_path.stem}-pos.{pos_format}",
                fmt=pos_format,
                exclude_dnp=exclude_dnp,
            )
        ),
    )

    sch_path = _schematic_next_to(board_path)
    if sch_path is not None:
        step_run(
            "bom",
            lambda: str(
                kicad_cli.export_bom(
                    sch_path,
                    out / f"{board_path.stem}-bom.csv",
                    group_by="Value,Footprint",
                    exclude_dnp=exclude_dnp,
                )
            ),
        )
    else:
        manifest["errors"].append({"step": "bom", "error": "no schematic next to the board"})

    if step:
        step_run(
            "step", lambda: str(kicad_cli.export_step(board_path, out / f"{board_path.stem}.step"))
        )
    if ipc2581:
        step_run(
            "ipc2581",
            lambda: str(kicad_cli.export_ipc2581(board_path, out / f"{board_path.stem}.xml")),
        )

    if make_zip:
        archive = out / f"{board_path.stem}-fab.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(out.rglob("*")):
                if path.is_file() and path != archive:
                    zf.write(path, path.relative_to(out))
        manifest["zip"] = str(archive)

    write_json(out / "manifest.json", manifest)
    manifest["ok"] = not manifest["errors"]
    return manifest


def _schematic_next_to(board_path: Path) -> Path | None:
    try:
        return schematic.find_root_schematic(board_path.with_suffix(".kicad_sch"))
    except EdaError:
        try:
            return schematic.find_root_schematic(board_path.parent)
        except EdaError:
            return None


def _sorted_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file())


def _short(exc: Exception, limit: int = 300) -> str:
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return message[:limit] + (" ..." if len(message) > limit else "")


def bom(
    target: str | os.PathLike[str],
    dest: str | os.PathLike[str],
    *,
    group_by: str = "Value,Footprint",
    exclude_dnp: bool = True,
    fields: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Grouped bill of materials as CSV, plus a parsed summary."""
    import csv

    sch = schematic.find_root_schematic(target)
    out = Path(dest)
    kicad_cli.export_bom(sch, out, group_by=group_by, exclude_dnp=exclude_dnp, fields=fields)
    rows: list[dict[str, str]] = []
    if out.exists():
        with out.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    total = 0
    for row in rows:
        for key in ("Qty", "QUANTITY", "Quantity"):
            if key in row and str(row[key]).strip().isdigit():
                total += int(row[key])
                break
    return {
        "schematic": str(sch),
        "csv": str(out),
        "line_items": len(rows),
        "total_parts": total,
        "rows": rows,
    }
