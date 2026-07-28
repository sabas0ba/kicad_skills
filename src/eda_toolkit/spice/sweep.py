"""Statistical and corner analysis on top of a plain SPICE deck.

A single simulation says what the circuit does with ideal parts. These say what
it does with the parts you can actually buy, over the temperature range it has
to work in:

* :func:`monte_carlo` - resample component values inside their tolerance and
  report the spread of a chosen measurement.
* :func:`temperature_sweep` - run the same deck at several temperatures.

Both reuse :mod:`eda_toolkit.spice.runner`, so every trial produces the usual
measurements and the metric is picked out of them by name
(``ac.v(out).f_minus_3db_hz``, ``tran.v(out).overshoot_pct``, ``op.v(out)``).
"""

from __future__ import annotations

import math
import os
import random
import re
import statistics
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..util import EdaError, ensure_dir, write_json
from . import runner

# SPICE engineering suffixes. Order matters: "meg" must beat "m".
SUFFIXES: tuple[tuple[str, float], ...] = (
    ("meg", 1e6),
    ("mil", 25.4e-6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
)
VALUE_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z]*)$")
# R1 n+ n- 10k   /   C3 out 0 100n
PASSIVE_LINE = re.compile(
    r"^(?P<ref>[RCLrcl]\w*)(?P<mid>\s+\S+\s+\S+\s+)(?P<value>\S+)(?P<rest>.*)$"
)
PARAM_LINE = re.compile(
    r"^(?P<head>\s*\.param\s+)(?P<name>\w+)(?P<eq>\s*=\s*)(?P<value>\S+)", re.IGNORECASE
)


def parse_value(text: str) -> float:
    """``"4k7"`` is not SPICE; ``"4.7k"``, ``"100n"``, ``"1meg"`` are."""
    match = VALUE_RE.match(text.strip())
    if not match:
        raise EdaError(f"cannot read the SPICE value {text!r}")
    number, suffix = float(match.group(1)), match.group(2).lower()
    if not suffix:
        return number
    for name, factor in SUFFIXES:
        if suffix.startswith(name):
            return number * factor
    return number  # unknown trailing unit (e.g. "10ohm") - the number stands


def format_value(value: float) -> str:
    """Back to a compact SPICE literal, so the deck stays readable."""
    if value == 0:
        return "0"
    magnitude = abs(value)
    for name, factor in (
        ("meg", 1e6),
        ("k", 1e3),
        ("", 1.0),
        ("m", 1e-3),
        ("u", 1e-6),
        ("n", 1e-9),
        ("p", 1e-12),
        ("f", 1e-15),
    ):
        if magnitude >= factor:
            return f"{value / factor:.6g}{name}"
    return f"{value:.6g}"


def deck_values(text: str) -> dict[str, float]:
    """Every passive value and ``.param`` the deck defines, by name."""
    found: dict[str, float] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        param = PARAM_LINE.match(stripped)
        if param:
            try:
                found[param.group("name")] = parse_value(param.group("value"))
            except EdaError:
                pass
            continue
        passive = PASSIVE_LINE.match(stripped)
        if passive:
            try:
                found[passive.group("ref")] = parse_value(passive.group("value"))
            except EdaError:
                pass
    return found


def apply_values(text: str, overrides: dict[str, float]) -> str:
    """Rewrite passive values and ``.param`` assignments in place."""
    if not overrides:
        return text
    wanted = {k.lower(): v for k, v in overrides.items()}
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        param = PARAM_LINE.match(stripped) if stripped else None
        if param and param.group("name").lower() in wanted:
            name = param.group("name")
            seen.add(name.lower())
            value = format_value(wanted[name.lower()])
            out.append(f"{param.group('head')}{name}{param.group('eq')}{value}")
            continue
        passive = (
            PASSIVE_LINE.match(stripped) if stripped and not stripped.startswith("*") else None
        )
        if passive and passive.group("ref").lower() in wanted:
            ref = passive.group("ref")
            seen.add(ref.lower())
            value = format_value(wanted[ref.lower()])
            out.append(f"{ref}{passive.group('mid')}{value}{passive.group('rest')}")
            continue
        out.append(line)
    missing = sorted(set(wanted) - seen)
    if missing:
        raise EdaError(
            f"nothing to vary for {', '.join(missing)}: no matching R/C/L line or .param "
            "in the deck"
        )
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def set_temperature(text: str, celsius: float) -> str:
    """Replace (or add) the ``.temp`` directive."""
    lines = [ln for ln in text.splitlines() if not ln.strip().lower().startswith(".temp")]
    for index, line in enumerate(lines):
        if line.strip().lower().startswith(".end"):
            lines.insert(index, f".temp {celsius:g}")
            break
    else:
        lines.append(f".temp {celsius:g}")
    return "\n".join(lines) + "\n"


