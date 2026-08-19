"""A small two-layer maze router, for the signal nets of the examples.

The power nets in these designs are placed by hand: where the input loop goes,
how wide the copper is and which side of the package the switch node leaves on
are the design, not a detail to delegate. The escape from a fine-pitch package
is placed by hand too - at 0.65 mm pitch there is no room for a search to find,
only the one fan that fits. The rest is not: a logic input that gets from the
header to the pin without crossing anything is all that is being asked for, and
there are dozens of them.

So this routes those. Dijkstra over a grid, two copper layers, 45 degree steps,
with a turn penalty so it prefers a long straight to a staircase, a via penalty
so it stays on one layer while it can, and a surcharge on the back layer so that
what it does there is cross something rather than live there - a signal that
settles on the plane side for thirty millimetres fences a piece of the plane off
from the rest of it, and a fenced piece is not a ground plane. Obstacles are the pads of other
nets and the copper already laid down, each inflated by half the new track plus
the clearance, which is the same question ``check_board`` asks after the fact -
here it is asked before, one cell at a time.

A ground pour is not an obstacle here. The fill is computed from the copper
rather than assumed, so a track that crosses the plane cuts its own channel
through it and the search is free to use the back of the board anywhere.

It is not a replacement for a router that understands impedance, length matching
or differential pairs. It is what stops four more example boards from being four
more days of typing coordinates.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from itertools import pairwise

# Eight directions, orthogonal first so ties resolve toward straight lines.
STEPS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
LAYERS = ("F.Cu", "B.Cu")


@dataclass(frozen=True)
class Obstacle:
    """A rectangle of copper that a new track has to keep away from.

    ``layer`` of None means every copper layer, which is what a through-hole pad
    or a via is.
    """

    x0: float
    y0: float
    x1: float
    y1: float
    net: str
    layer: str | None = None


@dataclass
class Path:
    """What the search found: copper per layer, and the vias between."""

    runs: list[tuple[str, list[tuple[float, float]]]] = field(default_factory=list)
    vias: list[tuple[float, float]] = field(default_factory=list)


class Router:
    def __init__(
        self,
        width: float,
        height: float,
        *,
        pitch: float = 0.25,
        clearance: float = 0.25,
        margin: float = 1.0,
        via_size: float = 0.8,
        via_pitch: float = 1.2,
        via_cost: float = 25.0,
        back_cost: float = 0.4,
        crowd_cost: float = 8.0,
        crowd_radius: float = 0.9,
        layers: tuple[str, ...] = LAYERS,
    ) -> None:
        self.pitch = pitch
        self.clearance = clearance
        self.margin = margin
        self.via_size = via_size
        self.via_pitch = via_pitch
        self.via_cost = via_cost
        self.back_cost = back_cost
        self.crowd_cost = crowd_cost
        self.crowd_radius = crowd_radius
        self.layers = layers
        self.columns = int(width / pitch) + 1
        self.rows = int(height / pitch) + 1
        self.width = width
        self.height = height
        self.obstacles: list[Obstacle] = []
        self.via_sites: list[tuple[float, float]] = []

    # -- the world ---------------------------------------------------------
    def add(self, obstacle: Obstacle) -> None:
        self.obstacles.append(obstacle)

    def add_via(self, net: str, point: tuple[float, float], size: float | None = None) -> None:
        """A via, as an obstacle and as a drill others have to stay away from.

        Same net or not: two drills closer together than the fabricator's
        hole-to-hole minimum are one broken board, and a router that skips
        same-net obstacles will otherwise put its second via on top of its
        first.
        """
        half = (size or self.via_size) / 2
        self.add(
            Obstacle(point[0] - half, point[1] - half, point[0] + half, point[1] + half, net, None)
        )
        self.via_sites.append(point)

    def add_track(self, net: str, a, b, width: float, layer: str) -> None:
        """Copper already laid down, as a chain of boxes along the segment.

        One box around the whole segment would be right for an orthogonal run
        and far too greedy for a diagonal one - the bounding box of a 45 degree
        track walls off everything either side of it. Stepping along the segment
        instead costs a handful of boxes and gives the diagonal back the two
        triangles it never occupied.
        """
        half = width / 2
        length = math.dist(a, b)
        steps = max(1, math.ceil(length / self.pitch))
        for index in range(steps):
            t0, t1 = index / steps, (index + 1) / steps
            p = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
            q = (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)
            self.add(
                Obstacle(
                    min(p[0], q[0]) - half,
                    min(p[1], q[1]) - half,
                    max(p[0], q[0]) + half,
                    max(p[1], q[1]) + half,
                    net,
                    layer,
                )
            )

    def _cells_in(self, x0: float, y0: float, x1: float, y1: float, keep: float):
        for cx in range(
            max(int((x0 - keep) / self.pitch), 0),
            min(math.ceil((x1 + keep) / self.pitch), self.columns - 1) + 1,
        ):
            for cy in range(
                max(int((y0 - keep) / self.pitch), 0),
                min(math.ceil((y1 + keep) / self.pitch), self.rows - 1) + 1,
            ):
                yield (cx, cy)

    def _blocked(self, net: str, width: float) -> dict[str, set[tuple[int, int]]]:
        """Every cell a track of this net and width may not sit on, per layer."""
        keep = width / 2 + self.clearance
        blocked = {layer: set() for layer in self.layers}
        for obstacle in self.obstacles:
            if obstacle.net == net:
                continue
            targets = self.layers if obstacle.layer is None else (obstacle.layer,)
            cells = set(self._cells_in(obstacle.x0, obstacle.y0, obstacle.x1, obstacle.y1, keep))
            for layer in targets:
                if layer in blocked:
                    blocked[layer] |= cells
        # the board edge, which the track has to stay inside of
        edge = self.margin + width / 2
        outside = {
            (cx, cy)
            for cx in range(self.columns)
            for cy in range(self.rows)
            if not (
                edge <= cx * self.pitch <= self.width - edge
                and edge <= cy * self.pitch <= self.height - edge
            )
        }
        for layer in blocked:
            blocked[layer] |= outside
        return blocked

    # -- the search --------------------------------------------------------
    def route(
        self,
        net: str,
        start: tuple[float, float],
        goal: tuple[float, float],
        width: float,
        *,
        start_layer: str | None = "F.Cu",
        goal_layer: str | None = "F.Cu",
        crowd: list[tuple[float, float]] | None = None,
        back_cost: float | None = None,
        follow: list[list[tuple[float, float]]] | None = None,
    ) -> Path | None:
        """A path from ``start`` to ``goal``, or None when there is no room.

        ``crowd`` is where other nets still have to start or finish. Nothing
        forbids running across one - a board with a single lane out has to use
        it - but a search that does not know they are coming will happily park
        its track on top of the next net's only exit, and then that net has no
        route at all. Charging for the cell moves the first route aside when
        there is somewhere else to be, and leaves it there when there is not.

        ``back_cost`` overrides the router's own surcharge for this one net.
        A signal crossing a ground plane cuts the plane, and its own return
        current then has to go round the cut; a caller that knows the back
        layer is a plane charges enough for the crossing that the search takes
        any front-side detour it can find, and only crosses where the board
        leaves it nothing else.

        ``follow`` is where this net's siblings already run. A bus drawn by a
        person travels as a bundle - parallel lanes, one pitch apart, turning
        together (open any hand-routed two-layer board) - and a search that
        knows nothing of its siblings scatters the same four nets across four
        different corridors. Cells beside a sibling's path are discounted, so
        the bundle look wins every tie; the discount is a fraction of a step,
        so it never buys a detour that costs real millimetres.
        """
        blocked = self._blocked(net, width)
        back_cost = self.back_cost if back_cost is None else back_cost
        crowded: set[tuple[int, int]] = set()
        for point in crowd or ():
            if math.dist(point, start) < self.crowd_radius:
                continue
            crowded |= set(
                self._cells_in(point[0], point[1], point[0], point[1], self.crowd_radius)
            )
        # A via needs its own room, on both layers at once, before the search may
        # step through it.
        via_blocked = self._blocked(net, self.via_size)
        crowded_holes = {
            cell
            for site in self.via_sites
            for cell in self._cells_in(site[0], site[1], site[0], site[1], self.via_pitch)
        }
        vias_ok = {
            cell
            for cell in ((cx, cy) for cx in range(self.columns) for cy in range(self.rows))
            if cell not in crowded_holes
            and not any(cell in via_blocked[layer] for layer in self.layers)
        }

        # A layer of None is a through-hole pad: it is copper on both, so the
        # search may leave or arrive on either without paying for a via.
        origin = (*self._cell(start), self.layers.index(start_layer or "F.Cu"))
        goal_cell = self._cell(goal)
        goal_layers = (
            tuple(range(len(self.layers)))
            if goal_layer is None
            else (self.layers.index(goal_layer),)
        )
        # The endpoints sit on their own pads, which the inflation may have
        # covered; a route has to be allowed to begin and end on them.
        blocked[self.layers[origin[2]]].discard(origin[:2])
        for index in goal_layers:
            blocked[self.layers[index]].discard(goal_cell)

        # One to three cells out: the lane beside the sibling and the one
        # after it, so a bundle of four still feels the pull. Cell zero is the
        # sibling's own copper and already blocked.
        beside: set[tuple[int, int]] = set()
        if follow:
            on_path: set[tuple[int, int]] = set()
            for polyline in follow:
                for a, b in pairwise(polyline):
                    length = math.dist(a, b)
                    for index in range(int(length / self.pitch) + 1):
                        t = index * self.pitch / length if length else 0.0
                        on_path.add(
                            self._cell((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
                        )
            for cx, cy in on_path:
                for ox in (-3, -2, -1, 0, 1, 2, 3):
                    for oy in (-3, -2, -1, 0, 1, 2, 3):
                        beside.add((cx + ox, cy + oy))
            beside -= on_path
        follow_bonus = 0.3

        turn_penalty = 3.0
        best: dict[tuple[int, int, int, int], float] = {}
        came: dict[tuple[int, int, int, int], tuple[int, int, int, int] | None] = {}
        first = (*origin, -1)
        best[first] = 0.0
        came[first] = None
        queue: list[tuple[float, tuple[int, int, int, int]]] = [(0.0, first)]
        while queue:
            cost, state = heapq.heappop(queue)
            if best.get(state, math.inf) < cost:
                continue
            cx, cy, layer, heading = state
            if (cx, cy) == goal_cell and layer in goal_layers:
                return self._reconstruct(state, came, start, goal)
            for index, (dx, dy) in enumerate(STEPS):
                nxt = (cx + dx, cy + dy)
                if not (0 <= nxt[0] < self.columns and 0 <= nxt[1] < self.rows):
                    continue
                if nxt in blocked[self.layers[layer]] and not (
                    nxt == goal_cell and layer in goal_layers
                ):
                    continue
                step = math.hypot(dx, dy)
                total = (
                    cost
                    + step
                    + (back_cost * step if layer else 0.0)
                    + (0.0 if heading in (index, -1) else turn_penalty)
                    + (self.crowd_cost if nxt in crowded else 0.0)
                    - (follow_bonus * step if nxt in beside else 0.0)
                )
                self._relax(queue, best, came, state, (*nxt, layer, index), total)
            for other in range(len(self.layers)):
                if other == layer or (cx, cy) not in vias_ok:
                    continue
                self._relax(
                    queue, best, came, state, (cx, cy, other, heading), cost + self.via_cost
                )
        return None

    @staticmethod
    def _relax(queue, best, came, state, nxt, total) -> None:
        if total < best.get(nxt, math.inf):
            best[nxt] = total
            came[nxt] = state
            heapq.heappush(queue, (total, nxt))

    def _reconstruct(self, state, came, start, goal) -> Path:
        chain = []
        while state is not None:
            chain.append(state)
            state = came[state]
        chain.reverse()

        runs: list[tuple[str, list[tuple[float, float]]]] = []
        run: list[tuple[float, float]] = []
        layer = chain[0][2]
        for cx, cy, this_layer, _ in chain:
            point = self._point((cx, cy))
            if this_layer != layer:
                runs.append((self.layers[layer], run))
                layer, run = this_layer, [point]
                continue
            if not run or run[-1] != point:
                run.append(point)
        runs.append((self.layers[layer], run))
        runs = [(name, list(points)) for name, points in runs if points]

        # Anchor the two ends on the pads rather than on the grid. A run that is
        # a single point is a layer change on the spot, and it has to take the
        # anchored point with it - otherwise the via lands on the nearest cell
        # and the pad it was meant to drill under is a tenth of a millimetre
        # away, which is a dangling track end and nothing else.
        runs[0][1][0] = start
        runs[-1][1][-1] = goal
        if len(runs) > 1 and len(runs[0][1]) == 1:
            runs[1][1][0] = start
        if len(runs) > 1 and len(runs[-1][1]) == 1:
            runs[-2][1][-1] = goal

        path = Path()
        path.vias = [runs[index][1][-1] for index in range(len(runs) - 1)]
        path.runs = [
            (name, self._simplify(points))
            for name, points in runs
            if len(self._simplify(points)) > 1
        ]
        return path

    def _cell(self, point: tuple[float, float]) -> tuple[int, int]:
        return (
            min(max(round(point[0] / self.pitch), 0), self.columns - 1),
            min(max(round(point[1] / self.pitch), 0), self.rows - 1),
        )

    def _point(self, cell: tuple[int, int]) -> tuple[float, float]:
        return (round(cell[0] * self.pitch, 4), round(cell[1] * self.pitch, 4))

    @staticmethod
    def _simplify(points) -> list[tuple[float, float]]:
        """Drop every point that continues the previous direction.

        The grid produces one point per cell; a KiCad track wants one per corner,
        and `route.mixed_track_widths` and the eye both prefer the shorter list.
        """
        if len(points) < 3:
            return list(points)
        out = [points[0]]
        for previous, current, following in zip(points, points[1:], points[2:], strict=False):
            before = (current[0] - previous[0], current[1] - previous[1])
            after = (following[0] - current[0], following[1] - current[1])
            if _direction(before) != _direction(after):
                out.append(current)
        out.append(points[-1])
        deduped = [out[0]]
        for point in out[1:]:
            if math.dist(point, deduped[-1]) > 1e-9:
                deduped.append(point)
        return deduped


def _direction(delta: tuple[float, float]) -> tuple[float, float]:
    length = math.hypot(*delta)
    return (0.0, 0.0) if length == 0 else (round(delta[0] / length, 6), round(delta[1] / length, 6))
