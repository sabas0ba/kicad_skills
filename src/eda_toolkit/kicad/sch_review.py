"""Schematic review: ERC plus design-practice heuristics."""

from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..util import (
    COLLAPSE_LIMIT,
    Finding,
    RuleSpec,
    collapse_findings,
    sort_findings,
    summarize,
)
from . import kicad_cli, schematic
from . import netlist as netlist_mod

RULES: list[Callable[[ReviewContext], list[Finding]]] = []

# Drawing conventions a human draughtsman keeps without thinking and a generator
# has to be told. All in mm; override per project with --threshold key=value.
THRESHOLDS: dict[str, float] = {
    # KiCad's default schematic grid is 50 mil. Anything placed off it is the
    # single most common way a generated sheet looks connected and is not.
    "grid_mm": 1.27,
    # How much body to assume around a symbol's pin extent when looking for
    # overlapping parts (the instance does not carry the library outline).
    "symbol_margin_mm": 1.27,
    "max_symbols_per_sheet": 60,
    # Fraction of multi-pin nets that must carry a name a human chose.
    "min_named_net_ratio": 0.4,
    # A capacitor wants this much headroom over the rail it sits on.
    "capacitor_derating_factor": 1.5,
    # Above this fraction of label-stub connections, the sheet is a name table
    # rather than a drawing. KiCad's own demo projects sit well under it.
    "max_label_ratio": 0.5,
    # A symbol needs this many pins in one straight row before which way the
    # row faces is worth judging - two-pin parts face wherever they like.
    "min_row_pins": 4,
    # The strip inside the page edge that belongs to the drawing frame and its
    # rulers; anything placed there prints on top of them.
    "page_margin_mm": 10.0,
}

# Geometry lives on a 1/100 mm world; anything below this is file noise.
GEOM_TOL = 0.01

# Every rule id here is built from a literal prefix, so none has to be declared
# the way pcb_review's DRC buckets do.
DYNAMIC_RULE_IDS: tuple[str, ...] = ()

# Every finding this module can produce, and the condition that produces it.
# This is the checkable half of the documentation: tests/test_rule_spec.py fails
# if a rule emits an id that is missing here, or if an id here is emitted by no
# rule. `eda gate --list-rules` prints it.
RULE_SPEC: dict[str, RuleSpec] = {
    # -- KiCad's own ERC ---------------------------------------------------
    "erc.*": RuleSpec(
        "one entry per violation KiCad's own ERC reports, keeping its type and severity",
        "as KiCad graded it",
        dynamic=True,
    ),
    "erc.unavailable": RuleSpec("kicad-cli was not available, so ERC did not run", "info"),
    # -- annotation and fields --------------------------------------------
    "schematic.duplicate_reference": RuleSpec(
        "two symbols share a reference and are not units of one part", "error"
    ),
    "schematic.unannotated": RuleSpec("a symbol whose reference is empty or ends in '?'", "error"),
    "schematic.missing_value": RuleSpec("a part with an empty Value field", "warning"),
    "schematic.missing_footprint": RuleSpec(
        "a part that is on the board, is not DNP, and has no Footprint", "warning"
    ),
    "schematic.missing_datasheet": RuleSpec(
        "a U/IC/Q/D/Y/L part whose Datasheet field is empty or '~'", "info"
    ),
    "schematic.dnp": RuleSpec(
        "a part marked do-not-populate, listed so it is not forgotten", "info"
    ),
    # -- connectivity ------------------------------------------------------
    "net.single_pin": RuleSpec(
        "a net that reaches exactly one pin: 'warning' when KiCad named it "
        "(unconnected-(U1-Pad3)), 'info' when a human did. A pin carrying a "
        "no-connect flag on the sheet is a stated decision and is not reported",
        "warning / info",
    ),
    "net.no_driver": RuleSpec(
        "a multi-pin net whose pins are all of electrical type 'input'", "warning"
    ),
    "power.no_ground": RuleSpec("no net classifies as ground (GND/VSS/AGND...)", "error"),
    "power.no_supply": RuleSpec("no net classifies as a supply (+3V3/VCC/VDD...)", "warning"),
    "power.many_supplies": RuleSpec("more than six distinct supply nets", "info"),
    "power.unused_rail": RuleSpec(
        "a power or ground net that reaches no component pin at all", "warning"
    ),
    # -- circuit practice --------------------------------------------------
    "analog.missing_decoupling": RuleSpec(
        "an IC supply net with no capacitor that also touches ground. When the "
        "netlist carries pin electrical types, only power_in pins ask for one, "
        "and a net an output pin drives is never asked - a capacitor on an "
        "op-amp output is a stability problem, not decoupling",
        "warning",
    ),
    "analog.no_dc_path": RuleSpec(
        "a net every pin of which belongs to a capacitor or a connector, so "
        "nothing sets its DC level - an AC-coupled output with no bleed "
        "resistor is the usual case",
        "warning",
    ),
    "analog.i2c_pullup": RuleSpec(
        "a net named SDA/SCL (optionally I2Cn_ prefixed) with no resistor on it", "warning"
    ),
    "analog.led_no_series_resistor": RuleSpec(
        "an LED with no resistor on the nets either terminal reaches", "warning"
    ),
    # -- drawing readability ----------------------------------------------
    "readability.off_grid_pin": RuleSpec(
        "a symbol pin whose x or y is not a multiple of the grid; KiCad connects "
        "on exact coordinates, so a wire meeting it only appears to",
        "warning",
        threshold="grid_mm",
    ),
    "readability.off_grid_wire": RuleSpec(
        "a wire vertex off the grid, so it cannot meet an on-grid pin",
        "warning",
        threshold="grid_mm",
    ),
    "readability.off_grid_junction": RuleSpec(
        "a junction dot off the grid, joining nothing where it stands",
        "warning",
        threshold="grid_mm",
    ),
    "readability.off_grid_label": RuleSpec(
        "a label off the grid, so it names no wire", "info", threshold="grid_mm"
    ),
    "readability.diagonal_wire": RuleSpec(
        "a wire segment whose ends differ in both x and y", "info"
    ),
    "readability.missing_junction": RuleSpec(
        "a wire end that lies in the interior of another wire with no junction "
        "dot there; KiCad treats that as crossing, not connected",
        "warning",
    ),
    "readability.dangling_wire": RuleSpec(
        "a wire end that coincides with no pin, label, junction, no-connect, "
        "sheet pin, bus entry or other wire",
        "warning",
    ),
    "readability.overlapping_symbols": RuleSpec(
        "two symbols on one sheet whose pin extents, grown by the margin, overlap in both axes",
        "warning",
        threshold="symbol_margin_mm",
    ),
    "readability.outside_page": RuleSpec(
        "a symbol, label or wire vertex outside the page rectangle, so it is "
        "absent from the plotted sheet and the PDF",
        "warning",
    ),
    "readability.sheet_density": RuleSpec(
        "one sheet carrying more non-power symbols than the limit",
        "info",
        threshold="max_symbols_per_sheet",
    ),
    "readability.unnamed_nets": RuleSpec(
        "of ten or more multi-pin nets, the fraction carrying a human-chosen "
        "name is below the limit",
        "info",
        threshold="min_named_net_ratio",
    ),
    "readability.label_only": RuleSpec(
        "more than max_label_ratio of the sheet's signal connections are a "
        "stub ending in a net label rather than a drawn wire between pins. A "
        "valid netlist, and a drawing that reads as a name table - the most "
        "recognisable mark of a generated sheet. Power-symbol hookups are "
        "exempt: that is what power symbols are for",
        "warning",
        threshold="max_label_ratio",
    ),
    "readability.title_block": RuleSpec(
        "the root sheet's title block is missing a title, rev, date or company", "info"
    ),
    "readability.power_symbol_orientation": RuleSpec(
        "a rotated power symbol: rails point up and grounds hang down on every "
        "sheet a reader has ever seen, and a sideways rail name lands on the "
        "next pin's label. Info: humans rotate them freely (twelve of the "
        "eighteen demo projects do), and the ai-generated policy promotes it "
        "regardless",
        "info",
    ),
    "readability.wire_through_junction": RuleSpec(
        "a junction dot in the interior of a wire segment instead of at a "
        "break between two. KiCad's editor always splits the wire when a "
        "branch tees in; KiCad 9's connectivity requires it, and connects "
        "only one side of an unbroken wire",
        "warning",
    ),
    "readability.overlapping_wires": RuleSpec(
        "two collinear wire segments sharing more than a point of the same "
        "line - one wire drawn over another reads as one and edits as two",
        "warning",
    ),
    "readability.facing_away": RuleSpec(
        "a single-row symbol (a connector, usually) most of whose connected "
        "pins point away from the pins they connect to, so every wire must "
        "double back around the body; mirroring the symbol is the fix. Info, "
        "not warning: humans park edge connectors facing outward on purpose, "
        "and the ai-generated policy promotes it regardless",
        "info",
        threshold="min_row_pins",
    ),
    "readability.margin_intrusion": RuleSpec(
        "a pin or a text note inside the page's frame strip or on the title "
        "block, where it prints over the sheet furniture. Info, not warning: "
        "mounting holes and logos live there on purpose on human sheets, and "
        "the ai-generated policy promotes it regardless",
        "info",
        threshold="page_margin_mm",
    ),
    "readability.text_over_wire": RuleSpec(
        "a symbol's reference, value or rating printed across a wire - the "
        "netlist is unaffected and the plot is unreadable",
        "warning",
    ),
    "readability.text_over_symbol": RuleSpec(
        "a text note whose estimated extent overlaps a symbol's pin box - "
        "notes belong beside the circuit, not on it (the extent is estimated "
        "from the string, so this is graded info, not error)",
        "info",
    ),
    # -- specification -----------------------------------------------------
    "spec.missing_rating": RuleSpec(
        "a non-DNP R without tolerance/power, C without voltage/tolerance, or L "
        "without a current rating, read from the symbol's fields",
        "info",
    ),
    "spec.missing_part_number": RuleSpec(
        "a non-DNP U/IC/Q/D/Y/K/J/SW part with no MPN, manufacturer or distributor field",
        "info",
    ),
    "spec.voltage_derating": RuleSpec(
        "a capacitor whose stated voltage rating is below the highest rail its "
        "nets name ('error'), or below that rail times the derating factor "
        "('warning'). Rails that state no voltage are not judged",
        "error / warning",
        threshold="capacitor_derating_factor",
    ),
    "spec.no_design_notes": RuleSpec(
        "no sheet carries a text note and no part carries a description, so the "
        "reasoning behind the values is recorded nowhere",
        "info",
    ),
    "internal.*": RuleSpec(
        "a rule raised an exception; reported instead of failing the review", "info", dynamic=True
    ),
}


