"""Draw the board with its review findings marked on it.

A finding in a list is a sentence; the same finding on the artwork is a place.
"148 corners bend off the 45-degree grid" reads as a statistic and gets waived;
the same 148 marks scattered over one fan and nowhere else reads as a cause,
and the cause is what a reviewer acts on.

So this draws the copper from the parsed board - not a fabrication plot, a
diagnostic one - and puts a numbered mark wherever a rule found something. The
numbers key into a legend written beside it, so the picture and the list are
one artefact rather than two that have to be held in the head at once.

Rendered here rather than composited onto ``kicad-cli``'s PDF output because
that would mean reverse-engineering the page transform to place a dot; drawing
from the geometry is both exact and answerable to nothing but the board.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from . import pcb

# Severity drives the mark's colour, the same three the reports use.
_SEVERITY_COLOUR = {
    "error": (220, 50, 47),
    "warning": (203, 122, 0),
    "info": (38, 139, 210),
}
_LAYER_COLOUR = {"F.Cu": (196, 60, 60), "B.Cu": (60, 110, 196)}
_PAD_COLOUR = (150, 150, 150)
_OUTLINE_COLOUR = (40, 40, 40)
_BACKGROUND = (250, 250, 248)


def positions_of(finding: dict[str, Any]) -> list[tuple[float, float]]:
    """Where a finding sits on the board, if it says.

    Rules opt in by putting ``positions`` in their details; nothing is parsed
    back out of the human-readable message, because a message is for reading.
    """
    details = finding.get("details") or {}
    raw = details.get("positions") or []
    out = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                out.append((float(item[0]), float(item[1])))
            except (TypeError, ValueError):
                continue
    return out


def render_review_map(
    board: pcb.Board,
    findings: Sequence[dict[str, Any]],
    path: str | os.PathLike[str],
    *,
    scale: float = 8.0,
    margin: float = 6.0,
) -> dict[str, Any]:
    """Draw the board and mark every finding that carries a position.

    ``scale`` is pixels per millimetre. Returns the legend, so a caller that
    wants the numbering in its own report does not have to re-derive it.
    """
    from PIL import Image, ImageDraw  # imported here: the CLI must load without it

    marked = [(f, positions_of(f)) for f in findings]
    marked = [(f, p) for f, p in marked if p]

    xs: list[float] = []
    ys: list[float] = []
    for edge in board.edges:
        for x, y in edge.get("points", ()):
            xs.append(x)
            ys.append(y)
    for track in board.tracks:
        xs += [track.start[0], track.end[0]]
        ys += [track.start[1], track.end[1]]
    for _f, points in marked:
        xs += [p[0] for p in points]
        ys += [p[1] for p in points]
    if not xs:
        raise ValueError("nothing on this board to draw")

    x0, x1 = min(xs) - margin, max(xs) + margin
    y0, y1 = min(ys) - margin, max(ys) + margin
    width = max(1, int((x1 - x0) * scale))
    height = max(1, int((y1 - y0) * scale))

    def at(x: float, y: float) -> tuple[float, float]:
        return ((x - x0) * scale, (y - y0) * scale)

    image = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(image)

    for edge in board.edges:
        points = [at(*p) for p in edge.get("points", ())]
        if len(points) >= 2:
            closed = edge.get("type") in ("gr_rect", "gr_poly")
            draw.line(points + ([points[0]] if closed else []), fill=_OUTLINE_COLOUR, width=2)

    for fp in board.footprints:
        for pad in fp.pads:
            box = pad.bbox(angle_offset=fp.angle)
            draw.rectangle([at(box[0], box[1]), at(box[2], box[3])], fill=_PAD_COLOUR)

    # Back copper first so the front reads on top of it, as on the bench.
    for layer in ("B.Cu", "F.Cu"):
        colour = _LAYER_COLOUR.get(layer, (120, 120, 120))
        for track in board.tracks:
            if track.layer != layer:
                continue
            draw.line(
                [at(*track.start), at(*track.end)],
                fill=colour,
                width=max(1, int(track.width * scale)),
            )
    for via in board.vias:
        radius = max(2.0, via.size * scale / 2)
        cx, cy = at(via.x, via.y)
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=(90, 90, 90))

    legend: list[dict[str, Any]] = []
    for index, (finding, points) in enumerate(marked, start=1):
        colour = _SEVERITY_COLOUR.get(finding.get("severity", "info"), (100, 100, 100))
        for px, py in points:
            cx, cy = at(px, py)
            radius = 5.0
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius], outline=colour, width=2
            )
            draw.text((cx + radius + 1, cy - radius - 1), str(index), fill=colour)
        legend.append(
            {
                "index": index,
                "rule": finding.get("rule"),
                "severity": finding.get("severity"),
                "message": finding.get("message"),
                "marks": len(points),
            }
        )

    image.save(os.fspath(path))
    return {"path": str(path), "legend": legend, "scale": scale, "size": [width, height]}
