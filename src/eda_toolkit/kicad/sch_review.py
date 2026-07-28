"""Schematic review: ERC plus design-practice heuristics."""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from ..util import Finding, sort_findings, summarize
from . import kicad_cli, netlist as netlist_mod, schematic

RULES: list[Callable[["ReviewContext"], list[Finding]]] = []


def rule(func: Callable[["ReviewContext"], list[Finding]]):
    RULES.append(func)
    return func


class ReviewContext:
    """Everything the rules need: parsed sheets, netlist and derived indexes."""

    def __init__(self, target: str | os.PathLike[str], *, use_cli: bool = True) -> None:
        self.root_sch: Path = schematic.find_root_schematic(target)
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
    def from_netlist(cls, netlist: dict[str, Any], *, symbols: list[schematic.Symbol] | None = None,
                     erc: dict[str, Any] | None = None,
                     docs: list[schematic.SchematicDoc] | None = None) -> "ReviewContext":
        """Build a context from an in-memory netlist.

        Useful for reviewing a netlist that was produced elsewhere, and for
        exercising the rules in the test-suite without touching the filesystem.
        """
        ctx = cls.__new__(cls)
        ctx.root_sch = Path(netlist.get("schematic", "<memory>"))
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
            return [Finding("erc.unavailable", "info",
                            f"ERC could not be run: {ctx.erc['error']}")]
        return [Finding("erc.unavailable", "info",
                        "kicad-cli is not available, ERC was skipped "
                        "(run inside the container for full coverage)")]
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
                Finding("schematic.duplicate_reference", "error",
                        f"reference {ref} is used by {count} symbols", location=ref)
            )
    for sym in ctx.parts:
        if sym.unannotated:
            findings.append(
                Finding("schematic.unannotated", "error",
                        f"symbol {sym.lib_id} at ({sym.x}, {sym.y}) is not annotated "
                        f"(reference {sym.reference!r})",
                        location=f"{sym.sheet}:{sym.reference or sym.lib_id}")
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
            findings.append(Finding("schematic.missing_value", "warning",
                                    f"{sym.reference} has no Value", location=where))
        if not sym.footprint.strip() and sym.on_board and not sym.dnp:
            findings.append(Finding("schematic.missing_footprint", "warning",
                                    f"{sym.reference} ({sym.value}) has no footprint assigned",
                                    location=where))
        if not sym.datasheet.strip() or sym.datasheet.strip() == "~":
            if ctx.prefix(sym.reference) in ("U", "IC", "Q", "D", "Y", "L"):
                findings.append(Finding("schematic.missing_datasheet", "info",
                                        f"{sym.reference} ({sym.value}) has no datasheet link",
                                        location=where))
        if sym.dnp:
            findings.append(Finding("schematic.dnp", "info",
                                    f"{sym.reference} ({sym.value}) is marked DNP", location=where))
    return findings


@rule
def rule_single_pin_nets(ctx: ReviewContext) -> list[Finding]:
    """A net with a single pin is almost always a wiring mistake."""
    findings = []
    for net in ctx.nets:
        if net["pin_count"] == 1:
            node = net["nodes"][0]
            findings.append(
                Finding("net.single_pin", "warning",
                        f"net {net['name']} only connects {node['ref']}.{node['pin']}",
                        location=net["name"], details={"node": node})
            )
    return findings


@rule
def rule_floating_inputs(ctx: ReviewContext) -> list[Finding]:
    """Input pins that see no driver."""
    findings = []
    driver_types = {"output", "power_out", "bidirectional", "tri_state", "open_collector",
                    "open_emitter", "passive", "unspecified", "free"}
    for net in ctx.nets:
        types = [n.get("type", "") for n in net["nodes"]]
        if not any(t in driver_types for t in types):
            inputs = [n for n in net["nodes"] if n.get("type") == "input"]
            if inputs and len(net["nodes"]) > 1:
                pin_list = ", ".join("{}.{}".format(n["ref"], n["pin"]) for n in inputs)
                findings.append(
                    Finding("net.no_driver", "warning",
                            f"net {net['name']} only has input pins ({pin_list})",
                            location=net["name"])
                )
    return findings


@rule
def rule_decoupling(ctx: ReviewContext) -> list[Finding]:
    """Every IC supply pin should have a local decoupling capacitor."""
    findings = []
    for ref in ctx.ic_refs():
        supply_nets = set()
        for pin in ctx.pins_by_ref[ref]:
            net_name = pin["net"]
            kind = netlist_mod.classify_net(net_name)
            if pin.get("type") == "power_in" or kind == "power":
                if kind != "ground":
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
                    Finding("analog.missing_decoupling", "warning",
                            f"supply net {net_name} of {ref} has no capacitor to ground",
                            location=f"{ref} / {net_name}")
                )
    return findings


@rule
def rule_power_nets(ctx: ReviewContext) -> list[Finding]:
    """Summarise supplies and flag suspicious naming."""
    findings = []
    power_nets = {n["name"] for n in ctx.nets if netlist_mod.classify_net(n["name"]) == "power"}
    ground_nets = {n["name"] for n in ctx.nets if netlist_mod.classify_net(n["name"]) == "ground"}
    if not ground_nets:
        findings.append(Finding("power.no_ground", "error",
                                "no ground net found (expected GND/VSS/AGND...)"))
    if not power_nets:
        findings.append(Finding("power.no_supply", "warning",
                                "no power net found (expected +3V3/VCC/VDD...)"))
    if len(power_nets) > 6:
        findings.append(Finding("power.many_supplies", "info",
                                f"{len(power_nets)} distinct supply nets: {', '.join(sorted(power_nets))}"))
    return findings


@rule
def rule_i2c_pullups(ctx: ReviewContext) -> list[Finding]:
    """I2C-looking nets need pull-up resistors."""
    findings = []
    for net in ctx.nets:
        base = net["name"].split("/")[-1].upper()
        if re.fullmatch(r"(I2C\d?_)?(SDA|SCL)(\d)?", base):
            if not any(ctx.is_resistor(r) for r in ctx.refs_on_net(net["name"])):
                findings.append(
                    Finding("analog.i2c_pullup", "warning",
                            f"{net['name']} looks like an I2C line but has no resistor on it",
                            location=net["name"])
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
                Finding("analog.led_no_series_resistor", "warning",
                        f"{sym.reference} ({sym.value}) has no series resistor on either terminal",
                        location=sym.reference)
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
                Finding("power.unused_rail", "warning",
                        f"rail {net['name']} is declared but reaches no component pin",
                        location=net["name"])
            )
    return findings


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


def review(target: str | os.PathLike[str], *, use_cli: bool = True) -> dict[str, Any]:
    ctx = ReviewContext(target, use_cli=use_cli)
    findings: list[Finding] = []
    for func in RULES:
        try:
            findings.extend(func(ctx))
        except Exception as exc:  # a broken rule must not kill the report
            findings.append(Finding(f"internal.{func.__name__}", "info",
                                    f"rule failed: {type(exc).__name__}: {exc}"))
    findings = sort_findings(findings)
    return {
        "schematic": str(ctx.root_sch),
        "statistics": statistics(ctx),
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
            {"name": n["name"], "pin_count": n["pin_count"],
             "class": netlist_mod.classify_net(n["name"]),
             "nodes": [f"{x['ref']}.{x['pin']}" for x in n["nodes"]]}
            for n in sorted(nl.get("nets", []), key=lambda n: n["name"])
        ],
    }
