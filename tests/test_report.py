"""The report renderers are pure: they turn a collected payload into a page."""

from pathlib import Path

import pytest

from eda_toolkit import report


def payload(tmp_path, **sections):
    return {
        "target": "board",
        "out_dir": str(tmp_path),
        "title": "Demo board",
        "sections": sections,
        "errors": [],
    }


def review(findings, **summary):
    counts = {"error": 0, "warning": 0, "info": 0, **summary}
    return {"summary": counts, "findings": findings}


def test_markdown_has_a_verdict_and_the_findings_table(tmp_path):
    data = payload(
        tmp_path,
        schematic_review=review(
            [{"rule": "power.no_decoupling", "severity": "warning", "message": "U1 has none"}],
            warning=1,
        ),
    )
    text = report.render_markdown(data)
    assert "# Demo board" in text
    assert "## Verdict" in text
    assert "1 warning" in text
    assert "`power.no_decoupling`" in text


def test_pipes_in_a_message_do_not_break_the_table(tmp_path):
    data = payload(
        tmp_path,
        board_review=review(
            [{"rule": "drc.x", "severity": "error", "message": "a | b", "location": "F.Cu"}],
            error=1,
        ),
    )
    lines = report.render_markdown(data).splitlines()
    row = next(line for line in lines if "drc.x" in line)
    assert r"a \| b" in row
    assert row.replace(r"\|", "").count("|") == 5  # four cells, not five


def test_findings_are_capped_and_the_remainder_is_pointed_at(tmp_path):
    findings = [
        {"rule": "r", "severity": "info", "message": f"m{i}", "location": ""} for i in range(40)
    ]
    text = report.render_markdown(payload(tmp_path, board_review=review(findings, info=40)))
    assert text.count("| info |") == 25
    assert "15 more in report.json" in text


def test_image_paths_are_relative_to_the_report(tmp_path):
    images = {
        "images": [str(tmp_path / "schematic" / "sheet.png")],
        "pdf": str(tmp_path / "schematic" / "schematic.pdf"),
    }
    text = report.render_markdown(payload(tmp_path, schematic_images=images))
    assert "![schematic](schematic/sheet.png)" in text
    assert "[schematic PDF](schematic/schematic.pdf)" in text


def test_paths_outside_the_report_directory_are_left_alone(tmp_path):
    images = {"images": ["/elsewhere/sheet.png"]}
    text = report.render_markdown(payload(tmp_path, schematic_images=images))
    assert "/elsewhere/sheet.png" in text


def test_board_section_links_the_contact_sheet_and_the_glb(tmp_path):
    board_images = {
        "images": [
            {"view": "front", "path": str(tmp_path / "board" / "front.png")},
            {"view": "3d-top", "path": str(tmp_path / "board" / "3d-top.png")},
        ],
        "contact_sheet": str(tmp_path / "board" / "contact-sheet.png"),
        "glb": str(tmp_path / "board" / "board.glb"),
    }
    text = report.render_markdown(payload(tmp_path, board_images=board_images))
    assert "![all layers](board/contact-sheet.png)" in text
    assert "![3d-top](board/3d-top.png)" in text
    # the flat plots are already on the contact sheet, only the 3D views repeat
    assert "board/front.png" not in text
    assert "[3D model (GLB)](board/board.glb)" in text


def test_html_escapes_and_colours_severities(tmp_path):
    data = payload(
        tmp_path,
        board_review=review(
            [{"rule": "drc.clearance", "severity": "error", "message": "<b>too close</b>"}],
            error=1,
        ),
    )
    html = report.render_html(data)
    assert "&lt;b&gt;too close&lt;/b&gt;" in html
    assert report.SEVERITY_COLOUR["error"] in html
    assert html.startswith("<!doctype html>")


def test_failed_sections_are_shown_not_swallowed(tmp_path):
    data = payload(tmp_path)
    data["errors"] = [{"section": "board_images", "error": "EdaError: no board"}]
    assert "## Sections that failed" in report.render_markdown(data)
    assert "board_images" in report.render_html(data)


def test_simulation_plots_are_embedded(tmp_path):
    simulation = {
        "plots": [{"analysis": "ac", "image": str(tmp_path / "simulation" / "ac.png")}],
    }
    text = report.render_markdown(payload(tmp_path, simulation=simulation))
    assert "![ac](simulation/ac.png)" in text


def test_a_section_that_raises_is_recorded_and_the_rest_still_run(tmp_path, monkeypatch):
    """One broken export must not cost you the whole report."""
    from eda_toolkit.kicad import pcb, schematic

    monkeypatch.setattr(schematic, "find_root_schematic", _raise)
    monkeypatch.setattr(pcb, "find_board", _raise)
    result = report.build(tmp_path, tmp_path / "out")
    assert (tmp_path / "out" / "report.md").exists()
    assert (tmp_path / "out" / "report.html").exists()
    assert result["sections"] == {}


def _raise(*_args, **_kwargs):
    raise RuntimeError("nope")


@pytest.mark.kicad
def test_build_produces_a_report_for_the_example_project(tmp_path, example_project):
    result = report.build(example_project, tmp_path / "out", dpi=60, three_d=False)
    assert "schematic_review" in result["sections"]
    assert "board_review" in result["sections"]
    markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "## Verdict" in markdown
    assert Path(result["sections"]["schematic_images"]["pdf"]).exists()


def test_copper_section_lists_the_current_limited_nets(tmp_path):
    electrical = {
        "temperature_rise_c": 10.0,
        "nets": [
            {
                "net": "+5V",
                "narrowest_mm": 0.25,
                "narrowest_layer": "F.Cu",
                "current_a": 0.92,
                "length_mm": 48.5,
                "resistance_mohm": 93.1,
            }
        ],
        "impedance": [
            {
                "layer": "F.Cu",
                "kind": "microstrip",
                "height_mm": 0.1,
                "epsilon_r": 4.5,
                "width_50r_mm": 0.141,
                "width_90r_diff_mm": 0.13,
            }
        ],
    }
    text = report.render_markdown(payload(tmp_path, electrical=electrical))
    assert "## Copper" in text
    assert "| +5V | 0.25 mm | F.Cu | 0.92 A |" in text
    assert "| F.Cu | microstrip | 0.1 mm | 4.5 | 0.141 mm | 0.13 mm |" in text
    assert "0.92 A" in report.render_html(payload(tmp_path, electrical=electrical))
