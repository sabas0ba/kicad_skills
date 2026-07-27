"""Thin wrapper around ``kicad-cli`` (available inside the container image)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence

from ..util import CommandResult, EdaError, ensure_dir, require_tool, run, which

KICAD_CLI = os.environ.get("KICAD_CLI", "kicad-cli")


def available() -> bool:
    return which(KICAD_CLI) is not None


def _home_env() -> dict[str, str]:
    """kicad-cli needs a writable HOME for its settings directory."""
    home = os.environ.get("HOME", "")
    if home and os.access(home, os.W_OK):
        return {}
    tmp_home = Path(tempfile.gettempdir()) / "eda-kicad-home"
    ensure_dir(tmp_home)
    return {"HOME": str(tmp_home)}


def invoke(args: Sequence[str], *, timeout: int = 900, check: bool = True) -> CommandResult:
    require_tool(
        KICAD_CLI,
        "Run this command through ./bin/eda so it executes inside the eda-toolkit container.",
    )
    return run([KICAD_CLI, *args], timeout=timeout, check=check, env=_home_env())


def version() -> str:
    return invoke(["version"], timeout=60).stdout.strip()


# -- schematic ------------------------------------------------------------


def erc(schematic: str | os.PathLike[str], *, severity_all: bool = True,
        units: str = "mm") -> dict[str, Any]:
    """Run ERC and return the JSON report."""
    sch = Path(schematic)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "erc.json"
        args = ["sch", "erc", "--format", "json", "--units", units, "-o", str(out)]
        if severity_all:
            args.append("--severity-all")
        args.append(str(sch))
        result = invoke(args, check=False)
        if not out.exists():
            raise EdaError(f"kicad-cli sch erc produced no report:\n{result.stderr or result.stdout}")
        return json.loads(out.read_text(encoding="utf-8"))


def export_netlist(schematic: str | os.PathLike[str], dest: str | os.PathLike[str],
                   fmt: str = "kicadxml") -> Path:
    out = Path(dest)
    ensure_dir(out.parent)
    invoke(["sch", "export", "netlist", "--format", fmt, "-o", str(out), str(schematic)])
    if not out.exists():
        raise EdaError(f"netlist export produced no file: {out}")
    return out


def export_bom(schematic: str | os.PathLike[str], dest: str | os.PathLike[str]) -> Path:
    out = Path(dest)
    ensure_dir(out.parent)
    invoke(["sch", "export", "bom", "-o", str(out), str(schematic)])
    return out


def export_sch_pdf(schematic: str | os.PathLike[str], dest: str | os.PathLike[str]) -> Path:
    out = Path(dest)
    ensure_dir(out.parent)
    invoke(["sch", "export", "pdf", "-o", str(out), str(schematic)])
    return out


def export_sch_svg(schematic: str | os.PathLike[str], out_dir: str | os.PathLike[str]) -> Path:
    out = ensure_dir(out_dir)
    invoke(["sch", "export", "svg", "-o", str(out), str(schematic)])
    return out


# -- board ----------------------------------------------------------------


def drc(board: str | os.PathLike[str], *, schematic_parity: bool = True,
        all_track_errors: bool = True, units: str = "mm") -> dict[str, Any]:
    """Run DRC (with zone refill) and return the JSON report."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "drc.json"
        args = ["pcb", "drc", "--format", "json", "--units", units, "--severity-all",
                "--refill-zones", "-o", str(out)]
        if schematic_parity:
            args.append("--schematic-parity")
        if all_track_errors:
            args.append("--all-track-errors")
        args.append(str(board))
        result = invoke(args, check=False)
        if not out.exists():
            raise EdaError(f"kicad-cli pcb drc produced no report:\n{result.stderr or result.stdout}")
        return json.loads(out.read_text(encoding="utf-8"))


def board_stats(board: str | os.PathLike[str]) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "stats.txt"
        invoke(["pcb", "export", "stats", "-o", str(out), str(board)], check=False)
        return out.read_text(encoding="utf-8") if out.exists() else ""


def export_pcb_pdf(board: str | os.PathLike[str], dest: str | os.PathLike[str],
                   layers: Sequence[str], *, mirror: bool = False,
                   black_and_white: bool = False) -> Path:
    """Single-file PDF plot of the given layer list, scaled to fill the page.

    ``pcb export pdf`` has no --page-size-mode (that is an SVG only option) and
    leaves out the drawing sheet unless --include-border-title is given.
    """
    out = Path(dest)
    ensure_dir(out.parent)
    args = [
        "pcb", "export", "pdf", "--mode-single", "--scale", "0",
        "--layers", ",".join(layers), "-o", str(out),
    ]
    if mirror:
        args.append("--mirror")
    if black_and_white:
        args.append("--black-and-white")
    args.append(str(board))
    invoke(args)
    return out


def export_pcb_svg(board: str | os.PathLike[str], dest: str | os.PathLike[str],
                   layers: Sequence[str], *, mirror: bool = False) -> Path:
    out = Path(dest)
    ensure_dir(out.parent)
    args = [
        "pcb", "export", "svg", "--mode-single", "--exclude-drawing-sheet",
        "--page-size-mode", "2", "--layers", ",".join(layers), "-o", str(out),
    ]
    if mirror:
        args.append("--mirror")
    args.append(str(board))
    invoke(args)
    return out


def render(board: str | os.PathLike[str], dest: str | os.PathLike[str], *, side: str = "top",
           width: int = 1600, height: int = 1200, quality: str = "basic",
           rotate: str | None = None, zoom: float | None = None) -> Path:
    """3D render to PNG. Works headless but needs the 3D models from the image."""
    out = Path(dest)
    ensure_dir(out.parent)
    args = ["pcb", "render", "--side", side, "-w", str(width), "-h", str(height),
            "--quality", quality, "--background", "opaque"]
    if rotate:
        args += ["--rotate", rotate]
    if zoom:
        args += ["--zoom", str(zoom)]
    args += ["-o", str(out), str(board)]
    invoke(args, timeout=1800)
    return out
