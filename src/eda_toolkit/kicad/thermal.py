"""Steady-state thermal spreading on the board's own copper.

The question a datasheet cannot answer is the one this module does: *this*
board, *this* copper, these watts - where does the heat go, and how hot does
the silicon's neighbourhood get before convection carries the power away?

The model is the thin-plate one every spreadsheet-grade thermal estimate uses,
solved properly instead of averaged. The board becomes a grid; each cell
carries a sheet conductance - copper where the artwork put copper, laminate
where it did not - heat enters under the parts the caller names, leaves every
cell by convection from both faces, and the temperature rise solves

    div(g * grad(T)) + q = h * (T - T_ambient)

by the same red-black over-relaxation `field2d` uses for potential. At steady
state every watt in equals every watt convected out, and the tests hold the
solver to exactly that balance rather than to another model.

What it is honest about:

* Power is the caller's statement. The board file does not know what U1
  dissipates, and guessing would be inventing the requirement. A run names
  its watts (``--power U1=1.2``) and the output carries them back.
* Convection is a linear film coefficient, both faces, still air by default.
  That is the roughest part of any board-level thermal model - enclosures,
  orientation and altitude all move it - so it is a parameter, not a truth.
* It is 2.5D. Copper and laminate are summed into one in-plane conductance
  per cell; the vertical gradient through 1.6 mm of board is ignored, which
  is a good trade below a few watts per square centimetre and a poor one on
  a power module. Vias enter as copper, not as modelled barrels.

None of that stops the answers being useful, because the questions a board
review asks are comparative: does the tab's pour actually spread, which part
is the hot one, is this fill a heat path or a picture of one.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np

from . import outline as outline_geom

# Conductivities, W/(m K). Copper is copper; the laminate figure is the
# *in-plane* one, which the glass weave roughly doubles over the through-plane
# figure most tables quote.
K_COPPER = 385.0
K_LAMINATE_IN_PLANE = 0.8

# Combined convection plus radiation from one face into still air, W/(m^2 K).
# The classic hand number; an enclosure or a fan makes it a lie in either
# direction, which is why the CLI exposes it.
DEFAULT_HTC_W_M2K = 10.0

DEFAULT_STEP_MM = 0.5
DEFAULT_AMBIENT_C = 25.0
DEFAULT_BOARD_THICKNESS_MM = 1.6


def _primitive_points(primitives) -> list[tuple[float, float]]:
    """Every pad-local point a custom pad's primitives reach."""
    points: list[tuple[float, float]] = []
    for kind, *geom in primitives:
        if kind == "circle":
            pcx, pcy, radius = geom
            points += [(pcx - radius, pcy - radius), (pcx + radius, pcy + radius)]
        elif kind == "poly":
            points += list(geom[0])
    return points or [(0.0, 0.0)]


