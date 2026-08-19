"""PCB artwork review: DRC plus layout-practice heuristics."""

from __future__ import annotations

import math
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from ..util import (
    COLLAPSE_LIMIT,
    Finding,
    RuleSpec,
    collapse_findings,
    sort_findings,
    summarize,
)
from . import electrical, kicad_cli, pcb
from . import netlist as netlist_mod

RULES: list[Callable[[PcbContext], list[Finding]]] = []

# Defaults are deliberately conservative "hobby / low-cost fab" limits.
THRESHOLDS = {
    "min_track_mm": 0.15,
    "min_via_drill_mm": 0.3,
    "min_annular_ring_mm": 0.13,
    "min_edge_clearance_mm": 0.3,
    "max_decoupling_distance_mm": 5.0,
    "max_drill_sizes": 6,
    "min_silk_text_height_mm": 0.8,
    # Placement conventions. A part off the grid or turned to 37 degrees costs
    # nothing electrically and makes the board unreadable and awkward to place.
    "placement_grid_mm": 0.5,
    "rotation_step_deg": 90.0,
    # A decoupling capacitor whose ground pad has to travel this far to reach a
    # via has already lost the inductance argument.
    "max_decoupling_via_mm": 1.5,
    # Interior angle below which a corner is an acid trap and an impedance step.
    "min_track_angle_deg": 90.0,
    # How far in from the board edge a connector may sit before the cable has
    # to cross the board to reach it.
    "max_connector_edge_mm": 6.0,
    # How near a pad a track may change width: a neck is allowed to end where
    # the thing that forced it does, and nowhere else.
    "width_step_free_mm": 3.0,
    # How much of its own outline a ground pour has to actually fill. Below
    # this it is not a plane, it is infill between the traces that cut it.
    "min_pour_coverage": 0.8,
    # How much of a ground pour's copper the largest connected island has to
    # hold. Below this the plane is not a plane: it is pieces, and a return
    # current that starts on one of them has to leave the layer to get home.
    "min_pour_island_fraction": 0.7,
    # A power net may neck down this many millimetres in total (pad entries,
    # fine-pitch escapes); beyond it the neck is the track.
    "power_neck_mm": 10.0,
    # Routed copper length over the minimum spanning tree of the net's pads.
    # Above this the route is taking the scenic tour an autorouter leaves. The
    # MST ignores obstacles, so honest routing runs well over 1x; on KiCad's
    # own demo corpus, 4x is where human boards stop and machine tours begin.
    "detour_ratio": 4.0,
    # How far a signal may run over gaps in the other layer's ground fill
    # before the return current's detour is worth a finding.
    "return_path_mm": 10.0,
    # One run of copper, from the pad or junction at one end to the pad or
    # junction at the other, over the straight line between those two ends.
    # `detour_ratio` asks the same question of a whole net and a net can hide
    # one bad connection inside a lot of good ones; this asks it of each run,
    # which is the shape a reader actually sees on the plot.
    "wander_ratio": 2.0,
}

# Copper geometry is stored in nm; anything below this is file noise.
GEOM_TOL = 0.001

# `rule_drc` builds its ids from a bucket name held in a variable, so unlike
# every other rule here its prefixes cannot be read out of the source. They are
# declared instead; tests/test_rule_spec.py requires each to be in RULE_SPEC.
DYNAMIC_RULE_IDS = ("drc.*", "drc.unconnected.*", "drc.parity.*")

# Every finding this module can produce, and the condition that produces it.
# tests/test_rule_spec.py keeps it honest; `eda gate --list-rules` prints it.
RULE_SPEC: dict[str, RuleSpec] = {
    # -- KiCad's own DRC ---------------------------------------------------
    "drc.*": RuleSpec(
        "one entry per violation KiCad's own DRC reports, keeping its type and severity",
        "as KiCad graded it",
        dynamic=True,
    ),
    "drc.unconnected.*": RuleSpec(
        "a ratsnest connection KiCad's DRC found unrouted", "error", dynamic=True
    ),
    "drc.parity.*": RuleSpec(
        "a disagreement between the board and the schematic (net, footprint, part)",
        "as KiCad graded it",
        dynamic=True,
    ),
    "drc.unavailable": RuleSpec("kicad-cli was not available, so DRC did not run", "info"),
    # -- the board itself --------------------------------------------------
    "board.no_outline": RuleSpec("there is no Edge.Cuts geometry at all", "error"),
    "board.size": RuleSpec("the outline's bounding size, reported as context", "info"),
    "board.tiny_outline": RuleSpec("the outline is under 5 mm in either axis", "warning"),
    "board.copper_outside_outline": RuleSpec(
        "a track end, via or pad lies outside the closed outline, so it would be "
        "milled away; measured against the flattened Edge.Cuts, cutouts included",
        "error",
    ),
    "board.edge_clearance": RuleSpec(
        "copper whose own half-width leaves less than the limit to the outline",
        "warning",
        threshold="min_edge_clearance_mm",
    ),
    # -- fab capability ----------------------------------------------------
    "track.below_minimum": RuleSpec(
        "a track segment narrower than the fab minimum", "error", threshold="min_track_mm"
    ),
    "track.thin_power": RuleSpec(
        "a power or ground net with a contiguous run of track under 0.4 mm "
        "longer than power_neck_mm, reported with the current the thinnest "
        "actually carries at a 10 C rise (IPC-2221) from the board's own "
        "stackup. Short necks - pad entries, fine-pitch escapes - are what "
        "the allowance is for",
        "warning",
        threshold="power_neck_mm",
    ),
    "via.small_drill": RuleSpec(
        "a via drilled smaller than the fab minimum", "warning", threshold="min_via_drill_mm"
    ),
    "via.annular_ring": RuleSpec(
        "a via whose (size - drill)/2 is under the limit",
        "warning",
        threshold="min_annular_ring_mm",
    ),
    "fab.many_drill_sizes": RuleSpec(
        "more distinct drill diameters than the limit, which costs money",
        "info",
        threshold="max_drill_sizes",
    ),
    # -- routing -----------------------------------------------------------
    "route.unrouted_net": RuleSpec(
        "a net with pads on two or more footprints and no track, via or zone. "
        "Only evaluated when DRC could not run; DRC is authoritative",
        "error",
    ),
    "route.no_tracks": RuleSpec("the board has no routed tracks at all", "warning"),
    "route.layer_usage": RuleSpec("track segments per copper layer, as context", "info"),
    "route.stub": RuleSpec(
        "a track end meeting no pad, via or other track, on a net with no pour on "
        "that layer to land in",
        "warning",
    ),
    "route.acute_angle": RuleSpec(
        "two same-net segments meeting at an interior angle below the limit: an "
        "acid trap, and a discontinuity for anything fast",
        "info",
        threshold="min_track_angle_deg",
    ),
    "route.right_angle": RuleSpec(
        "two same-net segments meeting at a full 90 degrees - two 45s cost "
        "nothing, and the sharp corner is a small discontinuity and a "
        "manufacturing nick risk",
        "info",
        threshold="min_track_angle_deg",
    ),
    "route.odd_angle": RuleSpec(
        "two same-net segments meeting more than 2 degrees off the 45-degree "
        "grid - a 20 or 70 degree bend reads as a slip of the mouse, and a "
        "fan that needs one should say so",
        "info",
        threshold="min_track_angle_deg",
    ),
    "route.mixed_track_widths": RuleSpec("a net routed at three or more distinct widths", "info"),
    "route.detour": RuleSpec(
        "a net whose routed copper is longer than detour_ratio times the "
        "minimum spanning tree of its pads, by more than 10 mm - the scenic "
        "tour an autorouter takes where a person would go round the block. "
        "Nets with a pour and ground nets are not judged",
        "warning",
        threshold="detour_ratio",
    ),
    "route.wander": RuleSpec(
        "one continuous run of copper - pad or junction at each end, nothing "
        "branching in between - longer than wander_ratio times the straight "
        "line between its own two ends, by more than 5 mm. Where `route.detour` "
        "asks whether a net is long, this asks whether a track goes out and "
        "comes back, which is what the eye catches first",
        "warning",
        threshold="wander_ratio",
    ),
    "route.return_path": RuleSpec(
        "on a two-layer board with a filled ground pour, a signal track that "
        "runs more than return_path_mm in total over the pour's clearance cuts "
        "on the opposite layer - the return current has to go round the gap, "
        "and the loop grows by the detour",
        "warning",
        threshold="return_path_mm",
    ),
    # -- placement ---------------------------------------------------------
    "layout.outside_outline": RuleSpec("a footprint origin outside the board outline", "error"),
    "layout.zone_outside_outline": RuleSpec(
        "a zone - a pour or a keep-out - whose drawn outline lies wholly "
        "outside the board. A keep-out off the board keeps nothing out, and "
        "every plot of the board is scaled to fit it in",
        "error",
    ),
    "layout.pad_collision": RuleSpec(
        "pads of two different footprints whose extents overlap on a shared layer",
        "warning",
    ),
    "layout.off_grid_placement": RuleSpec(
        "a footprint origin that is not a multiple of the placement grid",
        "info",
        threshold="placement_grid_mm",
    ),
    "layout.odd_rotation": RuleSpec(
        "a footprint turned to something other than a multiple of the step",
        "info",
        threshold="rotation_step_deg",
    ),
    "layout.double_sided_assembly": RuleSpec(
        "footprints on the bottom side, which costs an assembly pass", "info"
    ),
    # -- power integrity ---------------------------------------------------
    "layout.no_decoupling": RuleSpec(
        "an IC supply pad with no capacitor footprint on that net", "warning"
    ),
    "layout.decoupling_distance": RuleSpec(
        "the nearest decoupling capacitor to an IC supply pad is further than the limit",
        "warning",
        threshold="max_decoupling_distance_mm",
    ),
    "layout.decoupling_via": RuleSpec(
        "a decoupling capacitor's ground pad is further than the limit from the "
        "nearest via on that net, measured from the pad edge, so the return loop "
        "runs through track instead of the plane. Only evaluated when a ground "
        "pour exists, and skipped where the pad's own layer already carries it",
        "warning",
        threshold="max_decoupling_via_mm",
    ),
    "layout.no_ground_plane": RuleSpec("no ground zone anywhere on the board", "warning"),
    "layout.unfilled_zone": RuleSpec(
        "a ground zone with fill enabled but no computed fill in the file", "warning"
    ),
    "layout.ground_plane": RuleSpec("the ground zones that do exist, as context", "info"),
    # -- silkscreen and mechanical ----------------------------------------
    "silk.text_over_text": RuleSpec(
        "two silkscreen strings on the same side whose extents overlap. The "
        "board is the only documentation an assembled part has, and two "
        "labels printed through each other document nothing",
        "warning",
    ),
    "silk.over_pad": RuleSpec(
        "a visible silkscreen string whose estimated extent overlaps a pad on the "
        "same side; ink on a pad keeps solder off it",
        "warning",
    ),
    "silk.text_too_small": RuleSpec(
        "visible silkscreen shorter than the screen printer's limit",
        "warning",
        threshold="min_silk_text_height_mm",
    ),
    "silk.missing_board_id": RuleSpec(
        "no free silkscreen text at all, so the bare board states neither its "
        "name nor its revision - ten boards on a bench, and no way to tell "
        "which is which. Info, like the other silk-completeness rules; the "
        "ai-generated policy promotes it regardless",
        "info",
    ),
    "silk.unlabeled_connector": RuleSpec(
        "a connector with no free silkscreen text near it: nothing says which "
        "pin carries what, and reversed hookup is how boards die",
        "info",
    ),
    "silk.unlabeled_indicator": RuleSpec(
        "an LED or a switch with no silkscreen text within 10 mm saying what "
        "it means - a designator names the schematic line, not the function",
        "warning",
    ),
    "layout.connector_not_at_edge": RuleSpec(
        "a connector whose pads sit further than `max_connector_edge_mm` in "
        "from the nearest board edge, so its cable has to cross the board",
        "warning",
        threshold="max_connector_edge_mm",
    ),
    "route.width_step": RuleSpec(
        "a track that changes width further than `width_step_free_mm` from any "
        "pad, where the narrow side is not a pad's own neck either - the narrow "
        "section already set the current the run can carry, so the wide section "
        "buys nothing. A neck out of a fine-pitch row is allowed the same "
        "`power_neck_mm` budget `track.thin_power` gives it",
        "warning",
        threshold="width_step_free_mm",
    ),
    "route.under_package": RuleSpec(
        "a track of another net threaded under a package's body, where there "
        "is no plane between it and the die and no way to probe or rework it",
        "warning",
    ),
    "layout.pour_coverage": RuleSpec(
        "a ground pour that fills less than `min_pour_coverage` of its own "
        "outline - the tracks crossing it took the rest as clearance, and what "
        "is left is infill between traces rather than a plane a return can "
        "follow. Read alongside how dense the board is: the same copper on a "
        "smaller board scores lower, and making a board smaller is usually an "
        "improvement, so this reports rather than faults",
        "info",
        threshold="min_pour_coverage",
    ),
    "layout.pour_fragmented": RuleSpec(
        "a ground pour whose largest connected island holds less than "
        "`min_pour_island_fraction` of its filled copper - the tracks crossing "
        "it have cut the plane into pieces, and a return that starts on one of "
        "them has to leave the layer to get home",
        "warning",
        threshold="min_pour_island_fraction",
    ),
    "layout.pour_single_sided": RuleSpec(
        "a two-layer board whose ground pour covers only one face - the other "
        "face's spare copper is doing nothing, and its edge traces have no "
        "adjacent return",
        "info",
    ),
    "silk.missing_reference": RuleSpec(
        "a non-virtual footprint with no silkscreen text of its own", "info"
    ),
    "mechanical.no_mounting_holes": RuleSpec("no H*/MH* footprint on the board", "info"),
    "test.no_testpoints": RuleSpec("no TP* footprint on the board", "info"),
    "internal.*": RuleSpec(
        "a rule raised an exception; reported instead of failing the review", "info", dynamic=True
    ),
}


