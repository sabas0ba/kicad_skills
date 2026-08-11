import math

import pytest

from eda_toolkit.kicad import netlist as netlist_mod
from eda_toolkit.kicad import schematic
from eda_toolkit.util import EdaError


def test_parse_example(example_sch):
    doc = schematic.parse(example_sch)
    assert doc.version == 20231120
    assert doc.title_block["title"] == "RC filter + unity gain buffer"
    refs = {s.reference for s in doc.symbols if not s.is_power}
    assert refs == {"J1", "R1", "C1", "U1", "C2"}
    r1 = doc.symbol_by_ref("R1")
    assert r1.value == "10k"
    assert r1.footprint == "Resistor_SMD:R_0805_2012Metric"
    assert len(r1.pins) == 2
    assert not r1.dnp


def test_power_symbols_are_flagged(example_sch):
    doc = schematic.parse(example_sch)
    power = [s for s in doc.symbols if s.is_power]
    assert {s.value for s in power} == {"GND", "+5V", "PWR_FLAG"}
    flags = [s for s in power if s.is_power_flag]
    assert len(flags) == 2
    assert all(not s.is_power_flag for s in power if s.value in ("GND", "+5V"))


@pytest.mark.parametrize(
    "angle,mirror,expected",
    [
        (0, "", (100.0, 96.19)),
        (90, "", (96.19, 100.0)),
        (180, "", (100.0, 103.81)),
        (270, "", (103.81, 100.0)),
        (0, "x", (100.0, 103.81)),
        (0, "y", (100.0, 96.19)),
    ],
)
def test_transform_pin(angle, mirror, expected):
    x, y = schematic.transform_pin(0.0, 3.81, 100.0, 100.0, angle, mirror)
    assert math.isclose(x, expected[0], abs_tol=1e-6)
    assert math.isclose(y, expected[1], abs_tol=1e-6)


def test_pin_positions_land_on_the_wires(example_sch):
    """R1 is rotated 90 degrees: its pins must be left and right of the body."""
    doc = schematic.parse(example_sch)
    r1 = doc.symbol_by_ref("R1")
    xs = sorted(round(p.x, 2) for p in r1.pins)
    ys = {round(p.y, 2) for p in r1.pins}
    assert len(ys) == 1  # horizontal
    assert xs == [100.33, 107.95]


def test_fallback_netlist_matches_expected_topology(example_project):
    docs = schematic.parse_project(example_project)
    nets = schematic.build_netlist(docs)["nets"]
    by_name = {n["name"]: {f"{x['ref']}.{x['pin']}" for x in n["nodes"]} for n in nets}
    assert by_name["IN"] == {"J1.1", "R1.1"}
    assert by_name["MID"] == {"R1.2", "C1.1", "U1.3"}
    assert by_name["OUT"] == {"U1.4", "U1.1", "J1.2"}
    assert by_name["GND"] == {"C1.2", "C2.2", "J1.3", "U1.2"}
    assert by_name["+5V"] == {"C2.1", "U1.5"}


def test_pwr_flag_does_not_name_a_net(example_project):
    docs = schematic.parse_project(example_project)
    names = {n["name"] for n in schematic.build_netlist(docs)["nets"]}
    assert "PWR_FLAG" not in names


def test_find_root_schematic_accepts_project_dir_and_pro(example_project, example_sch):
    assert schematic.find_root_schematic(example_project) == example_sch
    assert schematic.find_root_schematic(example_project / "example.kicad_pro") == example_sch
    assert schematic.find_root_schematic(example_sch) == example_sch


def test_find_root_schematic_rejects_other_files(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    with pytest.raises(EdaError):
        schematic.find_root_schematic(other)
    with pytest.raises(EdaError):
        schematic.find_root_schematic(tmp_path)


def test_classify_net():
    assert netlist_mod.classify_net("GND") == "ground"
    assert netlist_mod.classify_net("/GND") == "ground"
    assert netlist_mod.classify_net("AGND") == "ground"
    assert netlist_mod.classify_net("+5V") == "power"
    assert netlist_mod.classify_net("VDD") == "power"
    assert netlist_mod.classify_net("/MID") == "signal"
    assert netlist_mod.classify_net("SDA") == "signal"


def test_netlist_get_without_cli(example_project):
    data = netlist_mod.get(example_project, prefer_cli=False)
    assert data["source"] == "geometry-fallback"
    assert len(data["nets"]) == 5
    assert netlist_mod.nets_of(data, "R1") == {"1": "IN", "2": "MID"}


def test_the_page_size_is_resolved_from_its_name(example_sch):
    doc = schematic.parse(example_sch)
    assert doc.paper == "A4"
    assert doc.paper_size == (297.0, 210.0)


def test_a_portrait_page_swaps_the_axes(tmp_path):
    body = '(kicad_sch (version 20231120) (generator "t") (paper "A3" portrait))'
    path = tmp_path / "p.kicad_sch"
    path.write_text(body, encoding="utf-8")
    assert schematic.parse(path).paper_size == (297.0, 420.0)


def test_a_user_page_states_its_own_size(tmp_path):
    body = '(kicad_sch (version 20231120) (generator "t") (paper "User" 400 250))'
    path = tmp_path / "u.kicad_sch"
    path.write_text(body, encoding="utf-8")
    assert schematic.parse(path).paper_size == (400.0, 250.0)


def test_sheet_pins_and_bus_entries_are_collected(tmp_path):
    body = (
        '(kicad_sch (version 20231120) (generator "t")'
        "  (bus_entry (at 10 20) (size 2.54 2.54))"
        "  (sheet (at 30 40) (size 20 20)"
        '    (property "Sheetname" "io") (property "Sheetfile" "io.kicad_sch")'
        '    (pin "SDA" bidirectional (at 30 45 180))'
        '    (pin "SCL" bidirectional (at 30 47.54 180))))'
    )
    path = tmp_path / "s.kicad_sch"
    path.write_text(body, encoding="utf-8")
    doc = schematic.parse(path)
    assert doc.bus_entries == [((10.0, 20.0), (12.54, 22.54))]
    assert doc.sheets[0].pins == [(30.0, 45.0), (30.0, 47.54)]


def test_symbol_bbox_spans_the_pins(example_sch):
    doc = schematic.parse(example_sch)
    r1 = doc.symbol_by_ref("R1")
    x0, y0, x1, y1 = r1.bbox()
    assert (round(x1 - x0, 2), round(y1 - y0, 2)) == (7.62, 0.0)  # R1 is drawn rotated
    assert r1.bbox(margin=1.0)[0] == pytest.approx(x0 - 1.0)


def test_a_one_pin_symbol_has_no_bbox(example_sch):
    doc = schematic.parse(example_sch)
    gnd = next(s for s in doc.symbols if s.value == "GND")
    assert gnd.bbox() is None


@pytest.mark.parametrize(
    ("name", "volts"),
    [
        ("+3V3", 3.3),
        ("3V3", 3.3),
        ("+5V", 5.0),
        ("-12V", -12.0),
        ("/power/VDD_1V8", 1.8),
        ("VBUS", 5.0),
        ("VCC", None),
        ("GND", None),
        ("SDA", None),
    ],
)
def test_rail_voltage_is_read_off_the_net_name(name, volts):
    assert netlist_mod.rail_voltage(name) == volts
