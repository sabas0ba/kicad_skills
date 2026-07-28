"""Rules are exercised through in-memory netlists so they stay fast and precise."""

from eda_toolkit.kicad import sch_review
from eda_toolkit.kicad.schematic import Symbol


def make_ctx(nets, symbols=(), erc=None):
    netlist = {
        "source": "test",
        "nets": [
            {"name": name, "nodes": nodes, "pin_count": len(nodes)}
            for name, nodes in nets.items()
        ],
    }
    return sch_review.ReviewContext.from_netlist(netlist, symbols=list(symbols), erc=erc)


def node(ref, pin, type_="passive", name=""):
    return {"ref": ref, "pin": pin, "pin_name": name, "type": type_}


def symbol(ref, value="1k", lib_id="Device:R", **kwargs):
    kwargs.setdefault("footprint", "Resistor_SMD:R_0805_2012Metric")
    kwargs.setdefault("datasheet", "~")
    return Symbol(uuid=f"u-{ref}", lib_id=lib_id, reference=ref, value=value, **kwargs)


def rules_of(findings):
    return {f.rule for f in findings}


def test_single_pin_net_is_reported():
    """An auto-named stub is a defect; a named one is context."""
    ctx = make_ctx({
        "unconnected-(U1-Pad7)": [node("U1", "7")],
        "SPARE_IO": [node("U1", "8")],
        "GND": [node("R1", "2"), node("C1", "2")],
    })
    findings = {f.location: f for f in sch_review.rule_single_pin_nets(ctx)}
    assert set(findings) == {"unconnected-(U1-Pad7)", "SPARE_IO"}
    assert findings["unconnected-(U1-Pad7)"].severity == "warning"
    assert findings["SPARE_IO"].severity == "info"


def test_collapsing_folds_a_noisy_rule():
    from eda_toolkit.util import Finding, collapse_findings

    findings = [Finding("net.single_pin", "warning", f"net N{i}", location=f"N{i}")
                for i in range(20)]
    findings.append(Finding("power.no_ground", "error", "no ground"))
    collapsed = collapse_findings(findings, limit=6)
    assert len(collapsed) == 2
    folded = next(f for f in collapsed if f.rule == "net.single_pin")
    assert folded.details["count"] == 20
    assert len(folded.details["examples"]) == 6
    assert next(f for f in collapsed if f.rule == "power.no_ground").details == {}


def test_collapsing_can_be_disabled():
    from eda_toolkit.util import Finding, collapse_findings

    findings = [Finding("x.y", "info", str(i)) for i in range(10)]
    assert len(collapse_findings(findings, limit=0)) == 10


def test_duplicate_and_unannotated_references():
    ctx = make_ctx({}, symbols=[symbol("R1"), symbol("R1"), symbol("R?")])
    rules = rules_of(sch_review.rule_annotation(ctx))
    assert rules == {"schematic.duplicate_reference", "schematic.unannotated"}


def test_multi_unit_symbols_are_not_duplicates():
    ctx = make_ctx({}, symbols=[symbol("U1", lib_id="X:Y", unit=1),
                                symbol("U1", lib_id="X:Y", unit=2)])
    assert sch_review.rule_annotation(ctx) == []


def test_missing_fields():
    ctx = make_ctx({}, symbols=[
        symbol("R1", value=""),
        symbol("C1", footprint=""),
        symbol("U1", lib_id="MCU:X", datasheet=""),
        symbol("R9", dnp=True),
    ])
    rules = rules_of(sch_review.rule_fields(ctx))
    assert "schematic.missing_value" in rules
    assert "schematic.missing_footprint" in rules
    assert "schematic.missing_datasheet" in rules
    assert "schematic.dnp" in rules


def test_missing_decoupling_is_detected():
    ctx = make_ctx({
        "+3V3": [node("U1", "8", "power_in"), node("R1", "1")],
        "GND": [node("U1", "4", "power_in")],
        "A": [node("U1", "1"), node("R1", "2")],
        "B": [node("U1", "2")],
        "C": [node("U1", "3")],
        "D": [node("U1", "5")],
        "E": [node("U1", "6")],
    })
    findings = sch_review.rule_decoupling(ctx)
    assert [f.rule for f in findings] == ["analog.missing_decoupling"]
    assert "+3V3" in findings[0].message


