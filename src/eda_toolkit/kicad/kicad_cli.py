"""Thin wrapper around ``kicad-cli`` (available inside the container image)."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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


LIBRARY_TABLES = ("sym-lib-table", "fp-lib-table", "design-block-lib-table")
TEMPLATE_DIR = Path(os.environ.get("KICAD_TEMPLATE_DIR", "/usr/share/kicad/template"))
_VERSION_CACHE: str | None = None


def version() -> str:
    """kicad-cli's version, cached so library seeding costs one process, not many."""
    global _VERSION_CACHE
    if _VERSION_CACHE is None:
        result = run([KICAD_CLI, "version"], timeout=60, check=False, env=_home_env())
        _VERSION_CACHE = result.stdout.strip()
    return _VERSION_CACHE


def ensure_library_tables(env: dict[str, str] | None = None) -> list[str]:
    """Seed KiCad's global library tables into the config directory.

    Without them every symbol and footprint resolves to "the current
    configuration does not include the library ...", which buries the real
    findings under one violation per component. The GUI writes these on first
    start; kicad-cli never does. The container entrypoint seeds them too, but
    doing it here means the toolkit behaves the same when the entrypoint is
    bypassed - a plain ``docker run --entrypoint``, or the image used as a base.
    """
    home = Path((env or {}).get("HOME") or os.environ.get("HOME") or tempfile.gettempdir())
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    release = ".".join(version().split(".")[:2])
    if not release:
        return []
    config_dir = config_root / "kicad" / release
    seeded: list[str] = []
    for table in LIBRARY_TABLES:
        source = TEMPLATE_DIR / table
        target = config_dir / table
        if not source.exists() or target.exists():
            continue
        try:
            ensure_dir(config_dir)
            target.write_bytes(source.read_bytes())
            seeded.append(table)
        except OSError:  # read-only config: KiCad will report the missing libraries
            break
    return seeded


def invoke(args: Sequence[str], *, timeout: int = 900, check: bool = True,
           seed_libraries: bool = True) -> CommandResult:
    require_tool(
        KICAD_CLI,
        "Run this command through ./bin/eda.sh so it executes inside the eda-toolkit container.",
    )
    env = _home_env()
    if seed_libraries:
        ensure_library_tables(env)
    return run([KICAD_CLI, *args], timeout=timeout, check=check, env=env)


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


def export_bom(schematic: str | os.PathLike[str], dest: str | os.PathLike[str], *,
               group_by: str = "", exclude_dnp: bool = False,
               fields: Sequence[str] | None = None) -> Path:
    out = Path(dest)
    ensure_dir(out.parent)
    args = ["sch", "export", "bom", "-o", str(out)]
    if group_by:
        args += ["--group-by", group_by]
    if fields:
        args += ["--fields", ",".join(fields)]
    if exclude_dnp:
        args.append("--exclude-dnp")
    args.append(str(schematic))
    invoke(args)
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


# -- fabrication outputs ---------------------------------------------------


def export_gerbers(board: str | os.PathLike[str], out_dir: str | os.PathLike[str],
                   layers: Sequence[str], *, precision: int = 6,
                   subtract_soldermask: bool = True) -> Path:
    """Plot the Gerber set (X2 with netlist attributes, as fabs expect today)."""
    out = ensure_dir(out_dir)
    args = ["pcb", "export", "gerbers", "--layers", ",".join(layers),
            "--precision", str(precision), "--check-zones", "-o", str(out)]
    if subtract_soldermask:
        args.append("--subtract-soldermask")
    args.append(str(board))
    invoke(args)
    return out


def export_drill(board: str | os.PathLike[str], out_dir: str | os.PathLike[str], *,
                 fmt: str = "excellon", units: str = "mm",
                 separate_th: bool = False) -> Path:
    """Excellon drill files plus a map and a hit report."""
    out = ensure_dir(out_dir)
    args = ["pcb", "export", "drill", "--format", fmt, "--excellon-units", units,
            "--generate-map", "--map-format", "pdf", "--generate-report",
            "--report-path", str(Path(out) / "drill-report.txt"), "-o", str(out)]
    if separate_th:
        args.append("--excellon-separate-th")
    args.append(str(board))
    invoke(args)
    return out


def export_pos(board: str | os.PathLike[str], dest: str | os.PathLike[str], *,
               fmt: str = "csv", side: str = "both", units: str = "mm",
               exclude_dnp: bool = True) -> Path:
    """Pick and place file for the assembly house."""
    out = Path(dest)
    ensure_dir(out.parent)
    args = ["pcb", "export", "pos", "--format", fmt, "--side", side,
            "--units", units, "-o", str(out)]
    if exclude_dnp:
        args.append("--exclude-dnp")
    args.append(str(board))
    invoke(args)
    return out


def export_step(board: str | os.PathLike[str], dest: str | os.PathLike[str], *,
                drill_origin: bool = False) -> Path:
    """3D model for mechanical fit checks."""
    out = Path(dest)
    ensure_dir(out.parent)
    args = ["pcb", "export", "step", "--no-dnp", "-o", str(out)]
    if drill_origin:
        args += ["--drill-origin"]
    args.append(str(board))
    invoke(args, timeout=1800)
    return out


def export_ipc2581(board: str | os.PathLike[str], dest: str | os.PathLike[str]) -> Path:
    """Single-file fabrication exchange format (IPC-2581B)."""
    out = Path(dest)
    ensure_dir(out.parent)
    invoke(["pcb", "export", "ipc2581", "-o", str(out), str(board)])
    return out