def _copper_masks(board: Any, x0: float, y0: float, nx: int, ny: int, step: float):
    """One boolean occupancy grid per copper layer, cell centres tested.

    Binary rather than fractional: at half a millimetre a cell is either
    under this artwork's copper or it is not, and the spreading answer moves
    less than the film coefficient's uncertainty either way.
    """
    from matplotlib.path import Path

    xs = x0 + (np.arange(nx) + 0.5) * step
    ys = y0 + (np.arange(ny) + 0.5) * step
    gx, gy = np.meshgrid(xs, ys)
    centres = np.column_stack([gx.ravel(), gy.ravel()])
    masks: dict[str, np.ndarray] = {
        layer: np.zeros((ny, nx), dtype=bool) for layer in board.copper_layers
    }
    # drilled regions: no copper, and no laminate either - a row of mounting
    # holes must not conduct as if the board were solid across them
    holes = np.zeros((ny, nx), dtype=bool)

    def cells(bx0, by0, bx1, by1):
        ix0 = max(0, int((bx0 - x0) / step))
        iy0 = max(0, int((by0 - y0) / step))
        ix1 = min(nx, int((bx1 - x0) / step) + 2)
        iy1 = min(ny, int((by1 - y0) / step) + 2)
        return ix0, iy0, ix1, iy1

    def capsule(layer: str, a, b, half: float) -> None:
        (ax, ay), (bx, by) = a, b
        ix0, iy0, ix1, iy1 = cells(
            min(ax, bx) - half, min(ay, by) - half, max(ax, bx) + half, max(ay, by) + half
        )
        if ix1 <= ix0 or iy1 <= iy0:
            return
        cx, cy = gx[iy0:iy1, ix0:ix1], gy[iy0:iy1, ix0:ix1]
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        if span <= 0:
            t = np.zeros_like(cx)
        else:
            t = np.clip(((cx - ax) * dx + (cy - ay) * dy) / span, 0.0, 1.0)
        near = (cx - (ax + t * dx)) ** 2 + (cy - (ay + t * dy)) ** 2 <= half * half
        masks[layer][iy0:iy1, ix0:ix1] |= near

    for track in board.tracks:
        if track.layer not in masks:
            continue
        half = track.width / 2
        if track.kind == "arc" and getattr(track, "mid", None):
            # the copper follows the curve, not the chord
            points = outline_geom.arc_points(track.start, track.mid, track.end)
            for a, b in itertools.pairwise(points):
                capsule(track.layer, a, b, half)
        else:
            capsule(track.layer, track.start, track.end, half)

    # a via reaches only the layers of its span: a blind via warms nothing on
    # the face it never touches
    order = {layer: i for i, layer in enumerate(board.copper_layers)}
    for via in board.vias:
        half = via.size / 2
        ix0, iy0, ix1, iy1 = cells(via.x - half, via.y - half, via.x + half, via.y + half)
        if ix1 <= ix0 or iy1 <= iy0:
            continue
        cx, cy = gx[iy0:iy1, ix0:ix1], gy[iy0:iy1, ix0:ix1]
        radial = (cx - via.x) ** 2 + (cy - via.y) ** 2
        disc = radial <= half * half
        drill = getattr(via, "drill", 0) or 0
        if drill:
            # the annulus is copper; the drilled middle is air, same as a pad.
            # Only a through via removes the whole stack's material - a blind
            # or buried barrel leaves laminate above or below it, and the
            # collapsed sheet keeps that rather than punch a full hole
            disc &= radial > (drill / 2) ** 2
            if getattr(via, "type", "through") == "through":
                holes[iy0:iy1, ix0:ix1] |= radial <= (drill / 2) ** 2
        indices = [order[layer] for layer in getattr(via, "layers", []) if layer in order]
        if len(indices) >= 2:
            reached = set(board.copper_layers[min(indices) : max(indices) + 1])
        else:
            reached = set(board.copper_layers)
        for layer, mask in masks.items():
            if layer in reached:
                mask[iy0:iy1, ix0:ix1] |= disc

    for fp in board.footprints:
        for pad in fp.pads:
            pad_drill = getattr(pad, "drill", None)
            if pad_drill:
                # the hole as drawn: a slot is a capsule, not a circle, and an
                # offset drill is not at the pad's centre
                dw, dht = getattr(pad, "drill_size", None) or (pad_drill, pad_drill)
                dox, doy = getattr(pad, "drill_offset", (0.0, 0.0)) or (0.0, 0.0)
                reach = max(dw, dht) / 2 + math.hypot(dox, doy)
                hx0, hy0, hx1, hy1 = cells(
                    pad.x - reach, pad.y - reach, pad.x + reach, pad.y + reach
                )
                if hx1 > hx0 and hy1 > hy0:
                    hcx, hcy = gx[hy0:hy1, hx0:hx1], gy[hy0:hy1, hx0:hx1]
                    hangle = math.radians((getattr(pad, "angle", 0.0) or 0.0) + (fp.angle or 0.0))
                    hcos, hsin = math.cos(hangle), math.sin(hangle)
                    hdx, hdy = hcx - pad.x, hcy - pad.y
                    lx = hdx * hcos + hdy * hsin - dox
                    ly = -hdx * hsin + hdy * hcos - doy
                    if dw >= dht:
                        radius = dht / 2
                        t = np.clip(lx, -(dw / 2 - radius), dw / 2 - radius)
                        hole = (lx - t) ** 2 + ly**2 <= radius * radius
                    else:
                        radius = dw / 2
                        t = np.clip(ly, -(dht / 2 - radius), dht / 2 - radius)
                        hole = lx**2 + (ly - t) ** 2 <= radius * radius
                    holes[hy0:hy1, hx0:hx1] |= hole
            if getattr(pad, "type", "") == "np_thru_hole":
                # a non-plated hole is the absence of copper, not a disc of it
                continue
            box = pad.bbox(angle_offset=fp.angle)
            primitives = list(getattr(pad, "primitives", ()) or ())
            if primitives:
                # a custom pad's anchor understates it: widen the window to the
                # farthest primitive vertex, whatever the rotation
                reach = max(math.hypot(*p) for p in _primitive_points(primitives))
                reach = max(reach, math.hypot(pad.size[0], pad.size[1]) / 2)
                box = (pad.x - reach, pad.y - reach, pad.x + reach, pad.y + reach)
            ix0, iy0, ix1, iy1 = cells(*box)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            cx, cy = gx[iy0:iy1, ix0:ix1], gy[iy0:iy1, ix0:ix1]
            # test against the pad's own rotated shape, not its bounding box -
            # a long pad at 45 degrees must not gain triangles of copper
            angle = math.radians((getattr(pad, "angle", 0.0) or 0.0) + (fp.angle or 0.0))
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            dx, dy = cx - pad.x, cy - pad.y
            local_x = dx * cos_a + dy * sin_a
            local_y = -dx * sin_a + dy * cos_a
            half_w, half_h = pad.size[0] / 2, pad.size[1] / 2
            shape = getattr(pad, "shape", "rect")
            if shape == "circle" or (shape == "oval" and abs(half_w - half_h) < 1e-9):
                radius = min(half_w, half_h)
                inside = local_x**2 + local_y**2 <= radius * radius
            elif shape == "oval":
                # KiCad's oval is a capsule - a rectangle with semicircular
                # ends - not an ellipse
                radius = min(half_w, half_h)
                if half_w >= half_h:
                    t = np.clip(local_x, -(half_w - radius), half_w - radius)
                    inside = (local_x - t) ** 2 + local_y**2 <= radius * radius
                else:
                    t = np.clip(local_y, -(half_h - radius), half_h - radius)
                    inside = local_x**2 + (local_y - t) ** 2 <= radius * radius
            elif shape == "roundrect":
                # the stated corner radius comes off each corner; the copper
                # between the radii stays square
                radius = min(half_w, half_h) * 2 * (getattr(pad, "roundrect_rratio", 0.0) or 0.0)
                dx_r = np.clip(np.abs(local_x) - (half_w - radius), 0.0, None)
                dy_r = np.clip(np.abs(local_y) - (half_h - radius), 0.0, None)
                inside = (
                    (np.abs(local_x) <= half_w)
                    & (np.abs(local_y) <= half_h)
                    & (dx_r * dx_r + dy_r * dy_r <= radius * radius)
                )
            else:
                inside = (np.abs(local_x) <= half_w) & (np.abs(local_y) <= half_h)
            for kind, *geom in primitives:
                # the drawn copper of a custom pad, in the same pad-local frame
                if kind == "circle":
                    pcx, pcy, radius = geom
                    inside |= (local_x - pcx) ** 2 + (local_y - pcy) ** 2 <= radius * radius
                elif kind == "poly" and len(geom[0]) >= 3:
                    poly = Path(geom[0])
                    pts = np.column_stack([local_x.ravel(), local_y.ravel()])
                    inside |= poly.contains_points(pts).reshape(local_x.shape)
            drill = getattr(pad, "drill", None)
            if drill:
                # the drilled hole is air, not copper - a large plated
                # mounting hole must not read as a solid conductive disc
                inside &= local_x**2 + local_y**2 > (drill / 2) ** 2
            for layer, mask in masks.items():
                suffix = layer.split(".")[-1]
                if any(pl == layer or pl == f"*.{suffix}" for pl in pad.layers):
                    mask[iy0:iy1, ix0:ix1] |= inside

    for zone in board.zones:
        if zone.keepout:
            continue
        for layer, points in zone.fills:
            if layer not in masks or len(points) < 3:
                continue
            path = Path(points)
            hit = path.contains_points(centres).reshape(ny, nx)
            masks[layer] |= hit

    # copper drawn as filled graphics - a heatsink patch, an antenna - is
    # copper the same way a pour is
    for layer, points in getattr(board, "copper_shapes", ()) or ():
        if layer not in masks or len(points) < 3:
            continue
        masks[layer] |= Path(points).contains_points(centres).reshape(ny, nx)

    # and a stroked graphic's ink is copper too: a hollow frame keeps its
    # outline, a line drawn as artwork conducts like the trace it is
    for layer, points, width in getattr(board, "copper_strokes", ()) or ():
        if layer not in masks or width <= 0:
            continue
        for a, b in itertools.pairwise(points):
            capsule(layer, a, b, width / 2)
    return masks, holes


