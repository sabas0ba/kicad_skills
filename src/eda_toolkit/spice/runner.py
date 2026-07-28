"""Run ngspice in batch mode and turn the results into CSV / JSON / PNG."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..util import EdaError, ensure_dir, require_tool, run, which, write_json
from . import measure as measure_mod
from . import rawfile

NGSPICE = os.environ.get("NGSPICE", "ngspice")

ANALYSIS_DIRECTIVES = (
    ".ac",
    ".tran",
    ".dc",
    ".op",
    ".noise",
    ".disto",
    ".pz",
    ".sens",
    ".tf",
    ".four",
    ".sp",
)
ERROR_PATTERNS = (
    re.compile(r"^\s*Error[: ]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"fatal error", re.IGNORECASE),
    re.compile(r"simulation\(s\) aborted", re.IGNORECASE),
    re.compile(r"can't find|unknown subckt|undefined parameter", re.IGNORECASE),
    re.compile(r"singular matrix", re.IGNORECASE),
    re.compile(r"no such (vector|parameter)", re.IGNORECASE),
)


def available() -> bool:
    return which(NGSPICE) is not None


def version() -> str:
    result = run([NGSPICE, "-v"], timeout=60, check=False)
    lines = [ln.strip() for ln in (result.stdout + result.stderr).splitlines() if ln.strip()]
    for line in lines:
        if "ngspice" in line.lower() and any(ch.isdigit() for ch in line):
            return line.strip("* ")
    return lines[0] if lines else "unknown"


def lint_netlist(text: str) -> list[str]:
    """Cheap sanity checks that catch the usual netlist mistakes."""
    problems: list[str] = []
    lines = [ln.strip() for ln in text.splitlines()]
    body = [ln for ln in lines if ln and not ln.startswith("*")]
    if not body:
        problems.append("netlist is empty")
        return problems
    lower = "\n".join(body).lower()
    if not any(d in lower for d in ANALYSIS_DIRECTIVES) and ".control" not in lower:
        problems.append(
            "no analysis directive found (.op/.dc/.ac/.tran/...) - the run will produce no data"
        )
    if not re.search(r"^\s*\.end\s*$", text, re.IGNORECASE | re.MULTILINE):
        problems.append("missing '.end' line")
    node_zero = re.search(r"\b0\b", text)
    if not node_zero:
        problems.append("no node 0 (ground) referenced - ngspice needs a ground node")
    return problems


def _prepare_deck(netlist_path: Path, work: Path, raw_name: str) -> Path:
    """Copy the deck into the work dir; force an ASCII rawfile when we control the run."""
    text = netlist_path.read_text(encoding="utf-8", errors="replace")
    dest = work / netlist_path.name
    dest.write_text(text, encoding="utf-8")
    return dest


def run_netlist(
    netlist: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    timeout: int = 600,
    make_plots: bool = True,
    extra_args: Sequence[str] = (),
) -> dict[str, Any]:
    """Simulate a SPICE deck and write raw/CSV/plots/summary into ``out_dir``."""
    require_tool(NGSPICE, "Run this command through ./bin/eda so it executes inside the container.")
    netlist_path = Path(netlist).resolve()
    if not netlist_path.exists():
        raise EdaError(f"no such netlist: {netlist}")
    out = ensure_dir(out_dir).resolve()
    work = ensure_dir(out / "work")

    # relative .include/.lib paths must keep working
    for sibling in netlist_path.parent.iterdir():
        if sibling.is_file() and sibling.suffix.lower() in {
            ".lib",
            ".mod",
            ".sub",
            ".cir",
            ".inc",
            ".txt",
            ".model",
        }:
            shutil.copy2(sibling, work / sibling.name)

    deck = _prepare_deck(netlist_path, work, "sim.raw")
    warnings = lint_netlist(deck.read_text(encoding="utf-8"))

    raw_path = work / "sim.raw"
    result = run(
        [NGSPICE, "-b", "-r", str(raw_path), *extra_args, deck.name],
        cwd=work,
        timeout=timeout,
        check=False,
    )
    log = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    (out / "ngspice.log").write_text(log, encoding="utf-8")

    errors = sorted(
        {m.group(0).strip() for pattern in ERROR_PATTERNS for m in pattern.finditer(log)}
    )
    summary: dict[str, Any] = {
        "netlist": str(netlist_path),
        "out_dir": str(out),
        "returncode": result.returncode,
        "log": str(out / "ngspice.log"),
        "lint": warnings,
        "errors": errors,
        "plots": [],
    }

    if not raw_path.exists():
        summary["ok"] = False
        summary["message"] = (
            "ngspice produced no rawfile. Check ngspice.log - the most common causes are a "
            "syntax error, a missing model/.include, or a missing analysis directive."
        )
        write_json(out / "summary.json", summary)
        raise EdaError(summary["message"] + "\n" + log[-4000:])

    plots = rawfile.parse(raw_path)
    csv_dir = ensure_dir(out / "csv")
    plot_dir = ensure_dir(out / "plots") if make_plots else None

    for index, plot in enumerate(plots, start=1):
        entry = plot.to_dict()
        slug = f"{index:02d}-{plot.analysis}"
        entry["csv"] = str(rawfile.to_csv(plot, csv_dir / f"{slug}.csv"))
        entry["measurements"] = measure_mod.measure(plot)
        if plot_dir is not None:
            try:
                entry["plot"] = str(plot_png(plot, plot_dir / f"{slug}.png"))
            except Exception as exc:  # plotting must never break a good simulation
                entry["plot_error"] = f"{type(exc).__name__}: {exc}"
        summary["plots"].append(entry)

    summary["raw"] = str(raw_path)
    summary["ok"] = result.returncode == 0 and not errors
    write_json(out / "summary.json", summary)
    return summary


def plot_png(
    plot: rawfile.Plot, dest: str | os.PathLike[str], *, signals: Sequence[str] | None = None
) -> Path:
    """Render a plot: Bode pair for AC, linear traces otherwise."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    out = Path(dest)
    ensure_dir(out.parent)
    names = list(signals) if signals else plot.signals()
    names = [n for n in names if n in plot.data][:12]
    sweep = plot.data[plot.sweep]
    x = np.abs(sweep) if plot.analysis == "ac" else np.real(sweep)

    if plot.analysis == "ac":
        fig, (ax_mag, ax_phase) = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
        for name in names:
            values = plot.data[name]
            ax_mag.semilogx(x, 20 * np.log10(np.maximum(np.abs(values), 1e-30)), label=name)
            ax_phase.semilogx(x, np.angle(values, deg=True), label=name)
        ax_mag.set_ylabel("magnitude [dB]")
        ax_mag.grid(True, which="both", alpha=0.3)
        ax_mag.legend(fontsize="small")
        ax_phase.set_ylabel("phase [deg]")
        ax_phase.set_xlabel("frequency [Hz]")
        ax_phase.grid(True, which="both", alpha=0.3)
        fig.suptitle(plot.plotname)
    else:
        fig, ax = plt.subplots(figsize=(9, 5))
        for name in names:
            ax.plot(x, np.real(plot.data[name]), label=name)
        ax.set_xlabel(plot.sweep)
        ax.set_ylabel("value")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize="small")
        ax.set_title(plot.plotname)

    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def netlist_from_schematic(
    target: str | os.PathLike[str], dest: str | os.PathLike[str], *, fmt: str = "spice"
) -> Path:
    """Export a SPICE netlist from a KiCad schematic (needs Spice model fields)."""
    from ..kicad import kicad_cli, schematic

    sch = schematic.find_root_schematic(target)
    return kicad_cli.export_netlist(sch, dest, fmt=fmt)