def rule(func: Callable[[PcbContext], list[Finding]]):
    RULES.append(func)
    return func


class PcbContext:
    def __init__(
        self,
        target: str | os.PathLike[str],
        *,
        use_cli: bool = True,
        thresholds: dict[str, float] | None = None,
    ) -> None:
        self.path = pcb.find_board(target)
        self.board = pcb.parse(self.path)
        self.thresholds = {**THRESHOLDS, **(thresholds or {})}
        self.drc: dict[str, Any] | None = None
        if use_cli and kicad_cli.available():
            try:
                self.drc = kicad_cli.drc(self.path)
            except Exception as exc:  # pragma: no cover - depends on kicad build
                self.drc = {"error": str(exc)}

        self.pads_by_net: dict[str, list[tuple[pcb.Footprint, pcb.Pad]]] = defaultdict(list)
        for fp in self.board.footprints:
            for pad in fp.pads:
                if pad.net:
                    self.pads_by_net[pad.net].append((fp, pad))

    @classmethod
    def from_board(
        cls,
        board: pcb.Board,
        *,
        thresholds: dict[str, float] | None = None,
        drc: dict[str, Any] | None = None,
    ) -> PcbContext:
        """Build a context from an already parsed board (used by the tests)."""
        ctx = cls.__new__(cls)
        ctx.path = board.path
        ctx.board = board
        ctx.thresholds = {**THRESHOLDS, **(thresholds or {})}
        ctx.drc = drc
        ctx.pads_by_net = defaultdict(list)
        for fp in board.footprints:
            for pad in fp.pads:
                if pad.net:
                    ctx.pads_by_net[pad.net].append((fp, pad))
        return ctx

    def net_class_of(self, net: str) -> str:
        return netlist_mod.classify_net(net)

    def ic_footprints(self) -> list[pcb.Footprint]:
        return [
            fp
            for fp in self.board.footprints
            if fp.ref.startswith(("U", "IC")) or len(fp.pads) >= 6
        ]


@rule
def rule_drc(ctx: PcbContext) -> list[Finding]:
    """Surface kicad-cli DRC violations (including unconnected items and parity)."""
    if not ctx.drc:
        return [
            Finding(
                "drc.unavailable",
                "info",
                "kicad-cli is not available, DRC was skipped "
                "(run inside the container for full coverage)",
            )
        ]
    if "error" in ctx.drc:
        return [Finding("drc.unavailable", "info", f"DRC could not be run: {ctx.drc['error']}")]

    findings: list[Finding] = []
    buckets = (
        ("violations", "drc"),
        ("unconnected_items", "drc.unconnected"),
        ("schematic_parity", "drc.parity"),
    )
    for key, prefix in buckets:
        for violation in ctx.drc.get(key, []) or []:
            sev = {"error": "error", "warning": "warning"}.get(
                str(violation.get("severity", "warning")).lower(), "info"
            )
            items = violation.get("items", [])
            where = "; ".join(
                str(it.get("description", "")) for it in items if it.get("description")
            )
            findings.append(
                Finding(
                    rule=f"{prefix}.{violation.get('type', 'violation')}",
                    severity="error" if key == "unconnected_items" else sev,
                    message=violation.get("description", "DRC violation"),
                    location=where[:300],
                    details={"items": items},
                )
            )
    return findings


@rule
def rule_outline(ctx: PcbContext) -> list[Finding]:
    """The board needs an Edge.Cuts outline."""
    bbox = ctx.board.outline_bbox()
    if bbox is None:
        return [
            Finding("board.no_outline", "error", "no Edge.Cuts geometry: the board has no outline")
        ]
    size = ctx.board.size_mm() or (0, 0)
    findings = [Finding("board.size", "info", f"board outline is {size[0]} x {size[1]} mm")]
    if size[0] < 5 or size[1] < 5:
        findings.append(
            Finding(
                "board.tiny_outline",
                "warning",
                f"board outline is suspiciously small ({size[0]} x {size[1]} mm)",
            )
        )
    return findings


@rule
def rule_track_width(ctx: PcbContext) -> list[Finding]:
    """Tracks below the fab minimum, and thin power tracks."""
    findings = []
    limit = ctx.thresholds["min_track_mm"]
    thin = [t for t in ctx.board.tracks if t.width < limit - 1e-9]
    if thin:
        widths = sorted({round(t.width, 3) for t in thin})
        findings.append(
            Finding(
                "track.below_minimum",
                "error",
                f"{len(thin)} track segment(s) narrower than {limit} mm (widths: {widths})",
                details={"nets": sorted({t.net for t in thin if t.net})[:20]},
            )
        )
    power_tracks = [
        t for t in ctx.board.tracks if ctx.net_class_of(t.net) in ("power", "ground") and t.net
    ]
    if power_tracks:
        narrow = [t for t in power_tracks if t.width < 0.4]
        # A short neck is what a pad entry or a fine-pitch escape looks like;
        # only a *contiguous* narrow run longer than the allowance is really
        # the track. Summing per net would damn a wide rail for having many
        # pins, each with its own few-millimetre escape.
        neck = ctx.thresholds["power_neck_mm"]
        parent: dict[tuple, tuple] = {}

        def find(a):
            parent.setdefault(a, a)
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def key(net, point):
            return (net, round(point[0], 2), round(point[1], 2))

        for t in narrow:
            a, b = key(t.net, t.start), key(t.net, t.end)
            parent[find(a)] = find(b)
        run_len: dict[tuple, float] = defaultdict(float)
        run_net: dict[tuple, str] = {}
        for t in narrow:
            root = find(key(t.net, t.start))
            run_len[root] += t.length
            run_net[root] = t.net
        over = {run_net[root] for root, length in run_len.items() if length > neck}
        narrow = [t for t in narrow if t.net in over]
        if narrow:
            by_net = Counter(t.net for t in narrow)
            # Say what the width actually buys rather than only that it is thin:
            # the same 0.25 mm is fine for a sensor rail and hopeless for a motor.
            thinnest = min(narrow, key=lambda t: t.width)
            thickness, source = electrical.copper_thickness(ctx.board, thinnest.layer)
            external = (
                thinnest.layer
                in (ctx.board.copper_layers or [None])[:1]
                + (ctx.board.copper_layers or [None])[-1:]
            )
            amps = electrical.current_capacity(
                thinnest.width, thickness, temperature_rise_c=10.0, external=external
            )
            findings.append(
                Finding(
                    "track.thin_power",
                    "warning",
                    f"power/ground net(s) with a contiguous run of track "
                    f"narrower than 0.4 mm longer than {neck} mm - the thinnest "
                    f"is {thinnest.width} mm on {thinnest.layer}, good for "
                    f"{amps:.2f} A at a 10 C rise (IPC-2221)",
                    details={
                        "nets": dict(by_net.most_common(10)),
                        "thinnest_mm": thinnest.width,
                        "thinnest_layer": thinnest.layer,
                        "current_a_at_10c": round(amps, 3),
                        "copper_thickness_mm": thickness,
                        "copper_thickness_source": source,
                    },
                )
            )
    return findings


@rule
def rule_vias(ctx: PcbContext) -> list[Finding]:
    """Via drill and annular ring against fab limits."""
    findings = []
    min_drill = ctx.thresholds["min_via_drill_mm"]
    min_ring = ctx.thresholds["min_annular_ring_mm"]
    small = [v for v in ctx.board.vias if v.drill and v.drill < min_drill - 1e-9]
    if small:
        findings.append(
            Finding(
                "via.small_drill",
                "warning",
                f"{len(small)} via(s) with a drill below {min_drill} mm "
                f"(smallest {min(v.drill for v in small)} mm)",
            )
        )
    tight = [v for v in ctx.board.vias if v.annular_ring < min_ring - 1e-9]
    if tight:
        findings.append(
            Finding(
                "via.annular_ring",
                "warning",
                f"{len(tight)} via(s) with an annular ring below {min_ring} mm "
                f"(smallest {round(min(v.annular_ring for v in tight), 3)} mm)",
            )
        )
    return findings


@rule
def rule_drill_variety(ctx: PcbContext) -> list[Finding]:
    """Too many distinct drill sizes costs money at the fab."""
    drills = {round(v.drill, 3) for v in ctx.board.vias if v.drill}
    drills |= {round(p.drill, 3) for fp in ctx.board.footprints for p in fp.pads if p.drill}
    limit = int(ctx.thresholds["max_drill_sizes"])
    if len(drills) > limit:
        return [
            Finding(
                "fab.many_drill_sizes",
                "info",
                f"{len(drills)} distinct drill sizes ({sorted(drills)}) - "
                "consolidating them lowers fab cost",
            )
        ]
    return []


