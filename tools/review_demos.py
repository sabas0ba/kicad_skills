"""Run both reviews over KiCad's bundled demo projects and aggregate the findings.

A rule can be correct on the test fixture and still be useless in practice: the
question is how often it fires on real designs. This runs `eda sch review` and
`eda pcb review` over the 18 demo projects that ship inside the image and prints
a per-rule tally, which is the signal for grading a rule or collapsing it.

    docker run --rm -v "$PWD:/work" -w /work -e PYTHONPATH=/work/src \\
      --entrypoint python3 eda-toolkit:10.0.4 tools/review_demos.py /tmp/out

The projects are copied to a scratch directory first: ERC and DRC write next to
the design, and /usr/share is read only.
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEMOS = Path("/usr/share/kicad/demos")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/demo-review")
WORK = Path(os.environ.get("EDA_DEMO_WORK", "/tmp/demo-work"))
TIMEOUT = 900


def run(argv: list[str], timeout: int = TIMEOUT):
    started = time.time()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, time.time() - started, "timeout"
    elapsed = time.time() - started
    if not proc.stdout.strip():
        return None, elapsed, (proc.stderr or "no output")[:400]
    try:
        return json.loads(proc.stdout), elapsed, None
    except json.JSONDecodeError:
        return None, elapsed, (proc.stdout[:200] + " | " + proc.stderr[:200])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    work = WORK
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    summary = []
    sch_rules: collections.Counter = collections.Counter()
    pcb_rules: collections.Counter = collections.Counter()

    for demo in sorted(p for p in DEMOS.iterdir() if p.is_dir()):
        project = work / demo.name.replace(" ", "_")
        shutil.copytree(demo, project)
        entry = {"demo": demo.name}

        has_sch = any(project.rglob("*.kicad_sch"))
        has_pcb = any(project.rglob("*.kicad_pcb"))

        if has_sch:
            data, elapsed, error = run(["eda", "sch", "review", str(project)])
            entry["sch"] = {"seconds": round(elapsed, 1), "error": error}
            if data:
                entry["sch"].update(
                    {"summary": data["summary"], "stats": data["statistics"]}
                )
                for f in data["findings"]:
                    sch_rules[(f["rule"], f["severity"])] += 1
                (OUT / f"{demo.name.replace(' ', '_')}-sch.json").write_text(
                    json.dumps(data, indent=1)
                )

        if has_pcb:
            data, elapsed, error = run(["eda", "pcb", "review", str(project)])
            entry["pcb"] = {"seconds": round(elapsed, 1), "error": error}
            if data:
                entry["pcb"].update(
                    {"summary": data["summary"], "stats": {
                        k: data["statistics"][k] for k in
                        ("size_mm", "layer_count", "footprints", "nets", "tracks", "vias", "zones")
                        if k in data["statistics"]}}
                )
                for f in data["findings"]:
                    pcb_rules[(f["rule"], f["severity"])] += 1
                (OUT / f"{demo.name.replace(' ', '_')}-pcb.json").write_text(
                    json.dumps(data, indent=1)
                )

        summary.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    report = {
        "projects": summary,
        "sch_rule_counts": {f"{r}|{s}": n for (r, s), n in sch_rules.most_common()},
        "pcb_rule_counts": {f"{r}|{s}": n for (r, s), n in pcb_rules.most_common()},
    }
    (OUT / "aggregate.json").write_text(json.dumps(report, indent=1))
    print("\n=== schematic rules ===")
    for (rule, sev), n in sch_rules.most_common():
        print(f"{n:6d}  {sev:8s} {rule}")
    print("\n=== board rules ===")
    for (rule, sev), n in pcb_rules.most_common():
        print(f"{n:6d}  {sev:8s} {rule}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
