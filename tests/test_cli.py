import json

import pytest

from eda_toolkit import cli


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_help_and_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    with pytest.raises(SystemExit):
        cli.main([])


def test_sch_info_json(capsys, example_project):
    code, out, _ = run(["sch", "info", str(example_project), "--no-cli"], capsys)
    payload = json.loads(out)
    assert code == 0
    assert {c["reference"] for c in payload["components"]} == {"J1", "R1", "C1", "U1", "C2"}


def test_sch_review_text_mode(capsys, example_project):
    code, out, _ = run(["sch", "review", str(example_project), "--no-cli", "--text"], capsys)
    assert code == 0
    assert "## summary" in out
    assert "netlist_source" in out


def test_sch_review_writes_report(capsys, example_project, tmp_path):
    dest = tmp_path / "report.json"
    code, _out, _ = run(
        ["sch", "review", str(example_project), "--no-cli", "-o", str(dest)], capsys
    )
    assert code == 0
    assert json.loads(dest.read_text())["summary"]["error"] == 0


def test_pcb_review_threshold_override(capsys, example_project):
    code, out, _ = run(
        ["pcb", "review", str(example_project), "--no-cli", "--threshold", "min_track_mm=0.5"],
        capsys,
    )
    payload = json.loads(out)
    assert payload["thresholds"]["min_track_mm"] == 0.5
    assert code == 2  # tracks are now considered too thin -> errors
    assert any(f["rule"] == "track.below_minimum" for f in payload["findings"])


def test_pcb_review_bad_threshold(capsys, example_project):
    code, _, err = run(
        ["pcb", "review", str(example_project), "--no-cli", "--threshold", "nonsense"], capsys
    )
    assert code == 1
    assert "key=value" in err


def test_pcb_info(capsys, example_project):
    code, out, _ = run(["pcb", "info", str(example_project)], capsys)
    payload = json.loads(out)
    assert code == 0
    assert payload["layer_count"] == 2
    assert payload["size_mm"] == [40.0, 30.0]


def test_sim_lint_accepts_the_reference_deck(capsys, rc_netlist):
    code, out, _ = run(["sim", "lint", str(rc_netlist)], capsys)
    assert code == 0
    assert json.loads(out)["ok"] is True


def test_sim_lint_flags_a_broken_deck(capsys, tmp_path):
    deck = tmp_path / "broken.cir"
    deck.write_text("* nothing but a title\nR1 a b 1k\n")
    code, out, _ = run(["sim", "lint", str(deck)], capsys)
    assert code == 1
    problems = json.loads(out)["problems"]
    assert any("analysis directive" in p for p in problems)
    assert any(".end" in p for p in problems)


