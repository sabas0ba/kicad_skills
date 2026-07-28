#!/usr/bin/env bash
# End-to-end smoke test: runs every top-level command against the example
# project. Meant to be executed INSIDE the container (make smoke).
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=$(mktemp -d)
PROJECT="$OUT/project"
cp -r tests/fixtures/example_project "$PROJECT"
trap 'rm -rf "$OUT"' EXIT

# Use the working tree, not the copy baked into the image.
export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}$PWD/src"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
have() { python3 -c "import json,sys; d=json.load(open('$1')); sys.exit(0 if $2 else 1)"; }

step "doctor"
eda doctor > "$OUT/doctor.json"
have "$OUT/doctor.json" "d['ok']"
python3 -c "import json;d=json.load(open('$OUT/doctor.json'));print('kicad-cli',d['kicad_cli'],'|','ngspice',d['ngspice'])"

step "schematic info"
eda sch info "$PROJECT" > "$OUT/sch-info.json"
have "$OUT/sch-info.json" "len(d['components']) == 5 and len(d['nets']) == 5"

step "schematic review (ERC + heuristics)"
eda sch review "$PROJECT" -o "$OUT/sch-review.json" > /dev/null
have "$OUT/sch-review.json" "d['summary']['error'] == 0 and d['statistics']['erc_available']"

step "schematic render"
eda sch render "$PROJECT" -o "$OUT/sch-img" > "$OUT/sch-img.json"
have "$OUT/sch-img.json" "len(d['images']) >= 1"

step "board info"
eda pcb info "$PROJECT" > "$OUT/pcb-info.json"
have "$OUT/pcb-info.json" "d['layer_count'] == 2 and d['size_mm'] == [40.0, 30.0]"

step "board review (DRC + heuristics)"
eda pcb review "$PROJECT" -o "$OUT/pcb-review.json" > /dev/null
have "$OUT/pcb-review.json" "d['summary']['error'] == 0 and d['drc_available']"

step "board render (layers + 3D + contact sheet)"
eda pcb render "$PROJECT" -o "$OUT/art" --dpi 120 --views front copper-front > "$OUT/art.json"
have "$OUT/art.json" "len(d['images']) >= 5 and not d['errors'] and 'contact_sheet' in d"
test -s "$OUT/art/contact-sheet.png"

step "GLB 3D model"
eda pcb glb "$PROJECT" -o "$OUT/board.glb" > "$OUT/glb.json"
have "$OUT/glb.json" "d['bytes'] > 1000"

step "schematic PDF"
eda sch pdf "$PROJECT" -o "$OUT/schematic.pdf" > "$OUT/schpdf.json"
have "$OUT/schpdf.json" "d['bytes'] > 1000"

step "one-command report"
eda report "$PROJECT" -o "$OUT/report" --dpi 100 --no-3d > "$OUT/report.json"
have "$OUT/report.json" "not d['errors'] and {'schematic_review','board_review','bom'} <= set(d['sections'])"
test -s "$OUT/report/report.html"
test -s "$OUT/report/report.md"

step "bill of materials"
eda sch bom "$PROJECT" -o "$OUT/bom.csv" > "$OUT/bom.json"
have "$OUT/bom.json" "d['total_parts'] == 5 and d['line_items'] >= 3"

step "fabrication package"
eda pcb fab "$PROJECT" -o "$OUT/fab" > "$OUT/fab.json"
have "$OUT/fab.json" "d['ok'] and {s['step'] for s in d['steps']} >= {'gerbers','drill','position','bom'}"
test -s "$OUT/fab/gerbers/drill-report.txt"

step "spice simulation"
eda sim run tests/fixtures/spice/rc_lowpass.cir -o "$OUT/sim" > "$OUT/sim.json"
have "$OUT/sim.json" "d['ok'] and len(d['plots']) == 2"
python3 - "$OUT/sim.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
ac = [p for p in data["plots"] if p["analysis"] == "ac"][0]
fc = ac["measurements"]["signals"]["v(out)"]["f_minus_3db_hz"]
assert 950 < fc < 1050, f"RC corner frequency is off: {fc}"
print(f"-3 dB corner = {fc:.1f} Hz (expected 1000 Hz)")
PY

step "monte carlo tolerance analysis"
eda sim montecarlo tests/fixtures/spice/rc_lowpass.cir -o "$OUT/mc" \
    --vary R1=1% --vary C1=1% --metric "ac.v(out).f_minus_3db_hz" \
    --trials 20 --distribution uniform --seed 5 > "$OUT/mc.json"
have "$OUT/mc.json" "d['ok'] and d['statistics']['samples'] == 20 and 0 < d['statistics']['spread_pct'] < 6"
python3 -c "
import json,sys
d = json.load(open('$OUT/mc.json'))
s = d['statistics']
print(f\"fc = {s['mean']:.1f} Hz mean, {s['min']:.1f}..{s['max']:.1f} Hz over {s['samples']} trials\")
"

step "temperature sweep"
eda sim temperature tests/fixtures/spice/rc_lowpass.cir -o "$OUT/temp" \
    --temperatures 0 25 85 --metric "ac.v(out).f_minus_3db_hz" > "$OUT/temp.json"
have "$OUT/temp.json" "d['ok'] and len(d['points']) == 3"

step "datasheet parsing (locally generated PDF, no network)"
python3 - "$OUT/fake.pdf" <<'PY'
import sys
from reportlab.pdfgen import canvas
c = canvas.Canvas(sys.argv[1])
c.drawString(72, 720, "EDA1234 Absolute Maximum Ratings")
c.drawString(72, 700, "Supply voltage 6.0 V")
c.save()
PY
eda datasheet parse "$OUT/fake.pdf" -o "$OUT/ds" --renders > "$OUT/ds.json"
have "$OUT/ds.json" "d['info']['page_count'] == 1 and len(d['renders']['items']) == 1"
eda datasheet find "$OUT/fake.pdf" "absolute maximum" > "$OUT/find.json"
have "$OUT/find.json" "d and d[0]['page'] == 1"

printf '\n\033[32mAll smoke checks passed.\033[0m\n'
