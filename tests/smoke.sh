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

step "design gate (one verdict against a policy)"
eda gate "$PROJECT" --policy ai-generated -o "$OUT/gate.json" > /dev/null
have "$OUT/gate.json" "d['pass'] and d['counts']['error'] == 0"
# ... and a policy the project cannot meet has to fail, with the reason named.
printf '{"name": "zero-tolerance", "limits": {"info": 0}}\n' > "$OUT/strict.json"
if eda gate "$PROJECT" --policy "$OUT/strict.json" > "$OUT/gate-fail.json"; then
    echo "the gate passed a policy that allows no findings at all" >&2
    exit 1
fi
have "$OUT/gate-fail.json" "not d['pass'] and d['blocking'] and d['exceeded']['info']['limit'] == 0"

step "board render (layers + 3D + contact sheet)"
eda pcb render "$PROJECT" -o "$OUT/art" --dpi 120 --views front copper-front > "$OUT/art.json"
have "$OUT/art.json" "len(d['images']) >= 5 and not d['errors'] and 'contact_sheet' in d"
test -s "$OUT/art/contact-sheet.png"

step "board render on a transparent background"
eda pcb render "$PROJECT" -o "$OUT/art-clear" --dpi 100 --views front --no-3d --background transparent > "$OUT/art-clear.json"
have "$OUT/art-clear.json" "not d['errors'] and d['background'] == 'transparent'"
python3 -c "from PIL import Image; im = Image.open('$OUT/art-clear/front.png'); assert im.mode == 'RGBA' and im.getpixel((2, 2))[3] == 0, im.mode"

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

step "diff between two revisions"
cp -r "$PROJECT" "$OUT/revised"
# Two kinds of change, because they surface through different channels: a
# re-valued part shows in the component table, a moved one in the drawing.
sed -i 's/(property "Value" "10k"/(property "Value" "4k7"/' "$OUT/revised/example.kicad_sch"
grep -q '"4k7"' "$OUT/revised/example.kicad_sch"  # the fixture still had a 10k to change
sed -i 's/(symbol (lib_id "Device:C") (at 147.32 60.96 0)/(symbol (lib_id "Device:C") (at 154.94 66.04 0)/' \
    "$OUT/revised/example.kicad_sch"
grep -q "154.94 66.04" "$OUT/revised/example.kicad_sch"  # C2 was still where we expected
eda diff "$PROJECT" "$OUT/revised" -o "$OUT/diff" --dpi 100 > "$OUT/diff.json"
have "$OUT/diff.json" "not d['identical'] and d['sections']['schematic']['components']['changed']"
have "$OUT/diff.json" "d['sections']['schematic_drawing']['pages'][0]['removed_pixels'] > 0"
have "$OUT/diff.json" "d['sections']['schematic_drawing']['pages'][0]['added_pixels'] > 0"
grep -q "4k7" "$OUT/diff/diff.md"
test -s "$OUT/diff/diff/sheet-diff.png"

step "copper: current capacity, resistance, impedance"
eda pcb electrical "$PROJECT" > "$OUT/electrical.json"
have "$OUT/electrical.json" "d['nets'] and all(n['current_a'] > 0 for n in d['nets'])"
python3 -c "
import json
d = json.load(open('$OUT/electrical.json'))
tight = d['nets'][0]
print(f\"tightest net {tight['net']}: {tight['narrowest_mm']} mm -> {tight['current_a']} A\")
"

# The impedance half of this command needs a stackup that states epsilon_r,
# and this fixture declares no stackup at all - so what smoke can prove here
# is the wiring: the flag parses, the solve path runs, and the JSON says it
# was asked for. Every solved number itself is covered by tests/test_field2d.py
# and tests/test_electrical.py against references that are not other models.
step "copper: the field solver is wired to the flag"
eda pcb electrical "$PROJECT" --solve > "$OUT/electrical-solved.json"
have "$OUT/electrical-solved.json" \
  "any('field solver' in note for note in d['notes'])"
have "$OUT/electrical-solved.json" \
  "all('width_50r_solved_ohm' in row for row in d['impedance'] if row.get('width_50r_mm'))"
python3 -c "
import json
d = json.load(open('$OUT/electrical-solved.json'))
rows = d['impedance']
print(f'{len(rows)} impedance row(s); solved columns present where a width was proposed')
"

step "thermal: where the stated watts end up, and how fast"
eda pcb thermal "$PROJECT" --power U1=1.2 --transient 60 -o "$OUT/thermal" > "$OUT/thermal.json"
have "$OUT/thermal.json" \
  "d['max_temperature_c'] > d['ambient_c'] and d['balance']['residual'] < 0.01"
have "$OUT/thermal.json" \
  "d['transient']['balance']['residual'] < 1e-6 and d['transient']['curve']"
test -s "$OUT/thermal/thermal.png"
python3 -c "
import json
d = json.load(open('$OUT/thermal.json'))
hot = d['parts'][0]
print(f\"{hot['ref']} at {hot['power_w']} W: {hot['temperature_c']} degC, \"
      f\"balance residual {d['balance']['residual']}, \"
      f\"reached {d['transient']['reached_fraction']:.0%} of steady in 60 s\")
"

# The fixture routes two nets, nowhere near each other - so like --solve
# above, what smoke proves is the wiring: the command runs, the assumptions
# are stated, and an empty pair list is the truthful answer for this board.
# The physics is covered by tests/test_crosstalk.py against anchors that are
# not other simulators (stripline's forward silence, the lumped RC clock).
step "crosstalk: the coupled runs, or the truthful lack of them"
eda pcb crosstalk "$PROJECT" > "$OUT/crosstalk.json"
have "$OUT/crosstalk.json" \
  "d['assumptions']['method'].startswith('weak-coupling') and isinstance(d['pairs'], list)"

step "bill of materials"
eda sch bom "$PROJECT" -o "$OUT/bom.csv" > "$OUT/bom.json"
have "$OUT/bom.json" "d['total_parts'] == 5 and d['line_items'] >= 3"

step "fabrication package (with a dark layer preview)"
eda pcb fab "$PROJECT" -o "$OUT/fab" --preview --preview-dpi 100 --background black > "$OUT/fab.json"
have "$OUT/fab.json" "d['ok'] and {s['step'] for s in d['steps']} >= {'gerbers','drill','position','preview','bom'}"
test -s "$OUT/fab/gerbers/drill-report.txt"
test -s "$OUT/fab/preview/contact-sheet.png"
# the pictures are for us, not for the board house
python3 -c "import zipfile; n = zipfile.ZipFile('$OUT/fab/example-fab.zip').namelist(); assert n and not any(x.startswith('preview/') for x in n), n"

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