def _outline_mask(board: Any, x0: float, y0: float, nx: int, ny: int, step: float) -> np.ndarray:
    if not board.outline_closed():
        return np.ones((ny, nx), dtype=bool)
    mask = np.zeros((ny, nx), dtype=bool)
    for iy in range(ny):
        py = y0 + (iy + 0.5) * step
        for ix in range(nx):
            mask[iy, ix] = board.edge_clearance_at(x0 + (ix + 0.5) * step, py) >= 0
    return mask


def _source_mask(
    board: Any, ref: str, x0: float, y0: float, nx: int, ny: int, step: float
) -> np.ndarray:
    """The cells this part's watts land on, as a boolean grid.

    A cell belongs to the source if its *centre* is inside the courtyard: a
    4 mm courtyard on a 1 mm grid heats 4 cells, not the 5 the outer edges
    touch. The courtyard is the retained polygon where the footprint drew
    one - an axis-aligned box would heat the empty corners of a rotated
    package and report it cooler for having been turned - and its bounding
    box only where it did not.
    """
    fp = board.footprint_by_ref(ref)
    if fp is None:
        raise ValueError(f"no footprint {ref!r} on this board")
    mask = np.zeros((ny, nx), dtype=bool)
    box = fp.courtyard_box()
    if box is None:
        box = (fp.x - step, fp.y - step, fp.x + step, fp.y + step)
    # a courtyard wholly off the grid gets no fictitious edge cell - the
    # caller's outline check must see zero cells and refuse it
    if box[2] <= x0 or box[0] >= x0 + nx * step or box[3] <= y0 or box[1] >= y0 + ny * step:
        return mask

    def span(low: float, high: float, origin: float, count: int) -> tuple[int, int]:
        first = math.ceil((low - origin) / step - 0.5)
        last = math.floor((high - origin) / step - 0.5)
        if last < first:  # smaller than a cell: the one holding its centre
            first = last = int(((low + high) / 2 - origin) / step)
        return max(0, first), min(count, last + 1)

    ix0, ix1 = span(box[0], box[2], x0, nx)
    iy0, iy1 = span(box[1], box[3], y0, ny)
    if ix1 <= ix0 or iy1 <= iy0:
        return mask
    points = list(getattr(fp, "courtyard", ()) or ())
    if len(points) >= 3:
        from matplotlib.path import Path

        xs = x0 + (np.arange(ix0, ix1) + 0.5) * step
        ys = y0 + (np.arange(iy0, iy1) + 0.5) * step
        cgx, cgy = np.meshgrid(xs, ys)
        centres = np.column_stack([cgx.ravel(), cgy.ravel()])
        inside = Path(points).contains_points(centres).reshape(iy1 - iy0, ix1 - ix0)
        mask[iy0:iy1, ix0:ix1] = inside
    if not mask.any():
        mask[iy0:iy1, ix0:ix1] = True
    return mask


