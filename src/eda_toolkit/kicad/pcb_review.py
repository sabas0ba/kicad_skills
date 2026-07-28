"""PCB artwork review: DRC plus layout-practice heuristics."""

from __future__ import annotations

import math
import os
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from ..util import COLLAPSE_LIMIT, Finding, collapse_findings, sort_findings, summarize
from . import kicad_cli, pcb
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
        if narrow:
            by_net = Counter(t.net for t in narrow)
            findings.append(
                Finding(
                    "track.thin_power",
                    "warning",
                    "power/ground tracks narrower than 0.4 mm - check the current rating",
                    details={"nets": dict(by_net.most_common(10))},
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
    """Copper too close to the board edge (bounding-box approximation)."""
    bbox = ctx.board.outline_bbox()
    if not bbox:
        return []
    min_x, min_y, max_x, max_y = bbox
    clearance = ctx.thresholds["min_edge_clearance_mm"]
    offenders: list[str] = []
    for track in ctx.board.tracks:
        for x, y in (track.start, track.end):
            margin = min(x - min_x, max_x - x, y - min_y, max_y - y) - track.width / 2
            if margin < clearance:
                offenders.append(f"{track.net or 'track'}@({round(x, 2)},{round(y, 2)})")
                break
    for fp in ctx.board.footprints:
        for pad in fp.pads:
            margin = (
                min(pad.x - min_x, max_x - pad.x, pad.y - min_y, max_y - pad.y) - max(pad.size) / 2
            )
            if margin < clearance:
                offenders.append(f"{fp.ref}.{pad.number}")
    if offenders:
        return [
            Finding(
                "board.edge_clearance",
                "warning",
                f"{len(offenders)} copper item(s) within {clearance} mm of the board "
                "outline bounding box",
                details={"items": offenders[:20]},
            )
        ]
    return []


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
def rule_placement(ctx: PcbContext) -> list[Finding]:
    """Footprints outside the board outline, and mixed-side assembly cost."""
    findings = []
    bbox = ctx.board.outline_bbox()
    if bbox:
        min_x, min_y, max_x, max_y = bbox
        outside = [
            fp.ref
            for fp in ctx.board.footprints
            if not (min_x <= fp.x <= max_x and min_y <= fp.y <= max_y)
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
