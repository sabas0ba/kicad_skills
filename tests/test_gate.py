"""The gate turns a pile of findings into one verdict, so what it *excludes*
from that verdict matters as much as what it blocks on."""

import json

import pytest

from eda_toolkit import gate
from eda_toolkit.util import EdaError


def finding(rule, severity="info", location=""):
    return {"rule": rule, "severity": severity, "message": rule, "location": location}


def test_builtin_policies_resolve_by_name():
    assert gate.load_policy(None).name == "default"
    assert gate.load_policy("ai-generated").name == "ai-generated"


def test_an_unknown_policy_names_the_ones_that_exist():
    with pytest.raises(EdaError) as exc:
        gate.load_policy("strictest")
    assert "ai-generated" in str(exc.value)


def test_a_policy_promotes_the_severity_a_rule_reported():
    policy = gate.BUILTIN_POLICIES["ai-generated"]
    assert policy.effective_severity(finding("readability.diagonal_wire")) == "error"
    assert policy.effective_severity(finding("layout.connection_span", "warning")) == "error"
    assert policy.effective_severity(finding("erc.power_pin_not_driven", "error")) == "error"


def test_the_most_specific_pattern_wins():
    policy = gate.policy_from_dict(
        {
            "extends": "ai-generated",
            "severity": {"readability.diagonal_wire": "info"},
        }
    )
    assert policy.effective_severity(finding("readability.diagonal_wire")) == "info"
    assert policy.effective_severity(finding("readability.dangling_wire")) == "error"


def test_context_rules_are_never_promoted():
    """`board.size` is a fact about the board, not a defect - a policy that
    turned it into an error would be one no design could ever pass."""
    policy = gate.BUILTIN_POLICIES["ai-generated"]
    applied = gate._apply(policy, [finding("board.size"), finding("layout.odd_rotation")], "board")
    assert [f["severity"] for f in applied] == ["info", "error"]


def test_a_waiver_without_a_reason_is_refused():
    with pytest.raises(EdaError) as exc:
        gate.policy_from_dict({"waivers": [{"rule": "drc.lib_footprint_mismatch"}]})
    assert "reason" in str(exc.value)


def test_a_waived_finding_leaves_the_verdict_but_stays_on_the_record():
    policy = gate.policy_from_dict(
        {
            "severity": {"silk.over_pad": "error"},
            "waivers": [{"rule": "silk.*", "reason": "the panel frame prints over the fiducials"}],
        }
    )
    applied = gate._apply(policy, [finding("silk.over_pad", "warning")], "board")
    assert applied[0]["waiver"]["reason"].startswith("the panel frame")


def test_a_waiver_can_be_narrowed_to_one_location():
    policy = gate.policy_from_dict(
        {"waivers": [{"rule": "route.stub", "location": "B.Cu", "reason": "test coupon"}]}
    )
    applied = gate._apply(
        policy,
        [finding("route.stub", location="GND on B.Cu at (1, 2)"), finding("route.stub", "warning")],
        "board",
    )
    assert [("waiver" in f) for f in applied] == [True, False]


def test_a_bad_severity_or_limit_is_refused():
    with pytest.raises(EdaError):
        gate.policy_from_dict({"severity": {"route.stub": "fatal"}})
    with pytest.raises(EdaError):
        gate.policy_from_dict({"limits": {"error": -1}})
    with pytest.raises(EdaError):
        gate.policy_from_dict({"thresholds": {"pcb": {"min_track_mm": 0.2}}})


def test_a_policy_file_can_be_json_or_toml(tmp_path):
    body = {
        "name": "house",
        "extends": "fabrication",
        "limits": {"error": 0, "warning": 2},
        "waivers": [{"rule": "drc.lib_footprint_mismatch", "reason": "simplified footprints"}],
    }
    as_json = tmp_path / "policy.json"
    as_json.write_text(json.dumps(body), encoding="utf-8")
    from_json = gate.load_policy(as_json)
    assert from_json.name == "house"
    assert from_json.limits == {"error": 0, "warning": 2}

    as_toml = tmp_path / "policy.toml"
    as_toml.write_text(
        'name = "house"\nextends = "fabrication"\n'
        "[limits]\nerror = 0\nwarning = 2\n"
        '[[waivers]]\nrule = "drc.lib_footprint_mismatch"\nreason = "simplified footprints"\n',
        encoding="utf-8",
    )
    assert gate.load_policy(as_toml).to_dict() == from_json.to_dict()


def test_the_example_project_passes_every_builtin_policy(example_project):
    for name in gate.BUILTIN_POLICIES:
        report = gate.run(example_project, policy=gate.BUILTIN_POLICIES[name], use_cli=False)
        assert report["pass"], f"{name}: {[f['rule'] for f in report['blocking']]}"
        assert report["counts"]["error"] == 0


def test_a_limit_that_is_exceeded_names_the_blocking_findings(example_project):
    """Nothing about the example project is an error, so the only way to make it
    fail is to say that its informational findings are not acceptable either."""
    policy = gate.policy_from_dict({"name": "zero-tolerance", "limits": {"info": 0}})
    report = gate.run(example_project, policy=policy, use_cli=False)
    assert not report["pass"]
    assert report["exceeded"]["info"]["limit"] == 0
    assert {f["origin"] for f in report["blocking"]} == {"schematic", "board"}


def test_a_missing_board_is_skipped_rather_than_failed(tmp_path, example_sch):
    project = tmp_path / "sch-only"
    project.mkdir()
    (project / "example.kicad_sch").write_text(example_sch.read_text(), encoding="utf-8")
    report = gate.run(project, use_cli=False)
    assert report["pass"]
    assert "no .kicad_pcb found" in report["board"]["skipped"]
    assert report["schematic"]["summary"]["error"] == 0


def test_thresholds_reach_the_reviews(example_project):
    policy = gate.policy_from_dict(
        {
            "thresholds": {"board": {"min_track_mm": 5.0}},
            "severity": {"track.below_minimum": "error"},
        }
    )
    report = gate.run(example_project, policy=policy, use_cli=False)
    assert not report["pass"]
    assert [f["rule"] for f in report["blocking"]] == ["track.below_minimum"]


def test_a_target_with_no_design_at_all_is_an_error(tmp_path):
    """Both stages skipping would otherwise report a clean bill of health for nothing."""
    with pytest.raises(EdaError) as exc:
        gate.run(tmp_path, use_cli=False)
    assert "no schematic and no board" in str(exc.value)