def parse_tolerance(spec: str) -> tuple[str, float]:
    """``"R1=1%"`` or ``"C1=0.1"`` (a fraction) -> ``("R1", 0.01)``."""
    name, _, tol = spec.partition("=")
    if not name or not tol:
        raise EdaError(f"expected NAME=TOLERANCE, got {spec!r}")
    tol = tol.strip()
    fraction = float(tol[:-1]) / 100.0 if tol.endswith("%") else float(tol)
    if not 0 < fraction < 1:
        raise EdaError(f"tolerance for {name} must be between 0 and 100%, got {tol!r}")
    return name.strip(), fraction


def sample(
    nominal: float, tolerance: float, rng: random.Random, distribution: str = "normal"
) -> float:
    """One component value. Normal: tolerance is +-3 sigma, clipped to the band."""
    if distribution == "uniform":
        return rng.uniform(nominal * (1 - tolerance), nominal * (1 + tolerance))
    if distribution == "worst":
        return nominal * (1 + tolerance * rng.choice((-1.0, 1.0)))
    value = rng.gauss(nominal, nominal * tolerance / 3.0)
    return min(max(value, nominal * (1 - tolerance)), nominal * (1 + tolerance))


def read_metric(summary: dict[str, Any], metric: str) -> float | None:
    """Pull ``analysis.signal.key`` (or ``op.signal``) out of a run summary."""
    parts = metric.split(".", 2)
    analysis = parts[0]
    for plot in summary.get("plots", []):
        if plot.get("analysis") != analysis:
            continue
        measurements = plot.get("measurements", {})
        if analysis == "op" and len(parts) == 2:
            value = measurements.get("values", {}).get(parts[1])
            return None if value is None else float(value)
        if len(parts) < 3:
            continue
        entry = measurements.get("signals", {}).get(parts[1], {})
        value = entry.get(parts[2])
        if value is not None:
            return float(value)
    return None


def _statistics(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0}
    ordered = sorted(values)
    mean = statistics.fmean(ordered)
    stdev = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
    return {
        "samples": len(ordered),
        "mean": mean,
        "stdev": stdev,
        "min": ordered[0],
        "max": ordered[-1],
        "p05": ordered[max(0, int(0.05 * (len(ordered) - 1)))],
        "median": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, math.ceil(0.95 * (len(ordered) - 1)))],
        "spread_pct": (ordered[-1] - ordered[0]) / abs(mean) * 100.0 if mean else None,
    }


def _histogram(values: Iterable[float], dest: Path, *, metric: str, nominal: float | None) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ensure_dir(dest.parent)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.hist(list(values), bins=24, color="#4c72b0", edgecolor="white")
    if nominal is not None:
        ax.axvline(nominal, color="#c44e52", linestyle="--", label=f"nominal {nominal:.6g}")
        ax.legend(fontsize="small")
    ax.set_xlabel(metric)
    ax.set_ylabel("trials")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(dest, dpi=130)
    plt.close(fig)
    return dest


