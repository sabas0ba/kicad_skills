"""Crosstalk on this artwork: the coupled runs, given their physics.

`emc.parallel_run` finds the geometry - two nets sharing a channel for
longer than the 3W rule likes - and stops there, because a review rule's
job is to point. This module carries on: the same coupled runs, posed as
the cross-sections they actually are, solved for their capacitance and
inductance matrices by `field2d`, and turned into the two numbers a designer
argues about - how many millivolts arrive at the near end, and how many at
the far end, for a stated edge.

The estimate is the classic weak-coupling one, and it is honest about being
that:

* Backward (near-end) crosstalk rises to ``(Lm/L + Cm/C)/4`` of the
  aggressor's swing and holds it for a round trip of the coupled length;
  an edge slower than the round trip only reaches the fraction that fits.
* Forward (far-end) crosstalk grows with coupled length and edge rate as
  ``(Cm/C - Lm/L)/2 * (delay/rise)``. In a homogeneous dielectric the two
  ratios are equal and it *vanishes* - stripline's quiet is physics, not
  luck - and on an outer layer ``Lm/L`` wins, so the far-end pulse is
  negative-going. The solver reproduces both, and the tests hold it there.
* Both ends of the victim are assumed matched. Real terminations reflect
  what arrives; a mismatched near end sends its noise to the far end with
  the sign flipped. The numbers here are the coupling itself, which is the
  part the artwork controls.

Power and ground nets are not victims here - their story is planes and
decoupling, told elsewhere - and the halves of a differential pair are not
each other's aggressors: a pair is parallel on purpose.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from typing import Any

from . import electrical, field2d, pcb
from . import netlist as netlist_mod
from . import outline as outline_geom
from .pcb_review import GEOM_TOL, _pair_stem, _parallel_overlap_span

DEFAULT_RISE_NS = 1.0
DEFAULT_SWING_V = 3.3
DEFAULT_MIN_COUPLED_MM = 5.0
# the same 3W window the review rule uses; wider would count pairs the rule
# would never name, narrower would miss some it does
SPACING_W = 3.0
# each unique cross-section costs a field solve of a few seconds, and a
# routed bus is many pairs of one geometry - the cap is on solves, not pairs
MAX_SOLVES = 12
# two runs the extractor says overlap can still have touching edges after
# averaging; the solver needs an actual gap, and this small it is DRC's
# problem before it is crosstalk's
MIN_GAP_MM = 0.05


def _signal_segments(board: Any) -> list[pcb.Track]:
    """Straight signal copper, arcs flattened the way the review rule does."""
    segments: list[pcb.Track] = []
    for track in board.tracks:
        if not track.net or netlist_mod.classify_net(track.net) != "signal":
            continue
        if track.kind == "arc" and getattr(track, "mid", None):
            points = outline_geom.arc_points(track.start, track.mid, track.end, steps=8)
            for a, b in itertools.pairwise(points):
                if math.dist(a, b) > GEOM_TOL:
                    segments.append(
                        pcb.Track(a, b, track.width, track.layer, track.net_code, track.net)
                    )
            continue
        if track.length > GEOM_TOL:
            segments.append(track)
    return segments


def coupled_pairs(board: Any, *, spacing_w: float = SPACING_W) -> list[dict[str, Any]]:
    """Per pair of nets per layer: how long they run coupled, and how far apart.

    The same spatial hash and the same overlap arithmetic as
    `emc.parallel_run`, accumulating what the solver needs alongside what the
    rule needed: length-weighted centre distance and track width, so each
    pair collapses to the one cross-section that best describes it.
    """
    segments = _signal_segments(board)
    cell = 4.0
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, track in enumerate(segments):
        reach = spacing_w * track.width
        x0 = min(track.start[0], track.end[0]) - reach
        x1 = max(track.start[0], track.end[0]) + reach
        y0 = min(track.start[1], track.end[1]) - reach
        y1 = max(track.start[1], track.end[1]) + reach
        for cx in range(math.floor(x0 / cell), math.floor(x1 / cell) + 1):
            for cy in range(math.floor(y0 / cell), math.floor(y1 / cell) + 1):
                cells[(cx, cy)].append(index)

    sums: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
        lambda: {"length": 0.0, "distance": 0.0, "width": 0.0}
    )
    seen: set[tuple[int, int]] = set()
    for bucket in cells.values():
        for i in bucket:
            for j in bucket:
                if j <= i or (i, j) in seen:
                    continue
                seen.add((i, j))
                a, b = segments[i], segments[j]
                if a.net == b.net or a.layer != b.layer:
                    continue
                stem_a, stem_b = _pair_stem(a.net), _pair_stem(b.net)
                if stem_a is not None and stem_a == stem_b:
                    continue
                length, distance = _parallel_overlap_span(
                    a.start, a.end, b.start, b.end, spacing_w * max(a.width, b.width)
                )
                if length <= 0:
                    continue
                key = (*sorted((a.net, b.net)), a.layer)
                entry = sums[key]
                entry["length"] += length
                entry["distance"] += length * distance
                entry["width"] += length * (a.width + b.width) / 2

    pairs = [
        {
            "nets": [net_a, net_b],
            "layer": layer,
            "coupled_mm": round(entry["length"], 1),
            "centre_mm": round(entry["distance"] / entry["length"], 3),
            "width_mm": round(entry["width"] / entry["length"], 3),
        }
        for (net_a, net_b, layer), entry in sums.items()
        if entry["length"] > 0
    ]
    pairs.sort(key=lambda pair: -pair["coupled_mm"])
    return pairs


def _cross_section(board: Any, layer: str) -> dict[str, Any]:
    """The dielectric this layer's pairs couple over, assumed where unstated.

    A board with no stackup detail still deserves an estimate - crosstalk is
    about ratios, which forgive an epsilon nobody stated better than an
    impedance does - so the fallback is the classic two-layer guess, and the
    answer says it guessed.
    """
    stated = electrical.layer_geometry(board, layer)
    if stated is not None:
        section = dict(stated)
        section["assumed"] = False
        return section
    thickness = float((getattr(board, "setup", {}) or {}).get("thickness") or 1.6)
    copper, _source = electrical.copper_thickness(board, layer)
    return {
        "layer": layer,
        "kind": "microstrip",
        "height_mm": round(thickness - 2 * copper, 4),
        "epsilon_r": 4.5,
        "assumed": True,
    }


def analyse(
    board: Any,
    *,
    rise_ns: float = DEFAULT_RISE_NS,
    swing_v: float = DEFAULT_SWING_V,
    min_coupled_mm: float = DEFAULT_MIN_COUPLED_MM,
    limit: int = MAX_SOLVES,
) -> dict[str, Any]:
    """NEXT and FEXT for every coupled run worth solving, on this artwork."""
    if not math.isfinite(rise_ns) or rise_ns <= 0:
        raise ValueError(f"the edge's rise time must be positive, got {rise_ns}")
    if not math.isfinite(swing_v) or swing_v <= 0:
        raise ValueError(f"the aggressor's swing must be positive, got {swing_v}")

    pairs = [pair for pair in coupled_pairs(board) if pair["coupled_mm"] >= min_coupled_mm]
    solved_sections: dict[tuple, dict[str, Any] | str] = {}
    out_pairs: list[dict[str, Any]] = []
    for pair in pairs:
        section = _cross_section(board, pair["layer"])
        thickness, _source = electrical.copper_thickness(board, pair["layer"])
        gap = max(MIN_GAP_MM, pair["centre_mm"] - pair["width_mm"])
        entry: dict[str, Any] = dict(pair)
        entry["gap_mm"] = round(gap, 3)
        entry["cross_section"] = section

        er_range = section.get("epsilon_r_range")
        if er_range is not None and er_range[1] > 1.1 * er_range[0]:
            # the same refusal `--solve` makes, and it matters more here: a
            # homogeneous solve of a mixed gap does not merely blur the
            # numbers, it *manufactures* stripline's forward cancellation -
            # kl = kc is a property of one medium, and reporting "FEXT: none"
            # for a gap that holds two would be the model asserting physics
            # the board does not have
            entry["not_solved"] = (
                "dielectrics differ across the gap; an averaged solve would "
                "invent the forward-crosstalk cancellation"
            )
            out_pairs.append(entry)
            continue

        key = (
            section["kind"],
            round(pair["width_mm"], 2),
            round(gap, 2),
            round(thickness, 4),
            section["height_mm"],
            section.get("height_below_mm"),
            section["epsilon_r"],
        )
        if key not in solved_sections:
            if len([v for v in solved_sections.values() if not isinstance(v, str)]) >= limit:
                entry["not_solved"] = f"beyond the {limit}-solve budget; geometry only"
                out_pairs.append(entry)
                continue
            try:
                solved_sections[key] = field2d.coupled_matrices(
                    pair["width_mm"],
                    thickness,
                    section["height_mm"],
                    section["epsilon_r"],
                    gap,
                    stripline=section["kind"] == "stripline",
                    trace_below_mm=section.get("height_below_mm"),
                )
            except ValueError as exc:
                solved_sections[key] = str(exc)
        solved = solved_sections[key]
        if isinstance(solved, str):
            entry["not_solved"] = solved
            out_pairs.append(entry)
            continue

        kc = solved["capacitive_coupling"]
        kl = solved["inductive_coupling"]
        if (kc + kl) / 2 > 0.25:
            # the estimate treats the victim as a perturbation; this close,
            # the pair is halfway to being a transmission structure of its
            # own and the numbers read as a floor, not a prediction
            entry["strong_coupling"] = (
                "coupling beyond the weak-coupling estimate's comfort - "
                "read these as at-least numbers"
            )
        delay_s = solved["delay_ns_m"] * 1e-9 * (pair["coupled_mm"] / 1000.0)
        rise_s = rise_ns * 1e-9
        kb = (kl + kc) / 4.0
        saturated = 2 * delay_s >= rise_s
        next_v = kb * swing_v * (1.0 if saturated else 2 * delay_s / rise_s)
        fext_v = ((kc - kl) / 2.0) * (delay_s / rise_s) * swing_v
        entry["coupling"] = {
            k: solved[k] for k in ("capacitive_coupling", "inductive_coupling", "z_odd_ohm")
        }
        entry["next"] = {
            "coefficient": round(kb, 4),
            "mv": round(next_v * 1000, 1),
            # a round trip of the coupled run: the edge that beats it only
            # ever sees part of the line, and the noise scales down with it
            "saturated": saturated,
            "coupled_delay_ps": round(delay_s * 1e12, 1),
        }
        entry["fext"] = {
            "mv": round(fext_v * 1000, 1),
            "note": (
                "homogeneous dielectric: forward crosstalk cancels"
                if section["kind"] == "stripline"
                else "negative-going: the inductive coupling wins on an outer layer"
            ),
        }
        out_pairs.append(entry)

    return {
        "assumptions": {
            "rise_ns": rise_ns,
            "swing_v": swing_v,
            "spacing_w": SPACING_W,
            "min_coupled_mm": min_coupled_mm,
            "victim_terminations": "matched both ends - reflections are not modelled",
            "method": "weak-coupling estimate over 2D quasi-static matrices",
        },
        "pairs": out_pairs,
    }