def _relax(
    rise: np.ndarray,
    face_x: np.ndarray,
    face_y: np.ndarray,
    sink: np.ndarray,
    source: np.ndarray,
    *,
    max_sweeps: int = 20_000,
    tol: float = 1e-6,
) -> np.ndarray:
    """Red-black SOR on the discrete heat balance of every interior cell.

    ``face_x[i, j]`` is the conductance between cell (i, j) and (i, j+1) - a
    harmonic mean, so a face against a dead cell carries nothing and the board
    edge is adiabatic by construction rather than by boundary bookkeeping.
    """
    u = rise.copy()
    ny, nx = u.shape
    ge = np.zeros((ny, nx))
    gw = np.zeros((ny, nx))
    gn = np.zeros((ny, nx))
    gs = np.zeros((ny, nx))
    ge[:, :-1] = face_x
    gw[:, 1:] = face_x
    gn[:-1, :] = face_y
    gs[1:, :] = face_y
    total = ge + gw + gn + gs + sink
    solvable = total > 0
    total_safe = np.where(solvable, total, 1.0)
    jj, ii = np.meshgrid(np.arange(nx), np.arange(ny))
    colours = [((ii + jj) % 2 == parity) & solvable for parity in (0, 1)]
    omega = 2.0 / (1.0 + math.sin(math.pi / max(nx, ny)))
    check_every = 20
    for sweep in range(max_sweeps):
        largest = 0.0
        for colour in colours:
            nb = np.zeros_like(u)
            nb[:, :-1] += ge[:, :-1] * u[:, 1:]
            nb[:, 1:] += gw[:, 1:] * u[:, :-1]
            nb[:-1, :] += gn[:-1, :] * u[1:, :]
            nb[1:, :] += gs[1:, :] * u[:-1, :]
            step = omega * ((nb + source) / total_safe - u)
            if sweep % check_every == 0:
                scale = max(1e-12, float(np.max(np.abs(u))))
                largest = max(largest, float(np.max(np.abs(np.where(colour, step, 0.0)))) / scale)
            u += np.where(colour, step, 0.0)
        if sweep % check_every == 0 and largest < tol:
            break
    return u


