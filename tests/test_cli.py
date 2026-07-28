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
    assert {"doctor", "report", "datasheet", "sim", "sch", "pcb"} <= set(top)
    assert set(subcommands(top["pcb"])) >= {"render", "glb", "fab", "review"}
    assert set(subcommands(top["sch"])) >= {"render", "pdf", "review", "bom"}


def test_report_defaults_are_the_useful_ones():
    args = cli.build_parser().parse_args(["report", "board.kicad_pcb", "-o", "out"])
    assert args.func is cli.cmd_report
    assert (args.no_3d, args.no_per_layer, args.no_bom, args.glb) == (False, False, False, False)
