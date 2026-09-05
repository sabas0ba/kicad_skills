"""The golden contract must reject plausible regressions, not just parse files."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "example_contracts", ROOT / "tools/check_example_contracts.py"
)
assert SPEC and SPEC.loader
contracts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contracts)


@pytest.fixture
def motor(monkeypatch):
    nodes = {
        "VINT": ["U1.14", "C4.1"],
        "VCP": ["U1.11", "C3.2"],
        "nFAULT": ["U1.8", "J4.7"],
        "VM": ["U1.12", "C2.1", "C3.1"],
        "GND": ["U1.13", "U1.3", "U1.6", "C2.2", "C4.2"],
    }
    netlist = {
        "nets": [
            {
                "name": f"/{name}",
                "nodes": [dict(zip(("ref", "pin"), node.split("."), strict=True)) for node in pins],
            }
            for name, pins in nodes.items()
        ]
    }
    monkeypatch.setattr(contracts.schematic, "build_netlist", lambda _docs: netlist)
    doc = NS(
        title_block={"title": "DRV8833PW, 2 x 0.5 A RMS"},
        symbols=[
            NS(
                reference=ref,
                value=value,
                is_power=False,
                footprint=contracts.MOTOR_PACKAGE if ref == "U1" else "",
                properties={"MPN": "DRV8833PWR"} if ref == "U1" else {},
            )
            for ref, value in {"U1": "DRV8833PW", "C2": "10u", "C3": "10n", "C4": "2u2"}.items()
        ],
    )
    pads = {}
    for name, pins in nodes.items():
        for node in pins:
            ref, number = node.split(".")
            pads.setdefault(ref, []).append(NS(number=number, net=f"/{name}"))
    board = NS(
        footprints=[
            NS(ref=ref, pads=p, lib_id=contracts.MOTOR_PACKAGE if ref == "U1" else "")
            for ref, p in pads.items()
        ]
    )
    return doc, board, netlist


def test_motor_electrical_contract_accepts_explicit_requirements(motor):
    assert contracts.motor_contract(*motor[:2]) == []


@pytest.mark.parametrize("ref,value", [("C2", "100n"), ("C4", "1u")])
def test_motor_rejects_undersized_bypass(motor, ref, value):
    doc, board, _ = motor
    next(p for p in doc.symbols if p.reference == ref).value = value
    assert any(ref in e for e in contracts.motor_contract(doc, board))


def test_motor_rejects_wrong_package_current_rating(motor):
    doc, board, _ = motor
    doc.title_block["title"] = "DRV8833, 2 x 1.5 A RMS"
    assert any("0.5 A" in e for e in contracts.motor_contract(doc, board))


@pytest.mark.parametrize("stage", ["schematic", "board"])
def test_motor_rejects_mpn_footprint_mismatch(motor, stage):
    doc, board, _ = motor
    if stage == "schematic":
        doc.symbols[0].footprint = "wrong:exposed_pad_package"
    else:
        board.footprints[0].lib_id = "wrong:exposed_pad_package"
    assert any(stage in e and "footprint" in e for e in contracts.motor_contract(doc, board))


@pytest.mark.parametrize("stage", ["schematic", "board"])
@pytest.mark.parametrize("net", ["VINT", "VCP", "nFAULT", "VM", "GND"])
def test_motor_checks_topology_on_both_sides(motor, stage, net):
    doc, board, netlist = motor
    if stage == "schematic":
        next(n for n in netlist["nets"] if n["name"] == f"/{net}")["nodes"].pop()
    else:
        for fp in board.footprints:
            fp.pads = [p for p in fp.pads if p.net != f"/{net}"]
    assert any(stage in e and net in e for e in contracts.motor_contract(doc, board))


def test_motor_rejects_vint_external_load(motor):
    doc, board, netlist = motor
    next(n for n in netlist["nets"] if n["name"] == "/VINT")["nodes"].append(
        {"ref": "R1", "pin": "1"}
    )
    assert any("VINT" in e for e in contracts.motor_contract(doc, board))


@pytest.fixture
def plane():
    outline = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    return NS(
        copper_layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        tracks=[],
        zones=[
            NS(
                keepout=False,
                layers=["In1.Cu"],
                net="GND",
                outline=outline,
                fills=[("In1.Cu", outline[:])],
            ),
        ],
    )


def test_plane_contract_checks_real_filled_region(plane):
    assert contracts.plane_contract(plane) == []
    plane.zones[0].fills = []
    assert contracts.plane_contract(plane)


def test_inner_power_zone_must_match_exact_pad_net_name(plane):
    plane.zones.append(NS(keepout=False, layers=["In2.Cu"], net="VM"))
    assert any("exact" in e for e in contracts.plane_contract(plane, power_net="/VM"))
    plane.zones[-1].net = "/VM"
    assert contracts.plane_contract(plane, power_net="/VM") == []


def test_plane_contract_rejects_fragmented_fill(plane):
    # Total coverage is 95%, but neither island carries 90%: summing them
    # would miss the plane split which this regression check protects against.
    plane.zones[0].fills = [
        ("In1.Cu", [(0, 0), (10, 0), (10, 4.75), (0, 4.75)]),
        ("In1.Cu", [(0, 5.25), (10, 5.25), (10, 10), (0, 10)]),
    ]
    assert contracts.plane_contract(plane)


@pytest.mark.parametrize("change", ["two_layers", "foreign_track", "foreign_zone", "no_zone"])
def test_plane_contract_rejects_structural_regressions(plane, change):
    if change == "two_layers":
        plane.copper_layers = ["F.Cu", "B.Cu"]
    elif change == "foreign_track":
        plane.tracks.append(NS(layer="In1.Cu", net="CLK"))
    elif change == "foreign_zone":
        plane.zones[0].net = "+3V3"
    else:
        plane.zones.clear()
    assert contracts.plane_contract(plane)


def test_verdict_requires_both_halves_and_specific_negative_controls():
    good = {"pass": True, "schematic": {"schematic": {}}, "board": {"board": {}}}
    assert contracts.verdict_contract(good, reviewed=True) == []
    for stage in ("schematic", "board"):
        skipped = deepcopy(good)
        skipped[stage] = {"skipped": "not found"}
        assert contracts.verdict_contract(skipped, reviewed=True)
        del skipped[stage]
        assert contracts.verdict_contract(skipped, reviewed=True)
    negative = {
        **good,
        "pass": False,
        "blocking": [
            {"rule": rule}
            for rule in (
                "readability.title_block",
                "spec.missing_rating",
                "spec.missing_part_number",
            )
        ],
    }
    assert contracts.verdict_contract(negative, reviewed=False) == []
    negative["blocking"].pop()
    assert contracts.verdict_contract(negative, reviewed=False)
    assert contracts.verdict_contract(good, reviewed=False)


@pytest.mark.parametrize("rule", ["erc.unavailable", "drc.unavailable", "internal.rule_example"])
@pytest.mark.parametrize("bucket", ["findings", "waived"])
def test_verdict_rejects_failed_checks_even_if_informational_or_waived(rule, bucket):
    verdict = {
        "pass": True,
        "schematic": {"schematic": {}},
        "board": {"board": {}},
        bucket: [{"rule": rule, "severity": "info"}],
    }
    assert any("incomplete checks" in e for e in contracts.verdict_contract(verdict, reviewed=True))


def test_single_example_and_reviewed_only_cli_scope(tmp_path, monkeypatch):
    parsed = []

    def parse(path):
        parsed.append(path)
        return NS()

    monkeypatch.setattr(contracts.pcb, "parse", parse)
    verdict = {"pass": True, "schematic": {"schematic": {}}, "board": {"board": {}}}
    (tmp_path / "buck-5v-reviewed-gate.json").write_text(json.dumps(verdict))
    argv = [str(tmp_path), "--only", "buck-5v", "--verdicts", str(tmp_path)]
    assert contracts.main([*argv, "--reviewed-only"]) == 0
    assert parsed == [tmp_path / "buck-5v/reviewed/buck-5v.kicad_pcb"]
    # The normal golden invocation must not silently omit negative controls.
    with pytest.raises(FileNotFoundError):
        contracts.main(argv)


@pytest.mark.parametrize("severity", ["error", "warning"])
@pytest.mark.parametrize("demoted", [False, True])
def test_reviewed_contract_does_not_hide_native_drc_findings(severity, demoted):
    verdict = {
        "pass": True,
        "schematic": {"schematic": {}},
        "board": {"board": {}},
        "findings": [
            {
                "rule": "drc.via_dangling",
                "reported_severity": severity,
                "severity": "info" if demoted else severity,
            }
        ],
    }
    assert any(
        "unwaived KiCad DRC" in e for e in contracts.verdict_contract(verdict, reviewed=True)
    )