def monte_carlo(
    netlist: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    tolerances: dict[str, float],
    metric: str,
    trials: int = 100,
    distribution: str = "normal",
    seed: int = 0,
    timeout: int = 600,
    keep_runs: bool = False,
) -> dict[str, Any]:
    """Resample component values inside their tolerance and report the spread."""
    deck = Path(netlist)
    text = deck.read_text(encoding="utf-8", errors="replace")
    out = ensure_dir(out_dir)
    nominal_values = deck_values(text)

    unknown = [name for name in tolerances if name not in nominal_values]
    if unknown:
        raise EdaError(
            f"no nominal value found for {', '.join(sorted(unknown))}. "
            f"The deck defines: {', '.join(sorted(nominal_values)) or '(nothing)'}"
        )

    rng = random.Random(seed)
    work = ensure_dir(out / "trials")
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    # trial 0 is the nominal circuit, so the spread has a reference
    runs: list[tuple[str, dict[str, float]]] = [("nominal", {})]
    for index in range(trials):
        runs.append(
            (
                f"{index:04d}",
                {
                    name: sample(nominal_values[name], tol, rng, distribution)
                    for name, tol in tolerances.items()
                },
            )
        )

    for label, overrides in runs:
        trial_deck = work / f"{label}.cir"
        trial_deck.write_text(apply_values(text, overrides), encoding="utf-8")
        try:
            summary = runner.run_netlist(
                trial_deck, work / label, timeout=timeout, make_plots=False
            )
        except EdaError as exc:
            failures.append({"trial": label, "error": str(exc)[:200], "values": overrides})
            continue
        value = read_metric(summary, metric)
        if value is None:
            failures.append(
                {
                    "trial": label,
                    "error": f"metric {metric} not in the results",
                    "values": overrides,
                }
            )
            continue
        results.append({"trial": label, "metric": value, "values": overrides})
        if not keep_runs:
            _prune(work / label)

    nominal_metric = next((r["metric"] for r in results if r["trial"] == "nominal"), None)
    samples = [r["metric"] for r in results if r["trial"] != "nominal"]
    report: dict[str, Any] = {
        "netlist": str(deck),
        "out_dir": str(out),
        "metric": metric,
        "trials": trials,
        "distribution": distribution,
        "seed": seed,
        "tolerances": tolerances,
        "nominal_values": {k: nominal_values[k] for k in tolerances},
        "nominal_metric": nominal_metric,
        "statistics": _statistics(samples),
        "failures": failures[:20],
        "failure_count": len(failures),
    }
    if samples:
        try:
            report["histogram"] = str(
                _histogram(samples, out / "histogram.png", metric=metric, nominal=nominal_metric)
            )
        except Exception as exc:  # plotting must not sink the analysis
            report["histogram_error"] = f"{type(exc).__name__}: {exc}"
    _write_csv(out / "trials.csv", results, sorted(tolerances))
    report["csv"] = str(out / "trials.csv")
    report["ok"] = bool(samples) and not failures
    write_json(out / "monte-carlo.json", report)
    return report


def temperature_sweep(
    netlist: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    temperatures: Sequence[float],
    metric: str | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    """Run the deck at each temperature and collect the metric (if given)."""
    deck = Path(netlist)
    text = deck.read_text(encoding="utf-8", errors="replace")
    out = ensure_dir(out_dir)
    points: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for celsius in temperatures:
        label = f"T{celsius:g}".replace("-", "m").replace(".", "p")
        trial_deck = out / f"{label}.cir"
        trial_deck.write_text(set_temperature(text, celsius), encoding="utf-8")
        try:
            summary = runner.run_netlist(trial_deck, out / label, timeout=timeout, make_plots=False)
        except EdaError as exc:
            failures.append({"temperature_c": celsius, "error": str(exc)[:200]})
            continue
        entry: dict[str, Any] = {
            "temperature_c": celsius,
            "summary": str(out / label / "summary.json"),
        }
        if metric:
            entry["metric"] = read_metric(summary, metric)
        points.append(entry)

    values = [p["metric"] for p in points if p.get("metric") is not None]
    report = {
        "netlist": str(deck),
        "out_dir": str(out),
        "metric": metric,
        "temperatures_c": list(temperatures),
        "points": points,
        "statistics": _statistics(values) if values else {"samples": 0},
        "failures": failures,
        "ok": bool(points) and not failures,
    }
    if len(values) > 1:
        first, last = values[0], values[-1]
        span = temperatures[-1] - temperatures[0]
        if span:
            report["drift_per_celsius"] = (last - first) / span
    write_json(out / "temperature-sweep.json", report)
    return report


def _write_csv(dest: Path, results: Sequence[dict[str, Any]], names: Sequence[str]) -> Path:
    import csv

    ensure_dir(dest.parent)
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["trial", "metric", *names])
        for row in results:
            writer.writerow(
                [
                    row["trial"],
                    f"{row['metric']:.10g}",
                    *[
                        f"{row['values'].get(n, ''):.10g}" if n in row["values"] else ""
                        for n in names
                    ],
                ]
            )
    return dest


def _prune(directory: Path) -> None:
    """Keep the trial directories from filling the disk; the summary stays."""
    import shutil

    for child in ("work", "csv", "plots"):
        shutil.rmtree(directory / child, ignore_errors=True)