def rule(func: Callable[[ReviewContext], list[Finding]]):
    RULES.append(func)
    return func


class ReviewContext:
    """Everything the rules need: parsed sheets, netlist and derived indexes."""

    def __init__(
        self,
        target: str | os.PathLike[str],
        *,
        use_cli: bool = True,
        thresholds: dict[str, float] | None = None,
    ) -> None:
        self.root_sch: Path = schematic.find_root_schematic(target)
        self.thresholds = {**THRESHOLDS, **(thresholds or {})}
        self.docs = schematic.parse_project(self.root_sch)
        self.netlist = netlist_mod.get(self.root_sch, prefer_cli=use_cli)
        self.symbols = [s for doc in self.docs for s in doc.symbols]
        self.parts = [s for s in self.symbols if not s.is_power]
        self.nets = self.netlist.get("nets", [])
        self.erc: dict[str, Any] | None = None
        if use_cli and kicad_cli.available():
            try:
                self.erc = kicad_cli.erc(self.root_sch)
            except Exception as exc:  # pragma: no cover - depends on kicad build
                self.erc = {"error": str(exc)}

        self.pins_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.net_by_ref_pin: dict[tuple[str, str], str] = {}
        for net in self.nets:
            for node in net["nodes"]:
                self.pins_by_ref[node["ref"]].append({**node, "net": net["name"]})
                self.net_by_ref_pin[(node["ref"], node["pin"])] = net["name"]

    @classmethod
    def from_netlist(
        cls,
        netlist: dict[str, Any],
        *,
        symbols: list[schematic.Symbol] | None = None,
        erc: dict[str, Any] | None = None,
        docs: list[schematic.SchematicDoc] | None = None,
        thresholds: dict[str, float] | None = None,
    ) -> ReviewContext:
        """Build a context from an in-memory netlist.

        Useful for reviewing a netlist that was produced elsewhere, and for
        exercising the rules in the test-suite without touching the filesystem.
        """
        ctx = cls.__new__(cls)
        ctx.root_sch = Path(netlist.get("schematic", "<memory>"))
        ctx.thresholds = {**THRESHOLDS, **(thresholds or {})}
        ctx.docs = docs or []
        ctx.netlist = netlist
        ctx.symbols = symbols or []
        ctx.parts = [s for s in ctx.symbols if not s.is_power]
        ctx.nets = netlist.get("nets", [])
        ctx.erc = erc
        ctx.pins_by_ref = defaultdict(list)
        ctx.net_by_ref_pin = {}
        for net in ctx.nets:
            for node in net["nodes"]:
                ctx.pins_by_ref[node["ref"]].append({**node, "net": net["name"]})
                ctx.net_by_ref_pin[(node["ref"], node["pin"])] = net["name"]
        return ctx

    # -- helpers -----------------------------------------------------------
    def refs_on_net(self, net_name: str) -> set[str]:
        for net in self.nets:
            if net["name"] == net_name:
                return {n["ref"] for n in net["nodes"]}
        return set()

    def prefix(self, ref: str) -> str:
        return re.sub(r"[\d?]+$", "", ref)

    def is_capacitor(self, ref: str) -> bool:
        return self.prefix(ref) == "C"

    def is_resistor(self, ref: str) -> bool:
        return self.prefix(ref) in ("R", "RN")

    def ic_refs(self) -> list[str]:
        out = []
        for ref, pins in self.pins_by_ref.items():
            if self.prefix(ref) in ("U", "IC") or len(pins) >= 6:
                out.append(ref)
        return sorted(out)


@rule
def rule_erc(ctx: ReviewContext) -> list[Finding]:
    """Surface kicad-cli ERC violations as findings."""
    if not ctx.erc or "error" in ctx.erc:
        if ctx.erc and "error" in ctx.erc:
            return [Finding("erc.unavailable", "info", f"ERC could not be run: {ctx.erc['error']}")]
        return [
            Finding(
                "erc.unavailable",
                "info",
                "kicad-cli is not available, ERC was skipped "
                "(run inside the container for full coverage)",
            )
        ]
    findings: list[Finding] = []
    for sheet in ctx.erc.get("sheets", []):
        sheet_path = sheet.get("path", "/")
        for violation in sheet.get("violations", []):
            sev = {"error": "error", "warning": "warning"}.get(
                str(violation.get("severity", "warning")).lower(), "info"
            )
            items = violation.get("items", [])
            where = "; ".join(
                f"{it.get('description', '')}".strip() for it in items if it.get("description")
            )
            findings.append(
                Finding(
                    rule=f"erc.{violation.get('type', 'violation')}",
                    severity=sev,
                    message=violation.get("description", "ERC violation"),
                    location=f"{sheet_path} {where}".strip(),
                    details={"items": items},
                )
            )
    return findings


@rule
def rule_annotation(ctx: ReviewContext) -> list[Finding]:
    """References must be unique and annotated."""
    findings = []
    counts = Counter(s.reference for s in ctx.parts if s.reference)
    for ref, count in counts.items():
        if count > 1 and not _is_multi_unit(ctx, ref):
            findings.append(
                Finding(
                    "schematic.duplicate_reference",
                    "error",
                    f"reference {ref} is used by {count} symbols",
                    location=ref,
                )
            )
    for sym in ctx.parts:
        if sym.unannotated:
            findings.append(
                Finding(
                    "schematic.unannotated",
                    "error",
                    f"symbol {sym.lib_id} at ({sym.x}, {sym.y}) is not annotated "
                    f"(reference {sym.reference!r})",
                    location=f"{sym.sheet}:{sym.reference or sym.lib_id}",
                )
            )
    return findings