def test_datasheet_find_and_text(capsys, tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "d.pdf"
    c = canvas.Canvas(str(pdf))
    c.drawString(72, 720, "Electrical Characteristics of the EDA1234")
    c.save()

    code, out, _ = run(["datasheet", "find", str(pdf), "Electrical"], capsys)
    assert code == 0
    assert json.loads(out)[0]["page"] == 1

    code, out, _ = run(["datasheet", "info", str(pdf)], capsys)
    assert json.loads(out)["page_count"] == 1


def test_unknown_file_reports_a_clean_error(capsys, tmp_path):
    code, _, err = run(["sch", "review", str(tmp_path / "nope.kicad_sch"), "--no-cli"], capsys)
    assert code == 1
    assert "error:" in err
    assert "Traceback" not in err


def test_doctor(capsys):
    code, out, _ = run(["doctor"], capsys)
    payload = json.loads(out)
    assert "python_modules" in payload
    assert payload["python_modules"]["numpy"] is True
    assert code in (0, 1)  # 1 when kicad-cli/ngspice are absent (outside the container)


def test_every_subcommand_is_reachable():
    """A command whose parser is never registered is a command nobody can run."""

    def subcommands(parser):
        action = next(a for a in parser._actions if getattr(a, "choices", None))
        return action.choices

    top = subcommands(cli.build_parser())
    assert {"doctor", "report", "gate", "diff", "datasheet", "sim", "sch", "pcb"} <= set(top)
    assert set(subcommands(top["pcb"])) >= {"render", "glb", "fab", "review"}
    assert set(subcommands(top["sch"])) >= {"render", "pdf", "review", "bom"}


def test_background_choices_match_the_renderer():
    """cli.py spells the list out to stay import-light; it must not drift."""
    from eda_toolkit.kicad import render

    assert set(cli.BACKGROUND_CHOICES) == set(render.BACKGROUNDS)


def test_background_defaults_to_white_and_rejects_anything_else():
    parser = cli.build_parser()
    for argv in (
        ["pcb", "render", "board.kicad_pcb", "-o", "out"],
        ["pcb", "fab", "board.kicad_pcb", "-o", "out"],
    ):
        assert parser.parse_args(argv).background == "white"
        with pytest.raises(SystemExit):
            parser.parse_args([*argv, "--background", "puce"])


def test_fab_preview_is_off_by_default():
    args = cli.build_parser().parse_args(["pcb", "fab", "board.kicad_pcb", "-o", "out"])
    assert args.preview is False


def test_report_defaults_are_the_useful_ones():
    args = cli.build_parser().parse_args(["report", "board.kicad_pcb", "-o", "out"])
    assert args.func is cli.cmd_report
    assert (args.no_3d, args.no_per_layer, args.no_bom, args.glb) == (False, False, False, False)


def test_gate_passes_the_example_project(capsys, example_project):
    code, out, _ = run(["gate", str(example_project), "--no-cli"], capsys)
    payload = json.loads(out)
    assert code == 0
    assert payload["pass"] is True
    assert payload["policy"]["name"] == "default"


def test_gate_exits_two_when_the_policy_is_not_met(capsys, example_project, tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"name": "zero", "limits": {"info": 0}}), encoding="utf-8")
    code, out, _ = run(
        ["gate", str(example_project), "--no-cli", "--policy", str(policy), "--text"], capsys
    )
    assert code == 2
    assert "gate FAIL" in out
    assert "## blocking" in out


def test_gate_lists_its_builtin_policies(capsys):
    code, out, _ = run(["gate", "--list-policies"], capsys)
    assert code == 0
    assert set(json.loads(out)) == {"default", "ai-generated", "fabrication"}


def test_gate_needs_a_target(capsys):
    code, _out, err = run(["gate"], capsys)
    assert code == 1
    assert "needs a target" in err


def test_gate_writes_its_verdict(capsys, example_project, tmp_path):
    dest = tmp_path / "gate.json"
    code, _out, _ = run(
        ["gate", str(example_project), "--no-cli", "--policy", "ai-generated", "-o", str(dest)],
        capsys,
    )
    assert code == 0
    assert json.loads(dest.read_text())["policy"]["name"] == "ai-generated"


def test_sch_review_threshold_override(capsys, example_project):
    code, out, _ = run(
        ["sch", "review", str(example_project), "--no-cli", "--threshold", "grid_mm=2.54"], capsys
    )
    assert code == 0
    assert json.loads(out)["thresholds"]["grid_mm"] == 2.54


def test_a_threshold_that_is_not_a_number_is_a_usage_error(capsys):
    for spec in ("min_track_mm=wide", "min_track_mm"):
        code, _out, err = run(["pcb", "review", "x", "--threshold", spec], capsys)
        assert code == 1
        assert "--threshold" in err


def test_gate_lists_every_rule_with_what_it_checks(capsys):
    code, out, _ = run(["gate", "--list-rules"], capsys)
    assert code == 0
    catalogue = json.loads(out)
    entry = catalogue["readability.missing_junction"]
    assert entry["origin"] == "schematic"
    assert "junction" in entry["checks"]
    assert entry["blocks_under"] == ["ai-generated"]


def test_gate_list_rules_has_a_readable_form(capsys):
    code, out, _ = run(["gate", "--list-rules", "--text"], capsys)
    assert code == 0
    assert "## schematic" in out and "## board" in out
    assert "--threshold grid_mm=1.27" in out
