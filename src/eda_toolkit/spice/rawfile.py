"""Parser for ngspice/spice3 raw files (ASCII and binary, real and complex)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


class RawFileError(ValueError):
    pass


@dataclass
class Plot:
    """One analysis result inside a raw file."""

    title: str = ""
    date: str = ""
    plotname: str = ""
    flags: list[str] = field(default_factory=list)
    variables: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def complex(self) -> bool:
        return "complex" in self.flags

    @property
    def analysis(self) -> str:
        name = self.plotname.lower()
        if "ac analysis" in name:
            return "ac"
        if "transient" in name:
            return "tran"
        if "dc transfer" in name or "dc analysis" in name:
            return "dc"
        if "operating point" in name:
            return "op"
        if "noise" in name:
            return "noise"
        if "distortion" in name:
            return "disto"
        return name.replace(" ", "_") or "unknown"

    @property
    def sweep(self) -> str:
        return self.variables[0]["name"] if self.variables else ""

    @property
    def points(self) -> int:
        return len(next(iter(self.data.values()))) if self.data else 0

    def signals(self) -> list[str]:
        return [v["name"] for v in self.variables[1:]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plotname": self.plotname,
            "analysis": self.analysis,
            "flags": self.flags,
            "sweep": self.sweep,
            "points": self.points,
            "variables": [v["name"] for v in self.variables],
        }


_HEADER_KEYS = (
    "title",
    "date",
    "plotname",
    "flags",
    "no. variables",
    "no. points",
    "command",
    "option",
    "variables",
    "binary",
    "values",
    "backannotation",
)


def parse(path: str | os.PathLike[str]) -> list[Plot]:
    data = Path(path).read_bytes()
    return parse_bytes(data)


def parse_bytes(blob: bytes) -> list[Plot]:
    plots: list[Plot] = []
    pos = 0
    length = len(blob)
    while pos < length:
        # skip leading whitespace between plots
        while pos < length and blob[pos : pos + 1] in (b"\n", b"\r", b" ", b"\t"):
            pos += 1
        if pos >= length:
            break
        plot, pos = _parse_one(blob, pos)
        plots.append(plot)
    if not plots:
        raise RawFileError("no plots found in raw file")
    return plots


def _readline(blob: bytes, pos: int) -> tuple[str, int]:
    end = blob.find(b"\n", pos)
    if end == -1:
        return blob[pos:].decode("utf-8", "replace").rstrip("\r"), len(blob)
    return blob[pos:end].decode("utf-8", "replace").rstrip("\r"), end + 1


def _parse_one(blob: bytes, pos: int) -> tuple[Plot, int]:
    plot = Plot()
    n_vars = n_points = 0
    variables: list[dict[str, Any]] = []
    mode = None

    while pos < len(blob):
        line, next_pos = _readline(blob, pos)
        stripped = line.strip()
        lower = stripped.lower()
        if not stripped:
            pos = next_pos
            continue
        if lower.startswith("variables:"):
            pos = next_pos
            for _ in range(n_vars):
                var_line, pos = _readline(blob, pos)
                parts = var_line.split()
                if len(parts) < 3:
                    raise RawFileError(f"malformed variable line: {var_line!r}")
                variables.append(
                    {"index": int(parts[0]), "name": parts[1], "type": parts[2], "extra": parts[3:]}
                )
            continue
        if lower.startswith("binary:"):
            mode, pos = "binary", next_pos
            break
        if lower.startswith("values:"):
            mode, pos = "ascii", next_pos
            break
        key, _, value = stripped.partition(":")
        key_l = key.strip().lower()
        value = value.strip()
        if key_l == "title":
            plot.title = value
        elif key_l == "date":
            plot.date = value
        elif key_l == "plotname":
            plot.plotname = value
        elif key_l == "flags":
            plot.flags = value.lower().split()
        elif key_l == "no. variables":
            n_vars = int(value)
        elif key_l == "no. points":
            n_points = int(value)
        pos = next_pos

    if mode is None:
        raise RawFileError("raw file header without a Binary:/Values: section")
    if not variables:
        raise RawFileError("raw file has no variable definitions")

    plot.variables = variables
    is_complex = "complex" in plot.flags
    columns: np.ndarray

    if mode == "binary":
        item = np.complex128 if is_complex else np.float64
        count = n_vars * n_points
        nbytes = count * (16 if is_complex else 8)
        if pos + nbytes > len(blob):
            raise RawFileError("truncated binary raw data")
        columns = np.frombuffer(blob[pos : pos + nbytes], dtype=item).reshape(n_points, n_vars)
        pos += nbytes
    else:
        values = np.zeros((n_points, n_vars), dtype=np.complex128 if is_complex else np.float64)
        point = 0
        while point < n_points and pos < len(blob):
            line, pos = _readline(blob, pos)
            if not line.strip():
                continue
            parts = line.split()
            # first line of a point starts with the point index
            if parts and re.fullmatch(r"\d+", parts[0]):
                parts = parts[1:]
            col = 0
            values[point, col] = _to_number(parts[0], is_complex)
            col = 1
            while col < n_vars and pos < len(blob):
                line, pos = _readline(blob, pos)
                if not line.strip():
                    continue
                values[point, col] = _to_number(line.strip().split()[0], is_complex)
                col += 1
            point += 1
        columns = values

    for var in variables:
        column = columns[:, var["index"]]
        if not is_complex:
            column = column.real.astype(np.float64)
        plot.data[var["name"]] = np.ascontiguousarray(column)

    return plot, pos


def _to_number(token: str, is_complex: bool) -> complex | float:
    if is_complex:
        if "," in token:
            re_s, _, im_s = token.partition(",")
            return complex(float(re_s), float(im_s))
        return complex(float(token), 0.0)
    return float(token)


def to_csv(plot: Plot, dest: str | os.PathLike[str]) -> Path:
    """Write one plot as CSV (complex data becomes ``<name>_re``/``_im`` columns)."""
    import csv

    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    columns: list[np.ndarray] = []
    for var in plot.variables:
        column = plot.data[var["name"]]
        if np.iscomplexobj(column):
            names += [
                f"{var['name']}_re",
                f"{var['name']}_im",
                f"{var['name']}_mag",
                f"{var['name']}_deg",
            ]
            columns += [column.real, column.imag, np.abs(column), np.angle(column, deg=True)]
        else:
            names.append(var["name"])
            columns.append(column)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(names)
        for row in zip(*columns, strict=True):
            writer.writerow([f"{v:.10g}" for v in row])
    return out