def _is_multi_unit(ctx: ReviewContext, ref: str) -> bool:
    units = {s.unit for s in ctx.parts if s.reference == ref}
    return len(units) > 1


@rule
def rule_fields(ctx: ReviewContext) -> list[Finding]:
    """Value / footprint / datasheet completeness."""
    findings = []
    for sym in ctx.parts:
        where = f"{sym.sheet}:{sym.reference}"
        if not sym.value.strip():
            findings.append(
                Finding(
                    "schematic.missing_value",
                    "warning",
                    f"{sym.reference} has no Value",
                    location=where,
                )
            )
        if not sym.footprint.strip() and sym.on_board and not sym.dnp:
            findings.append(
                Finding(
                    "schematic.missing_footprint",
                    "warning",
                    f"{sym.reference} ({sym.value}) has no footprint assigned",
                    location=where,
                )
            )
        needs_datasheet = ctx.prefix(sym.reference) in ("U", "IC", "Q", "D", "Y", "L")
        if needs_datasheet and sym.datasheet.strip() in ("", "~"):
            findings.append(
                Finding(
                    "schematic.missing_datasheet",
                    "info",
                    f"{sym.reference} ({sym.value}) has no datasheet link",
                    location=where,
                )
            )
        if sym.dnp:
            findings.append(
                Finding(
                    "schematic.dnp",
                    "info",
                    f"{sym.reference} ({sym.value}) is marked DNP",
                    location=where,
                )
            )
    return findings


AUTO_NET_NAME = re.compile(r"^(/.*)?(unconnected-|Net-\(|N\$\d)", re.IGNORECASE)


@rule
def rule_single_pin_nets(ctx: ReviewContext) -> list[Finding]:
    """A net that reaches exactly one pin.

    An auto-named one (``unconnected-(U1-Pad3)``) is a dangling wire and worth a
    warning. A net the designer named is usually a deliberate stub - a spare
    module pin brought out to a label - so it is reported as context, not as a
    defect. Running this over KiCad's demo projects, the labelled kind
    outnumbered the real ones by more than 20:1.
    """
    findings = []
    flagged = {(round(x, 2), round(y, 2)) for doc in ctx.docs for x, y in doc.no_connects}
    pin_at: dict[tuple[str, str], tuple[float, float]] = {}
    if flagged:
        for doc in ctx.docs:
            for sym in doc.symbols:
                for pin in sym.pins:
                    pin_at[(sym.reference, pin.number)] = (round(pin.x, 2), round(pin.y, 2))
    for net in ctx.nets:
        if net["pin_count"] != 1:
            continue
        node = net["nodes"][0]
        auto_named = bool(AUTO_NET_NAME.match(net["name"]))
        if auto_named and pin_at.get((node["ref"], node["pin"])) in flagged:
            # the sheet says so with a no-connect flag: a decision, not a defect
            continue
        findings.append(
            Finding(
                "net.single_pin",
                "warning" if auto_named else "info",
                f"net {net['name']} only connects {node['ref']}.{node['pin']}"
                + ("" if auto_named else " (named net, so probably a deliberate stub)"),
                location=net["name"],
                details={"node": node, "auto_named": auto_named},
            )
        )
    return findings


@rule
def rule_floating_inputs(ctx: ReviewContext) -> list[Finding]:
    """Input pins that see no driver."""
    findings = []
    driver_types = {
        "output",
        "power_out",
        "bidirectional",
        "tri_state",
        "open_collector",
        "open_emitter",
        "passive",
        "unspecified",
        "free",
    }
    for net in ctx.nets:
        types = [n.get("type", "") for n in net["nodes"]]
        if not any(t in driver_types for t in types):
            inputs = [n for n in net["nodes"] if n.get("type") == "input"]
            if inputs and len(net["nodes"]) > 1:
                pin_list = ", ".join("{}.{}".format(n["ref"], n["pin"]) for n in inputs)
                findings.append(
                    Finding(
                        "net.no_driver",
                        "warning",
                        f"net {net['name']} only has input pins ({pin_list})",
                        location=net["name"],
                    )
                )
    return findings


@rule
def rule_decoupling(ctx: ReviewContext) -> list[Finding]:
    """Every IC supply pin should have a local decoupling capacitor.

    Judged from evidence before names. A net is a supply *for this IC* when the
    IC reaches it through a ``power_in`` pin; a name that merely looks like a
    rail (VREF, VBUS on a carrier) is not enough once the netlist carries pin
    types. And a net that some ``output`` pin drives is never asked to add a
    capacitor: a reference made by an op-amp is decoupled at one's peril - the
    capacitor lands inside somebody's control loop.
    """
    findings = []
    driven_by_output = {
        net["name"]
        for net in ctx.nets
        if any(node.get("type") == "output" for node in net["nodes"])
    }
    for ref in ctx.ic_refs():
        supply_nets = set()
        for pin in ctx.pins_by_ref[ref]:
            net_name = pin["net"]
            kind = netlist_mod.classify_net(net_name)
            if kind == "ground" or net_name in driven_by_output:
                continue
            ptype = pin.get("type") or ""
            is_supply = ptype == "power_in" if ptype else kind == "power"
            if is_supply:
                supply_nets.add(net_name)
        for net_name in sorted(supply_nets):
            caps = [r for r in ctx.refs_on_net(net_name) if ctx.is_capacitor(r)]
            decouplers = []
            for cap in caps:
                cap_nets = {p["net"] for p in ctx.pins_by_ref[cap]}
                if any(netlist_mod.classify_net(n) == "ground" for n in cap_nets):
                    decouplers.append(cap)
            if not decouplers:
                findings.append(
                    Finding(
                        "analog.missing_decoupling",
                        "warning",
                        f"supply net {net_name} of {ref} has no capacitor to ground",
                        location=f"{ref} / {net_name}",
                    )
                )
    return findings


@rule
def rule_no_dc_path(ctx: ReviewContext) -> list[Finding]:
    """A node that nothing biases.

    A net whose every pin belongs to a capacitor or a connector has no DC path
    to anywhere: whatever charge it starts with is what it keeps. The usual way
    to build one is an AC-coupled output taken straight to a connector - it
    works on the bench, drifts with leakage, and pops on connection. A bleed
    resistor is the one-part answer, and its absence is invisible to ERC
    because the connectivity is perfectly valid.
    """
    findings = []
    for net in ctx.nets:
        nodes = net["nodes"]
        if len(nodes) < 2:
            continue
        if netlist_mod.classify_net(net["name"]) != "signal":
            continue
        prefixes = {ctx.prefix(node["ref"]) for node in nodes}
        if not prefixes <= {"C", "J", "P"} or "C" not in prefixes:
            continue
        findings.append(
            Finding(
                "analog.no_dc_path",
                "warning",
                f"every pin on {net['name']} is a capacitor or connector - "
                "nothing sets its DC level, so it floats until something "
                "leaks; a bleed resistor to a rail fixes it",
                location=net["name"],
            )
        )
    return findings


def _wire_components(
    docs,
) -> tuple[dict[tuple[float, float], tuple[float, float]], _SegmentIndex]:
    """Union-find over the drawn wires: which points are one piece of copper."""
    segments = [seg for doc in docs for seg in _wire_segments(doc)]
    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def key(point: tuple[float, float]) -> tuple[float, float]:
        return (round(point[0], 2), round(point[1], 2))

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    for a, b in segments:
        union(key(a), key(b))
    index = _SegmentIndex(segments)
    # an endpoint resting on the middle of another wire joins it
    for a, b in segments:
        for end in (key(a), key(b)):
            cell = (index._cell(end[0]), index._cell(end[1]))
            for other in index.cells.get(cell, ()):
                seg = index.segments[other]
                if _on_interior(end, *seg):
                    union(end, key(seg[0]))
    # resolve every key fully before handing the map out
    return {k: find(k) for k in list(parent)}, index