@rule
def rule_unrouted(ctx: PcbContext) -> list[Finding]:
    """Nets with pads on more than one footprint but no copper connecting them.

    DRC reports this authoritatively; this is the fallback when kicad-cli is
    unavailable, and it also catches nets that are completely untouched.
    """
    if ctx.drc and "error" not in ctx.drc:
        return []
    findings = []
    routed_nets = {t.net for t in ctx.board.tracks if t.net}
    routed_nets |= {v.net for v in ctx.board.vias if v.net}
    zone_nets = {z.net for z in ctx.board.zones if z.net}
    for net, pads in sorted(ctx.pads_by_net.items()):
        if len({fp.ref for fp, _ in pads}) < 2:
            continue
        if net in routed_nets or net in zone_nets:
            continue
        findings.append(
            Finding(
                "route.unrouted_net",
                "error",
                f"net {net} has {len(pads)} pads but no tracks, vias or zone",
                location=net,
            )
        )
    return findings


@rule
def rule_edge_clearance(ctx: PcbContext) -> list[Finding]:
    """Copper too close to (or past) the real Edge.Cuts geometry.

    Measured against the flattened outline, not its bounding box: a round board,
    a notch or a mounting-hole cutout all change the answer, and the bounding
    box gets every one of them wrong in both directions.
    """
    board = ctx.board
    if not board.edge_segments():
        return []
    limit = ctx.thresholds["min_edge_clearance_mm"]
    closed = board.outline_closed()

    close: list[dict[str, Any]] = []
    outside: list[dict[str, Any]] = []

    def check(x: float, y: float, half_extent: float, label: str) -> None:
        signed = board.edge_clearance_at(x, y)
        margin = round(signed - half_extent, 4)
        if margin >= limit:
            return
        entry = {"item": label, "at": [round(x, 3), round(y, 3)], "margin_mm": margin}
        # Sitting outside the outline and merely overhanging it are different
        # problems: the first is misplaced copper, the second is a clearance call.
        (outside if signed < 0 and closed else close).append(entry)

    for track in board.tracks:
        for x, y in (track.start, track.end):
            check(x, y, track.width / 2, f"track {track.net or '(no net)'}")
    for via in board.vias:
        check(via.x, via.y, via.size / 2, f"via {via.net or '(no net)'}")
    for fp in board.footprints:
        for pad in fp.pads:
            check(pad.x, pad.y, max(pad.size) / 2, f"{fp.ref}.{pad.number}")

    findings: list[Finding] = []
    if outside:
        findings.append(
            Finding(
                "board.copper_outside_outline",
                "error",
                f"{len(outside)} copper item(s) lie outside the board outline "
                "(they would be milled away)",
                details={"items": outside[:20], "count": len(outside)},
            )
        )
    if close:
        findings.append(
            Finding(
                "board.edge_clearance",
                "warning",
                f"{len(close)} copper item(s) within {limit} mm of the board outline"
                + ("" if closed else "; the outline is not closed, so only distance was checked"),
                details={"items": close[:20], "count": len(close), "outline_closed": closed},
            )
        )
    return findings


@rule
def rule_zone_outside_outline(ctx: PcbContext) -> list[Finding]:
    """A zone drawn somewhere the board is not.

    A footprint may carry zones of its own - a module's pad keep-out is the
    common one - and KiCad stores those in *board* coordinates while every
    other thing in a footprint is stored relative to it. A placer that moves
    the pads and the graphics and forgets the zone leaves the keep-out where
    the library drew it, which is the origin.

    Nothing complains. The keep-out keeps nothing out, DRC is silent because
    an empty region violates no rule, and the only visible sign is the plot:
    "fit to page" fits the *bounding box*, so every view of the board comes
    out at half scale in one corner with the missing half blank. That is how
    this was found.
    """
    box = ctx.board.outline_bbox()
    if not box:
        return []
    x0, y0, x1, y1 = box
    strays = []
    for zone in ctx.board.zones:
        if not zone.outline:
            continue
        zx0 = min(p[0] for p in zone.outline)
        zy0 = min(p[1] for p in zone.outline)
        zx1 = max(p[0] for p in zone.outline)
        zy1 = max(p[1] for p in zone.outline)
        if zx1 < x0 or zx0 > x1 or zy1 < y0 or zy0 > y1:
            kind = "keep-out" if zone.keepout else f"{zone.net or 'unnamed'} pour"
            strays.append(
                f"{kind} on {'/'.join(zone.layers) or '?'} at "
                f"({zx0:.1f}, {zy0:.1f})-({zx1:.1f}, {zy1:.1f})"
            )
    if not strays:
        return []
    return [
        Finding(
            "layout.zone_outside_outline",
            "error",
            f"{len(strays)} zone(s) lie wholly outside the board outline "
            f"({x0:.1f}, {y0:.1f})-({x1:.1f}, {y1:.1f})",
            details={"zones": sorted(strays)[:20], "count": len(strays)},
        )
    ]


@rule
def rule_decoupling_placement(ctx: PcbContext) -> list[Finding]:
    """Decoupling caps must sit next to the pin they decouple."""
    findings = []
    limit = ctx.thresholds["max_decoupling_distance_mm"]
    for fp in ctx.ic_footprints():
        supply_pads = [p for p in fp.pads if p.net and ctx.net_class_of(p.net) == "power"]
        for pad in supply_pads:
            caps = [
                (cap_fp, cap_pad)
                for cap_fp, cap_pad in ctx.pads_by_net.get(pad.net, [])
                if cap_fp.ref.startswith("C")
            ]
            if not caps:
                findings.append(
                    Finding(
                        "layout.no_decoupling",
                        "warning",
                        f"{fp.ref}.{pad.number} ({pad.net}) has no capacitor on that net",
                        location=f"{fp.ref}.{pad.number}",
                    )
                )
                continue
            distance = min(math.dist((pad.x, pad.y), (cap_pad.x, cap_pad.y)) for _, cap_pad in caps)
            if distance > limit:
                nearest = min(caps, key=lambda c: math.dist((pad.x, pad.y), (c[1].x, c[1].y)))[0]
                findings.append(
                    Finding(
                        "layout.decoupling_distance",
                        "warning",
                        f"nearest decoupling cap {nearest.ref} for {fp.ref}.{pad.number} "
                        f"({pad.net}) is {round(distance, 2)} mm away (limit {limit} mm)",
                        location=f"{fp.ref}.{pad.number}",
                        details={"distance_mm": round(distance, 2), "cap": nearest.ref},
                    )
                )
    return findings


@rule
def rule_ground_plane(ctx: PcbContext) -> list[Finding]:
    """Analog boards want a solid ground pour."""
    ground_zones = [
        z for z in ctx.board.zones if ctx.net_class_of(z.net) == "ground" and not z.keepout
    ]
    if not ground_zones:
        return [
            Finding(
                "layout.no_ground_plane",
                "warning",
                "no ground zone found - a ground pour improves return paths and EMC",
            )
        ]
    unfilled = [z for z in ground_zones if z.fill_enabled and not z.filled]
    if unfilled:
        return [
            Finding(
                "layout.unfilled_zone",
                "warning",
                f"{len(unfilled)} ground zone(s) have no computed fill in the file - "
                "run 'Fill all zones' (or 'eda pcb drc', which refills) before plotting",
            )
        ]
    return [
        Finding(
            "layout.ground_plane",
            "info",
            f"{len(ground_zones)} ground zone(s) on layers "
            f"{sorted({layer for z in ground_zones for layer in z.layers})}",
        )
    ]


@rule
def rule_silkscreen(ctx: PcbContext) -> list[Finding]:
    """Reference designators should be present on silkscreen."""
    findings = []
    labelled = {t["footprint"] for t in ctx.board.silk_texts if t["footprint"]}
    missing = [
        fp.ref
        for fp in ctx.board.footprints
        if fp.ref and fp.ref not in labelled and "virtual" not in fp.attributes
    ]
    if missing:
        findings.append(
            Finding(
                "silk.missing_reference",
                "info",
                f"{len(missing)} footprint(s) have no visible silkscreen reference",
                details={"refs": sorted(missing)[:25]},
            )
        )
    return findings


@rule
def rule_board_markings(ctx: PcbContext) -> list[Finding]:
    """What the silkscreen says about the board itself and its connectors.

    A reference designator names a part; nothing else on a generated board
    names the *board*, or says which connector pin carries what. Both are the
    difference between a board and a puzzle the day it is unplugged.
    """
    findings = []
    free = [t for t in ctx.board.silk_texts if not t["footprint"]]
    if not free:
        findings.append(
            Finding(
                "silk.missing_board_id",
                "info",
                "the silkscreen carries only reference designators - no board "
                "name, no revision, and nothing naming any connector pin",
            )
        )
    unlabeled = []
    for fp in ctx.board.footprints:
        if not fp.ref.startswith("J") or len(fp.pads) < 2:
            continue
        if not any(math.hypot(t["x"] - fp.x, t["y"] - fp.y) < 12.0 for t in free):
            unlabeled.append(fp.ref)
    if unlabeled:
        findings.append(
            Finding(
                "silk.unlabeled_connector",
                "info",
                f"{len(unlabeled)} connector(s) have no silkscreen text beside "
                "them saying which pin carries what",
                details={"refs": sorted(unlabeled)},
            )
        )
    return findings


@rule
def rule_indicator_markings(ctx: PcbContext) -> list[Finding]:
    """An indicator or a control with nothing on the silk saying what it means.

    A lit LED and a switch are the board's only interface to somebody holding
    it. "D3" tells them which schematic line it is, which is not the question
    being asked; "3V3 OK" is.
    """
    free = [t for t in ctx.board.silk_texts if not t["footprint"]]
    unlabeled = []
    for fp in ctx.board.footprints:
        indicator = "LED" in (fp.lib_id or "").upper() or fp.ref.startswith("SW")
        if not indicator:
            continue
        if not any(math.hypot(t["x"] - fp.x, t["y"] - fp.y) < 10.0 for t in free):
            unlabeled.append(fp.ref)
    if not unlabeled:
        return []
    return [
        Finding(
            "silk.unlabeled_indicator",
            "warning",
            f"{len(unlabeled)} indicator(s) or control(s) carry no silkscreen "
            "text saying what they mean - a designator is not a meaning",
            details={"refs": sorted(unlabeled)},
        )
    ]


