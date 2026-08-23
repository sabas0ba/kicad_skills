"""Rules are exercised through in-memory netlists so they stay fast and precise."""

from pathlib import Path

from eda_toolkit.kicad import sch_review
from eda_toolkit.kicad.schematic import Label, Pin, SchematicDoc, Sheet, Symbol, Wire


def make_ctx(nets, symbols=(), erc=None):
    netlist = {
        "source": "test",
        "nets": [
            {"name": name, "nodes": nodes, "pin_count": len(nodes)} for name, nodes in nets.items()
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
    ctx = make_ctx(
        {
            "unconnected-(U1-Pad7)": [node("U1", "7")],
            "SPARE_IO": [node("U1", "8")],
            "GND": [node("R1", "2"), node("C1", "2")],
        }
    )
    findings = {f.location: f for f in sch_review.rule_single_pin_nets(ctx)}
    assert set(findings) == {"unconnected-(U1-Pad7)", "SPARE_IO"}
    assert findings["unconnected-(U1-Pad7)"].severity == "warning"
    assert findings["SPARE_IO"].severity == "info"


def test_collapsing_folds_a_noisy_rule():
    from eda_toolkit.util import Finding, collapse_findings

    findings = [
        Finding("net.single_pin", "warning", f"net N{i}", location=f"N{i}") for i in range(20)
    ]
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
    ctx = make_ctx(
        {}, symbols=[symbol("U1", lib_id="X:Y", unit=1), symbol("U1", lib_id="X:Y", unit=2)]
    )
    assert sch_review.rule_annotation(ctx) == []


def test_missing_fields():
    ctx = make_ctx(
        {},
        symbols=[
            symbol("R1", value=""),
            symbol("C1", footprint=""),
            symbol("U1", lib_id="MCU:X", datasheet=""),
            symbol("R9", dnp=True),
        ],
    )
    rules = rules_of(sch_review.rule_fields(ctx))
    assert "schematic.missing_value" in rules
    assert "schematic.missing_footprint" in rules
    assert "schematic.missing_datasheet" in rules
    assert "schematic.dnp" in rules


def test_missing_decoupling_is_detected():
    ctx = make_ctx(
        {
            "+3V3": [node("U1", "8", "power_in"), node("R1", "1")],
            "GND": [node("U1", "4", "power_in")],
            "A": [node("U1", "1"), node("R1", "2")],
            "B": [node("U1", "2")],
            "C": [node("U1", "3")],
            "D": [node("U1", "5")],
            "E": [node("U1", "6")],
        }
    )
    findings = sch_review.rule_decoupling(ctx)
    assert [f.rule for f in findings] == ["analog.missing_decoupling"]
    assert "+3V3" in findings[0].message


def test_decoupling_present_is_silent():
    ctx = make_ctx(
        {
            "+3V3": [node("U1", "8", "power_in"), node("C1", "1")],
            "GND": [node("U1", "4", "power_in"), node("C1", "2")],
            "A": [node("U1", "1")],
            "B": [node("U1", "2")],
            "C": [node("U1", "3")],
            "D": [node("U1", "5")],
            "E": [node("U1", "6")],
        }
    )
    assert sch_review.rule_decoupling(ctx) == []


def test_decoupling_trusts_pin_types_over_names():
    """VREF is named like a rail; the pin types say it is an op-amp output."""
    ctx = make_ctx(
        {
            "VREF": [
                node("U2", "1", "output"),
                node("U2", "4", "input"),
                node("R5", "2"),
                node("U1", "3", "input"),
            ],
            "GND": [node("U2", "5", "power_in")],
        }
    )
    assert sch_review.rule_decoupling(ctx) == []


def test_decoupling_still_reads_names_when_types_are_absent():
    """A netlist with no pin types falls back to the old name-based judgment."""
    ctx = make_ctx(
        {
            "+5V": [node("U1", "8", ""), node("R1", "1", "")],
            "GND": [node("U1", "4", "")],
            "A": [node("U1", "1", "")],
            "B": [node("U1", "2", "")],
            "C": [node("U1", "3", "")],
            "D": [node("U1", "5", "")],
            "E": [node("U1", "6", "")],
        }
    )
    assert [f.rule for f in sch_review.rule_decoupling(ctx)] == ["analog.missing_decoupling"]


def test_no_dc_path_flags_a_floating_ac_output():
    ctx = make_ctx(
        {
            "OUT_AC": [node("C6", "2"), node("J3", "1")],
            "OUT": [node("U1", "1", "output"), node("C6", "1")],
            "IN_DC": [node("C3", "2"), node("R5", "1"), node("R1", "1")],
        }
    )
    findings = sch_review.rule_no_dc_path(ctx)
    assert [f.location for f in findings] == ["OUT_AC"]
    # IN_DC has a resistor on it, OUT has the op-amp: both have a DC path


def test_single_pin_net_with_a_no_connect_flag_is_a_decision():
    doc = sheet(symbols=[placed("U1", 0, 0, [pin("7", GRID, GRID)])])
    doc.no_connects = [(GRID, GRID)]
    ctx = make_ctx_with_docs({"unconnected-(U1-Pad7)": [node("U1", "7")]}, [doc])
    assert sch_review.rule_single_pin_nets(ctx) == []


def test_single_pin_net_without_the_flag_still_fires():
    doc = sheet(symbols=[placed("U1", 0, 0, [pin("7", GRID, GRID)])])
    ctx = make_ctx_with_docs({"unconnected-(U1-Pad7)": [node("U1", "7")]}, [doc])
    assert [f.rule for f in sch_review.rule_single_pin_nets(ctx)] == ["net.single_pin"]


def test_label_only_reads_the_wire_graph():
    """Ten stubs with labels fire; the same pins joined by wires do not."""
    stubs = sheet(
        symbols=[
            placed(f"R{i}", 0, i * 10, [pin("1", 0.0, i * 10.0), pin("2", 5.08, i * 10.0)])
            for i in range(1, 7)
        ],
        wires=[[(0.0, i * 10.0), (-2.54, i * 10.0)] for i in range(1, 7)]
        + [[(5.08, i * 10.0), (7.62, i * 10.0)] for i in range(1, 7)],
        labels=[Label(text=f"N{i}", kind="local", x=-2.54, y=i * 10.0) for i in range(1, 7)]
        + [Label(text=f"M{i}", kind="local", x=7.62, y=i * 10.0) for i in range(1, 7)],
    )
    findings = sch_review.rule_label_only(sheet_ctx(stubs))
    assert [f.rule for f in findings] == ["readability.label_only"]

    wired = sheet(
        symbols=[
            placed(f"R{i}", 0, i * 10, [pin("1", 0.0, i * 10.0), pin("2", 5.08, i * 10.0)])
            for i in range(1, 7)
        ]
        + [placed(f"C{i}", 20, i * 10, [pin("1", 20.0, i * 10.0)]) for i in range(1, 7)],
        wires=[[(5.08, i * 10.0), (20.0, i * 10.0)] for i in range(1, 7)]
        + [[(0.0, i * 10.0), (-2.54, i * 10.0)] for i in range(1, 7)],
        labels=[Label(text=f"N{i}", kind="local", x=-2.54, y=i * 10.0) for i in range(1, 7)],
    )
    assert sch_review.rule_label_only(sheet_ctx(wired)) == []


def test_i2c_without_pullups():
    ctx = make_ctx(
        {
            "SDA": [node("U1", "1"), node("U2", "1")],
            "SCL": [node("U1", "2"), node("U2", "2"), node("R5", "1")],
        }
    )
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
        {
            "+5V": [node("R1", "1")],
            "N1": [node("R1", "2"), node("D1", "2")],
            "GND": [node("D1", "1")],
        },
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
                    {
                        "type": "power_pin_not_driven",
                        "severity": "error",
                        "description": "Input Power pin not driven",
                        "items": [{"description": "Symbol #PWR01"}],
                    },
                    {
                        "type": "unconnected_wire_endpoint",
                        "severity": "warning",
                        "description": "Unconnected wire endpoint",
                        "items": [],
                    },
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


# -- readability -----------------------------------------------------------
#
# These rules read the drawing rather than the netlist, so they are exercised
# through hand-built sheets: the geometry is the input under test.

GRID = 1.27


def pin(number, x, y, type_="passive"):
    return Pin(number=number, name="~", electrical_type=type_, x=x, y=y, unit=1)


def placed(ref, x, y, pins=(), lib_id="Device:R", **kwargs):
    """A symbol with real sheet geometry (make_ctx's `symbol` has none)."""
    return Symbol(
        uuid=f"u-{ref}",
        lib_id=lib_id,
        reference=ref,
        value="10k",
        x=x,
        y=y,
        pins=list(pins),
        **kwargs,
    )


def sheet(symbols=(), wires=(), junctions=(), labels=(), paper=None, **kwargs):
    doc = SchematicDoc(path=Path("sheet.kicad_sch"), version=20231120, generator="test")
    doc.symbols = list(symbols)
    doc.wires = [Wire(points=list(points)) for points in wires]
    doc.junctions = list(junctions)
    doc.labels = list(labels)
    if paper:
        doc.paper, doc.paper_size = "A4", paper
    for key, value in kwargs.items():
        setattr(doc, key, value)
    return doc


def sheet_ctx(doc, nets=None, **kwargs):
    return make_ctx_with_docs(nets or {}, [doc], **kwargs)


def make_ctx_with_docs(nets, docs, thresholds=None):
    netlist = {
        "source": "test",
        "nets": [
            {"name": name, "nodes": nodes, "pin_count": len(nodes)} for name, nodes in nets.items()
        ],
    }
    return sch_review.ReviewContext.from_netlist(
        netlist,
        symbols=[s for doc in docs for s in doc.symbols],
        docs=docs,
        thresholds=thresholds,
    )


def test_off_grid_geometry_is_split_by_what_it_breaks():
    doc = sheet(
        symbols=[placed("R1", 0, 0, [pin("1", 10.0, 20.0), pin("2", GRID, 2 * GRID)])],
        wires=[[(10.0, 20.0), (10.0, 30.0)]],
        junctions=[(3 * GRID, 4 * GRID)],
    )
    findings = {f.rule: f for f in sch_review.rule_off_grid(sheet_ctx(doc))}
    assert set(findings) == {"readability.off_grid_pin", "readability.off_grid_wire"}
    assert findings["readability.off_grid_pin"].details["count"] == 1
    assert findings["readability.off_grid_pin"].severity == "warning"
    # the on-grid junction and the second pin are not reported
    assert findings["readability.off_grid_wire"].details["count"] == 2


def test_the_grid_is_configurable():
    doc = sheet(wires=[[(2.54, 2.54), (2.54, 5.08)]])
    assert sch_review.rule_off_grid(sheet_ctx(doc)) == []
    strict = sheet_ctx(doc, thresholds={"grid_mm": 2.0})
    assert [f.rule for f in sch_review.rule_off_grid(strict)] == ["readability.off_grid_wire"]


def test_diagonal_wires_are_reported_once():
    doc = sheet(wires=[[(0, 0), (GRID, GRID)], [(0, 0), (0, GRID)]])
    findings = sch_review.rule_diagonal_wires(sheet_ctx(doc))
    assert [f.rule for f in findings] == ["readability.diagonal_wire"]
    assert findings[0].details["count"] == 1


def test_a_tee_without_a_junction_is_not_a_connection():
    tee = [[(0, 0), (0, 10 * GRID)], [(0, 5 * GRID), (10 * GRID, 5 * GRID)]]
    findings = sch_review.rule_missing_junction(sheet_ctx(sheet(wires=tee)))
    assert [f.rule for f in findings] == ["readability.missing_junction"]
    assert findings[0].details["count"] == 1
    # the same geometry with the dot is silent
    with_dot = sheet(wires=tee, junctions=[(0, 5 * GRID)])
    assert sch_review.rule_missing_junction(sheet_ctx(with_dot)) == []


def test_crossing_wires_need_no_junction():
    crossing = [[(-10, 0), (10, 0)], [(0, -10), (0, 10)]]
    assert sch_review.rule_missing_junction(sheet_ctx(sheet(wires=crossing))) == []


def test_a_wire_that_reaches_a_pin_is_not_dangling():
    doc = sheet(
        symbols=[placed("R1", 0, 0, [pin("1", 0.0, 0.0)])],
        wires=[[(0.0, 0.0), (0.0, 10 * GRID)]],
        labels=[Label(text="OUT", kind="local", x=0.0, y=10 * GRID)],
    )
    assert sch_review.rule_dangling_wire(sheet_ctx(doc)) == []


def test_a_wire_that_reaches_nothing_is_dangling():
    doc = sheet(wires=[[(0.0, 0.0), (0.0, 10 * GRID)]])
    findings = sch_review.rule_dangling_wire(sheet_ctx(doc))
    assert findings[0].details["count"] == 2


def test_a_wire_into_a_sheet_pin_is_not_dangling():
    doc = sheet(wires=[[(0.0, 0.0), (0.0, 10 * GRID)]])
    doc.sheets = [Sheet(name="io", filename="io.kicad_sch", uuid="s", pins=[(0.0, 0.0)])]
    assert [f.details["count"] for f in sch_review.rule_dangling_wire(sheet_ctx(doc))] == [1]


def test_overlapping_symbols_are_reported_as_pairs():
    pins_a = [pin("1", 0, 0), pin("2", 4 * GRID, 0)]
    pins_b = [pin("1", GRID, 0), pin("2", 5 * GRID, 0)]
    doc = sheet(symbols=[placed("R1", 0, 0, pins_a), placed("R2", GRID, 0, pins_b)])
    findings = sch_review.rule_symbol_overlap(sheet_ctx(doc))
    assert findings[0].details["examples"] == ["sheet.kicad_sch:R1 / R2"]


def test_symbols_side_by_side_do_not_overlap():
    pins_a = [pin("1", 0, 0), pin("2", 4 * GRID, 0)]
    pins_b = [pin("1", 20 * GRID, 0), pin("2", 24 * GRID, 0)]
    doc = sheet(symbols=[placed("R1", 0, 0, pins_a), placed("R2", 20 * GRID, 0, pins_b)])
    assert sch_review.rule_symbol_overlap(sheet_ctx(doc)) == []


def test_items_beyond_the_page_border_are_reported():
    doc = sheet(
        symbols=[placed("R1", 400.0, 50.0, [pin("1", 400.0, 50.0)])],
        wires=[[(10.0, 10.0), (20.0, 20.0)]],
        paper=(297.0, 210.0),
    )
    findings = sch_review.rule_outside_page(sheet_ctx(doc))
    assert findings[0].details["count"] == 1
    assert "R1" in findings[0].details["examples"][0]


def test_a_sheet_with_no_page_size_is_not_judged():
    doc = sheet(symbols=[placed("R1", 4000.0, 50.0, [pin("1", 0, 0)])])
    assert sch_review.rule_outside_page(sheet_ctx(doc)) == []


def test_a_crowded_sheet_is_reported():
    doc = sheet(symbols=[placed(f"R{i}", 0, 0) for i in range(70)])
    findings = sch_review.rule_sheet_density(sheet_ctx(doc))
    assert findings[0].details == {"symbols": 70, "limit": 60}
    relaxed = sheet_ctx(doc, thresholds={"max_symbols_per_sheet": 100})
    assert sch_review.rule_sheet_density(relaxed) == []


def test_mostly_generated_net_names_are_reported():
    nets = {f"Net-(U1-Pad{i})": [node("U1", str(i)), node("R1", "1")] for i in range(12)}
    assert sch_review.rule_unnamed_nets(make_ctx(nets))[0].details["named"] == 0
    named = {f"SIG{i}": [node("U1", str(i)), node("R1", "1")] for i in range(12)}
    assert sch_review.rule_unnamed_nets(make_ctx(named)) == []


def test_a_handful_of_nets_is_not_judged_on_naming():
    nets = {f"Net-(U1-Pad{i})": [node("U1", str(i)), node("R1", "1")] for i in range(4)}
    assert sch_review.rule_unnamed_nets(make_ctx(nets)) == []


def test_an_empty_title_block_is_reported():
    doc = sheet()
    doc.title_block = {"title": "Buffer", "rev": "A"}
    findings = sch_review.rule_title_block(sheet_ctx(doc))
    assert findings[0].details["missing"] == ["date", "company"]


# -- specification ---------------------------------------------------------


def test_passives_must_state_their_ratings():
    parts = [
        symbol("R1", properties={"Reference": "R1", "Tolerance": "1%"}),
        symbol(
            "C1", value="100n", properties={"Reference": "C1", "Voltage": "50V", "Tolerance": "10%"}
        ),
        symbol("C2", value="100n"),
    ]
    findings = {
        f.location.split(":")[-1]: f for f in sch_review.rule_missing_ratings(make_ctx({}, parts))
    }
    assert set(findings) == {"R1", "C2"}
    assert findings["R1"].details["missing"] == ["power"]
    assert findings["C2"].details["missing"] == ["voltage", "tolerance"]


def test_active_parts_need_a_part_number():
    parts = [
        symbol("U1", value="LM321", properties={"MPN": "LM321MF/NOPB"}),
        symbol("U2", value="LM358"),
        symbol("R1"),
    ]
    findings = sch_review.rule_missing_part_number(make_ctx({}, parts))
    assert [f.location.split(":")[-1] for f in findings] == ["U2"]


def test_a_capacitor_is_derated_against_the_rail_it_sits_on():
    nets = {
        "+12V": [node("C1", "1"), node("C2", "1"), node("C3", "1")],
        "GND": [node("C1", "2"), node("C2", "2"), node("C3", "2")],
    }
    parts = [
        symbol("C1", value="100n", properties={"Voltage": "6.3V"}),  # under the rail
        symbol("C2", value="100n", properties={"Voltage": "16V"}),  # over it, but tight
        symbol("C3", value="100n", properties={"Voltage": "50V"}),  # comfortable
    ]
    findings = {
        f.location.split(":")[-1]: f
        for f in sch_review.rule_capacitor_derating(make_ctx(nets, parts))
    }
    assert findings["C1"].severity == "error"
    assert findings["C1"].details == {"rating_v": 6.3, "rail_v": 12.0}
    assert findings["C2"].severity == "warning"
    assert "C3" not in findings


def test_a_rail_that_names_no_voltage_derates_nothing():
    nets = {"VCC": [node("C1", "1")], "GND": [node("C1", "2")]}
    parts = [symbol("C1", value="100n", properties={"Voltage": "6.3V"})]
    assert sch_review.rule_capacitor_derating(make_ctx(nets, parts)) == []


def test_a_sheet_with_no_note_is_reported_once():
    doc = sheet(symbols=[placed("R1", 0, 0)])
    assert [f.rule for f in sch_review.rule_design_notes(sheet_ctx(doc))] == [
        "spec.no_design_notes"
    ]
    doc.texts = ["fc = 1/(2 pi R C) = 1.6 kHz"]
    assert sch_review.rule_design_notes(sheet_ctx(doc)) == []


def test_a_junction_inside_an_unbroken_wire_is_reported():
    """The dot in a wire's middle, not at a break: KiCad 9 connects one side."""
    unbroken = sheet(
        wires=[[(0.0, 0.0), (25.4, 0.0)], [(12.7, 0.0), (12.7, 12.7)]],
        junctions=[(12.7, 0.0)],
    )
    findings = sch_review.rule_wire_through_junction(sheet_ctx(unbroken))
    assert [f.rule for f in findings] == ["readability.wire_through_junction"]

    split = sheet(
        wires=[
            [(0.0, 0.0), (12.7, 0.0)],
            [(12.7, 0.0), (25.4, 0.0)],
            [(12.7, 0.0), (12.7, 12.7)],
        ],
        junctions=[(12.7, 0.0)],
    )
    assert sch_review.rule_wire_through_junction(sheet_ctx(split)) == []


def test_two_wires_on_one_line_are_reported():
    """An overlap along the line fires; segments merely chained do not."""
    overlapping = sheet(wires=[[(0.0, 0.0), (25.4, 0.0)], [(12.7, 0.0), (38.1, 0.0)]])
    findings = sch_review.rule_overlapping_wires(sheet_ctx(overlapping))
    assert [f.rule for f in findings] == ["readability.overlapping_wires"]

    chained = sheet(wires=[[(0.0, 0.0), (12.7, 0.0)], [(12.7, 0.0), (25.4, 0.0)]])
    assert sch_review.rule_overlapping_wires(sheet_ctx(chained)) == []


def test_a_connector_facing_away_from_its_signals_is_reported():
    """Pins right of the body, partners to the left: the row wants mirroring."""
    rows = [10.0, 12.54, 15.08, 17.62]
    connector = placed(
        "J1",
        50,
        13,
        [pin(str(i + 1), 55.0, y) for i, y in enumerate(rows)],
        lib_id="Connector:Conn_01x04_Pin",
    )
    mcu = placed(
        "U1",
        10,
        13,
        [pin(str(i + 1), 15.0, y) for i, y in enumerate(rows)],
        lib_id="MCU:GENERIC",
    )
    nets = {f"SIG{i}": [node("J1", str(i + 1)), node("U1", str(i + 1))] for i in range(4)}
    away = sheet_ctx(sheet(symbols=[connector, mcu]), nets)
    findings = sch_review.rule_facing_away(away)
    assert [f.location for f in findings] == ["J1"]

    mirrored = placed(
        "J1",
        50,
        13,
        [pin(str(i + 1), 45.0, y) for i, y in enumerate(rows)],
        lib_id="Connector:Conn_01x04_Pin",
        mirror="y",
    )
    faced = sheet_ctx(sheet(symbols=[mirrored, mcu]), nets)
    assert sch_review.rule_facing_away(faced) == []


def test_furniture_intrusion_is_reported():
    """A connector on the title block, and a note past the frame strip."""
    from eda_toolkit.kicad.schematic import Text

    doc = sheet(
        symbols=[
            placed("J1", 280, 195, [pin("1", 280.0, 195.0), pin("2", 280.0, 197.54)]),
            placed("R1", 100, 100, [pin("1", 100.0, 100.0), pin("2", 105.08, 100.0)]),
        ],
        paper=(297.0, 210.0),
        text_items=[Text("clearance is 0.2 mm", 20.0, 205.0, "left bottom")],
    )
    findings = sch_review.rule_margin_intrusion(sheet_ctx(doc))
    assert [f.rule for f in findings] == ["readability.margin_intrusion"]
    examples = findings[0].details["examples"]
    assert any("J1" in e for e in examples) and any("clearance" in e for e in examples)

    clear = sheet(
        symbols=[placed("R1", 100, 100, [pin("1", 100.0, 100.0), pin("2", 105.08, 100.0)])],
        paper=(297.0, 210.0),
        text_items=[Text("clearance is 0.2 mm", 20.0, 150.0, "left bottom")],
    )
    assert sch_review.rule_margin_intrusion(sheet_ctx(clear)) == []


def test_a_power_symbols_name_reaching_the_frame_is_reported():
    """The PWR_FLAG whose printed name crosses into the ruler strip."""
    flag = placed(
        "#FLG01",
        14.0,
        50.0,
        [pin("1", 14.0, 50.0, "power_in")],
        lib_id="power:PWR_FLAG",
    )
    flag.properties = {"Value": "PWR_FLAG"}
    flag.property_at = {"Value": (13.0, 48.0, "right")}
    doc = sheet(symbols=[flag], paper=(297.0, 210.0))
    findings = sch_review.rule_margin_intrusion(sheet_ctx(doc))
    assert [f.rule for f in findings] == ["readability.margin_intrusion"]
    assert any("PWR_FLAG" in e for e in findings[0].details["examples"])

    # the same flag well inside the frame is clear
    flag.property_at = {"Value": (40.0, 48.0, "right")}
    assert sch_review.rule_margin_intrusion(sheet_ctx(doc)) == []


def test_a_note_printed_over_a_symbol_is_reported():
    from eda_toolkit.kicad.schematic import Text

    doc = sheet(
        symbols=[placed("U1", 60, 45, [pin("1", 55.0, 40.0), pin("2", 65.0, 50.0)])],
        text_items=[Text("the regulator wants 1 uF on its output", 40.0, 45.0, "left")],
    )
    findings = sch_review.rule_text_over_symbol(sheet_ctx(doc))
    assert [f.rule for f in findings] == ["readability.text_over_symbol"]

    beside = sheet(
        symbols=[placed("U1", 60, 45, [pin("1", 55.0, 40.0), pin("2", 65.0, 50.0)])],
        text_items=[Text("the regulator wants 1 uF on its output", 40.0, 70.0, "left")],
    )
    assert sch_review.rule_text_over_symbol(sheet_ctx(beside)) == []


def test_a_rotated_power_symbol_is_reported():
    turned = sheet(
        symbols=[
            Symbol(
                uuid="u-p1",
                lib_id="power:GND",
                reference="#PWR01",
                value="GND",
                x=50,
                y=50,
                angle=90.0,
                in_bom=False,
                on_board=True,
            )
        ]
    )
    findings = sch_review.rule_power_symbol_orientation(sheet_ctx(turned))
    assert [f.rule for f in findings] == ["readability.power_symbol_orientation"]

    upright = sheet(
        symbols=[
            Symbol(
                uuid="u-p1",
                lib_id="power:GND",
                reference="#PWR01",
                value="GND",
                x=50,
                y=50,
                angle=0.0,
                in_bom=False,
                on_board=True,
            )
        ]
    )
    assert sch_review.rule_power_symbol_orientation(sheet_ctx(upright)) == []


def test_a_field_printed_across_a_net_is_reported():
    # R1's value sits on the wire leaving its own top pin; R2's steps aside.
    on_the_wire = sheet(
        symbols=[
            placed(
                "R1",
                50,
                50,
                [pin("1", 50.0, 45.0)],
                properties={"Value": "10k"},
                property_at={"Value": (50.0, 42.0, "")},
            )
        ],
        wires=[[(50.0, 45.0), (50.0, 38.0)]],
    )
    findings = sch_review.rule_text_over_wire(sheet_ctx(on_the_wire))
    assert [f.rule for f in findings] == ["readability.text_over_wire"]
    assert findings[0].details["count"] == 1
    assert "R1 Value" in findings[0].details["examples"][0]

    beside_it = sheet(
        symbols=[
            placed(
                "R2",
                50,
                50,
                [pin("1", 50.0, 45.0)],
                properties={"Value": "10k"},
                property_at={"Value": (53.0, 42.0, "left")},
            )
        ],
        wires=[[(50.0, 45.0), (50.0, 38.0)]],
    )
    assert sch_review.rule_text_over_wire(sheet_ctx(beside_it)) == []


def test_a_hidden_field_is_not_a_field_on_the_plot():
    # a hidden reference prints nowhere, so it cannot print across a net
    hidden = sheet(
        symbols=[
            placed(
                "#FLG01",
                50,
                50,
                [pin("1", 50.0, 45.0)],
                properties={"Value": "PWR_FLAG"},
                property_at={},
            )
        ],
        wires=[[(50.0, 45.0), (50.0, 38.0)]],
    )
    assert sch_review.rule_text_over_wire(sheet_ctx(hidden)) == []


def test_two_strings_drawn_through_each_other_are_reported():
    from eda_toolkit.kicad.schematic import Label

    doc = sheet(
        symbols=[
            placed(
                "R1",
                50,
                50,
                [pin("1", 50.0, 45.0)],
                properties={"Value": "10k"},
                property_at={"Value": (52.0, 40.0, "left")},
            )
        ]
    )
    doc.labels = [Label(text="LED_A", kind="local", x=53.0, y=40.0, justify="left")]
    findings = sch_review.rule_text_over_text(sheet_ctx(doc))
    assert [f.rule for f in findings] == ["readability.text_over_text"]
    assert "R1 Value over label LED_A" in findings[0].details["examples"][0]

    # the same label a row down clears it
    doc.labels = [Label(text="LED_A", kind="local", x=53.0, y=46.0, justify="left")]
    assert sch_review.rule_text_over_text(sheet_ctx(doc)) == []


def test_a_name_printed_down_a_two_pin_part_is_reported():
    from eda_toolkit.kicad.schematic import Label

    # a symbol's extent comes from its pins, so an upright two-pin part is
    # zero wide - and a label down its middle would read as clear
    doc = sheet(symbols=[placed("D1", 50, 50, [pin("1", 50.0, 46.0), pin("2", 50.0, 54.0)])])
    doc.labels = [Label(text="LED_A", kind="local", x=50.0, y=47.0, angle=90.0, justify="right")]
    findings = sch_review.rule_text_over_text(sheet_ctx(doc))
    assert [f.rule for f in findings] == ["readability.text_over_text"]
    assert findings[0].details["examples"] == ["sheet.kicad_sch:label LED_A over D1"]

    # reading the other way, off the part, is clear
    doc.labels = [Label(text="LED_A", kind="local", x=50.0, y=45.0, angle=90.0, justify="left")]
    assert sch_review.rule_text_over_text(sheet_ctx(doc)) == []


def test_a_note_that_ends_inside_the_title_block_is_reported():
    from eda_toolkit.kicad.schematic import Text

    # The anchor is 12 mm clear of the block; the sentence is 67 mm long.
    doc = sheet(paper=(297.0, 210.0))
    doc.text_items = [
        Text(
            text="The back plane necessarily opens under the module's two rows",
            x=165.1,
            y=168.0,
            justify="left top",
        )
    ]
    findings = sch_review.rule_margin_intrusion(sheet_ctx(doc))
    assert [f.rule for f in findings] == ["readability.margin_intrusion"]

    # the same sentence in the left column clears it
    doc.text_items[0].x = 20.32
    assert sch_review.rule_margin_intrusion(sheet_ctx(doc)) == []


def test_a_value_clear_of_the_pins_can_still_be_drawn_through_the_part():
    # An LED's two pins are 2.54 mm apart and its emission arrows reach
    # 4.6 mm off to one side. Measured against the pins the value is clear;
    # measured against the shape KiCad draws it is printed on the arrows.
    led = placed(
        "D1",
        50.0,
        50.0,
        [pin("1", 50.0, 48.73), pin("2", 50.0, 51.27)],
        lib_id="Device:LED",
        outline=[(48.73, 48.73), (54.6, 51.27)],
        properties={"Value": "green"},
        property_at={"Value": (52.0, 50.0, "left")},
    )
    findings = sch_review.rule_text_over_text(sheet_ctx(sheet(symbols=[led])))
    assert findings[0].details["examples"] == ["sheet.kicad_sch:D1 Value over D1"]

    # the same string on the other side of the part, where nothing is drawn
    led.property_at["Value"] = (45.0, 50.0, "right")
    assert sch_review.rule_text_over_text(sheet_ctx(sheet(symbols=[led]))) == []


def test_a_rotated_part_prints_its_fields_on_the_side_kicad_puts_them():
    # KiCad adds the symbol's angle to the field's own; at half a turn it
    # keeps the glyphs upright and swaps the justification instead. So this
    # `justify left` value prints to the *left* of its anchor, over the part.
    part = placed(
        "R1",
        50.0,
        50.0,
        [pin("1", 50.0, 47.0), pin("2", 50.0, 53.0)],
        angle=90.0,
        properties={"Value": "10k"},
        property_at={"Value": (54.0, 50.0, "left")},
        property_angle={"Value": 90.0},
    )
    findings = sch_review.rule_text_over_text(sheet_ctx(sheet(symbols=[part])))
    assert findings[0].details["examples"] == ["sheet.kicad_sch:R1 Value over R1"]

    # read literally - no flip - the box is 4.2 mm to the right and clear
    part.property_angle["Value"] = 0.0
    part.angle = 0.0
    assert sch_review.rule_text_over_text(sheet_ctx(sheet(symbols=[part]))) == []


def test_a_note_printed_through_a_designator_is_reported():
    from eda_toolkit.kicad.schematic import Text

    doc = sheet(
        symbols=[
            placed(
                "R1",
                80.0,
                80.0,
                properties={"Reference": "R1"},
                property_at={"Reference": (60.0, 40.0, "left")},
            )
        ]
    )
    doc.text_items = [Text(text="C1 100n bypasses the rail", x=55.0, y=39.0, justify="left top")]
    findings = sch_review.rule_text_over_text(sheet_ctx(doc))
    assert findings[0].details["examples"] == [
        "sheet.kicad_sch:R1 Reference over note 'C1 100n bypasses the rai'"
    ]

    # the same note two rows further down clears the designator
    doc.text_items = [Text(text="C1 100n bypasses the rail", x=55.0, y=44.0, justify="left top")]
    assert sch_review.rule_text_over_text(sheet_ctx(doc)) == []
