"""Exact board-outline geometry.

Edge clearance used to be measured against the outline's *bounding box*. That is
right for a rectangle and wrong for everything else: a round board, a board with
a mounting-hole cutout or a notch reports copper as safe when it is over the
milling path, and reports copper as dangerous when it is nowhere near an edge.

This module flattens Edge.Cuts into line segments - arcs and circles included -
so distance and inside/outside can be answered against the real shape.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

Point = tuple[float, float]
Segment = tuple[Point, Point]

ARC_STEPS = 24
_EPS = 1e-9
# How far a chord may sit inside the true curve. A 100 mm-radius board edge
# tessellated in 24 fixed steps sags 0.2 mm - twice the edge-clearance rule -
# so the step count follows the radius instead of a constant.
CHORD_TOLERANCE_MM = 0.02
_MAX_CURVE_STEPS = 720


def _steps_for(radius: float, sweep: float, floor: int) -> int:
    """Subdivisions that keep the chord within ``CHORD_TOLERANCE_MM``."""
    if radius <= CHORD_TOLERANCE_MM:
        return max(2, floor)
    per_chord = 2.0 * math.acos(max(-1.0, 1.0 - CHORD_TOLERANCE_MM / radius))
    if per_chord <= 0:
        return max(2, floor)
    return max(2, floor, min(_MAX_CURVE_STEPS, math.ceil(abs(sweep) / per_chord)))


def _arc_centre(a: Point, b: Point, c: Point) -> tuple[Point, float] | None:
    """Circumcentre of three points, or None when they are collinear."""
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return (ux, uy), math.dist((ux, uy), a)


def arc_points(start: Point, mid: Point, end: Point, steps: int = ARC_STEPS) -> list[Point]:
    """Sample the circular arc that passes through start, mid and end."""
    centre = _arc_centre(start, mid, end)
    if centre is None:
        return [start, end]
    (ux, uy), radius = centre
    a0 = math.atan2(start[1] - uy, start[0] - ux)
    a1 = math.atan2(end[1] - uy, end[0] - ux)
    am = math.atan2(mid[1] - uy, mid[0] - ux)

    def turn(angle: float) -> float:
        return (angle - a0) % math.tau

    span = turn(a1)
    if span < _EPS:
        span = math.tau
    # The midpoint tells us which way round the circle the arc actually goes.
    sweep = span if turn(am) <= span else span - math.tau
    steps = _steps_for(radius, sweep, steps)
    return [
        (
            ux + radius * math.cos(a0 + sweep * i / steps),
            uy + radius * math.sin(a0 + sweep * i / steps),
        )
        for i in range(steps + 1)
    ]


def circle_points(centre: Point, radius: float, steps: int = ARC_STEPS * 2) -> list[Point]:
    steps = max(3, _steps_for(radius, math.tau, steps))
    cx, cy = centre
    pts = [
        (cx + radius * math.cos(math.tau * i / steps), cy + radius * math.sin(math.tau * i / steps))
        for i in range(steps)
    ]
    pts.append(pts[0])
    return pts


def bezier_points(controls: Sequence[Point], steps: int = ARC_STEPS) -> list[Point]:
    """A cubic (or quadratic) Bezier evaluated, not its control cage joined.

    KiCad's ``gr_curve`` stores endpoints and control points; the curve
    passes through the ends and only *toward* the controls, so joining the
    four as vertices detours through points no copper ever visits.
    """
    if len(controls) < 3:
        return list(controls)
    steps = max(4, steps)

    def at(t: float) -> Point:
        points = list(controls)
        while len(points) > 1:
            points = [
                (
                    points[i][0] + (points[i + 1][0] - points[i][0]) * t,
                    points[i][1] + (points[i + 1][1] - points[i][1]) * t,
                )
                for i in range(len(points) - 1)
            ]
        return points[0]

    return [at(i / steps) for i in range(steps + 1)]


def _chain(points: Sequence[Point], closed: bool = False) -> list[Segment]:
    segments = [(points[i], points[i + 1]) for i in range(len(points) - 1)]
    if closed and len(points) > 2 and points[0] != points[-1]:
        segments.append((points[-1], points[0]))
    return [(a, b) for a, b in segments if a != b]


def flatten(edges: Iterable[dict[str, Any]], *, steps: int = ARC_STEPS) -> list[Segment]:
    """Turn parsed Edge.Cuts items into straight segments."""
    segments: list[Segment] = []
    for edge in edges:
        kind = edge.get("type", "")
        start, end = edge.get("start"), edge.get("end")
        if start is None and end is None:
            # A bare point list, e.g. a board assembled in code rather than
            # parsed from a file. Two points describe the shape's endpoints.
            points = list(edge.get("points") or ())
            if len(points) == 2:
                start, end = points
            elif len(points) > 2:
                segments += _chain(points, closed=kind in ("gr_poly", "gr_rect"))
                continue
        if kind == "gr_line" and start and end:
            segments += _chain([start, end])
        elif kind == "gr_rect" and start and end:
            (x0, y0), (x1, y1) = start, end
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            segments += _chain(corners, closed=True)
        elif kind == "gr_circle" and edge.get("centre") and edge.get("radius"):
            segments += _chain(circle_points(edge["centre"], edge["radius"], steps * 2))
        elif kind == "gr_arc" and start and end and edge.get("mid"):
            segments += _chain(arc_points(start, edge["mid"], end, steps))
        elif kind == "gr_curve" and edge.get("polyline"):
            segments += _chain(bezier_points(edge["polyline"], steps))
        elif edge.get("polyline"):
            polyline = edge["polyline"]
            segments += _chain(polyline, closed=kind == "gr_poly")
        elif start and end:  # unknown shape: at least join its ends
            segments += _chain([start, end])
    return segments


def bbox(segments: Sequence[Segment]) -> tuple[float, float, float, float] | None:
    if not segments:
        return None
    xs = [p[0] for seg in segments for p in seg]
    ys = [p[1] for seg in segments for p in seg]
    return (min(xs), min(ys), max(xs), max(ys))


def is_closed(segments: Sequence[Segment], tol: float = 1e-3) -> bool:
    """True when every endpoint is shared, i.e. the outline forms closed loops.

    An open outline is a real defect, but it also makes "inside the board"
    meaningless - so callers use this to decide whether they may trust the
    inside/outside test.
    """
    if len(segments) < 3:
        return False
    digits = max(0, -round(math.log10(tol)))
    degree: dict[Point, int] = {}
    for a, b in segments:
        for point in (a, b):
            key = (round(point[0], digits), round(point[1], digits))
            degree[key] = degree.get(key, 0) + 1
    return all(count % 2 == 0 for count in degree.values())


def chain_loop(segments: Sequence[Segment], tol: float = 1e-3) -> list[Segment] | None:
    """The segments reordered into one closed walk, or None if they will not chain.

    An outline is drawn as unordered pieces; measuring *along* it needs them
    end to end. Greedy endpoint matching is enough because a valid outline
    meets itself only at endpoints. A board with more than one loop (a cutout)
    returns None - "along the rim" is ambiguous there and the caller should
    say nothing rather than measure the wrong loop.
    """
    if len(segments) < 3:
        return None
    digits = max(0, -round(math.log10(tol)))

    def key(point: Point) -> Point:
        return (round(point[0], digits), round(point[1], digits))

    remaining = list(segments)
    walk = [remaining.pop(0)]
    while remaining:
        tail = key(walk[-1][1])
        for i, (a, b) in enumerate(remaining):
            if key(a) == tail:
                walk.append(remaining.pop(i))
                break
            if key(b) == tail:
                walk.append((b, a))
                remaining.pop(i)
                break
        else:
            return None  # a second loop, or a break in this one
    if key(walk[-1][1]) != key(walk[0][0]):
        return None
    return walk


def loop_position(point: Point, loop: Sequence[Segment]) -> float:
    """Arc length along ``loop`` of the point on it nearest to ``point``."""
    px, py = point
    best = math.inf
    position = 0.0
    walked = 0.0
    for (x1, y1), (x2, y2) in loop:
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length < _EPS:
            continue
        t = ((px - x1) * dx + (py - y1) * dy) / (length * length)
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        d = math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
        if d < best:
            best = d
            position = walked + t * length
        walked += length
    return position


def _segment_distance(px: float, py: float, seg: Segment) -> float:
    (x1, y1), (x2, y2) = seg
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 < _EPS:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / length2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def distance(point: Point, segments: Sequence[Segment], *, limit: float | None = None) -> float:
    """Shortest distance from ``point`` to the outline.

    ``limit`` is an early-out: segments whose bounding box is further away than
    the best distance so far are skipped without the projection maths. Boards
    have thousands of tracks and hundreds of edge segments, so this matters.
    """
    px, py = point
    best = math.inf if limit is None else limit
    for seg in segments:
        (x1, y1), (x2, y2) = seg
        if (
            px < min(x1, x2) - best
            or px > max(x1, x2) + best
            or py < min(y1, y2) - best
            or py > max(y1, y2) + best
        ):
            continue
        best = min(best, _segment_distance(px, py, seg))
    return best


def _crossings_odd(px: float, py: float, segments: Sequence[Segment]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in segments:
        if (y1 > py) != (y2 > py):
            crossing = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if crossing > px:
                inside = not inside
    return inside


# Well below any real PCB feature, well above float noise on millimetre values.
NUDGE_MM = 1e-4


def contains(point: Point, segments: Sequence[Segment]) -> bool:
    """Even-odd ray cast: inside the outline, and outside any cutout.

    Board outlines are drawn, not generated, so they are full of degeneracies:
    a USB-stick board is a body outline plus a tab outline that share a seam,
    and a horizontal ray along that seam crosses vertices and collinear edges.
    Three rays a tenth of a micron apart, majority vote - the odd one out is
    always the degenerate one.
    """
    px, py = point
    votes = sum(_crossings_odd(px, py + d, segments) for d in (0.0, NUDGE_MM, -NUDGE_MM))
    return votes >= 2


def clearance(point: Point, segments: Sequence[Segment], *, closed: bool | None = None) -> float:
    """Signed distance to the outline: positive inside the board, negative outside."""
    d = distance(point, segments)
    if closed is None:
        closed = is_closed(segments)
    if closed and not contains(point, segments):
        return -d
    return d