@rule
def rule_connector_at_edge(ctx: PcbContext) -> list[Finding]:
    """A connector stranded in the middle of the board.

    A cable leaves the board; it should not have to cross it first. A connector
    away from the edge blocks the parts around it, fouls its own mating shell,
    and puts the harness over the artwork. Debug headers are the case that
    sometimes has to lose this argument - and it is an argument, made per
    board, not a default.
    """
    limit = ctx.thresholds["max_connector_edge_mm"]
    if limit <= 0 or not ctx.board.edges:
        return []
    xs = [p[0] for e in ctx.board.edges for p in e.get("points", ())]
    ys = [p[1] for e in ctx.board.edges for p in e.get("points", ())]
    if not xs or not ys:
        return []
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    stranded = []
    positions = []
    for fp in ctx.board.footprints:
        if not fp.ref.startswith("J") or len(fp.pads) < 2:
            continue
        box = fp.courtyard_box()
        if box is None:
            continue
        # The courtyard, not the pads: a screw terminal's body reaches past its
        # pads, and it is the body that has to be at the edge for the wire to
        # arrive from outside the board.
        gap = min(box[0] - x0, x1 - box[2], box[1] - y0, y1 - box[3])
        if gap > limit:
            stranded.append(f"{fp.ref} is {gap:.1f} mm in from the nearest edge")
            positions.append((fp.x, fp.y))
    if not stranded:
        return []
    return [
        Finding(
            "layout.connector_not_at_edge",
            "warning",
            f"{len(stranded)} connector(s) sit more than {limit:.0f} mm in from "
            "the board edge - the cable has to cross the board to reach them",
            details={"count": len(stranded), "examples": sorted(stranded), "positions": positions},
        )
    ]


@rule
def rule_track_width_steps(ctx: PcbContext) -> list[Finding]:
    """A track that changes width somewhere that explains nothing.

    Widening a run after it has already gone a distance narrow buys no current:
    the narrow part sets the limit. The one honest place to change width is
    where the constraint stops - at a pad, or at the edge of the pin field that
    forced the neck - so a step out in open copper is either a mistake or a
    router's idea of a compromise.
    """
    free = ctx.thresholds["width_step_free_mm"]
    neck_limit = ctx.thresholds["power_neck_mm"]
    board = ctx.board
    pads = [(pad.x, pad.y) for fp in board.footprints for pad in fp.pads]
    ends = _track_endpoints(board)

    def neck_length(start_key, index) -> float | None:
        """How far the narrow side runs back to a pad without changing width.

        A neck is allowed to be a neck: a 0.5 mm pin row holds 0.2 mm and
        nothing wider, and the escape that walks it out to a routable pitch is
        millimetres long before there is anywhere to widen. What is not allowed
        is a run that goes narrow across open board and then widens for no
        reason. So the question is not "is this step near a pad" but "is the
        narrow side still the pad's own neck", and the answer is the same
        budget `track.thin_power` gives it.
        """
        total = 0.0
        key, current = start_key, index
        seen = {current}
        while True:
            track = board.tracks[current]
            total += track.length
            if total > neck_limit:
                return None
            far = track.end if _key_of(track.start, track.layer) == key else track.start
            if any(math.dist(far, pad) <= 0.4 for pad in pads):
                return total
            nxt = [i for i in ends.get(_key_of(far, track.layer), ()) if i not in seen]
            if len(nxt) != 1:
                return None
            candidate = board.tracks[nxt[0]]
            if candidate.net != track.net or abs(candidate.width - track.width) >= 0.02:
                return None
            key, current = _key_of(far, track.layer), nxt[0]
            seen.add(current)

    steps = []
    positions = []
    for key, indices in ends.items():
        if len(indices) != 2:
            continue
        first, second = (board.tracks[i] for i in indices)
        if first.net != second.net or first.layer != second.layer:
            continue
        if abs(first.width - second.width) < 0.02:
            continue
        point = (key[0] / 1000, key[1] / 1000)
        if any(math.dist(point, pad) <= free for pad in pads):
            continue  # at a pad, which is where a neck is allowed to end
        narrow = indices[0] if first.width < second.width else indices[1]
        if neck_length(key, narrow) is not None:
            continue  # the narrow side is the pin field's own escape
        steps.append(
            f"{first.net or '(no net)'} steps {min(first.width, second.width):.2f} -> "
            f"{max(first.width, second.width):.2f} mm at "
            f"({round(point[0], 2)}, {round(point[1], 2)})"
        )
        positions.append(point)
    if not steps:
        return []
    return [
        Finding(
            "route.width_step",
            "warning",
            f"{len(steps)} place(s) where a track changes width more than "
            f"{free:.0f} mm from any pad - the narrow part already set the "
            "current, so the wide part buys nothing",
            details={"count": len(steps), "examples": sorted(steps)[:8], "positions": positions},
        )
    ]


@rule
def rule_route_under_package(ctx: PcbContext) -> list[Finding]:
    """Foreign copper threaded under a package's body.

    Under an integrated circuit there is no plane between the track and the
    die, the track cannot be probed or reworked, and on anything with an
    exposed pad it is running under grounded metal. Its own escapes belong
    there; nobody else's does.
    """
    board = ctx.board
    findings_by_fp: dict[str, list[str]] = {}
    positions: list[tuple[float, float]] = []
    for fp in board.footprints:
        if len(fp.pads) < 8:
            continue  # a package, not a passive
        box = _footprint_pad_box(fp)
        if box is None:
            continue
        # the body between the pad rows, not the pads themselves
        inset = 0.6
        body = (box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset)
        if body[2] - body[0] < 1.0 or body[3] - body[1] < 1.0:
            continue
        own = {pad.net for pad in fp.pads if pad.net}
        for track in board.tracks:
            if track.net in own or not track.net:
                continue
            if not _segment_hits_box(track.start, track.end, body):
                continue
            findings_by_fp.setdefault(fp.ref, []).append(track.net)
            positions.append(
                ((track.start[0] + track.end[0]) / 2, (track.start[1] + track.end[1]) / 2)
            )
    if not findings_by_fp:
        return []
    examples = [
        f"{ref}: {', '.join(sorted(set(nets))[:4])}" for ref, nets in sorted(findings_by_fp.items())
    ]
    total = sum(len(v) for v in findings_by_fp.values())
    return [
        Finding(
            "route.under_package",
            "warning",
            f"{total} track segment(s) of another net pass under "
            f"{len(findings_by_fp)} package(s) - no plane between the track and "
            "the die, and no way to probe or rework it",
            details={"count": total, "examples": examples, "positions": positions},
        )
    ]


def _segment_hits_box(a, b, box) -> bool:
    """Whether a segment has any part inside an axis-aligned box."""
    x0, y0, x1, y1 = box
    # cheap accept: either end inside
    for px, py in (a, b):
        if x0 <= px <= x1 and y0 <= py <= y1:
            return True
    # otherwise walk it; the boxes here are millimetres across, so a coarse
    # walk cannot miss one and costs nothing
    steps = max(2, int(math.dist(a, b) / 0.25))
    for index in range(steps + 1):
        t = index / steps
        px = a[0] + (b[0] - a[0]) * t
        py = a[1] + (b[1] - a[1]) * t
        if x0 <= px <= x1 and y0 <= py <= y1:
            return True
    return False


def _footprint_pad_box(fp) -> tuple[float, float, float, float] | None:
    boxes = [pad.bbox(angle_offset=fp.angle) for pad in fp.pads]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


@rule
def rule_pour_sides(ctx: PcbContext) -> list[Finding]:
    """Ground on one face of a two-layer board, and nothing on the other.

    The unused face's spare copper costs nothing to pour, drops the ground
    impedance, and gives edge traces a neighbouring return - if it is stitched.
    Reported as context: plenty of working boards ship one-sided, which is why
    this is info and not a defect.
    """
    board = ctx.board
    if len(board.copper_layers) != 2:
        return []
    ground_layers = set()
    for zone in board.zones:
        if zone.keepout or not netlist_helpers_is_ground(zone.net):
            continue
        ground_layers.update(layer for layer in zone.layers if layer.endswith(".Cu"))
    if not ground_layers or len(ground_layers) >= 2:
        return []
    return [
        Finding(
            "layout.pour_single_sided",
            "info",
            f"the ground pour covers only {sorted(ground_layers)[0]} - the other "
            "face's spare copper is unused, and its traces have no adjacent return",
            details={"layers": sorted(ground_layers)},
        )
    ]


@rule
def rule_pour_fragmented(ctx: PcbContext) -> list[Finding]:
    """A ground pour cut into pieces by the tracks crossing it.

    The pour is only a return path while it is *one* piece. A track laid
    across it takes a clearance channel with it, and enough of those turn the
    plane into islands: a return current that starts on one island has to
    leave the layer to get home, which is the loop the plane existed to close.
    Measured as the share of the pour's filled copper in its largest connected
    island - a nibbled edge is nothing, a bisected plane is the finding.

    An island with a via of the pour's own net in it is not on its own: it is
    the same copper as every other island with one, through the plane on the
    far side. So those count as one piece, and what is left to report is the
    piece that has no way home at all - which is the defect the message names.
    Stitching is the fix for a shredded front pour; placement is the fix for a
    bisected plane, and this rule is now only about the second.

    A track through the middle bisects the plane; the same track along the
    edge only trims it. That is the fix, and it is a placement decision.
    """
    limit = ctx.thresholds["min_pour_island_fraction"]
    findings = []
    for zone in ctx.board.zones:
        if zone.keepout or not zone.filled or not netlist_helpers_is_ground(zone.net):
            continue
        # a via of the pour's own net reaches the other side of the board,
        # which is where two pieces of this pour meet each other
        stitches = [(via.x, via.y) for via in ctx.board.vias if via.net == zone.net]
        by_layer: dict[str, list[list[tuple[float, float]]]] = {}
        for layer, points in zone.fills:
            if len(points) >= 3:
                by_layer.setdefault(layer, []).append(points)
        for layer, polygons in sorted(by_layer.items()):
            areas = [abs(_polygon_area(p)) for p in polygons]
            total = sum(areas)
            if total <= 0:
                continue
            islands = _merge_touching(polygons, areas, stitches)
            largest = max(islands)
            share = largest / total
            if share >= limit - 1e-9 or len(islands) < 2:
                continue
            findings.append(
                Finding(
                    "layout.pour_fragmented",
                    "warning",
                    f"the {zone.net} pour on {layer} is cut into {len(islands)} "
                    f"island(s); the largest holds {share:.0%} of its copper "
                    f"(limit {limit:.0%}) - a return starting on another one "
                    "has to leave the layer to get home",
                    details={
                        "layer": layer,
                        "islands": len(islands),
                        "largest_fraction": round(share, 3),
                        "areas_mm2": [round(a, 1) for a in sorted(islands, reverse=True)[:6]],
                    },
                )
            )
    return findings


@rule
def rule_pour_coverage(ctx: PcbContext) -> list[Finding]:
    """A ground pour that fills far less of its own outline than it claims.

    Every track crossing a pour takes a clearance channel with it, and enough
    of them turn the plane into infill between traces. The pour is still one
    piece and still passes every connectivity check; it is simply no longer a
    plane, and the return current under a signal has to find its way around
    the gaps instead of running back underneath it.

    Measured as the share of the pour's own outline that ended up as copper.
    That is the number the eye reads off the plot. It is *context*, not a
    verdict: it is a density, so compacting a board - shortening every run,
    which is the fix a reviewer asks for - lowers it even as the artwork gets
    better. Read it next to the board's size and part count, and use
    `layout.pour_fragmented` for the case that is a defect on its own terms.
    """
    limit = ctx.thresholds["min_pour_coverage"]
    if limit <= 0:
        return []
    findings = []
    for zone in ctx.board.zones:
        if zone.keepout or not zone.filled or not netlist_helpers_is_ground(zone.net):
            continue
        if len(zone.outline) < 3:
            continue
        by_layer: dict[str, list[list[tuple[float, float]]]] = {}
        for layer, points in zone.fills:
            if len(points) >= 3:
                by_layer.setdefault(layer, []).append(points)
        for layer, polygons in sorted(by_layer.items()):
            share = _fill_coverage(polygons, zone.outline)
            if share is None or share >= limit - 1e-9:
                continue
            findings.append(
                Finding(
                    "layout.pour_coverage",
                    "info",
                    f"the {zone.net} pour on {layer} fills {share:.0%} of its own "
                    f"outline (limit {limit:.0%}) - the rest went to the clearance "
                    "the tracks crossing it took, and what is left is infill "
                    "between traces rather than a plane",
                    details={"layer": layer, "coverage": round(share, 3)},
                )
            )
    return findings


