"""The contract a design has to satisfy before it counts as finished.

``sch review`` and ``pcb review`` answer "what is wrong with this design". That
is the right shape for a person, who reads the list and decides. It is the wrong
shape for a generator, which will happily emit a board, read a page of warnings
and call the job done - the review has no opinion about which of its own
findings were allowed to survive.

The gate is that opinion, written down. A policy names the rules that block, the
number of each severity that may remain, and the waivers - each of which must
carry a reason. Nothing can be passed over silently: a finding either blocks, or
appears under ``waived`` next to the sentence somebody wrote to excuse it. That
sentence lives in a file in the repository, so excusing a violation is a diff a
reviewer sees rather than a step a generator can skip.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import os
import tomllib
from pathlib import Path
from typing import Any

from .util import COLLAPSE_LIMIT, SEVERITIES, EdaError

SEVERITY_ORDER = {name: index for index, name in enumerate(SEVERITIES)}


@dataclasses.dataclass(frozen=True)
class Waiver:
    """One finding this project has decided to live with, and why."""

    rule: str
    reason: str
    location: str = ""

    def matches(self, finding: dict[str, Any]) -> bool:
        if not fnmatch.fnmatch(finding.get("rule", ""), self.rule):
            return False
        return not (self.location and self.location not in str(finding.get("location") or ""))

    def to_dict(self) -> dict[str, Any]:
        out = {"rule": self.rule, "reason": self.reason}
        if self.location:
            out["location"] = self.location
        return out


@dataclasses.dataclass(frozen=True)
class Policy:
    name: str
    description: str
    # rule glob -> the severity the gate should treat it as
    severity: dict[str, str] = dataclasses.field(default_factory=dict)
    # severity -> how many may remain; a severity that is absent is not limited
    limits: dict[str, int] = dataclasses.field(default_factory=lambda: {"error": 0})
    waivers: tuple[Waiver, ...] = ()
    thresholds: dict[str, dict[str, float]] = dataclasses.field(default_factory=dict)

    def effective_severity(self, finding: dict[str, Any]) -> str:
        """The severity this policy gives a finding, ignoring its own opinion.

        The longest matching pattern wins, so a project can promote a whole
        family and then exempt one member of it without ordering games.
        """
        best: tuple[int, str] | None = None
        for pattern, severity in self.severity.items():
            if fnmatch.fnmatch(finding.get("rule", ""), pattern):
                score = len(pattern.replace("*", ""))
                if best is None or score > best[0]:
                    best = (score, severity)
        return best[1] if best else finding.get("severity", "info")

    def waiver_for(self, finding: dict[str, Any]) -> Waiver | None:
        for waiver in self.waivers:
            if waiver.matches(finding):
                return waiver
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "severity": self.severity,
            "limits": self.limits,
            "waivers": [w.to_dict() for w in self.waivers],
            "thresholds": self.thresholds,
        }


# Rules that report what the design *is* rather than what is wrong with it -
# the board size, which layers carry copper, that a ground pour exists. They stay
# informational under every policy, because promoting them would make a gate that
# no correct design can pass.
CONTEXT_RULES = (
    "board.size",
    "layout.ground_plane",
    "layout.double_sided_assembly",
    "layout.pour_single_sided",
    "layout.pour_coverage",
    "route.layer_usage",
    "fab.many_drill_sizes",
    "mechanical.no_mounting_holes",
    "test.no_testpoints",
    "schematic.dnp",
    "power.many_supplies",
    "erc.unavailable",
    "drc.unavailable",
)

# Everything a generated design has to get right before a human is asked to look
# at it. A family is wildcarded only where every rule in it is a defect;
# `layout.*` and `route.*` are listed one by one, because both families also
# carry rules that only describe the board.
_AI_BLOCKING = (
    # the drawing has to be readable and actually connected
    "readability.*",
    # the parts have to be specified, not just valued
    "spec.*",
    # the circuit practices ERC has no opinion about
    "analog.*",
    "net.single_pin",
    "net.no_driver",
    "power.no_ground",
    "power.no_supply",
    "power.unused_rail",
    "schematic.missing_value",
    "schematic.missing_footprint",
    "schematic.missing_datasheet",
    # the artwork
    "silk.*",
    "layout.off_grid_placement",
    "layout.odd_rotation",
    "layout.pad_collision",
    "layout.outside_outline",
    "layout.no_decoupling",
    "layout.decoupling_distance",
    "layout.decoupling_via",
    "layout.no_ground_plane",
    "layout.unfilled_zone",
    "layout.pour_fragmented",
    "route.unrouted_net",
    "route.no_tracks",
    "route.stub",
    "route.acute_angle",
    "route.mixed_track_widths",
    "route.detour",
    "route.return_path",
    "via.small_drill",
    "via.annular_ring",
    "track.thin_power",
    "board.edge_clearance",
)

BUILTIN_POLICIES: dict[str, Policy] = {
    "default": Policy(
        name="default",
        description=(
            "What the review commands already enforce: an error blocks, a warning "
            "is for a human to judge."
        ),
        limits={"error": 0},
    ),
    "ai-generated": Policy(
        name="ai-generated",
        description=(
            "Everything a machine can check has to be clean before a human is "
            "asked to look. Readability, part specification and layout practice "
            "all block, because a generator has no eye to catch them later."
        ),
        severity=dict.fromkeys(_AI_BLOCKING, "error"),
        limits={"error": 0},
    ),
    "fabrication": Policy(
        name="fabrication",
        description=(
            "Ready to send out: ERC, DRC and everything that changes what comes "
            "back from the fab. Drawing style is not judged."
        ),
        severity={
            "erc.*": "error",
            "drc.*": "error",
            "route.unrouted_net": "error",
            "board.edge_clearance": "error",
            "silk.over_pad": "error",
            "silk.text_too_small": "error",
            "via.small_drill": "error",
            "via.annular_ring": "error",
            "layout.pad_collision": "error",
            "readability.*": "info",
            "spec.missing_rating": "info",
        },
        limits={"error": 0},
    ),
}


def load_policy(spec: str | os.PathLike[str] | None) -> Policy:
    """Resolve ``--policy``: a built-in name, or a path to JSON/TOML."""
    if spec is None:
        return BUILTIN_POLICIES["default"]
    text = str(spec)
    if text in BUILTIN_POLICIES:
        return BUILTIN_POLICIES[text]
    path = Path(text)
    if not path.exists():
        known = ", ".join(sorted(BUILTIN_POLICIES))
        raise EdaError(f"unknown policy {text!r}: not a file, and not one of {known}")
    return policy_from_dict(_read_policy_file(path), default_name=path.stem)


def _read_policy_file(path: Path) -> dict[str, Any]:
    if path.suffix == ".toml":
        with path.open("rb") as handle:
            return tomllib.load(handle)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EdaError(f"{path} is not valid JSON: {exc}") from exc


def policy_from_dict(data: dict[str, Any], *, default_name: str = "custom") -> Policy:
    """Build a policy from a parsed file, refusing the shapes that hide things."""
    base = BUILTIN_POLICIES.get(str(data.get("extends", "")).strip() or "default")
    if base is None:
        raise EdaError(f"policy extends unknown base {data.get('extends')!r}")

    severity = {**base.severity, **(data.get("severity") or {})}
    for pattern, value in severity.items():
        if value not in SEVERITIES:
            raise EdaError(
                f"policy severity for {pattern!r} is {value!r}, "
                f"expected one of {', '.join(SEVERITIES)}"
            )

    limits = {**base.limits, **(data.get("limits") or {})}
    for name, value in limits.items():
        if name not in SEVERITIES or not isinstance(value, int) or value < 0:
            raise EdaError(f"policy limit {name!r}={value!r} is not a severity and a count >= 0")

    waivers = []
    for entry in data.get("waivers") or []:
        rule = str(entry.get("rule", "")).strip()
        reason = str(entry.get("reason", "")).strip()
        if not rule:
            raise EdaError("every waiver needs a 'rule'")
        if not reason:
            # The whole point of the file: waiving a finding is a decision
            # somebody made, and a decision with no stated reason is the silent
            # dismissal this command exists to prevent.
            raise EdaError(f"the waiver for {rule!r} has no 'reason' - say why it is acceptable")
        waivers.append(Waiver(rule=rule, reason=reason, location=str(entry.get("location", ""))))

    thresholds = {**base.thresholds, **(data.get("thresholds") or {})}
    for section in thresholds:
        if section not in ("schematic", "board"):
            raise EdaError(f"thresholds section {section!r} is not 'schematic' or 'board'")

    return Policy(
        name=str(data.get("name") or default_name),
        description=str(data.get("description") or base.description),
        severity=severity,
        limits=limits,
        waivers=tuple(waivers),
        thresholds=thresholds,
    )


def catalogue() -> dict[str, dict[str, Any]]:
    """Every rule the reviews can produce, and what each policy does to it.

    This is the answer to "what is actually checked, and which of it stops me":
    the condition each rule tests, the severity it reports, the threshold that
    tunes it, and the built-in policies under which it blocks. It is assembled
    from the review modules rather than written out, so it cannot drift from
    them - `eda gate --list-rules` prints it.
    """
    from .kicad import pcb_review, sch_review

    out: dict[str, dict[str, Any]] = {}
    for origin, module in (("schematic", sch_review), ("board", pcb_review)):
        for rule_id, spec in module.RULE_SPEC.items():
            entry = out.setdefault(rule_id, {"origin": origin, **spec.to_dict()})
            if entry["origin"] != origin:
                entry["origin"] = "schematic + board"  # internal.* comes from both
            if spec.threshold:
                entry["threshold_default"] = module.THRESHOLDS[spec.threshold]

    for rule_id, entry in out.items():
        # Judge the rule at the worst severity it can report. A rule KiCad grades
        # for us (erc.*, drc.*) can always come back as an error.
        graded = [s for s in entry["severity"].split(" / ") if s in SEVERITY_ORDER]
        declared = min(graded, key=SEVERITY_ORDER.__getitem__) if graded else "error"
        entry["context_only"] = rule_id in CONTEXT_RULES
        entry["blocks_under"] = [
            name
            for name, policy in BUILTIN_POLICIES.items()
            if policy.limits.get(_effective(policy, rule_id, declared)) == 0
        ]
    return out


def _effective(policy: Policy, rule_id: str, declared: str) -> str:
    finding = {"rule": rule_id, "severity": declared}
    if rule_id in CONTEXT_RULES:
        return declared
    return policy.effective_severity(finding)


def _apply(policy: Policy, findings: list[dict[str, Any]], origin: str) -> list[dict[str, Any]]:
    out = []
    for finding in findings:
        entry = dict(finding)
        entry["origin"] = origin
        entry["reported_severity"] = finding.get("severity", "info")
        entry["severity"] = _effective(
            policy, str(finding.get("rule", "")), entry["reported_severity"]
        )
        waiver = policy.waiver_for(finding)
        if waiver is not None:
            entry["waiver"] = waiver.to_dict()
        out.append(entry)
    return out


def run(
    target: str | os.PathLike[str],
    *,
    policy: Policy | None = None,
    use_cli: bool = True,
    collapse: int = COLLAPSE_LIMIT,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Review the schematic and the board, then judge the result against a policy.

    A missing schematic or board is reported as skipped rather than as a
    failure: a design is gated all the way through its life, and for most of
    that life the artwork does not exist yet.
    """
    from .kicad import pcb_review, sch_review

    policy = policy or BUILTIN_POLICIES["default"]
    stages: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []

    # The stage name is also the key each review uses for the file it read.
    for name, module in (("schematic", sch_review), ("board", pcb_review)):
        section = {**(policy.thresholds.get(name) or {}), **(thresholds or {})}
        try:
            report = module.review(
                target, use_cli=use_cli, thresholds=section or None, collapse=collapse
            )
        except EdaError as exc:
            stages[name] = {"skipped": str(exc)}
            continue
        stages[name] = {name: report[name], "summary": report["summary"]}
        findings.extend(_apply(policy, report["findings"], name))

    if all("skipped" in stage for stage in stages.values()) or not stages:
        # Passing a target that holds no design at all would otherwise come back
        # as a clean bill of health for nothing.
        reasons = "; ".join(stage["skipped"] for stage in stages.values()) or "nothing to review"
        raise EdaError(f"no schematic and no board to gate in {target}: {reasons}")

    waived = [f for f in findings if "waiver" in f]
    judged = [f for f in findings if "waiver" not in f]
    counts = dict.fromkeys(SEVERITIES, 0)
    for finding in judged:
        counts[finding["severity"]] += 1

    exceeded = {
        severity: {"count": counts[severity], "limit": limit}
        for severity, limit in policy.limits.items()
        if counts.get(severity, 0) > limit
    }
    blocking = sorted(
        (f for f in judged if f["severity"] in exceeded),
        key=lambda f: (SEVERITY_ORDER[f["severity"]], f["rule"], f.get("location") or ""),
    )

    return {
        "target": str(target),
        "policy": policy.to_dict(),
        "pass": not exceeded,
        "counts": counts,
        "exceeded": exceeded,
        "schematic": stages.get("schematic", {"skipped": "not reviewed"}),
        "board": stages.get("board", {"skipped": "not reviewed"}),
        "blocking": blocking,
        "waived": waived,
        "findings": sorted(
            judged,
            key=lambda f: (SEVERITY_ORDER[f["severity"]], f["rule"], f.get("location") or ""),
        ),
    }
