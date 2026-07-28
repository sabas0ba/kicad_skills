"""Design diff. The comparisons are pure, so most of this needs no KiCad."""

import shutil

import pytest
from PIL import Image

from eda_toolkit.kicad import diff, pcb


def netlist(*nets):
    return {
        "nets": [
            {"name": name, "nodes": [{"ref": r, "pin": p} for r, p in pins]} for name, pins in nets
        ]
    }


def test_an_unchanged_netlist_says_so():
    before = netlist(("GND", [("R1", "1"), ("C1", "2")]))
    result = diff.compare_netlists(before, netlist(("GND", [("R1", "1"), ("C1", "2")])))
    assert result["identical"]
    assert result["unchanged"] == 1


def test_a_pin_moved_onto_another_net_shows_on_both():
    before = netlist(("GND", [("R1", "1"), ("C1", "2")]), ("VCC", [("U1", "8")]))
    after = netlist(("GND", [("R1", "1")]), ("VCC", [("U1", "8"), ("C1", "2")]))
    result = diff.compare_netlists(before, after)

    assert not result["identical"]
    changed = {entry["net"]: entry for entry in result["changed"]}
    assert changed["GND"]["removed_pins"] == ["C1.2"]
    assert changed["VCC"]["added_pins"] == ["C1.2"]


def test_a_renamed_net_is_a_rename_not_a_delete_and_an_add():
    before = netlist(("Net-(R1-Pad2)", [("R1", "2"), ("C1", "1")]))
    after = netlist(("VOUT", [("R1", "2"), ("C1", "1")]))
    result = diff.compare_netlists(before, after)

    assert result["renamed"] == [{"from": "Net-(R1-Pad2)", "to": "VOUT", "pins": ["C1.1", "R1.2"]}]
    assert result["added"] == [] and result["removed"] == []
    assert not result["identical"]  # a rename is still a change worth reading


def test_a_new_net_is_not_mistaken_for_a_rename():
    before = netlist(("GND", [("R1", "1")]))
    after = netlist(("GND", [("R1", "1")]), ("VCC", [("U1", "8")]))
    result = diff.compare_netlists(before, after)

    assert result["renamed"] == []
    assert [entry["net"] for entry in result["added"]] == ["VCC"]


def test_two_empty_nets_are_not_matched_up_as_a_rename():
    """Empty node sets are all equal; matching on them would invent renames."""
    before = netlist(("OLD_A", []), ("OLD_B", []))
    after = netlist(("NEW_A", []), ("NEW_B", []))
    result = diff.compare_netlists(before, after)

    assert result["renamed"] == []
    assert len(result["added"]) == 2 and len(result["removed"]) == 2


def component(reference, value="1k", footprint="R_0805", dnp=False, lib_id="Device:R"):
    return {
        "reference": reference,
        "value": value,
        "footprint": footprint,
        "dnp": dnp,
        "lib_id": lib_id,
    }


def test_component_value_and_footprint_changes_are_named():
    before = [component("R1", "1k"), component("C1", "100n", "C_0603")]
    after = [component("R1", "4k7"), component("C1", "100n", "C_0805")]
    result = diff.compare_components(before, after)

    changed = {entry["reference"]: entry["fields"] for entry in result["changed"]}
    assert changed["R1"]["value"] == {"from": "1k", "to": "4k7"}
    assert changed["C1"]["footprint"] == {"from": "C_0603", "to": "C_0805"}
    assert not result["identical"]


def test_marking_a_part_dnp_counts_as_a_change():
    result = diff.compare_components([component("R9")], [component("R9", dnp=True)])
    assert result["changed"][0]["fields"]["dnp"] == {"from": False, "to": True}


def test_added_and_removed_parts_carry_enough_to_read():
    result = diff.compare_components([component("R1")], [component("R2", "10k")])
    assert result["added"] == [{"reference": "R2", "value": "10k", "footprint": "R_0805"}]
    assert result["removed"] == [{"reference": "R1", "value": "1k", "footprint": "R_0805"}]


def test_moving_a_part_on_the_schematic_is_not_a_component_change():
    """Position is not in the tracked fields: a tidy-up must not read as a change."""
    before = [{**component("R1"), "position": [10, 10]}]
    after = [{**component("R1"), "position": [50, 90]}]
    assert diff.compare_components(before, after)["identical"]


# -- boards ----------------------------------------------------------------


def _board(example_pcb, **moves):
    board = pcb.parse(example_pcb)
    for ref, (dx, dy) in moves.items():
        footprint = board.footprint_by_ref(ref)
        footprint.x += dx
        footprint.y += dy
    return board


def test_a_board_compared_with_itself_is_identical(example_pcb):
    result = diff.compare_boards(pcb.parse(example_pcb), pcb.parse(example_pcb))
    assert result["identical"]
    assert result["moved"] == []


def test_a_moved_footprint_is_reported_with_its_distance(example_pcb):
    result = diff.compare_boards(pcb.parse(example_pcb), _board(example_pcb, R1=(3.0, 4.0)))
    assert not result["identical"]
    assert result["moved"][0] == {"reference": "R1", "moved_mm": 5.0}  # 3-4-5


def test_a_nudge_below_the_threshold_is_not_a_move(example_pcb):
    result = diff.compare_boards(pcb.parse(example_pcb), _board(example_pcb, R1=(0.001, 0.0)))
    assert result["moved"] == []


def test_a_rotated_part_is_a_change_even_where_it_stands_still(example_pcb):
    board = pcb.parse(example_pcb)
    board.footprint_by_ref("R1").angle += 90
    result = diff.compare_boards(pcb.parse(example_pcb), board)
    assert result["moved"][0]["angle"]["to"] == result["moved"][0]["angle"]["from"] + 90