def _fill_coverage(
    polygons: list[list[tuple[float, float]]],
    outline: list[tuple[float, float]],
    step: float = 0.5,
) -> float | None:
    """The share of a zone's outline that its fill actually covers.

    Rasterised rather than solved: the fill is hundreds of overlapping pieces,
    so their areas cannot simply be added, and "is there copper here" is a
    point test a grid answers directly. Half a millimetre is finer than any
    clearance channel these boards cut and coarse enough to stay instant.
    """
    x0 = min(p[0] for p in outline)
    x1 = max(p[0] for p in outline)
    y0 = min(p[1] for p in outline)
    y1 = max(p[1] for p in outline)
    nx = max(1, int((x1 - x0) / step) + 1)
    ny = max(1, int((y1 - y0) / step) + 1)
    if nx * ny > 400_000:  # a board too large to raster at this step
        return None

    # bucket the fills so each cell only asks the polygons that could cover it
    buckets: dict[tuple[int, int], list[int]] = {}
    boxes = []
    for index, poly in enumerate(polygons):
        bx0 = min(p[0] for p in poly)
        by0 = min(p[1] for p in poly)
        bx1 = max(p[0] for p in poly)
        by1 = max(p[1] for p in poly)
        boxes.append((bx0, by0, bx1, by1))
        for cx in range(int((bx0 - x0) / step), int((bx1 - x0) / step) + 1):
            for cy in range(int((by0 - y0) / step), int((by1 - y0) / step) + 1):
                buckets.setdefault((cx, cy), []).append(index)

    inside = covered = 0
    for iy in range(ny):
        py = y0 + iy * step
        for ix in range(nx):
            px = x0 + ix * step
            if not _point_in_polygon((px, py), outline):
                continue
            inside += 1
            for index in buckets.get((ix, iy), ()):
                bx0, by0, bx1, by1 = boxes[index]
                if (
                    bx0 <= px <= bx1
                    and by0 <= py <= by1
                    and _point_in_polygon((px, py), polygons[index])
                ):
                    covered += 1
                    break
    return covered / inside if inside else None


def _polygon_area(points: list[tuple[float, float]]) -> float:
    """The shoelace area of a closed polygon."""
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, [*points[1:], points[0]], strict=True):
        total += x0 * y1 - x1 * y0
    return total / 2


