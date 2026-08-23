"""`RULE_SPEC` is the specification of what the reviews check.

Documentation that describes behaviour is documentation that will eventually
describe behaviour the code no longer has. These tests read the rule modules
themselves - every ``Finding(...)`` they construct - and require the two sets to
agree exactly, in both directions. Adding a rule without saying what it checks
fails here, and so does describing a rule that no longer exists.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path

import pytest

from eda_toolkit import gate
from eda_toolkit.kicad import pcb_review, sch_review
from eda_toolkit.util import SEVERITIES

MODULES = {"schematic": sch_review, "board": pcb_review}
# Constructors whose first argument is the rule id.
EMITTERS = {"Finding", "_group_finding"}
# Helpers that only pass a rule id through: their own body says nothing about
# which rules exist, and their call sites are what carry the literal.
FORWARDERS = {"_group_finding"}


def emitted_ids(module) -> set[str]:
    """Every rule id the module's source can produce.

    An id built with an f-string (``f"erc.{type}"``) is reduced to its literal
    prefix plus ``*``. The one construction that has no literal prefix at all -
    pcb_review's DRC buckets - is declared in ``DYNAMIC_RULE_IDS`` instead, which
    the tests below still require to line up with ``RULE_SPEC``.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    inside_forwarder = {
        id(node)
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and fn.name in FORWARDERS
        for node in ast.walk(fn)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in inside_forwarder:
            continue
        if getattr(node.func, "id", getattr(node.func, "attr", "")) not in EMITTERS:
            continue
        first = next(
            (kw.value for kw in node.keywords if kw.arg == "rule"),
            node.args[0] if node.args else None,
        )
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.add(first.value)
        elif isinstance(first, ast.JoinedStr) and isinstance(first.values[0], ast.Constant):
            found.add(f"{first.values[0].value}*")
        elif first is not None:
            assert module.DYNAMIC_RULE_IDS, (
                f"{module.__name__}: a rule id built as {ast.dump(first)} cannot be "
                "read out of the source - declare its families in DYNAMIC_RULE_IDS"
            )
            found.update(module.DYNAMIC_RULE_IDS)
    return found | set(module.DYNAMIC_RULE_IDS)


def covers(specified: str, emitted: str) -> bool:
    """Whether an emitted id (literal or ``prefix*`` family) accounts for a spec entry."""
    return specified == emitted or fnmatch.fnmatch(specified, emitted)


@pytest.mark.parametrize("name", sorted(MODULES))
def test_every_rule_the_code_emits_is_specified(name):
    module = MODULES[name]
    missing = [
        emitted
        for emitted in emitted_ids(module)
        if not any(covers(spec, emitted) for spec in module.RULE_SPEC)
    ]
    assert not missing, (
        f"{name} emits {sorted(missing)} with no RULE_SPEC entry - say what the rule checks"
    )


@pytest.mark.parametrize("name", sorted(MODULES))
def test_every_specified_rule_is_one_the_code_emits(name):
    module = MODULES[name]
    emitted = emitted_ids(module)
    stale = [spec for spec in module.RULE_SPEC if not any(covers(spec, e) for e in emitted)]
    assert not stale, f"{name} documents {sorted(stale)}, which no rule produces any more"


@pytest.mark.parametrize("name", sorted(MODULES))
def test_every_spec_entry_is_complete(name):
    module = MODULES[name]
    for rule_id, spec in module.RULE_SPEC.items():
        assert spec.checks and spec.checks[0].islower(), f"{rule_id}: no condition stated"
        for severity in spec.severity.split(" / "):
            assert severity in SEVERITIES or spec.dynamic, (
                f"{rule_id}: severity {spec.severity!r} is not one of {SEVERITIES}"
            )
        if spec.threshold:
            assert spec.threshold in module.THRESHOLDS, (
                f"{rule_id}: threshold {spec.threshold!r} is not in {name}'s THRESHOLDS"
            )


def test_every_threshold_is_named_by_the_rule_it_tunes():
    """A threshold nothing documents is one nobody can discover from --list-rules."""
    for name, module in MODULES.items():
        used = {spec.threshold for spec in module.RULE_SPEC.values() if spec.threshold}
        assert set(module.THRESHOLDS) == used, (
            f"{name}: {sorted(set(module.THRESHOLDS) ^ used)} is in one of "
            "THRESHOLDS / RULE_SPEC but not the other"
        )


def test_the_policies_only_name_rules_that_exist():
    """A glob that matches nothing is a policy that silently does not apply."""
    known = set(gate.catalogue())
    for entry in gate.CONTEXT_RULES:
        assert entry in known, f"CONTEXT_RULES names {entry!r}, which is not a rule"
    for policy in gate.BUILTIN_POLICIES.values():
        for pattern in policy.severity:
            assert any(fnmatch.fnmatch(rule, pattern) for rule in known), (
                f"policy {policy.name!r} promotes {pattern!r}, which matches no rule"
            )


def test_the_catalogue_says_what_each_policy_does_to_a_rule():
    catalogue = gate.catalogue()
    assert catalogue["readability.diagonal_wire"]["blocks_under"] == ["ai-generated"]
    assert catalogue["board.size"]["blocks_under"] == []  # context, never promoted
    assert catalogue["readability.off_grid_pin"]["threshold"] == "grid_mm"
    assert catalogue["readability.off_grid_pin"]["origin"] == "schematic"
    # erc.* is graded by KiCad, and `fabrication` is the policy that blocks on it
    assert "fabrication" in catalogue["erc.*"]["blocks_under"]


RULE_LIKE = re.compile(r"`([a-z][a-z_]*\.[a-z_][a-z_.*]*)`")
# `schematic.pdf`, `report.json`: same shape as a rule id, not a rule id.
FILE_SUFFIXES = {
    "py",
    "md",
    "sh",
    "pdf",
    "json",
    "csv",
    "net",
    "glb",
    "png",
    "jpg",
    "toml",
    "yml",
    "yaml",
    "html",
    "cir",
    "txt",
    "zip",
    "step",
    "xml",
    "lock",
    "cfg",
}


def test_the_guides_only_name_rules_that_exist():
    """A guide naming a rule that was renamed sends the reader looking for nothing."""
    known = gate.catalogue()
    families = {rule.split(".")[0] for rule in known}
    for guide in sorted(Path(__file__).resolve().parent.parent.glob("docs/guides/*.md")):
        for token in RULE_LIKE.findall(guide.read_text(encoding="utf-8")):
            if token.split(".")[0] not in families or token.rsplit(".", 1)[-1] in FILE_SUFFIXES:
                continue  # a filename or a threshold, not a rule id
            assert any(covers(token, rule) or covers(rule, token) for rule in known), (
                f"{guide.name} names `{token}`, which is not a rule "
                f"(`eda gate --list-rules` is the list)"
            )