def test_decoupling_present_is_silent():
    ctx = make_ctx({
        "+3V3": [node("U1", "8", "power_in"), node("C1", "1")],
        "GND": [node("U1", "4", "power_in"), node("C1", "2")],
        "A": [node("U1", "1")], "B": [node("U1", "2")], "C": [node("U1", "3")],
        "D": [node("U1", "5")], "E": [node("U1", "6")],
    })
    assert sch_review.rule_decoupling(ctx) == []


def test_i2c_without_pullups():
    ctx = make_ctx({"SDA": [node("U1", "1"), node("U2", "1")],
                    "SCL": [node("U1", "2"), node("U2", "2"), node("R5", "1")]})
    findings = sch_review.rule_i2c_pullups(ctx)
    assert [f.location for f in findings] == ["SDA"]


def test_led_without_series_resistor():
    ctx = make_ctx(
        {"+5V": [node("D1", "2")], "GND": [node("D1", "1")]},
        symbols=[symbol("D1", value="red", lib_id="Device:LED")],
    )
    findings = sch_review.rule_led_series_resistor(ctx)
    assert [f.rule for f in findings] == ["analog.led_no_series_resistor"]


def test_led_with_series_resistor_is_silent():
    ctx = make_ctx(
        {"+5V": [node("R1", "1")], "N1": [node("R1", "2"), node("D1", "2")],
         "GND": [node("D1", "1")]},
        symbols=[symbol("D1", value="red", lib_id="Device:LED"), symbol("R1")],
    )
    assert sch_review.rule_led_series_resistor(ctx) == []


def test_missing_ground_and_supply():
    ctx = make_ctx({"A": [node("R1", "1"), node("R2", "1")]})
    rules = rules_of(sch_review.rule_power_nets(ctx))
    assert rules == {"power.no_ground", "power.no_supply"}


def test_net_without_driver():
    ctx = make_ctx({"CLK": [node("U1", "1", "input"), node("U2", "3", "input")]})
    findings = sch_review.rule_floating_inputs(ctx)
    assert [f.rule for f in findings] == ["net.no_driver"]
    assert "U1.1" in findings[0].message


def test_erc_violations_become_findings():
    erc = {
        "sheets": [
            {
                "path": "/",
                "violations": [
                    {"type": "power_pin_not_driven", "severity": "error",
                     "description": "Input Power pin not driven",
                     "items": [{"description": "Symbol #PWR01"}]},
                    {"type": "unconnected_wire_endpoint", "severity": "warning",
                     "description": "Unconnected wire endpoint", "items": []},
                ],
            }
        ]
    }
    findings = sch_review.rule_erc(make_ctx({}, erc=erc))
    assert [f.severity for f in findings] == ["error", "warning"]
    assert findings[0].rule == "erc.power_pin_not_driven"


def test_erc_unavailable_is_reported_as_info():
    findings = sch_review.rule_erc(make_ctx({}))
    assert [f.rule for f in findings] == ["erc.unavailable"]
    assert findings[0].severity == "info"


def test_review_of_the_example_project_is_clean(example_project):
    report = sch_review.review(example_project, use_cli=False)
    assert report["statistics"]["symbols"] == 5
    assert report["statistics"]["nets"] == 5
    assert report["summary"]["error"] == 0
    # only the "kicad-cli missing" note is expected without the container
    assert {f["rule"] for f in report["findings"]} == {"erc.unavailable"}


def test_info_lists_components_and_nets(example_project):
    data = sch_review.info(example_project, use_cli=False)
    assert {c["reference"] for c in data["components"]} == {"J1", "R1", "C1", "U1", "C2"}
    ground = next(n for n in data["nets"] if n["name"] == "GND")
    assert ground["class"] == "ground"
    assert ground["pin_count"] == 4


def test_a_broken_rule_does_not_break_the_report(monkeypatch, example_project):
    def exploding(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(sch_review, "RULES", [exploding, sch_review.rule_power_nets])
    report = sch_review.review(example_project, use_cli=False)
    rules = {f["rule"] for f in report["findings"]}
    assert "internal.exploding" in rules