def _polygons_touch(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    """Whether two filled polygons share copper - overlap, or touch on an edge.

    The bounding boxes are the cheap gate; a real board's fill polygons are
    L- and C-shaped, so an overlapping box on its own proves nothing and the
    edges have to be asked. Generated fills are rectangles, where the box test
    is already exact and the edge pass agrees with it.
    """
    ax0, ay0 = min(p[0] for p in a), min(p[1] for p in a)
    ax1, ay1 = max(p[0] for p in a), max(p[1] for p in a)
    bx0, by0 = min(p[0] for p in b), min(p[1] for p in b)
    bx1, by1 = max(p[0] for p in b), max(p[1] for p in b)
    tol = 1e-6
    if ax1 < bx0 - tol or bx1 < ax0 - tol or ay1 < by0 - tol or by1 < ay0 - tol:
        return False
    a_edges = list(zip(a, [*a[1:], a[0]], strict=True))
    b_edges = list(zip(b, [*b[1:], b[0]], strict=True))
    for p0, p1 in a_edges:
        for q0, q1 in b_edges:
            if _segments_meet(p0, p1, q0, q1):
                return True
    # one wholly inside the other still shares copper
    return _point_in_polygon(a[0], b) or _point_in_polygon(b[0], a)


def _segments_meet(p0, p1, q0, q1) -> bool:
    def side(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1, d2 = side(q0, q1, p0), side(q0, q1, p1)
    d3, d4 = side(p0, p1, q0), side(p0, p1, q1)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    # collinear touching counts: two rectangles welded along one edge are one
    for point, (s0, s1) in ((p0, (q0, q1)), (p1, (q0, q1)), (q0, (p0, p1)), (q1, (p0, p1))):
        if abs(side(s0, s1, point)) < 1e-9 and _between(s0, s1, point):
            return True
    return False


def _between(a, b, point) -> bool:
    return (
        min(a[0], b[0]) - 1e-9 <= point[0] <= max(a[0], b[0]) + 1e-9
        and min(a[1], b[1]) - 1e-9 <= point[1] <= max(a[1], b[1]) + 1e-9
    )


def _point_in_polygon(point, polygon) -> bool:
    x, y = point
    inside = False
    for (x0, y0), (x1, y1) in zip(polygon, [*polygon[1:], polygon[0]], strict=True):
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0 or 1e-12) + x0:
            inside = not inside
    return inside


def _merge_touching(
    polygons: list[list[tuple[float, float]]],
    areas: list[float],
    stitches: Sequence[tuple[float, float]] = (),
) -> list[float]:
    """Group polygons that share copper, and return one area per group.

    A generated fill is many welded rectangles that are electrically one
    island; counting the polygons would call an unbroken plane fragmented.
    Overlaps are counted once by taking the group's bounding-box area as an
    upper bound only when it is smaller than the sum - the pieces of a real
    island tile it, so the sum is what matters and the cap only stops the
    weld overlap from inflating it.

    ``stitches`` are points where the pour reaches the other side of the board.
    Two pieces that both hold one are the same copper, through that side, so
    they are grouped as one.
    """
    parent = list(range(len(polygons)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, one in enumerate(polygons):
        for j in range(i + 1, len(polygons)):
            if find(i) != find(j) and _polygons_touch(one, polygons[j]):
                parent[find(i)] = find(j)

    stitched: int | None = None
    for point in stitches:
        for index, polygon in enumerate(polygons):
            if not _point_in_polygon(point, polygon):
                continue
            if stitched is None:
                stitched = index
            elif find(index) != find(stitched):
                parent[find(index)] = find(stitched)
            break

    groups: dict[int, list[int]] = {}
    for index in range(len(polygons)):
        groups.setdefault(find(index), []).append(index)
    out = []
    for members in groups.values():
        total = sum(areas[i] for i in members)
        xs = [p[0] for i in members for p in polygons[i]]
        ys = [p[1] for i in members for p in polygons[i]]
        box = (max(xs) - min(xs)) * (max(ys) - min(ys))
        out.append(min(total, box) if box > 0 else total)
    return out


def netlist_helpers_is_ground(net: str) -> bool:
    return netlist_mod.classify_net(net or "") == "ground"


@rule
def rule_placement(ctx: PcbContext) -> list[Finding]:
    """Footprints outside the board outline, and mixed-side assembly cost."""
    findings = []
    bbox = ctx.board.outline_bbox()
    if bbox:
        min_x, min_y, max_x, max_y = bbox
        exact = ctx.board.outline_closed()
        outside = [
            fp.ref
            for fp in ctx.board.footprints
            if (
                ctx.board.edge_clearance_at(fp.x, fp.y) < 0
                if exact
                else not (min_x <= fp.x <= max_x and min_y <= fp.y <= max_y)
            )
        ]
        if outside:
            findings.append(
                Finding(
                    "layout.outside_outline",
                    "error",
                    f"{len(outside)} footprint(s) sit outside the board outline",
                    details={"refs": sorted(outside)[:25]},
                )
            )
    bottom = [fp for fp in ctx.board.footprints if fp.side == "bottom"]
    if bottom:
        findings.append(
            Finding(
                "layout.double_sided_assembly",
                "info",
                f"{len(bottom)} footprint(s) on the bottom side - double sided assembly",
                details={"refs": sorted(fp.ref for fp in bottom)[:25]},
            )
        )
    return findings


@rule
def rule_mounting_and_testpoints(ctx: PcbContext) -> list[Finding]:
    findings = []
    refs = [fp.ref for fp in ctx.board.footprints]
    if not any(r.startswith(("H", "MH")) for r in refs):
        findings.append(
            Finding(
                "mechanical.no_mounting_holes", "info", "no mounting hole footprint (H*/MH*) found"
            )
        )
    if not any(r.startswith("TP") for r in refs):
        findings.append(
            Finding(
                "test.no_testpoints",
                "info",
                "no test point footprints (TP*) found - consider adding them for bring-up",
            )
        )
    return findings


@rule
def rule_layer_usage(ctx: PcbContext) -> list[Finding]:
    """Report copper usage per layer - catches 'everything on one layer' boards."""
    per_layer = Counter(t.layer for t in ctx.board.tracks)
    if not per_layer:
        return [Finding("route.no_tracks", "warning", "the board has no routed tracks at all")]
    return [
        Finding(
            "route.layer_usage",
            "info",
            "track segments per layer: "
            + ", ".join(f"{k}={v}" for k, v in sorted(per_layer.items())),
        )
    ]


# ---------------------------------------------------------------------------
# Artwork readability and buildability: what a layout engineer objects to on
# sight. DRC passes a board whose silkscreen is unreadable, whose parts sit at
# 37 degrees and whose decoupling has no way down to the plane.
# ---------------------------------------------------------------------------


def _boxes_overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return (
        min(a[2], b[2]) - max(a[0], b[0]) > GEOM_TOL
        and min(a[3], b[3]) - max(a[1], b[1]) > GEOM_TOL
    )


def _silk_bbox(text: dict[str, Any]) -> tuple[float, ...] | None:
    """Rough extent of a silkscreen string.

    KiCad's stroke font advances roughly 0.75 of the glyph width per character;
    deliberately on the low side, so the rule reports overlap it is sure of
    rather than every label that passes near a pad.
    """
    height = float(text.get("height") or 0.0)
    width = float(text.get("width") or height)
    body = str(text.get("text") or "")
    if height <= 0 or not body:
        return None
    thickness = float(text.get("thickness") or 0.0)
    span = len(body) * width * 0.75 + thickness
    extent = height + thickness
    half_x, half_y = span / 2, extent / 2
    if round(abs(float(text.get("angle") or 0.0)) % 180) == 90:
        half_x, half_y = half_y, half_x
    x, y = float(text["x"]), float(text["y"])
    return (x - half_x, y - half_y, x + half_x, y + half_y)


def _pad_layers(pad: pcb.Pad, board: pcb.Board) -> set[str]:
    layers: set[str] = set()
    for layer in pad.layers:
        if layer.startswith("*"):
            suffix = layer[1:]
            layers |= {name for name in board.copper_layers if name.endswith(suffix)}
            layers |= {f"F{suffix}", f"B{suffix}"}
        else:
            layers.add(layer)
    return layers


class _PadIndex:
    """Pads bucketed by the board cells they cover, so a box query is local.

    Asking "which pads does this label print over" once per silkscreen string is
    quadratic against the pad list, and a dense board has thousands of both.
    """

    CELL = 5.0  # mm

    def __init__(self, board: pcb.Board) -> None:
        self.entries: list[tuple[tuple[float, ...], set[str], str]] = []
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for fp in board.footprints:
            for pad in fp.pads:
                box = pad.bbox(angle_offset=fp.angle)
                self.entries.append((box, _pad_layers(pad, board), f"{fp.ref}.{pad.number}"))
                for key in self._keys(box):
                    self.cells[key].append(len(self.entries) - 1)

    @classmethod
    def _keys(cls, box: tuple[float, ...]):
        for cx in range(math.floor(box[0] / cls.CELL), math.floor(box[2] / cls.CELL) + 1):
            for cy in range(math.floor(box[1] / cls.CELL), math.floor(box[3] / cls.CELL) + 1):
                yield (cx, cy)

    def near(self, box: tuple[float, ...]):
        seen: set[int] = set()
        for key in self._keys(box):
            for index in self.cells.get(key, ()):
                if index not in seen:
                    seen.add(index)
                    yield self.entries[index]


@rule
def rule_silk_text_size(ctx: PcbContext) -> list[Finding]:
    """Silkscreen below what the screen printer can hold."""
    limit = ctx.thresholds["min_silk_text_height_mm"]
    small = [
        t
        for t in ctx.board.silk_texts
        if not t.get("hidden") and 0 < float(t.get("height") or 0) < limit - 1e-9
    ]
    if not small:
        return []
    smallest = min(float(t["height"]) for t in small)
    return [
        Finding(
            "silk.text_too_small",
            "warning",
            f"{len(small)} silkscreen text(s) below {limit} mm high (smallest {smallest} mm) - "
            "the fab will print it as a smudge",
            details={
                "count": len(small),
                "examples": [
                    f"{t.get('footprint') or 'board'}: {t['text']!r} at {t['height']} mm"
                    for t in small[:8]
                ],
            },
        )
    ]


@rule
def rule_silk_over_pad(ctx: PcbContext) -> list[Finding]:
    """Silkscreen printed across a pad.

    Ink on a pad keeps solder off it. It is also the first thing to go when the
    reference designators are placed by whatever had room rather than by
    someone who will have to read them on the assembled board.
    """
    board = ctx.board
    pads = _PadIndex(board)
    collisions = []
    for text in board.silk_texts:
        if text.get("hidden"):
            continue
        box = _silk_bbox(text)
        if not box:
            continue
        side = "B." if str(text.get("layer", "")).startswith("B.") else "F."
        for pad_box, layers, label in pads.near(box):
            if not any(layer.startswith(side) for layer in layers):
                continue
            if _boxes_overlap(box, pad_box):
                collisions.append(f"{text['text']!r} over {label}")
    collisions = sorted(set(collisions))
    if not collisions:
        return []
    return [
        Finding(
            "silk.over_pad",
            "warning",
            f"{len(collisions)} silkscreen item(s) print across a pad",
            details={"count": len(collisions), "examples": collisions[:8]},
        )
    ]


@rule
def rule_silk_over_silk(ctx: PcbContext) -> list[Finding]:
    """Two silkscreen strings printed through each other.

    The schematic has `readability.text_over_text` for exactly this and the
    board had nothing, though the board is the harder case: a sheet can be
    zoomed and a bare board cannot, and the legend beside a connector is the
    only thing telling an assembler which pin is which.

    Sides are kept apart - front ink cannot collide with back ink - and the
    extent comes from the size the file states, not from a guess.
    """
    boxes = []
    for text in ctx.board.silk_texts:
        if text.get("hidden") or not str(text.get("text", "")).strip():
            continue
        box = _silk_bbox(text)
        if box:
            boxes.append((box, text))
    # Bucketed, for the same reason `_PadIndex` is: a baseboard carries a few
    # thousand silkscreen strings and asking each about every other one is
    # minutes of arithmetic to find the handful that touch.
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    cell = 5.0
    for index, (box, _text) in enumerate(boxes):
        for cx in range(math.floor(box[0] / cell), math.floor(box[2] / cell) + 1):
            for cy in range(math.floor(box[1] / cell), math.floor(box[3] / cell) + 1):
                cells[(cx, cy)].append(index)
    collisions = []
    for index, (box, one) in enumerate(boxes):
        side = str(one.get("layer", ""))[:2]
        near: set[int] = set()
        for cx in range(math.floor(box[0] / cell), math.floor(box[2] / cell) + 1):
            for cy in range(math.floor(box[1] / cell), math.floor(box[3] / cell) + 1):
                near.update(cells.get((cx, cy), ()))
        for other_index in near:
            if other_index <= index:
                continue
            other_box, other = boxes[other_index]
            if str(other.get("layer", ""))[:2] != side:
                continue
            if _boxes_overlap(box, other_box):
                collisions.append(f"{one['text']!r} over {other['text']!r}")
    collisions = sorted(set(collisions))
    if not collisions:
        return []
    return [
        _group_finding(
            "silk.text_over_text",
            "warning",
            f"{len(collisions)} pair(s) of silkscreen text print through each other",
            collisions,
        )
    ]


@rule
def rule_placement_grid(ctx: PcbContext) -> list[Finding]:
    """Footprint origins off the placement grid, and odd rotations.

    Neither costs anything electrically; together they are most of why a
    generated layout looks like a generated layout, and they make every later
    edit - aligning a row, matching a connector to a mating part - a fight.
    """
    grid = ctx.thresholds["placement_grid_mm"]
    step = ctx.thresholds["rotation_step_deg"]
    findings = []
    if grid > 0:
        off = [
            f"{fp.ref} at ({round(fp.x, 3)}, {round(fp.y, 3)})"
            for fp in ctx.board.footprints
            if abs(fp.x / grid - round(fp.x / grid)) * grid > GEOM_TOL
            or abs(fp.y / grid - round(fp.y / grid)) * grid > GEOM_TOL
        ]
        if off:
            findings.append(
                Finding(
                    "layout.off_grid_placement",
                    "info",
                    f"{len(off)} footprint(s) are not on the {grid} mm placement grid",
                    details={"count": len(off), "examples": off[:8]},
                )
            )
    if step > 0:
        odd = [
            f"{fp.ref} at {fp.angle} deg"
            for fp in ctx.board.footprints
            if min(fp.angle % step, step - (fp.angle % step)) > 1e-6
        ]
        if odd:
            findings.append(
                Finding(
                    "layout.odd_rotation",
                    "info",
                    f"{len(odd)} footprint(s) are turned to something other than a "
                    f"multiple of {step} deg",
                    details={"count": len(odd), "examples": odd[:8]},
                )
            )
    return findings


@rule
def rule_pad_collision(ctx: PcbContext) -> list[Finding]:
    """Pads of different footprints sharing the same copper.

    DRC is authoritative here when it can run; this catches the same thing from
    the geometry alone, which is what is left when the board is being generated
    rather than edited.
    """
    board = ctx.board
    entries = []
    for fp in board.footprints:
        for pad in fp.pads:
            entries.append((pad.bbox(angle_offset=fp.angle), fp, pad))
    entries.sort(key=lambda item: item[0][0])

    collisions = []
    for i, (box_a, fp_a, pad_a) in enumerate(entries):
        layers_a = _pad_layers(pad_a, board)
        for box_b, fp_b, pad_b in entries[i + 1 :]:
            if box_b[0] >= box_a[2] - GEOM_TOL:
                break  # sorted by left edge
            if fp_a.ref == fp_b.ref:
                continue
            if not layers_a & _pad_layers(pad_b, board):
                continue
            if _boxes_overlap(box_a, box_b):
                collisions.append(f"{fp_a.ref}.{pad_a.number} / {fp_b.ref}.{pad_b.number}")
    collisions = sorted(set(collisions))
    if not collisions:
        return []
    return [
        Finding(
            "layout.pad_collision",
            "warning",
            f"{len(collisions)} pad pair(s) from different footprints overlap - "
            "the parts are placed on top of each other",
            details={"count": len(collisions), "examples": collisions[:8]},
        )
    ]


def _key_of(point: tuple[float, float], layer: str) -> tuple[int, int, str]:
    return (round(point[0] * 1000), round(point[1] * 1000), layer)


def _track_endpoints(board: pcb.Board) -> dict[tuple[int, int, str], list[int]]:
    index: dict[tuple[int, int, str], list[int]] = defaultdict(list)
    for i, track in enumerate(board.tracks):
        for point in (track.start, track.end):
            index[(round(point[0] * 1000), round(point[1] * 1000), track.layer)].append(i)
    return index


@rule
def rule_track_angles(ctx: PcbContext) -> list[Finding]:
    """Corners tighter than a right angle.

    An acute corner traps etchant, so it keeps etching after the rest of the
    track is done, and it is a discontinuity for anything fast. Routing at 45
    degrees costs nothing and avoids both.
    """
    limit = ctx.thresholds["min_track_angle_deg"]
    if limit <= 0:
        return []
    board = ctx.board
    acute: list[str] = []
    right: list[str] = []
    odd: list[str] = []
    where: dict[str, list[tuple[float, float]]] = {"acute": [], "right": [], "odd": []}
    # Copper meeting on a pad is a junction, not a corner: three or more
    # branches leaving the same pad have angles between them, and none of them
    # is a bend in anything. Exactly two ends is different - the copper passes
    # *through* the pad, the two are one run, and the angle between them is as
    # much a bend as any other. A pad is also exactly where a route that has to
    # double back does it, so skipping those was skipping most of them.
    #
    # The exemption is the pad's own connection point, not a disc around it.
    # Measured by radius it covered a 0805's whole 0.47 mm and swallowed the
    # five or six ordinary corners that a chamfered pad entry leaves inside it,
    # which is how eleven of the thirteen hairpins on these boards went unseen.
    endpoints = _track_endpoints(board)
    ends_at: dict[tuple[int, int], int] = defaultdict(int)
    for key, indices in endpoints.items():
        ends_at[(key[0], key[1])] += len(indices)
    junctions = {
        (round(pad.x * 1000), round(pad.y * 1000))
        for fp in board.footprints
        for pad in fp.pads
        if ends_at[(round(pad.x * 1000), round(pad.y * 1000))] > 2
    }
    # Two branches leaving one pad at an angle is not an acid trap either: the
    # wedge between them is filled by the pad's own copper. Two branches
    # leaving it along the *same* line is a different thing - that is one run
    # drawn twice, and the pad does not excuse it - so the pad points are held
    # aside and judged on that alone, below.
    on_pad = {
        (round(pad.x * 1000), round(pad.y * 1000)) for fp in board.footprints for pad in fp.pads
    }
    for key, indices in endpoints.items():
        if len(indices) != 2 or (key[0], key[1]) in junctions:
            continue
        first, second = (board.tracks[i] for i in indices)
        if first.net != second.net or "arc" in (first.kind, second.kind):
            continue
        point = (key[0] / 1000, key[1] / 1000)
        vectors = []
        for track in (first, second):
            far = (
                track.end
                if math.dist(track.start, point) < math.dist(track.end, point)
                else (track.start)
            )
            length = math.dist(far, point)
            if length > GEOM_TOL:
                vectors.append(((far[0] - point[0]) / length, (far[1] - point[1]) / length))
        if len(vectors) != 2:
            continue
        cosine = max(-1.0, min(1.0, vectors[0][0] * vectors[1][0] + vectors[0][1] * vectors[1][1]))
        angle = math.degrees(math.acos(cosine))
        # Half a degree of slack, not an epsilon. The angle comes out of two
        # normalised vectors and an arc cosine, so a corner drawn at exactly 90
        # degrees lands a ten-thousandth under it - and a right angle reported as
        # tighter than a right angle is a finding nobody can act on.
        if (key[0], key[1]) in on_pad and angle > 5.0:
            continue
        if angle < limit - 0.5:
            acute.append(
                f"{first.net or '(no net)'} at "
                f"({round(point[0], 2)}, {round(point[1], 2)}): {round(angle)} deg"
            )
            where["acute"].append(point)
        elif abs(angle - 90.0) <= 0.5:
            right.append(
                f"{first.net or '(no net)'} at ({round(point[0], 2)}, {round(point[1], 2)})"
            )
            where["right"].append(point)
        elif min(angle % 45.0, 45.0 - angle % 45.0) > 2.0 and angle < 178.0:
            odd.append(
                f"{first.net or '(no net)'} at "
                f"({round(point[0], 2)}, {round(point[1], 2)}): {round(angle)} deg"
            )
            where["odd"].append(point)
    findings = []
    if acute:
        findings.append(
            Finding(
                "route.acute_angle",
                "info",
                f"{len(acute)} track corner(s) tighter than {limit} deg",
                details={
                    "count": len(acute),
                    "examples": sorted(acute)[:8],
                    "positions": where["acute"],
                },
            )
        )
    if right:
        findings.append(
            Finding(
                "route.right_angle",
                "info",
                f"{len(right)} track corner(s) turn a full 90 deg - two 45s cost "
                "nothing and read as routed rather than drawn",
                details={
                    "count": len(right),
                    "examples": sorted(right)[:8],
                    "positions": where["right"],
                },
            )
        )
    if odd:
        findings.append(
            Finding(
                "route.odd_angle",
                "info",
                f"{len(odd)} track corner(s) bend off the 45-degree grid - a "
                "20 or 70 degree turn reads as a slip of the mouse, not a route",
                details={
                    "count": len(odd),
                    "examples": sorted(odd)[:8],
                    "positions": where["odd"],
                },
            )
        )
    return findings


@rule
def rule_track_stubs(ctx: PcbContext) -> list[Finding]:
    """Track ends that connect to nothing.

    Copper with one free end is an antenna the schematic never asked for, and
    it is usually the tail of a route that was abandoned half way.
    """
    board = ctx.board
    if not board.tracks:
        return []
    zone_layers = {(z.net, layer) for z in board.zones if not z.keepout for layer in z.layers}
    via_points = {(round(v.x * 1000), round(v.y * 1000)) for v in board.vias}
    pad_boxes = [
        (pad.bbox(angle_offset=fp.angle), _pad_layers(pad, board))
        for fp in board.footprints
        for pad in fp.pads
    ]
    endpoints = _track_endpoints(board)
    # Only same-layer, same-net copper can pick up a loose end, so the "does it
    # land mid-track" scan never has to look at the whole board.
    siblings: dict[tuple[str, str], list[pcb.Track]] = defaultdict(list)
    for track in board.tracks:
        siblings[(track.layer, track.net)].append(track)

    stubs = []
    for track in board.tracks:
        if (track.net, track.layer) in zone_layers:
            continue  # a track that ends inside a pour of its own net is connected
        for point in (track.start, track.end):
            key = (round(point[0] * 1000), round(point[1] * 1000), track.layer)
            if len(endpoints.get(key, ())) > 1 or (key[0], key[1]) in via_points:
                continue
            if any(
                track.layer in layers
                and box[0] <= point[0] <= box[2]
                and box[1] <= point[1] <= box[3]
                for box, layers in pad_boxes
            ):
                continue
            if any(
                other is not track and _on_track(point, other)
                for other in siblings[(track.layer, track.net)]
            ):
                continue
            stubs.append(
                f"{track.net or '(no net)'} on {track.layer} at "
                f"({round(point[0], 2)}, {round(point[1], 2)})"
            )
    stubs = sorted(set(stubs))
    if not stubs:
        return []
    return [
        Finding(
            "route.stub",
            "warning",
            f"{len(stubs)} track end(s) reach no pad, via or other track",
            details={"count": len(stubs), "examples": stubs[:8]},
        )
    ]


def _on_track(point: tuple[float, float], track: pcb.Track) -> bool:
    a, b = track.start, track.end
    if not (min(a[0], b[0]) - GEOM_TOL <= point[0] <= max(a[0], b[0]) + GEOM_TOL):
        return False
    if not (min(a[1], b[1]) - GEOM_TOL <= point[1] <= max(a[1], b[1]) + GEOM_TOL):
        return False
    cross = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])
    length = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
    return abs(cross) / length <= GEOM_TOL


@rule
def rule_decoupling_via(ctx: PcbContext) -> list[Finding]:
    """Decoupling capacitors with no via down to the plane.

    The capacitor's job is to close a high-frequency loop, and the loop runs
    through the ground plane. A ground pad that has to travel millimetres of
    track to find a via has more inductance in the path than the part removes,
    however close the capacitor sits to the pin.

    A capacitor sitting on a layer that already carries the pour needs no via at
    all - its pad drops straight into the copper. Running this over the demo
    corpus without that exemption made it the noisiest rule in the whole suite,
    and most of what it reported was two-layer boards poured on both sides.
    """
    board = ctx.board
    ground_zone_layers = {
        layer
        for z in board.zones
        if not z.keepout and ctx.net_class_of(z.net) == "ground"
        for layer in z.layers
    }
    if not ground_zone_layers:
        return []  # no plane to reach: layout.no_ground_plane covers that case
    if not board.vias:
        return []
    limit = ctx.thresholds["max_decoupling_via_mm"]
    findings = []
    for fp in board.footprints:
        if not fp.ref.startswith("C"):
            continue
        nets = {ctx.net_class_of(p.net): p for p in fp.pads if p.net}
        if "power" not in nets or "ground" not in nets:
            continue
        pad = nets["ground"]
        if _pad_layers(pad, board) & ground_zone_layers:
            continue  # the pad is already in the pour
        vias = [v for v in board.vias if v.net == pad.net]
        if not vias:
            continue
        # Measure to the edge of the pad, not its centre. A bulk electrolytic has
        # a pad two and a half millimetres tall, so a via placed as close as it
        # can physically go is still 1.5 mm from the centre - the rule would ask
        # for a via inside the pad it is meant to sit beside.
        x0, y0, x1, y1 = pad.bbox(angle_offset=fp.angle)
        distance = min(
            math.dist(
                (min(max(v.x, x0), x1), min(max(v.y, y0), y1)),
                (v.x, v.y),
            )
            for v in vias
        )
        if distance > limit:
            findings.append(
                Finding(
                    "layout.decoupling_via",
                    "warning",
                    f"{fp.ref}: nearest {pad.net} via is {round(distance, 2)} mm from the "
                    f"edge of its ground pad (limit {limit} mm) - the return loop runs "
                    f"through that track",
                    location=fp.ref,
                    details={"distance_mm": round(distance, 2), "limit_mm": limit},
                )
            )
    return findings


@rule
def rule_track_width_consistency(ctx: PcbContext) -> list[Finding]:
    """Nets routed at several different widths.

    A width change mid-net is a deliberate act - a neck-down into a fine-pitch
    pad, a fat power spine - and worth being deliberate about. Three or more
    widths on one net is usually nobody having decided.
    """
    by_net: dict[str, set[float]] = defaultdict(set)
    for track in ctx.board.tracks:
        if track.net:
            by_net[track.net].add(round(track.width, 3))
    noisy = {net: sorted(widths) for net, widths in by_net.items() if len(widths) >= 3}
    if not noisy:
        return []
    return [
        Finding(
            "route.mixed_track_widths",
            "info",
            f"{len(noisy)} net(s) are routed at three or more different widths",
            details={
                "count": len(noisy),
                "examples": [f"{net}: {widths}" for net, widths in sorted(noisy.items())[:8]],
            },
        )
    ]


def review(
    target: str | os.PathLike[str],
    *,
    use_cli: bool = True,
    thresholds: dict[str, float] | None = None,
    collapse: int = COLLAPSE_LIMIT,
) -> dict[str, Any]:
    ctx = PcbContext(target, use_cli=use_cli, thresholds=thresholds)
    findings: list[Finding] = []
    for func in RULES:
        try:
            findings.extend(func(ctx))
        except Exception as exc:
            findings.append(
                Finding(
                    f"internal.{func.__name__}", "info", f"rule failed: {type(exc).__name__}: {exc}"
                )
            )
    findings = sort_findings(collapse_findings(findings, collapse))
    return {
        "board": str(ctx.path),
        "statistics": pcb.summary(ctx.board),
        "thresholds": ctx.thresholds,
        "drc_available": bool(ctx.drc and "error" not in ctx.drc),
        "summary": summarize(findings),
        "findings": [f.to_dict() for f in findings],
    }


def info(target: str | os.PathLike[str]) -> dict[str, Any]:
    board = pcb.parse(pcb.find_board(target))
    data = pcb.summary(board)
    data["footprints_detail"] = [fp.to_dict() for fp in board.footprints]
    data["nets_detail"] = sorted(
        (
            {
                "name": name,
                "pads": len(board.pads_on_net(name)),
                "class": netlist_mod.classify_net(name),
                "track_length_mm": round(sum(t.length for t in board.tracks if t.net == name), 2),
                "vias": len([v for v in board.vias if v.net == name]),
            }
            for name in board.nets.values()
            if name
        ),
        key=lambda n: n["name"],
    )
    return data


def _group_finding(rule_name: str, severity: str, message: str, items: list[str]) -> Finding:
    """One finding for a whole category, carrying the count and some examples."""
    return Finding(
        rule_name,
        severity,
        message,
        details={"count": len(items), "examples": items[:8]},
    )


def _mst_length(points: list[tuple[float, float]]) -> float:
    """Length of the Euclidean minimum spanning tree - the shortest a net's
    routing could conceivably be, ignoring obstacles."""
    if len(points) < 2:
        return 0.0
    in_tree = [points[0]]
    rest = list(points[1:])
    best = {p: math.dist(points[0], p) for p in rest}
    total = 0.0
    while rest:
        nearest = min(rest, key=lambda p: best[p])
        total += best[nearest]
        rest.remove(nearest)
        in_tree.append(nearest)
        for p in rest:
            d = math.dist(nearest, p)
            if d < best[p]:
                best[p] = d
    return total


# Below this much excess copper the tour is not worth talking about, whatever
# the ratio says - a 3 mm net routed at 9 mm is fine.
_DETOUR_FLOOR_MM = 10.0


@rule
def rule_detour(ctx: PcbContext) -> list[Finding]:
    """Routing that goes the long way round.

    DRC has no opinion about a track that wanders: it is exactly as legal at
    three times the length. A person notices immediately - the long diagonal
    across open board is the signature of an autorouter that found *a* path and
    stopped. Measured against the minimum spanning tree of the net's pads,
    which no real route beats, so the ratio is a true lower bound on the tour.
    """
    limit = ctx.thresholds["detour_ratio"]
    zoned = {z.net for z in ctx.board.zones if not z.keepout}
    routed_len: dict[str, float] = defaultdict(float)
    for track in ctx.board.tracks:
        if track.net:
            routed_len[track.net] += track.length
    offenders = []
    for net, pads in sorted(ctx.pads_by_net.items()):
        if net in zoned or ctx.net_class_of(net) == "ground":
            continue  # a pour reshapes the question
        points = sorted({(round(p.x, 3), round(p.y, 3)) for _, p in pads})
        routed = routed_len.get(net, 0.0)
        if len(points) < 2 or routed <= 0:
            continue
        shortest = _mst_length(points)
        if shortest < 0.5:
            continue
        ratio = routed / shortest
        if ratio > limit and routed - shortest > _DETOUR_FLOOR_MM:
            offenders.append(
                f"{net}: {routed:.1f} mm routed for {shortest:.1f} mm of net ({ratio:.1f}x)"
            )
    if not offenders:
        return []
    return [
        _group_finding(
            "route.detour",
            "warning",
            f"{len(offenders)} net(s) routed at more than {limit}x the length "
            "they need - the scenic tour reads as machine routing",
            offenders,
        )
    ]


# Below this much excess copper a wandering run is not worth talking about:
# the knee that takes a track round a pad is a detour and is also correct.
_WANDER_FLOOR_MM = 5.0


@rule
def rule_wander(ctx: PcbContext) -> list[Finding]:
    """A single run of copper that goes out and comes back.

    `route.detour` weighs a whole net, and a net hides things: six good
    connections and one that loops round the board average out under any
    ratio worth setting. What the eye catches on the plot is not the net, it
    is the one track that leaves its pad, travels three sides of a rectangle
    and arrives 4 mm away. So this walks each net's copper as a graph, cuts it
    at every pad and every junction, and measures each run that is left
    against the straight line between its own two ends.

    Ground is skipped for the same reason `route.detour` skips it: a net with
    a pour is not routed, it is filled, and the stitching that holds the fill
    together is not a tour.
    """
    limit = ctx.thresholds["wander_ratio"]
    zoned = {z.net for z in ctx.board.zones if not z.keepout}
    # A run from one end of a package to the other cannot take the straight
    # line: the straight line is through the package. Measured against it
    # every feedback wrap on a SOT-23 reads as a tour, so the baseline goes
    # round what the copper had to go round.
    courtyards = [box for fp in ctx.board.footprints if (box := fp.courtyard_box())]
    by_net: dict[str, list[pcb.Track]] = defaultdict(list)
    for track in ctx.board.tracks:
        if track.net and track.length > GEOM_TOL:
            by_net[track.net].append(track)

    offenders = []
    for net, tracks in sorted(by_net.items()):
        if net in zoned or ctx.net_class_of(net) == "ground":
            continue
        # A pad or a via is a place the run is allowed to end: copper that
        # stops there has arrived, not wandered. Vias also stitch the two
        # layers into one graph, which is why the node key drops the layer.
        anchors = {_node(p.x, p.y) for _fp, p in ctx.pads_by_net.get(net, [])} | {
            _node(v.x, v.y) for v in ctx.board.vias if v.net == net
        }
        graph: dict[tuple[int, int], list[tuple[tuple[int, int], float]]] = defaultdict(list)
        for track in tracks:
            a, b = _node(*track.start), _node(*track.end)
            if a == b:
                continue
            graph[a].append((b, track.length))
            graph[b].append((a, track.length))
        # Anything that is not a pad, not a via and not a corner is a branch:
        # the run ends there too, because past it the copper is someone else's.
        stops = {node for node in graph if node in anchors or len(graph[node]) != 2}
        seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        for start in stops:
            for first, first_len in graph[start]:
                if (start, first) in seen:
                    continue
                seen.add((start, first))
                previous, current, length = start, first, first_len
                # Bounded by the number of segments: a net whose copper closes
                # a ring of plain corners has no stop to arrive at, and the
                # walk would go round it for ever.
                for _ in range(len(tracks)):
                    if current in stops:
                        break
                    step = next(
                        ((n, d) for n, d in graph[current] if n != previous),
                        None,
                    )
                    if step is None:
                        break
                    previous, current = current, step[0]
                    length += step[1]
                seen.add((current, previous))
                direct = _shortest_clear(_point(start), _point(current), courtyards)
                if direct < GEOM_TOL:
                    continue
                if length > direct * limit and length - direct > _WANDER_FLOOR_MM:
                    offenders.append(
                        f"{net}: {length:.1f} mm of copper between "
                        f"({_point(start)[0]:.1f}, {_point(start)[1]:.1f}) and "
                        f"({_point(current)[0]:.1f}, {_point(current)[1]:.1f}), "
                        f"{direct:.1f} mm apart ({length / direct:.1f}x)"
                    )
    if not offenders:
        return []
    return [
        _group_finding(
            "route.wander",
            "warning",
            f"{len(offenders)} run(s) of copper more than {limit}x the straight "
            "line between their own ends - a track that goes out and comes back",
            sorted(offenders),
        )
    ]


def _shortest_clear(a, b, boxes) -> float:
    """The straight line, or a lower bound on the way round what it crosses.

    Not a real path search: the boxes the line crosses are taken as one
    rectangle and the shortest way round *that* is measured, over its four
    corners. Any real route round the obstacle is at least this long, so the
    ratio built on it stays a lower bound on how far the copper strayed.

    An end inside the rectangle - which every pad is, of its own package -
    reaches the nearest edge for nothing. That makes the baseline for a
    feedback wrap what it should be: half way round the package, not the
    diagonal across it.
    """
    blocking = [box for box in boxes if _segment_crosses_box(a, b, box)]
    if not blocking:
        return math.dist(a, b)
    box = (
        min(one[0] for one in blocking),
        min(one[1] for one in blocking),
        max(one[2] for one in blocking),
        max(one[3] for one in blocking),
    )
    x0, y0, x1, y1 = box
    start, goal = _onto(a, box), _onto(b, box)
    nodes = [start, goal, (x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    best = [math.inf] * len(nodes)
    best[0] = 0.0
    done = [False] * len(nodes)
    for _ in nodes:
        current = min(
            (index for index in range(len(nodes)) if not done[index]),
            key=lambda index: best[index],
            default=None,
        )
        if current is None or best[current] == math.inf:
            break
        done[current] = True
        for other in range(len(nodes)):
            if done[other] or _segment_crosses_box(nodes[current], nodes[other], box):
                continue
            step = best[current] + math.dist(nodes[current], nodes[other])
            best[other] = min(best[other], step)
    if best[1] == math.inf:
        return math.dist(a, b)
    return math.dist(a, start) + best[1] + math.dist(goal, b)


def _onto(point, box):
    """The point itself, or the nearest point on the box's edge when inside."""
    x, y = point
    x0, y0, x1, y1 = box
    if not (x0 < x < x1 and y0 < y < y1):
        return point
    return min(
        ((x0, y), (x1, y), (x, y0), (x, y1)),
        key=lambda candidate: math.dist(point, candidate),
    )


def _segment_crosses_box(a, b, box) -> bool:
    """Whether the open segment passes through the box's interior.

    An endpoint sitting on the box is not a crossing - every pad of a part is
    inside that part's own courtyard, and a run that starts there has not
    crossed anything yet.
    """
    x0, y0, x1, y1 = box
    steps = max(2, int(math.dist(a, b) / 0.2))
    for index in range(1, steps):
        t = index / steps
        px = a[0] + (b[0] - a[0]) * t
        py = a[1] + (b[1] - a[1]) * t
        if x0 < px < x1 and y0 < py < y1:
            return True
    return False


def _node(x: float, y: float) -> tuple[int, int]:
    """A copper endpoint as an integer key, so two segments that meet, meet."""
    return (round(x * 1000), round(y * 1000))


def _point(node: tuple[int, int]) -> tuple[float, float]:
    return (node[0] / 1000, node[1] / 1000)


def _inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Ray casting; edges count as inside, which errs toward covered."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=False):
        if (y1 > y) != (y2 > y):
            cross = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x <= cross:
                inside = not inside
    return inside


@rule
def rule_return_path(ctx: PcbContext) -> list[Finding]:
    """A signal running over a hole in its own return plane.

    Current comes back under the trace when it can. Where the opposite layer's
    pour has been cut - by another track's clearance channel, mostly - the
    return has to go round the cut, the loop area grows by the detour, and both
    emission and coupling grow with it. Judged only on two-layer boards with a
    filled ground pour, where "the other layer" is well defined; the gaps are
    the difference between the pour's outline and its computed fill.
    """
    copper = ctx.board.copper_layers or []
    if len(copper) != 2:
        return []
    limit = ctx.thresholds["return_path_mm"]
    step = 1.0
    plane: dict[str, list[tuple[tuple[float, ...], list[tuple[float, float]], bool]]] = {}
    for zone in ctx.board.zones:
        if zone.keepout or ctx.net_class_of(zone.net) != "ground" or not zone.fills:
            continue
        for layer in zone.layers:
            entries = plane.setdefault(layer, [])
            if zone.outline:
                xs = [x for x, _ in zone.outline]
                ys = [y for _, y in zone.outline]
                entries.append(((min(xs), min(ys), max(xs), max(ys)), zone.outline, True))
            for fill_layer, points in zone.fills:
                if fill_layer != layer or not points:
                    continue
                xs = [x for x, _ in points]
                ys = [y for _, y in points]
                entries.append(((min(xs), min(ys), max(xs), max(ys)), points, False))
    if not plane:
        return []

    def covered(point: tuple[float, float], layer: str) -> bool | None:
        """True over fill, False over a cut, None outside the pour entirely."""
        in_outline = False
        for bbox, polygon, is_outline in plane.get(layer, ()):
            if not (bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]):
                continue
            if not _inside(point, polygon):
                continue
            if is_outline:
                in_outline = True
            else:
                return True
        return False if in_outline else None

    other = {copper[0]: copper[1], copper[1]: copper[0]}
    exposed: dict[str, float] = defaultdict(float)
    for track in ctx.board.tracks:
        if not track.net or ctx.net_class_of(track.net) != "signal":
            continue
        if track.layer not in other or other[track.layer] not in plane:
            continue
        length = track.length
        samples = max(2, int(length / step) + 1)
        for index in range(samples):
            t = (index + 0.5) / samples
            point = (
                track.start[0] + (track.end[0] - track.start[0]) * t,
                track.start[1] + (track.end[1] - track.start[1]) * t,
            )
            if covered(point, other[track.layer]) is False:
                exposed[track.net] += length / samples
    offenders = [
        f"{net}: {mm:.1f} mm over cuts in the plane"
        for net, mm in sorted(exposed.items(), key=lambda kv: -kv[1])
        if mm > limit
    ]
    if not offenders:
        return []
    return [
        _group_finding(
            "route.return_path",
            "warning",
            f"{len(offenders)} signal net(s) run more than {limit} mm over "
            "gaps in the other layer's ground fill - the return current goes "
            "round the gap and the loop grows by the detour",
            offenders,
        )
    ]