@rule
def rule_label_only(ctx: ReviewContext) -> list[Finding]:
    """A sheet that connects by name rather than by wire.

    Every connection here is a pin, a stub, and a net label; the reader is left
    to grep. The netlist is exactly as valid as a drawn one, which is why no
    electrical check minds, and it is the single most recognisable mark of a
    generated schematic. Counted over the wire graph: a wire piece joining two
    or more component pins is a drawn connection, a piece with one pin and a
    label is a label stub. Power-symbol hookups are exempt - a rail *should* be
    a symbol, not a wire across the page.
    """
    if not ctx.docs:
        return []
    limit = ctx.thresholds["max_label_ratio"]
    roots, index = _wire_components(ctx.docs)

    def root_of(point: tuple[float, float]):
        k = (round(point[0], 2), round(point[1], 2))
        if k in roots:
            return roots[k]
        cell = (index._cell(k[0]), index._cell(k[1]))
        for other in index.cells.get(cell, ()):
            seg = index.segments[other]
            if _on_interior(k, *seg):
                return roots.get((round(seg[0][0], 2), round(seg[0][1], 2)))
        return None

    from collections import Counter as _Counter

    part_pins: _Counter = _Counter()
    power_pins: set = set()
    labelled: set = set()
    for doc in ctx.docs:
        for sym in doc.symbols:
            for pin in sym.pins:
                root = root_of((pin.x, pin.y))
                if root is None:
                    continue
                if sym.is_power:
                    power_pins.add(root)
                else:
                    part_pins[root] += 1
        for label in doc.labels:
            root = root_of((label.x, label.y))
            if root is not None:
                labelled.add(root)

    drawn = sum(1 for root, count in part_pins.items() if count >= 2)
    stubs = sum(
        1
        for root, count in part_pins.items()
        if count == 1 and root in labelled and root not in power_pins
    )
    total = drawn + stubs
    # A five-part sheet with three named nets is idiomatic, not machine-drawn;
    # the pattern this rule is after only means anything at scale.
    if total < 10:
        return []
    ratio = stubs / total
    if ratio <= limit:
        return []
    return [
        Finding(
            "readability.label_only",
            "warning",
            f"{stubs} of {total} signal connections are a stub ending in a "
            f"label ({ratio:.0%}, limit {limit:.0%}) - the circuit reads as a "
            "name table, not a drawing",
            details={"stubs": stubs, "drawn": drawn, "ratio": round(ratio, 3)},
        )
    ]


@rule
def rule_power_nets(ctx: ReviewContext) -> list[Finding]:
    """Summarise supplies and flag suspicious naming."""
    findings = []
    power_nets = {n["name"] for n in ctx.nets if netlist_mod.classify_net(n["name"]) == "power"}
    ground_nets = {n["name"] for n in ctx.nets if netlist_mod.classify_net(n["name"]) == "ground"}
    if not ground_nets:
        findings.append(
            Finding("power.no_ground", "error", "no ground net found (expected GND/VSS/AGND...)")
        )
    if not power_nets:
        findings.append(
            Finding("power.no_supply", "warning", "no power net found (expected +3V3/VCC/VDD...)")
        )
    if len(power_nets) > 6:
        findings.append(
            Finding(
                "power.many_supplies",
                "info",
                f"{len(power_nets)} distinct supply nets: {', '.join(sorted(power_nets))}",
            )
        )
    return findings


@rule
def rule_i2c_pullups(ctx: ReviewContext) -> list[Finding]:
    """I2C-looking nets need pull-up resistors."""
    findings = []
    for net in ctx.nets:
        base = net["name"].split("/")[-1].upper()
        looks_like_i2c = re.fullmatch(r"(I2C\d?_)?(SDA|SCL)(\d)?", base)
        if looks_like_i2c and not any(ctx.is_resistor(r) for r in ctx.refs_on_net(net["name"])):
            findings.append(
                Finding(
                    "analog.i2c_pullup",
                    "warning",
                    f"{net['name']} looks like an I2C line but has no resistor on it",
                    location=net["name"],
                )
            )
    return findings


@rule
def rule_led_series_resistor(ctx: ReviewContext) -> list[Finding]:
    """LEDs driven straight from a rail without current limiting."""
    findings = []
    for sym in ctx.parts:
        if ctx.prefix(sym.reference) != "D" and "led" not in sym.lib_id.lower():
            continue
        if "led" not in sym.lib_id.lower() and "led" not in sym.value.lower():
            continue
        neighbours: set[str] = set()
        for pin in ctx.pins_by_ref.get(sym.reference, []):
            neighbours |= ctx.refs_on_net(pin["net"])
        if not any(ctx.is_resistor(r) for r in neighbours):
            findings.append(
                Finding(
                    "analog.led_no_series_resistor",
                    "warning",
                    f"{sym.reference} ({sym.value}) has no series resistor on either terminal",
                    location=sym.reference,
                )
            )
    return findings