def test_a_part_that_left_the_board_is_reported(example_pcb):
    board = pcb.parse(example_pcb)
    board.footprints = [fp for fp in board.footprints if fp.ref != "C2"]
    result = diff.compare_boards(pcb.parse(example_pcb), board)
    assert result["unplaced"] == ["C2"]
    assert (
        result["statistics"]["footprints"]["to"] == result["statistics"]["footprints"]["from"] - 1
    )


# -- images ----------------------------------------------------------------


def _png(path, colour, size=(60, 40)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)


def test_identical_renders_produce_no_diff_image(tmp_path):
    _png(tmp_path / "old" / "front.png", (255, 255, 255))
    _png(tmp_path / "new" / "front.png", (255, 255, 255))
    result = diff.compare_images(tmp_path / "old", tmp_path / "new", tmp_path / "diff")
    assert result == [{"view": "front", "changed_pixels": 0, "changed_pct": 0.0}]


def test_a_changed_render_is_measured_and_drawn(tmp_path):
    _png(tmp_path / "old" / "front.png", (255, 255, 255))
    _png(tmp_path / "new" / "front.png", (255, 255, 255))
    with Image.open(tmp_path / "new" / "front.png") as image:
        image.paste((0, 0, 0), (0, 0, 30, 40))  # blacken half of it
        image.save(tmp_path / "new" / "front.png")

    result = diff.compare_images(tmp_path / "old", tmp_path / "new", tmp_path / "diff")[0]
    assert result["changed_pct"] == pytest.approx(50.0, abs=1.0)
    assert (tmp_path / "diff" / "front-diff.png").exists()


def test_a_plot_that_changed_size_is_reported_rather_than_compared(tmp_path):
    _png(tmp_path / "old" / "front.png", (255, 255, 255), size=(60, 40))
    _png(tmp_path / "new" / "front.png", (255, 255, 255), size=(80, 40))
    result = diff.compare_images(tmp_path / "old", tmp_path / "new", tmp_path / "diff")
    assert "size changed" in result[0]["error"]


def test_a_view_only_one_revision_has_is_reported(tmp_path):
    _png(tmp_path / "old" / "front.png", (255, 255, 255))
    (tmp_path / "new").mkdir()
    result = diff.compare_images(tmp_path / "old", tmp_path / "new", tmp_path / "diff")
    assert result[0]["error"] == "only in the old revision"


# -- the whole thing -------------------------------------------------------


def test_markdown_reads_as_a_change_summary(tmp_path):
    result = {
        "old": "a",
        "new": "b",
        "out_dir": str(tmp_path),
        "identical": False,
        "errors": [],
        "sections": {
            "schematic": {
                "nets": diff.compare_netlists(
                    netlist(("GND", [("R1", "1")])), netlist(("GND", [("R1", "1"), ("C1", "2")]))
                ),
                "components": diff.compare_components([component("R1")], [component("R1", "4k7")]),
                "identical": False,
            },
        },
    }
    text = diff.render_markdown(result)
    assert "# Design diff" in text
    assert "**GND**: gained C1.2" in text
    assert "**R1**: value '1k' -> '4k7'" in text


def test_a_project_diffed_against_itself_reports_no_change(tmp_path, example_project):
    copy = tmp_path / "same"
    shutil.copytree(example_project, copy)
    result = diff.build(example_project, copy, tmp_path / "out", images=False)

    assert result["identical"], result
    assert result["sections"]["board"]["identical"]
    assert "Nothing changed" in (tmp_path / "out" / "diff.md").read_text(encoding="utf-8")


@pytest.mark.kicad
def test_a_real_change_is_found_end_to_end(tmp_path, example_project):
    """Move a part and retag a resistor: the diff must name both."""
    changed = tmp_path / "changed"
    shutil.copytree(example_project, changed)

    board_file = changed / "example.kicad_pcb"
    text = board_file.read_text()
    moved = text.replace("(at 112 92)", "(at 115 96)", 1)
    assert moved != text, "the fixture moved; this test needs a real footprint position"
    board_file.write_text(moved)

    sch_file = changed / "example.kicad_sch"
    schematic_text = sch_file.read_text()
    retagged = schematic_text.replace('(property "Value" "10k"', '(property "Value" "4k7"', 1)
    assert retagged != schematic_text, "the fixture changed; R1 is no longer 10k"
    sch_file.write_text(retagged)

    result = diff.build(example_project, changed, tmp_path / "out", images=False)

    assert not result["identical"]
    values = [
        change
        for entry in result["sections"]["schematic"]["components"]["changed"]
        for change in entry["fields"].values()
    ]
    assert {"from": "10k", "to": "4k7"} in values
    assert result["sections"]["board"]["moved"][0]["moved_mm"] == 5.0
    text = (tmp_path / "out" / "diff.md").read_text(encoding="utf-8")
    assert "4k7" in text and "5.0 mm" in text


def test_a_value_change_alone_makes_the_diff_not_identical(tmp_path, example_project):
    """A re-valued resistor moves no nets. The verdict must still notice it.

    The first version of this reported the whole diff as identical, because the
    top-level verdict only read the connectivity half of the section.
    """
    changed = tmp_path / "changed"
    shutil.copytree(example_project, changed)
    sch_file = changed / "example.kicad_sch"
    text = sch_file.read_text()
    retagged = text.replace('(property "Value" "10k"', '(property "Value" "4k7"', 1)
    assert retagged != text, "the fixture changed; R1 is no longer 10k"
    sch_file.write_text(retagged)

    result = diff.build(example_project, changed, tmp_path / "out", images=False)

    assert result["sections"]["schematic"]["nets"]["identical"]  # nothing moved
    assert not result["sections"]["schematic"]["identical"]
    assert not result["identical"]
