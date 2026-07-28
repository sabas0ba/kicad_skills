"""Shared helpers: process execution, JSON output, findings."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


class EdaError(RuntimeError):
    """User facing error: printed without a traceback by the CLI."""


@dataclasses.dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout: int = 600,
    check: bool = False,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> CommandResult:
    """Run a subprocess, capturing output. Never raises on non-zero unless check."""
    argv = [str(a) for a in argv]
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
            input=stdin,
        )
    except FileNotFoundError as exc:
        raise EdaError(f"executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EdaError(f"command timed out after {timeout}s: {' '.join(argv)}") from exc
    result = CommandResult(argv, proc.returncode, proc.stdout, proc.stderr)
    if check and not result.ok:
        raise EdaError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result


def which(name: str) -> str | None:
    return shutil.which(name)


def require_tool(name: str, hint: str = "") -> str:
    path = shutil.which(name)
    if not path:
        raise EdaError(
            f"required tool '{name}' is not available in this environment. "
            + (hint or "Run the command inside the eda-toolkit container (see ./bin/eda.sh).")
        )
    return path


SEVERITIES = ("error", "warning", "info")


@dataclasses.dataclass
class Finding:
    """A single review observation."""

    rule: str
    severity: str  # error | warning | info
    message: str
    location: str | None = None
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"invalid severity {self.severity!r}")

    def to_dict(self) -> dict[str, Any]:
        out = {"rule": self.rule, "severity": self.severity, "message": self.message}
        if self.location:
            out["location"] = self.location
        if self.details:
            out["details"] = self.details
        return out


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(findings, key=lambda f: (order[f.severity], f.rule, f.location or ""))


def summarize(findings: Sequence[Finding]) -> dict[str, int]:
    counts = dict.fromkeys(SEVERITIES, 0)
    for f in findings:
        counts[f.severity] += 1
    return counts


COLLAPSE_LIMIT = 6


def collapse_findings(findings: Sequence[Finding], limit: int = COLLAPSE_LIMIT) -> list[Finding]:
    """Fold a flood of identical-rule findings into one summary finding.

    A board with 500 components can trip the same rule 500 times; a review that
    prints all of them is unreadable and hides the single-instance findings that
    usually matter more. Rules that fire more than ``limit`` times are replaced
    by one finding carrying the count and the first ``limit`` examples.
    """
    if limit <= 0:
        return list(findings)

    grouped: dict[tuple[str, str], list[Finding]] = {}
    for finding in findings:
        grouped.setdefault((finding.rule, finding.severity), []).append(finding)

    out: list[Finding] = []
    for (rule, severity), group in grouped.items():
        if len(group) <= limit:
            out.extend(group)
            continue
        examples = group[:limit]
        out.append(
            Finding(
                rule=rule,
                severity=severity,
                message=(f"{len(group)} occurrences of {rule}. First: {examples[0].message}"),
                location=f"{len(group)} locations",
                details={
                    "count": len(group),
                    "collapsed": True,
                    "examples": [f.to_dict() for f in examples],
                    "locations": [f.location for f in group if f.location][:50],
                },
            )
        )
    return out


def emit(payload: Any, *, as_json: bool, text_renderer=None) -> None:
    """Print a result either as JSON or via a text renderer."""
    if as_json or text_renderer is None:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=_default)
        sys.stdout.write("\n")
    else:
        text_renderer(payload)


def _default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_json(path: str | os.PathLike[str], payload: Any) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    p.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_default), encoding="utf-8"
    )
    return p


def parse_page_range(spec: str | None, page_count: int) -> list[int]:
    """Parse '1-3,7' into zero-based page indices. None/'' means all pages."""
    if not spec or spec.strip() in {"all", "*"}:
        return list(range(page_count))
    pages: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_s, _, end_s = chunk.partition("-")
            start = int(start_s) if start_s.strip() else 1
            end = int(end_s) if end_s.strip() else page_count
        else:
            start = end = int(chunk)
        if start < 1 or end < start:
            raise EdaError(f"invalid page range: {chunk!r}")
        for p in range(start, min(end, page_count) + 1):
            if p - 1 not in pages:
                pages.append(p - 1)
    return pages


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"
