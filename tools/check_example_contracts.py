#!/usr/bin/env python3
"""Check explicit golden-example requirements that ERC/DRC cannot infer.

This is a project-specific contract, not a claim of general circuit validation.
Datasheet values: TI DRV8833 SLVSAR1E, sections 1, 5, 7.3.3 and 10.1.
https://www.ti.com/lit/ds/symlink/drv8833.pdf
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eda_toolkit.kicad import pcb, schematic
from eda_toolkit.kicad.pcb_review import _polygon_area

EXAMPLES = ("buck-5v", "motor-driver", "pico-carrier", "opamp-filter", "fpga-audio")
MOTOR_PACKAGE = "Package_SO:TSSOP-16_4.4x5mm_P0.65mm"


def motor_contract(doc: schematic.SchematicDoc, board: pcb.Board) -> list[str]:
    errors = []
    parts = {p.reference: p for p in doc.symbols if not p.is_power}
    for ref, value in {"U1": "DRV8833PW", "C2": "10u", "C3": "10n", "C4": "2u2"}.items():
        if ref not in parts or parts[ref].value != value:
            errors.append(f"motor-driver: {ref} must be {value}")
    if "0.5 A RMS" not in doc.title_block.get("title", ""):
        errors.append("motor-driver: PW package must be identified as 0.5 A RMS, not 1.5 A")
    if parts.get("U1") and parts["U1"].properties.get("MPN") != "DRV8833PWR":
        errors.append("motor-driver: changing the package requires reviewing its current rating")
    if parts.get("U1") and parts["U1"].footprint != MOTOR_PACKAGE:
        errors.append("motor-driver: schematic footprint must match the selected PW package")
    driver = next((fp for fp in board.footprints if fp.ref == "U1"), None)
    if driver is None or driver.lib_id != MOTOR_PACKAGE:
        errors.append("motor-driver: board footprint must match the selected PW package")
    # Exact nodes protect against accidentally loading the unspecified VINT
    # supply, strapping the flying capacitor to GND, or adding a VM pull-up on
    # the logic interface. Board parity is independently checked by KiCad.
    expected = {
        "VINT": {"U1.14", "C4.1"},
        "VCP": {"U1.11", "C3.2"},
        "nFAULT": {"U1.8", "J4.7"},
    }
    nets = {
        name.lstrip("/"): {
            f"{node['ref']}.{node['pin']}"
            for node in net["nodes"]
            if not node["ref"].startswith("#")
        }
        for net in schematic.build_netlist([doc])["nets"]
        for name in [net["name"]]
    }
    board_nets: dict[str, set[str]] = {}
    for fp in board.footprints:
        for pad in fp.pads:
            board_nets.setdefault(pad.net.lstrip("/"), set()).add(f"{fp.ref}.{pad.number}")
    for name, nodes in expected.items():
        for stage, actual in (("schematic", nets), ("board", board_nets)):
            if actual.get(name) != nodes:
                errors.append(f"motor-driver: {stage} {name} must connect exactly {sorted(nodes)}")
    for name, nodes in {
        "VM": {"U1.12", "C2.1", "C3.1"},
        "GND": {"U1.13", "U1.3", "U1.6", "C2.2", "C4.2"},
    }.items():
        for stage, actual in (("schematic", nets), ("board", board_nets)):
            if not nodes <= actual.get(name, set()):
                errors.append(
                    f"motor-driver: {stage} {name} missing required bypass/sense connections"
                )
    return errors


def plane_contract(board: pcb.Board) -> list[str]:
    """Protect the stated inner GND layer; this does not prove signal integrity."""
    errors = []
    if board.copper_layers != ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]:
        errors.append("expected F.Cu / In1.Cu / In2.Cu / B.Cu")
    if any(t.layer == "In1.Cu" and t.net.lstrip("/") != "GND" for t in board.tracks):
        errors.append("foreign routing cuts the reserved In1.Cu GND layer")
    zones = [z for z in board.zones if not z.keepout and "In1.Cu" in z.layers]
    if len(zones) != 1 or zones[0].net.lstrip("/") != "GND":
        return [*errors, "expected one GND zone on In1.Cu"]
    zone = zones[0]
    area = abs(_polygon_area(zone.outline)) if zone.outline else 0.0
    fills = [points for layer, points in zone.fills if layer == "In1.Cu" and points]
    largest = max((abs(_polygon_area(points)) for points in fills), default=0.0)
    # A baseline-regression threshold, not a universal EMI/return-path limit.
    if not area or largest / area < 0.90:
        errors.append("In1.Cu must retain one filled GND region covering at least 90% of its zone")
    return errors


def verdict_contract(verdict: dict, *, reviewed: bool) -> list[str]:
    errors = []
    if verdict.get("pass") is not reviewed:
        errors.append("unexpected gate verdict")
    for stage in ("schematic", "board"):
        if stage not in verdict or "skipped" in verdict[stage] or stage not in verdict[stage]:
            errors.append(f"gate skipped {stage}")
    # Generic reviews remain useful without KiCad and report these as info.
    # A golden acceptance run must actually execute all its checks, even if
    # a policy would otherwise waive or demote the diagnostic.
    incomplete = {
        finding["rule"]
        for finding in [*verdict.get("findings", []), *verdict.get("waived", [])]
        if finding["rule"] in {"erc.unavailable", "drc.unavailable"}
        or finding["rule"].startswith("internal.")
    }
    if incomplete:
        errors.append(f"incomplete checks: {sorted(incomplete)}")
    if not reviewed:
        actual = {finding["rule"] for finding in verdict.get("blocking", [])}
        expected = {"readability.title_block", "spec.missing_rating", "spec.missing_part_number"}
        if missing := expected - actual:
            errors.append(f"negative baseline lost expected blockers: {sorted(missing)}")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--only", choices=EXAMPLES, help="check a single generated example")
    parser.add_argument("--verdicts", type=Path)
    parser.add_argument(
        "--reviewed-only",
        action="store_true",
        help="check only reviewed verdicts (the cross-version CI matrix)",
    )
    args = parser.parse_args(argv)
    errors = []
    for name in EXAMPLES:
        if args.only and name != args.only:
            continue
        project = args.root / name / "reviewed" / name
        board = pcb.parse(project.with_suffix(".kicad_pcb"))
        if name == "motor-driver":
            errors.extend(motor_contract(schematic.parse(project.with_suffix(".kicad_sch")), board))
        if name in ("motor-driver", "fpga-audio"):
            errors.extend(f"{name}: {e}" for e in plane_contract(board))
        if args.verdicts:
            for variant in ("reviewed",) if args.reviewed_only else ("reviewed", "as-generated"):
                verdict = json.loads((args.verdicts / f"{name}-{variant}-gate.json").read_text())
                errors.extend(
                    f"{name}/{variant}: {e}"
                    for e in verdict_contract(verdict, reviewed=variant == "reviewed")
                )
    for error in errors:
        print(error)
    if not errors:
        print("Golden electrical, plane and gate contracts: PASS")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