def analyse(
    board: Any,
    powers: dict[str, float],
    *,
    ambient_c: float = DEFAULT_AMBIENT_C,
    htc_w_m2k: float = DEFAULT_HTC_W_M2K,
    step_mm: float = DEFAULT_STEP_MM,
) -> dict[str, Any]:
    """Temperature-rise map for the stated dissipations, on this artwork.

    Returns the grid (for rendering), the balance that proves the solve, and
    the per-part and hottest-point summaries a review actually reads.
    """
    from .electrical import copper_thickness

    if not powers:
        raise ValueError("state at least one dissipation, e.g. {'U1': 1.2}")
    for ref, watts in powers.items():
        if not math.isfinite(watts) or watts <= 0:
            raise ValueError(f"{ref}: power must be positive, got {watts}")
    if not math.isfinite(step_mm) or step_mm <= 0:
        raise ValueError(f"the grid step must be positive, got {step_mm}")
    if not math.isfinite(htc_w_m2k) or htc_w_m2k <= 0:
        raise ValueError(
            f"the film coefficient must be positive, got {htc_w_m2k} - "
            "with no convection there is no steady state to solve for"
        )
    if not math.isfinite(ambient_c):
        raise ValueError(f"the ambient temperature must be finite, got {ambient_c}")
    bbox = board.outline_bbox()
    if bbox is None:
        raise ValueError("the board has no outline to solve on")
    if not board.outline_closed():
        raise ValueError(
            "the outline is not closed - inside the board is undefined, "
            "so there is nothing sound to solve on"
        )
    x0, y0, x1, y1 = bbox
    step = step_mm
    nx = max(4, math.ceil((x1 - x0) / step))
    ny = max(4, math.ceil((y1 - y0) / step))
    if nx * ny > 1_200_000:
        raise ValueError(f"{nx}x{ny} cells at {step} mm - raise --step for a board this size")

    outline = _outline_mask(board, x0, y0, nx, ny, step)
    masks, holes = _copper_masks(board, x0, y0, nx, ny, step)
    # what the board is actually made of: inside the outline, and not drilled
    # away - a hole carries neither copper nor laminate nor convecting face
    inside = outline & ~holes

    # sheet conductance per cell, W/K per square: laminate plus every copper
    # layer that actually has copper in the cell
    # KiCad names its dielectrics "core" and "prepreg"; the sum that matters
    # is everything between the outer copper faces, which also keeps solder
    # mask and silkscreen entries out of the board's structural thickness
    board_thickness = DEFAULT_BOARD_THICKNESS_MM
    declared = (getattr(board, "setup", {}) or {}).get("thickness")
    if declared:
        # a board with no detailed stackup still states its overall
        # thickness; a 0.8 mm board must not conduct like a 1.6 mm one
        board_thickness = float(declared)
    stackup = getattr(board, "stackup", []) or []
    coppers = [i for i, entry in enumerate(stackup) if entry.get("type") == "copper"]
    if len(coppers) >= 2:
        between = [
            float(entry["thickness"])
            for entry in stackup[coppers[0] : coppers[-1] + 1]
            if entry.get("type") != "copper" and entry.get("thickness")
        ]
        if between:
            board_thickness = sum(between)
    sheet = np.where(inside, K_LAMINATE_IN_PLANE * board_thickness * 1e-3, 0.0)
    copper_share = np.zeros((ny, nx))
    for layer, mask in masks.items():
        t_mm, _ = copper_thickness(board, layer)
        sheet += np.where(mask & inside, K_COPPER * t_mm * 1e-3, 0.0)
        copper_share += (mask & inside).astype(float)

    def harmonic(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        s = a + b
        return np.where(s > 0, 2 * a * b / np.where(s > 0, s, 1.0), 0.0)

    face_x = harmonic(sheet[:, :-1], sheet[:, 1:])
    face_y = harmonic(sheet[:-1, :], sheet[1:, :])

    area_m2 = (step * 1e-3) ** 2
    sink = np.where(inside, 2.0 * htc_w_m2k * area_m2, 0.0)

    source = np.zeros((ny, nx))
    placed: dict[str, np.ndarray] = {}
    for ref, watts in powers.items():
        patch = _source_mask(board, ref, x0, y0, nx, ny, step) & inside
        cells = int(np.sum(patch))
        if cells == 0:
            raise ValueError(f"{ref} sits outside the board outline")
        source += np.where(patch, watts / cells, 0.0)
        placed[ref] = patch

    rise = _relax(np.zeros((ny, nx)), face_x, face_y, sink, source)

    total_in = float(np.sum(source))
    total_out = float(np.sum(sink * rise))
    hot = np.unravel_index(int(np.argmax(np.where(inside, rise, -1.0))), rise.shape)
    parts = []
    for ref, patch in placed.items():
        rise_here = np.where(patch, rise, 0.0)
        parts.append(
            {
                "ref": ref,
                "power_w": powers[ref],
                "temperature_c": round(ambient_c + float(np.max(rise_here)), 1),
                "rise_c": round(float(np.max(rise_here)), 1),
            }
        )
    parts.sort(key=lambda p: -p["rise_c"])

    return {
        "ambient_c": ambient_c,
        "htc_w_m2k": htc_w_m2k,
        "step_mm": step,
        "board_thickness_mm": round(board_thickness, 3),
        "grid": [ny, nx],
        "max_temperature_c": round(ambient_c + float(rise[hot]), 1),
        "max_rise_c": round(float(rise[hot]), 1),
        "hotspot_mm": [round(x0 + (hot[1] + 0.5) * step, 2), round(y0 + (hot[0] + 0.5) * step, 2)],
        "parts": parts,
        "copper_coverage": round(float(np.sum(copper_share > 0)) / max(1, int(np.sum(inside))), 3),
        "balance": {
            "power_in_w": round(total_in, 4),
            "power_convected_w": round(total_out, 4),
            # at steady state these are the same number; the residual is the
            # solver's honesty metric, not a physical quantity
            "residual": round(abs(total_in - total_out) / total_in, 4),
        },
        "rise_grid": rise,
        "origin_mm": [x0, y0],
        "notes": [
            "2.5D thin-plate model: in-plane conduction through copper and laminate, "
            f"convection at {htc_w_m2k} W/m2K from both faces into still air. "
            "An enclosure or a fan moves that number first; state it with --htc.",
            "Power figures are the caller's statement, not read from the board.",
        ],
    }


def render(result: dict[str, Any], out_path: Any) -> None:
    """The rise map as an image, hottest point marked."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rise = result["rise_grid"]
    x0, y0 = result["origin_mm"]
    step = result["step_mm"]
    ny, nx = rise.shape
    # the longer side gets 8 inches and the shorter keeps the aspect, floored
    # so a very long, narrow board still fits its labels instead of asking
    # matplotlib for a hundred-thousand-pixel canvas
    longest = max(nx, ny)
    fig, ax = plt.subplots(figsize=(max(3.0, 8 * nx / longest), max(3.0, 8 * ny / longest)))
    image = ax.imshow(
        rise + result["ambient_c"],
        origin="lower",
        extent=(x0, x0 + nx * step, y0, y0 + ny * step),
        cmap="inferno",
    )
    hx, hy = result["hotspot_mm"]
    ax.plot(hx, hy, "wx", markersize=8)
    ax.annotate(
        f" {result['max_temperature_c']:.0f} degC",
        (hx, hy),
        color="white",
        fontsize=9,
        va="center",
    )
    fig.colorbar(image, ax=ax, label="degC")
    # KiCad's y axis grows downward; every other plot of this board is drawn
    # that way, so the heat map agrees with them instead of mirroring them
    ax.invert_yaxis()
    ax.set_xlabel("mm")
    ax.set_ylabel("mm")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