@rule
def rule_unconnected_power_symbols(ctx: ReviewContext) -> list[Finding]:
    """Power symbols that end up on a net with nothing else."""
    findings = []
    for net in ctx.nets:
        kind = netlist_mod.classify_net(net["name"])
        if kind in ("power", "ground") and net["pin_count"] == 0:
            findings.append(
                Finding(
                    "power.unused_rail",
                    "warning",
                    f"rail {net['name']} is declared but reaches no component pin",
                    location=net["name"],
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Readability: what an engineer judges in the first ten seconds of opening the
# sheet, and what a generator gets wrong most often. None of it changes the
# netlist - which is exactly why ERC stays silent about all of it.
# ---------------------------------------------------------------------------


def _grid_key(point: tuple[float, float]) -> tuple[int, int]:
    return (round(point[0] / GEOM_TOL), round(point[1] / GEOM_TOL))


def _is_off_grid(value: float, grid: float) -> bool:
    return abs(value / grid - round(value / grid)) * grid > GEOM_TOL


Segment = tuple[tuple[float, float], tuple[float, float]]


def _wire_segments(doc: schematic.SchematicDoc) -> list[Segment]:
    segments = []
    for wire in doc.wires:
        for a, b in zip(wire.points, wire.points[1:], strict=False):
            segments.append((a, b))
    return segments


class _SegmentIndex:
    """Which wire segments could possibly touch a given point.

    Both "does this end land on another wire" rules ask that question once per
    wire end, and answering it by scanning every segment is quadratic: a sheet
    with a few thousand segments took the better part of a minute. Bucketing the
    segments by the grid cells their bounding box covers makes each query
    proportional to what is actually nearby.
    """

    CELL = 12.7  # ten grid steps

    def __init__(self, segments: list[Segment]) -> None:
        self.segments = segments
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for index, (a, b) in enumerate(segments):
            for cx in range(self._cell(min(a[0], b[0])), self._cell(max(a[0], b[0])) + 1):
                for cy in range(self._cell(min(a[1], b[1])), self._cell(max(a[1], b[1])) + 1):
                    self.cells[(cx, cy)].append(index)

    @classmethod
    def _cell(cls, value: float) -> int:
        return math.floor(value / cls.CELL)

    def lands_on_another(self, point: tuple[float, float], skip: int) -> bool:
        """True when ``point`` sits in the interior of some segment other than ``skip``."""
        key = (self._cell(point[0]), self._cell(point[1]))
        return any(
            index != skip and _on_interior(point, *self.segments[index])
            for index in self.cells.get(key, ())
        )


def _on_interior(
    point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> bool:
    """True when ``point`` sits on segment a-b but is neither of its ends."""
    if math.dist(point, a) <= GEOM_TOL or math.dist(point, b) <= GEOM_TOL:
        return False
    if not (min(a[0], b[0]) - GEOM_TOL <= point[0] <= max(a[0], b[0]) + GEOM_TOL):
        return False
    if not (min(a[1], b[1]) - GEOM_TOL <= point[1] <= max(a[1], b[1]) + GEOM_TOL):
        return False
    cross = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])
    length = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
    return abs(cross) / length <= GEOM_TOL


def _group_finding(rule_name: str, severity: str, message: str, items: list[str]) -> Finding:
    """One finding for a whole category, carrying the count and some examples."""
    return Finding(
        rule_name,
        severity,
        message,
        details={"count": len(items), "examples": items[:8]},
    )


@rule
def rule_off_grid(ctx: ReviewContext) -> list[Finding]:
    """Geometry that does not land on the schematic grid.

    KiCad joins a wire to a pin only where their coordinates match exactly. A
    pin half a grid step off looks connected at any zoom level a human uses and
    is not - and because the net simply never forms, ERC reports a floating pin
    somewhere else entirely, which is a much harder trail to follow.
    """
    grid = ctx.thresholds["grid_mm"]
    if grid <= 0:
        return []
    off: dict[str, list[str]] = {"pin": [], "wire": [], "label": [], "junction": []}
    for doc in ctx.docs:
        for sym in doc.symbols:
            for pin in sym.pins:
                if _is_off_grid(pin.x, grid) or _is_off_grid(pin.y, grid):
                    off["pin"].append(
                        f"{doc.path.name}:{sym.reference or sym.lib_id}.{pin.number} "
                        f"at ({round(pin.x, 3)}, {round(pin.y, 3)})"
                    )
        for wire in doc.wires:
            for x, y in wire.points:
                if _is_off_grid(x, grid) or _is_off_grid(y, grid):
                    off["wire"].append(f"{doc.path.name}:({round(x, 3)}, {round(y, 3)})")
        for label in doc.labels:
            if _is_off_grid(label.x, grid) or _is_off_grid(label.y, grid):
                off["label"].append(f"{doc.path.name}:{label.text}")
        for x, y in doc.junctions:
            if _is_off_grid(x, grid) or _is_off_grid(y, grid):
                off["junction"].append(f"{doc.path.name}:({round(x, 3)}, {round(y, 3)})")

    consequences = {
        "pin": ("warning", "wires that meet them look connected and are not"),
        "wire": ("warning", "they cannot meet an on-grid pin"),
        "label": ("info", "the label attaches to nothing unless it sits on the wire"),
        "junction": ("warning", "the junction dot joins nothing where it stands"),
    }
    findings = []
    for kind, items in off.items():
        if not items:
            continue
        severity, why = consequences[kind]
        findings.append(
            _group_finding(
                f"readability.off_grid_{kind}",
                severity,
                f"{len(items)} {kind}(s) are off the {grid} mm grid - {why}",
                items,
            )
        )
    return findings


@rule
def rule_diagonal_wires(ctx: ReviewContext) -> list[Finding]:
    """Wires drawn at an angle.

    Orthogonal routing is the convention every schematic reader relies on to
    follow a signal across a sheet; a diagonal run reads as a mistake even when
    the connectivity is right.
    """
    diagonal = []
    for doc in ctx.docs:
        for a, b in _wire_segments(doc):
            if abs(a[0] - b[0]) > GEOM_TOL and abs(a[1] - b[1]) > GEOM_TOL:
                diagonal.append(
                    f"{doc.path.name}:({round(a[0], 2)}, {round(a[1], 2)}) -> "
                    f"({round(b[0], 2)}, {round(b[1], 2)})"
                )
    if not diagonal:
        return []
    return [
        _group_finding(
            "readability.diagonal_wire",
            "info",
            f"{len(diagonal)} wire segment(s) are drawn diagonally - "
            "schematics are read on the assumption that wires run orthogonally",
            diagonal,
        )
    ]


@rule
def rule_missing_junction(ctx: ReviewContext) -> list[Finding]:
    """A wire ending on another wire, with no junction dot.

    In KiCad a T-piece is only a connection when a junction is placed on it.
    Without one the two wires cross without touching, which draws exactly like
    the connection the author meant to make.
    """
    missing = []
    for doc in ctx.docs:
        segments = _wire_segments(doc)
        index = _SegmentIndex(segments)
        junctions = {_grid_key(j) for j in doc.junctions}
        for position, (start, end) in enumerate(segments):
            for point in (start, end):
                if _grid_key(point) in junctions:
                    continue
                if index.lands_on_another(point, position):
                    missing.append(f"{doc.path.name}:({round(point[0], 2)}, {round(point[1], 2)})")
    missing = sorted(set(missing))
    if not missing:
        return []
    return [
        _group_finding(
            "readability.missing_junction",
            "warning",
            f"{len(missing)} point(s) where a wire ends on another wire without a junction - "
            "KiCad treats those as crossing, not connected",
            missing,
        )
    ]


@rule
def rule_dangling_wire(ctx: ReviewContext) -> list[Finding]:
    """Wire ends that reach nothing at all."""
    dangling = []
    for doc in ctx.docs:
        segments = _wire_segments(doc)
        endpoint_count: Counter[tuple[int, int]] = Counter()
        for start, end in segments:
            endpoint_count[_grid_key(start)] += 1
            endpoint_count[_grid_key(end)] += 1

        anchors = {_grid_key(j) for j in doc.junctions}
        anchors |= {_grid_key(nc) for nc in doc.no_connects}
        anchors |= {_grid_key((label.x, label.y)) for label in doc.labels}
        anchors |= {_grid_key(pin.xy) for sym in doc.symbols for pin in sym.pins}
        anchors |= {_grid_key(pin) for sheet in doc.sheets for pin in sheet.pins}
        anchors |= {_grid_key(p) for entry in doc.bus_entries for p in entry}

        index = _SegmentIndex(segments)
        for position, (start, end) in enumerate(segments):
            for point in (start, end):
                key = _grid_key(point)
                if key in anchors or endpoint_count[key] > 1:
                    continue
                if index.lands_on_another(point, position):
                    continue
                dangling.append(f"{doc.path.name}:({round(point[0], 2)}, {round(point[1], 2)})")
    dangling = sorted(set(dangling))
    if not dangling:
        return []
    return [
        _group_finding(
            "readability.dangling_wire",
            "warning",
            f"{len(dangling)} wire end(s) reach no pin, label, junction or other wire",
            dangling,
        )
    ]


@rule
def rule_symbol_overlap(ctx: ReviewContext) -> list[Finding]:
    """Symbols drawn on top of each other.

    Placed by hand this practically never happens; placed by a generator that
    treats the sheet as a coordinate space it happens constantly, and the result
    is unreadable however correct the netlist is.
    """
    margin = ctx.thresholds["symbol_margin_mm"]
    overlaps = []
    for doc in ctx.docs:
        boxes = []
        for sym in doc.symbols:
            if sym.is_power:
                continue
            box = sym.bbox(margin=margin)
            if box:
                boxes.append((box, sym.reference or sym.lib_id))
        boxes.sort(key=lambda item: item[0][0])
        for i, (box_a, ref_a) in enumerate(boxes):
            for box_b, ref_b in boxes[i + 1 :]:
                if box_b[0] >= box_a[2] - GEOM_TOL:
                    break  # sorted by left edge: nothing further right can overlap
                if (
                    min(box_a[3], box_b[3]) - max(box_a[1], box_b[1]) > GEOM_TOL
                    and min(box_a[2], box_b[2]) - max(box_a[0], box_b[0]) > GEOM_TOL
                ):
                    overlaps.append(f"{doc.path.name}:{ref_a} / {ref_b}")
    if not overlaps:
        return []
    return [
        _group_finding(
            "readability.overlapping_symbols",
            "warning",
            f"{len(overlaps)} pair(s) of symbols overlap on the sheet",
            sorted(set(overlaps)),
        )
    ]


@rule
def rule_power_symbol_orientation(ctx: ReviewContext) -> list[Finding]:
    """Power symbols drawn sideways or upside down.

    A rail points up, a ground hangs down; that is the one orientation every
    reader assumes without looking. A generator that turns the symbol to
    follow the wire saves itself a bend and costs the reader the convention -
    the wire should bend instead.
    """
    turned = []
    for doc in ctx.docs:
        for sym in doc.symbols:
            if not sym.is_power or sym.is_power_flag:
                continue
            if abs(sym.angle % 360) > GEOM_TOL:
                turned.append(
                    f"{doc.path.name}:{sym.value or sym.lib_id} at "
                    f"({round(sym.x, 1)}, {round(sym.y, 1)}) turned {round(sym.angle)}"
                )
    turned = sorted(set(turned))
    if not turned:
        return []
    return [
        _group_finding(
            "readability.power_symbol_orientation",
            "info",
            f"{len(turned)} power symbol(s) are rotated - rails point up, "
            "grounds hang down; bend the wire, not the symbol",
            turned,
        )
    ]


@rule
def rule_wire_through_junction(ctx: ReviewContext) -> list[Finding]:
    """A wire drawn through a junction instead of broken at it.

    KiCad's editor splits a wire the moment a branch tees into it, so files it
    saves never contain one that runs through a junction. A generated file can,
    and the cost is version-dependent connectivity: KiCad 10 tolerates it,
    KiCad 9 attaches the branch to one side of the wire and silently drops
    everything past the dot. The netlist then disagrees between versions,
    which is about the worst failure a schematic file can have.
    """
    through = []
    for doc in ctx.docs:
        segments = _wire_segments(doc)
        index = _SegmentIndex(segments)
        for junction in doc.junctions:
            cell = (index._cell(junction[0]), index._cell(junction[1]))
            for position in index.cells.get(cell, ()):
                if _on_interior(junction, *index.segments[position]):
                    through.append(
                        f"{doc.path.name}:({round(junction[0], 2)}, {round(junction[1], 2)})"
                    )
                    break
    through = sorted(set(through))
    if not through:
        return []
    return [
        _group_finding(
            "readability.wire_through_junction",
            "warning",
            f"{len(through)} junction(s) sit in the middle of an unbroken wire - "
            "KiCad 9 connects only one side of it; break the wire at the dot",
            through,
        )
    ]


@rule
def rule_overlapping_wires(ctx: ReviewContext) -> list[Finding]:
    """Two wires drawn along the same stretch of line.

    They plot as one wire and edit as two: dragging one leaves the other
    behind, looking exactly like the connection that just moved. Nothing
    electrical minds, which is why only a drawing check can.
    """
    overlaps = []
    for doc in ctx.docs:
        by_line: dict[tuple[str, float], list[tuple[float, float]]] = defaultdict(list)
        for a, b in _wire_segments(doc):
            if abs(a[0] - b[0]) <= GEOM_TOL:
                by_line[("x", round(a[0], 2))].append(tuple(sorted((a[1], b[1]))))
            elif abs(a[1] - b[1]) <= GEOM_TOL:
                by_line[("y", round(a[1], 2))].append(tuple(sorted((a[0], b[0]))))
        for (axis, coordinate), spans in by_line.items():
            spans.sort()
            reach = spans[0][1]
            for lo, hi in spans[1:]:
                if lo < reach - GEOM_TOL:
                    overlaps.append(f"{doc.path.name}:{axis}={coordinate} near {round(lo, 2)}")
                reach = max(reach, hi)
    overlaps = sorted(set(overlaps))
    if not overlaps:
        return []
    return [
        _group_finding(
            "readability.overlapping_wires",
            "warning",
            f"{len(overlaps)} place(s) where two wire segments overlap along one "
            "line - drawn twice, plots as one, edits as two",
            overlaps,
        )
    ]


@rule
def rule_facing_away(ctx: ReviewContext) -> list[Finding]:
    """A pin row pointed away from everything it connects to.

    A connector drawn with its pins facing off the sheet while its signals go
    the other way forces every wire to lap the body - or, more usually, forces
    the sheet back to labels. The symbol wants mirroring, which costs nothing
    and was the difference between a wired breakout and a name table.
    """
    min_pins = int(ctx.thresholds["min_row_pins"])
    pin_at: dict[tuple[str, str], tuple[float, float]] = {}
    for doc in ctx.docs:
        for sym in doc.symbols:
            if not sym.is_power:
                for pin in sym.pins:
                    pin_at[(sym.reference, pin.number)] = pin.xy
    nodes_by_net: dict[str, list[dict[str, Any]]] = {net["name"]: net["nodes"] for net in ctx.nets}
    findings = []
    for doc in ctx.docs:
        for sym in doc.symbols:
            if sym.is_power or len(sym.pins) < min_pins:
                continue
            xs = {round(p.x, 2) for p in sym.pins}
            ys = {round(p.y, 2) for p in sym.pins}
            if len(xs) == 1 and len(ys) >= min_pins:
                axis, row = 0, next(iter(xs))
                origin = sym.x
            elif len(ys) == 1 and len(xs) >= min_pins:
                axis, row = 1, next(iter(ys))
                origin = sym.y
            else:
                continue  # pins on more than one line: not a single-row part
            facing = row - origin
            if abs(facing) < GEOM_TOL:
                continue
            toward = away = 0
            for pin in sym.pins:
                net = ctx.net_by_ref_pin.get((sym.reference, pin.number))
                if not net or netlist_mod.classify_net(net) in ("power", "ground"):
                    continue
                partners = [
                    pin_at[(node["ref"], node["pin"])]
                    for node in nodes_by_net.get(net, [])
                    if node["ref"] != sym.reference and (node["ref"], node["pin"]) in pin_at
                ]
                if not partners:
                    continue
                mean = sum(p[axis] for p in partners) / len(partners)
                if (mean - row) * facing >= 0:
                    toward += 1
                else:
                    away += 1
            if away >= min_pins and away > toward:
                findings.append(
                    Finding(
                        "readability.facing_away",
                        "info",
                        f"{sym.reference}: {away} of {away + toward} connected pins "
                        "point away from the pins they connect to - mirror the "
                        "symbol so the row faces its signals",
                        location=sym.reference,
                        details={"away": away, "toward": toward},
                    )
                )
    return findings


@rule
def rule_margin_intrusion(ctx: ReviewContext) -> list[Finding]:
    """Circuit or notes drawn on the sheet furniture.

    The outer strip of the page belongs to the frame and its rulers, and the
    bottom right corner to the title block. Anything placed there prints over
    them - a note that runs past the frame, or a connector parked on the title
    block, both found on real generated sheets by trying to wire them.
    """
    margin = ctx.thresholds["page_margin_mm"]
    intrusions = []
    for doc in ctx.docs:
        if not doc.paper_size:
            continue

        def in_furniture(x: float, y: float, size: tuple[float, float] = doc.paper_size) -> bool:
            width, height = size
            if x < margin or y < margin or x > width - margin or y > height - margin:
                return True
            # KiCad's standard title block: ~110 mm wide, ~30 mm tall, bottom right
            return x > width - 115.0 and y > height - 30.0

        for sym in doc.symbols:
            if sym.is_power:
                continue
            for pin in sym.pins:
                if in_furniture(pin.x, pin.y):
                    intrusions.append(f"{doc.path.name}:{sym.reference or sym.lib_id}")
                    break
        for item in doc.text_items:
            if in_furniture(item.x, item.y):
                snippet = item.text.splitlines()[0][:40]
                intrusions.append(f"{doc.path.name}:text '{snippet}'")
    intrusions = sorted(set(intrusions))
    if not intrusions:
        return []
    return [
        _group_finding(
            "readability.margin_intrusion",
            "info",
            f"{len(intrusions)} item(s) sit on the page frame or the title block "
            "and print over them",
            intrusions,
        )
    ]


@rule
def rule_text_over_wire(ctx: ReviewContext) -> list[Finding]:
    """A symbol's own field printed across a wire.

    The designator and the value are text on the page like any other, and a
    reader loses them under a wire exactly as under a symbol - worse, in fact,
    because a wire crossing a number turns a 4k7 into something nobody will
    trust. Nothing about it changes the netlist, so only the plot shows it,
    which is why it survived four rounds of review.
    """
    collisions: list[str] = []
    for doc in ctx.docs:
        segments = [
            (a, b) for wire in doc.wires for a, b in zip(wire.points, wire.points[1:], strict=False)
        ]
        if not segments:
            continue
        for sym in doc.symbols:
            for name, (px, py, justify) in sym.property_at.items():
                text = sym.properties.get(name, "")
                if not text or name in ("Footprint", "Datasheet", "MPN", "Manufacturer"):
                    continue
                box = _field_extent(text, px, py, justify)
                for a, b in segments:
                    if _segment_hits_rect(a, b, box):
                        collisions.append(
                            f"{doc.path.name}:{sym.reference or sym.lib_id} {name} '{text[:16]}'"
                        )
                        break
    collisions = sorted(set(collisions))
    if not collisions:
        return []
    return [
        Finding(
            "readability.text_over_wire",
            "warning",
            f"{len(collisions)} symbol field(s) print across a wire - a net "
            "drawn through a value is a value nobody can read off the plot",
            details={"count": len(collisions), "examples": collisions[:8]},
        )
    ]


def _field_extent(text: str, x: float, y: float, justify: str) -> tuple[float, float, float, float]:
    """The box a symbol field roughly covers, from its anchor and justify.

    1.4 mm per character is the stroke font's real advance at KiCad's default
    1.27 mm text size. Undersizing it reads two strings a third of a millimetre
    apart as clear of each other, which on the plot is one unreadable word.
    """
    width = len(text) * 1.4
    height = 1.6
    if "right" in justify:
        x0, x1 = x - width, x
    elif "left" in justify:
        x0, x1 = x, x + width
    else:
        x0, x1 = x - width / 2, x + width / 2
    return (x0, y - height / 2, x1, y + height / 2)


def _segment_hits_rect(a, b, box) -> bool:
    x0, y0, x1, y1 = box
    for px, py in (a, b):
        if x0 <= px <= x1 and y0 <= py <= y1:
            return True
    steps = max(2, int(math.dist(a, b) / 0.5))
    for index in range(steps + 1):
        t = index / steps
        px = a[0] + (b[0] - a[0]) * t
        py = a[1] + (b[1] - a[1]) * t
        if x0 <= px <= x1 and y0 <= py <= y1:
            return True
    return False


def _text_extent(item: schematic.Text) -> tuple[float, float, float, float]:
    """The box a note roughly covers, from its anchor, justify and content.

    KiCad does not store the rendered extent, so this assumes the default
    1.27 mm font: about 1.1 mm per character and 2.54 mm per line. Rough - which
    is why the rule built on it reports info, not error.
    """
    lines = item.text.splitlines() or [""]
    width = max(len(line) for line in lines) * 1.1
    height = len(lines) * 2.54
    justify = item.justify
    if "left" in justify:
        x0, x1 = item.x, item.x + width
    elif "right" in justify:
        x0, x1 = item.x - width, item.x
    else:
        x0, x1 = item.x - width / 2, item.x + width / 2
    if "top" in justify:
        y0, y1 = item.y, item.y + height
    elif "bottom" in justify:
        y0, y1 = item.y - height, item.y
    else:
        y0, y1 = item.y - height / 2, item.y + height / 2
    return (x0, y0, x1, y1)


@rule
def rule_text_over_symbol(ctx: ReviewContext) -> list[Finding]:
    """A design note printed over a symbol.

    The note and the circuit are both right; on top of each other neither can
    be read. Nothing about it changes the netlist, so it is only visible by
    looking at the plot - or by estimating the text's extent, which is what
    this does.
    """
    collisions = []
    for doc in ctx.docs:
        boxes = []
        for sym in doc.symbols:
            if sym.is_power:
                continue
            box = sym.bbox()
            if box:
                boxes.append((box, sym.reference or sym.lib_id))
        for item in doc.text_items:
            tx0, ty0, tx1, ty1 = _text_extent(item)
            for (bx0, by0, bx1, by1), ref in boxes:
                if (
                    min(tx1, bx1) - max(tx0, bx0) > GEOM_TOL
                    and min(ty1, by1) - max(ty0, by0) > GEOM_TOL
                ):
                    snippet = item.text.splitlines()[0][:40]
                    collisions.append(f"{doc.path.name}:'{snippet}' over {ref}")
                    break
    collisions = sorted(set(collisions))
    if not collisions:
        return []
    return [
        _group_finding(
            "readability.text_over_symbol",
            "info",
            f"{len(collisions)} note(s) print over a symbol - notes belong "
            "beside the circuit, not on it",
            collisions,
        )
    ]


@rule
def rule_outside_page(ctx: ReviewContext) -> list[Finding]:
    """Anything drawn beyond the page border is invisible in the PDF."""
    outside = []
    for doc in ctx.docs:
        if not doc.paper_size:
            continue
        width, height = doc.paper_size
        points: list[tuple[str, tuple[float, float]]] = [
            (sym.reference or sym.lib_id, (sym.x, sym.y)) for sym in doc.symbols
        ]
        points += [(label.text, (label.x, label.y)) for label in doc.labels]
        points += [("wire", point) for wire in doc.wires for point in wire.points]
        for name, (x, y) in points:
            if x < -GEOM_TOL or y < -GEOM_TOL or x > width + GEOM_TOL or y > height + GEOM_TOL:
                outside.append(f"{doc.path.name}:{name} at ({round(x, 1)}, {round(y, 1)})")
    if not outside:
        return []
    return [
        _group_finding(
            "readability.outside_page",
            "warning",
            f"{len(outside)} item(s) sit outside the page border - "
            "they are missing from the plotted sheet and the PDF",
            sorted(set(outside)),
        )
    ]


@rule
def rule_sheet_density(ctx: ReviewContext) -> list[Finding]:
    """One sheet holding more than a reader can follow."""
    limit = int(ctx.thresholds["max_symbols_per_sheet"])
    findings = []
    for doc in ctx.docs:
        count = len([s for s in doc.symbols if not s.is_power])
        if count > limit:
            findings.append(
                Finding(
                    "readability.sheet_density",
                    "info",
                    f"{doc.path.name} carries {count} symbols (over {limit}) - "
                    "splitting it into hierarchical sheets by function reads far better",
                    location=doc.path.name,
                    details={"symbols": count, "limit": limit},
                )
            )
    return findings


@rule
def rule_unnamed_nets(ctx: ReviewContext) -> list[Finding]:
    """Signals left with generated names.

    ``Net-(U1-Pad7)`` tells a reader nothing about what the wire carries, and it
    is what every net gets when nobody labels anything.
    """
    multi = [n for n in ctx.nets if n["pin_count"] >= 2]
    if len(multi) < 10:
        return []
    named = [n for n in multi if not AUTO_NET_NAME.match(n["name"])]
    ratio = len(named) / len(multi)
    if ratio >= ctx.thresholds["min_named_net_ratio"]:
        return []
    return [
        Finding(
            "readability.unnamed_nets",
            "info",
            f"only {len(named)} of {len(multi)} multi-pin nets carry a name a human chose "
            f"({ratio:.0%}) - labelling the signals is what makes the sheet followable",
            details={"named": len(named), "total": len(multi), "ratio": round(ratio, 3)},
        )
    ]


@rule
def rule_title_block(ctx: ReviewContext) -> list[Finding]:
    """The sheet should say what it is and which revision it is."""
    if not ctx.docs:
        return []
    block = ctx.docs[0].title_block
    missing = [key for key in ("title", "rev", "date", "company") if not block.get(key, "").strip()]
    if not missing:
        return []
    return [
        Finding(
            "readability.title_block",
            "info",
            f"the root sheet's title block has no {', '.join(missing)}",
            location=ctx.docs[0].path.name,
            details={"missing": missing},
        )
    ]


# ---------------------------------------------------------------------------
# Specification: the numbers that decide whether a part survives the circuit.
# A value alone ("100n") is not a part - the rating is what makes it orderable
# and the derating is what makes it last.
# ---------------------------------------------------------------------------

_VOLT_DECIMAL = re.compile(r"(\d+)V(\d+)", re.IGNORECASE)
_VOLT_SUFFIX = re.compile(r"(\d+(?:\.\d+)?)\s*V", re.IGNORECASE)

RATING_FIELDS = {
    "voltage": ("voltage", "voltage rating", "vdc", "rating"),
    "tolerance": ("tolerance", "tol"),
    "power": ("power", "wattage", "power rating"),
    "current": ("current", "current rating", "isat"),
}
PART_NUMBER_FIELDS = (
    "mpn",
    "manufacturer part number",
    "part number",
    "partnumber",
    "manufacturer",
    "lcsc",
    "digikey",
    "mouser",
)
# What each family has to state before the part is actually specified.
REQUIRED_RATINGS = {
    "C": ("voltage", "tolerance"),
    "R": ("tolerance", "power"),
    "L": ("current",),
}
PART_NUMBER_PREFIXES = ("U", "IC", "Q", "D", "Y", "K", "J", "SW")


def _field(sym: schematic.Symbol, names: tuple[str, ...]) -> str:
    lowered = {key.strip().lower(): value for key, value in sym.properties.items()}
    for name in names:
        value = lowered.get(name, "").strip()
        if value and value not in ("~", "-"):
            return value
    return ""


def _parse_voltage(text: str) -> float | None:
    """``50V``, ``6V3``, ``25 V DC`` or a bare ``16`` -> volts."""
    m = _VOLT_DECIMAL.search(text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = _VOLT_SUFFIX.search(text)
    if m:
        return float(m.group(1))
    try:
        return float(text.strip())
    except ValueError:
        return None


@rule
def rule_missing_ratings(ctx: ReviewContext) -> list[Finding]:
    """Passives whose value is stated but whose rating is not.

    ``C3 = 100n`` is not a specification: on a 24 V rail the 16 V part fails and
    the 50 V part does not, and the schematic that names neither cannot be
    reviewed, ordered or built twice the same way.
    """
    findings = []
    for sym in ctx.parts:
        required = REQUIRED_RATINGS.get(ctx.prefix(sym.reference))
        if not required or sym.dnp:
            continue
        missing = [name for name in required if not _field(sym, RATING_FIELDS[name])]
        if missing:
            findings.append(
                Finding(
                    "spec.missing_rating",
                    "info",
                    f"{sym.reference} ({sym.value}) states no {', '.join(missing)}",
                    location=f"{sym.sheet}:{sym.reference}",
                    details={"missing": missing},
                )
            )
    return findings


@rule
def rule_missing_part_number(ctx: ReviewContext) -> list[Finding]:
    """Active parts with no orderable identity."""
    findings = []
    for sym in ctx.parts:
        if ctx.prefix(sym.reference) not in PART_NUMBER_PREFIXES or sym.dnp:
            continue
        if not _field(sym, PART_NUMBER_FIELDS):
            findings.append(
                Finding(
                    "spec.missing_part_number",
                    "info",
                    f"{sym.reference} ({sym.value}) carries no manufacturer part number",
                    location=f"{sym.sheet}:{sym.reference}",
                )
            )
    return findings


@rule
def rule_capacitor_derating(ctx: ReviewContext) -> list[Finding]:
    """Capacitor voltage rating against the rail it actually sits on.

    Only rails that name their own voltage are judged: derating a part against a
    number nobody wrote down would be inventing the requirement. A ceramic also
    loses most of its capacitance well before its rating, which is why the
    default asks for headroom rather than for survival.
    """
    factor = ctx.thresholds["capacitor_derating_factor"]
    findings = []
    for sym in ctx.parts:
        if not ctx.is_capacitor(sym.reference):
            continue
        rating = _parse_voltage(_field(sym, RATING_FIELDS["voltage"]))
        if rating is None:
            continue  # rule_missing_ratings already says the rating is absent
        rails = [
            abs(v)
            for v in (
                netlist_mod.rail_voltage(pin["net"]) for pin in ctx.pins_by_ref[sym.reference]
            )
            if v is not None
        ]
        if not rails:
            continue
        rail = max(rails)
        where = f"{sym.sheet}:{sym.reference}"
        if rating < rail:
            findings.append(
                Finding(
                    "spec.voltage_derating",
                    "error",
                    f"{sym.reference} is rated {rating} V and sits on a {rail} V rail",
                    location=where,
                    details={"rating_v": rating, "rail_v": rail},
                )
            )
        elif rating < rail * factor:
            findings.append(
                Finding(
                    "spec.voltage_derating",
                    "warning",
                    f"{sym.reference} is rated {rating} V on a {rail} V rail - "
                    f"less than the {factor}x headroom a ceramic wants",
                    location=where,
                    details={"rating_v": rating, "rail_v": rail, "factor": factor},
                )
            )
    return findings


@rule
def rule_design_notes(ctx: ReviewContext) -> list[Finding]:
    """Whether the sheet records why it is the way it is.

    The reasoning behind a divider ratio, a part choice or a protection scheme
    is the one thing no netlist carries and no reviewer can reconstruct. A text
    note on the sheet next to the circuit it explains is where it belongs.
    """
    if not ctx.docs:
        return []
    if any(doc.texts for doc in ctx.docs):
        return []
    if any(_field(sym, ("description", "notes", "rationale")) for sym in ctx.parts):
        return []
    return [
        Finding(
            "spec.no_design_notes",
            "info",
            f"none of the {len(ctx.docs)} sheet(s) carry a text note - "
            "the reasoning behind the values and part choices is not written down anywhere",
        )
    ]


def statistics(ctx: ReviewContext) -> dict[str, Any]:
    by_prefix = Counter(ctx.prefix(s.reference) for s in ctx.parts if s.reference)
    return {
        "sheets": len(ctx.docs),
        "symbols": len(ctx.parts),
        "power_symbols": len([s for s in ctx.symbols if s.is_power]),
        "nets": len(ctx.nets),
        "connections": sum(n["pin_count"] for n in ctx.nets),
        "by_reference_prefix": dict(sorted(by_prefix.items())),
        "unique_values": len({s.value for s in ctx.parts if s.value}),
        "netlist_source": ctx.netlist.get("source"),
        "erc_available": bool(ctx.erc and "error" not in ctx.erc),
    }


def review(
    target: str | os.PathLike[str],
    *,
    use_cli: bool = True,
    thresholds: dict[str, float] | None = None,
    collapse: int = COLLAPSE_LIMIT,
) -> dict[str, Any]:
    ctx = ReviewContext(target, use_cli=use_cli, thresholds=thresholds)
    findings: list[Finding] = []
    for func in RULES:
        try:
            findings.extend(func(ctx))
        except Exception as exc:  # a broken rule must not kill the report
            findings.append(
                Finding(
                    f"internal.{func.__name__}", "info", f"rule failed: {type(exc).__name__}: {exc}"
                )
            )
    findings = sort_findings(collapse_findings(findings, collapse))
    return {
        "schematic": str(ctx.root_sch),
        "statistics": statistics(ctx),
        "thresholds": ctx.thresholds,
        "summary": summarize(findings),
        "findings": [f.to_dict() for f in findings],
    }


def info(target: str | os.PathLike[str], *, use_cli: bool = True) -> dict[str, Any]:
    """Structured description of the schematic - components, nets, hierarchy."""
    root = schematic.find_root_schematic(target)
    docs = schematic.parse_project(root)
    nl = netlist_mod.get(root, prefer_cli=use_cli)
    return {
        "schematic": str(root),
        "title_block": docs[0].title_block if docs else {},
        "sheets": [
            {
                "file": doc.path.name,
                "symbols": len([s for s in doc.symbols if not s.is_power]),
                "sub_sheets": [{"name": s.name, "file": s.filename} for s in doc.sheets],
                "notes": doc.texts,
            }
            for doc in docs
        ],
        "components": [s.to_dict() for doc in docs for s in doc.symbols if not s.is_power],
        "netlist_source": nl.get("source"),
        "nets": [
            {
                "name": n["name"],
                "pin_count": n["pin_count"],
                "class": netlist_mod.classify_net(n["name"]),
                "nodes": [f"{x['ref']}.{x['pin']}" for x in n["nodes"]],
            }
            for n in sorted(nl.get("nets", []), key=lambda n: n["name"])
        ],
    }
