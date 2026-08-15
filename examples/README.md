# Worked examples

Each project here exists twice, generated from one description by
[`tools/make_examples.py`](https://github.com/sabas0ba/kicad_skills/blob/main/tools/make_examples.py):

| | what it is |
| --- | --- |
| `as-generated/` | what falls out of a generator that got the connectivity roughly right and thought about nothing else |
| `reviewed/` | the same circuit after the `eda gate` loop has been closed |

A repository full of good designs proves only that good designs pass. The *pair*
is the evidence: that the rules catch what they claim to catch, and that fixing
what they report converges.

```bash
./bin/eda.sh gate examples/buck-5v/reviewed     --policy examples/buck-5v/gate.toml --text
./bin/eda.sh gate examples/buck-5v/as-generated --policy examples/buck-5v/gate.toml --text
```

Regenerate the projects with:

```bash
docker run --rm -u $(id -u):$(id -g) -v "$PWD:/work" -w /work \
  -e PYTHONPATH=/work/src -e HOME=/tmp/eda-home \
  --entrypoint python3 eda-toolkit:9.0.9 tools/make_examples.py examples/
```

and the images below with:

```bash
./bin/eda.sh sch render examples/buck-5v/reviewed -o build/render/reviewed/sch --dpi 150
./bin/eda.sh pcb render examples/buck-5v/reviewed -o build/render/reviewed/pcb \
    --dpi 300 --views front back --no-3d --no-sheet
```

The generator reads KiCad's own symbol and footprint libraries, so these are the
real parts, not simplified copies — which is why it runs inside the container.

After the five were built, the whole set was read the way an engineer would
read it — circuit theory, layout physics, readability — and every finding was
either turned into a rule, fixed in `reviewed/`, or answered with a reasoned
waiver in the project's `gate.toml`. That pass, with what each finding became,
is [REVIEW.md](REVIEW.md). Every `reviewed/` project now **passes** its own
gate; every `as-generated/` still fails it.

## When these were made, and by what

Both variants carry it in their title block, in the comment fields, on the
schematic and on the board:

```
(comment 1 "generated 2026-08-12 by Claude Code (claude-fable-5)")
(comment 2 "from tools/make_examples.py in sabas0ba/kicad_skills")
```

It matters most on `as-generated`. That variant is a record of what a generator
of this vintage actually produced, and the point of keeping it is to be able to
say later how much has changed — which needs a date on it. The stamp is frozen
in `GENERATED_ON` / `GENERATED_BY` at the top of the generator rather than read
from the clock, so regenerating an unchanged design still produces an unchanged
file; bump them when you regenerate, or pass `--generated-on` / `--generated-by`.

The design's own `title`, `date`, `rev` and `company` are a separate thing and
stay empty on `as-generated` — a generator that leaves them blank is one of the
findings, and the stamp deliberately does not paper over it.

## buck-5v — 12 V to 5 V at 2 A

LM2596S-5, catch diode, output inductor, screw terminals in and out.

Under KiCad's own ERC and DRC, and the `ai-generated` policy:

| | verdict | schematic (e/w/i) | board (e/w/i) |
| --- | --- | --- | --- |
| `reviewed` | **PASS**, 1 finding waived | 0 / 0 / 0 | 0 / 1 / 5 |
| `as-generated` | **FAIL**, 28 blocking | — | — |

### The two, side by side

Everything below is this repository's own output — `eda sch render` and
`eda pcb render`, run on the two variants and cropped. Nothing is drawn by hand.

**The schematic.** Left is what a generator leaves; right is after the loop. The
empty title block, the parts stacked on each other at the bottom right, and the
absence of any note explaining a single value are all visible before reading one
finding:

| as-generated | reviewed |
| --- | --- |
| ![schematic, as generated](buck-5v/images/schematic-as-generated.jpg) | ![schematic, reviewed](buck-5v/images/schematic-reviewed.jpg) |

**The board, front copper.** The same circuit, the same nets. On the left the
power rails are routed at signal width, J1 and D1 sit at 37°, and several tracks
simply stop in mid-air. On the right the power copper is 1.0 mm, every part is
square to the grid, and each ground stub ends in a via:

| as-generated | reviewed |
| --- | --- |
| ![board front, as generated](buck-5v/images/board-front-as-generated.jpg) | ![board front, reviewed](buck-5v/images/board-front-reviewed.jpg) |

**The board, back copper.** This is the ground plane, and the reason the
floorplan is what it is. Only the two screw terminals are through-hole, and both
sit outside the pour, so the bottom layer carries nothing but the plane and the
vias dropping into it. Ground now pours on both faces — the front copper joins
through-hole only, so no thermal spoke is hostage to a crowded pad — and a ring
of stitching vias around the rim ties the two planes together where edge noise
wants a short way home. The generated variant has no pour at all:

| as-generated | reviewed |
| --- | --- |
| ![board back, as generated](buck-5v/images/board-back-as-generated.jpg) | ![board back, reviewed](buck-5v/images/board-back-reviewed.jpg) |

Both variants also produce a complete fabrication package —
`eda pcb fab examples/buck-5v/reviewed -o fab/` writes the gerbers, the Excellon
drill file, the pick-and-place and the BOM.

What separates them, and which check finds it:

| in `as-generated` | found by |
| --- | --- |
| symbols and wires off the 1.27 mm grid | `readability.off_grid_pin` / `_wire` / `_label`, and KiCad's own `erc.endpoint_off_grid` |
| no PWR_FLAG on the externally supplied rails | `erc.power_pin_not_driven` |
| two symbols dropped on the same spot | `readability.overlapping_symbols` |
| no title block, no design notes | `readability.title_block`, `spec.no_design_notes` |
| no tolerance / voltage / current rating, no MPN | `spec.missing_rating`, `spec.missing_part_number` |
| capacitors chosen without derating the rail | `spec.voltage_derating` |
| no ground pour | `layout.no_ground_plane` |
| parts off the placement grid, turned to 37 degrees | `layout.off_grid_placement`, `layout.odd_rotation` |
| power routed at signal width | `track.thin_power` |
| routing left half finished | `route.stub`, `route.acute_angle` |

`reviewed` carries one waiver, in [`buck-5v/gate.toml`](https://github.com/sabas0ba/kicad_skills/blob/main/examples/buck-5v/gate.toml):
`layout.decoupling_distance` on U1.4, because that pin is FB — a sense input, not
a supply — and the rule cannot tell the two apart from the board alone. That is
the mechanism working as intended: the finding is not silenced, it is answered.

### What building it changed in the toolkit

Laying out a real board found four things the rules and the parser had wrong:

* `sexp.dumps` wrote every string unquoted, so KiCad refused the generated file.
* `layout.decoupling_via` measured to the pad *centre*. A bulk electrolytic has a
  2.5 mm pad, so the rule asked for a via inside the pad it was meant to sit beside.
* `route.acute_angle` compared against 90° with a 1e-6 epsilon, and a corner drawn
  at exactly 90° comes out of the arc cosine a ten-thousandth under it.
* `layout.decoupling_via` fired on every capacitor sitting in a pour on its own
  layer, where there is no via to be near — 26 findings across the KiCad demo
  corpus, 18 of them false.

## motor-driver — dual H-bridge, DRV8833, 2 × 1.5 A

Two brushed DC motors, screw terminals out, an eight pin logic header, and the
charge pump, bypass and pull-up the datasheet asks for.

| | verdict | schematic (e/w/i) | board (e/w/i) |
| --- | --- | --- | --- |
| `reviewed` | **PASS**, 4 findings waived | 0 / 3 / 0 | 0 / 5 / 7 |
| `as-generated` | **FAIL**, 35 blocking | — | — |

Under KiCad's own checks `reviewed` is spotless — zero DRC violations, zero
unconnected items, zero parity findings, on 9.0.9 and 10.0.4. What the gate
still found is now *answered* rather than pending: seven waivers in
[`motor-driver/gate.toml`](https://github.com/sabas0ba/kicad_skills/blob/main/examples/motor-driver/gate.toml),
each carrying the engineering argument — the charge pump wired the way the
datasheet asks, the escape geometry of a 0.65 mm package, a 2 mA LED branch on
a 1 A track. [REVIEW.md](REVIEW.md) is the pass that decided them.

| as-generated | reviewed |
| --- | --- |
| ![schematic, as generated](motor-driver/images/schematic-as-generated.jpg) | ![schematic, reviewed](motor-driver/images/schematic-reviewed.jpg) |
| ![board front, as generated](motor-driver/images/board-front-as-generated.jpg) | ![board front, reviewed](motor-driver/images/board-front-reviewed.jpg) |
| ![board back, as generated](motor-driver/images/board-back-as-generated.jpg) | ![board back, reviewed](motor-driver/images/board-back-reviewed.jpg) |

The back layer is worth looking at on its own. It is a ground pour with the
clearance around every foreign track and pad cut out of it, computed rather
than assumed — which is what lets the four signals that have to cross
something cross it.

### What this one is honest about

This is the first example that cannot be drawn on one signal layer. A DRV8833
brings VM, GND, VCP and VINT out in the *middle* of a row that also carries
four logic inputs, so whichever way round the connectors go, something has to
cross something. Three consequences show up in the findings, and all three are
real:

* **`track.thin_power`** — nothing leaves a TSSOP-16 wider than 0.3 mm. Two
  0.4 mm tracks and the clearance between them do not fit in a 0.65 mm row
  once the row starts to spread, so every pin escapes narrow and widens when
  it is clear. The rule counts the narrowest millimetre of the net and does
  not know the wide part is a millimetre away.
* **`layout.decoupling_distance` ×3** — the same 0.65 mm pitch is why. The
  escape has to walk the row out to a pitch a bypass capacitor can straddle
  before one can be placed, and that walk is five millimetres. The usual
  answer is a capacitor on the *back* of the board under the pins; this
  generator can only place parts on the front.
* **`analog.missing_decoupling` and `layout.decoupling_distance` on VCP** —
  VCP is a charge pump output and C3 is its flying capacitor, wired to VM
  rather than to ground. It is not decoupling and there is nothing to be
  near.

`route.acute_angle` is the fourth, and is the router's: it turns in 45°
steps and the fan-out leaves at 24°, so a corner between the two is sharper
than 90°. Whether that is worth reporting on a signal net is one of the
questions the rule pass has to answer.

### What building it changed in the toolkit

* **The board net names were wrong.** KiCad names a net after the sheet path
  of the label driving it — `/VM`, not `VM` — and only a power symbol keeps
  its bare name. That was 35 `net_conflict` findings on this board and none
  on buck-5v, because buck-5v's rails all came from power symbols.
* **Rotated footprints did not carry their pads' orientation**, which is one
  `lib_footprint_mismatch` each. Fixing it took buck-5v from three DRC
  warnings to none.
* **Footprints had none of the symbol's fields on them.** KiCad's own "update
  PCB from schematic" copies them across and then its parity check compares
  the two; without them that is one finding per field per part.

## pico-carrier — Raspberry Pi Pico, every pin broken out

A carrier board: the module, two twenty-pin headers beside it, and a 5 V input
that reaches VSYS the way the Pico datasheet asks for.

| | verdict | schematic (e/w/i) | board (e/w/i) |
| --- | --- | --- | --- |
| `reviewed` | **PASS**, 9 findings waived | 0 / 2 / 0 | 0 / 9 / 7 |
| `as-generated` | **FAIL**, 34 blocking | — | — |

Under KiCad's own checks `reviewed` has no errors and no unconnected items, on
9.0.9 and 10.0.4 — one `lib_footprint_mismatch` on the module and two silkscreen
warnings are all that is left. The gate findings are answered in
[`pico-carrier/gate.toml`](https://github.com/sabas0ba/kicad_skills/blob/main/examples/pico-carrier/gate.toml);
the schematic-side decoupling rule now reads pin electrical types, so VBUS —
a rail the *module* drives — is no longer asked for a capacitor at all.

| as-generated | reviewed |
| --- | --- |
| ![schematic, as generated](pico-carrier/images/schematic-as-generated.jpg) | ![schematic, reviewed](pico-carrier/images/schematic-reviewed.jpg) |
| ![board front, as generated](pico-carrier/images/board-front-as-generated.jpg) | ![board front, reviewed](pico-carrier/images/board-front-reviewed.jpg) |
| ![board back, as generated](pico-carrier/images/board-back-as-generated.jpg) | ![board back, reviewed](pico-carrier/images/board-back-reviewed.jpg) |

### What this one is honest about

Most of a carrier is one job done forty times, and the findings are about the
few places where it is not:

* **`analog.missing_decoupling` and `layout.no_decoupling` on VBUS and
  ADC_VREF** — both are rails the *module* drives and this board merely exposes.
  The rules cannot tell a supply this board makes from one it is handed, so they
  ask for a capacitor on a net whose source is somewhere else entirely.
* **`layout.decoupling_distance`** — 9 to 17 mm, and there is nowhere nearer:
  the header sits between the module pin and any part, by design, and the strip
  immediately outboard of the header carries the net name of every one of its
  twenty pins. A capacitor in that strip would be printed over, which is a pad
  that will not wet. The capacitors sit as close as the legend allows.
* **`layout.off_grid_placement`** — the module's pads are on a 2.54 mm pitch, so
  its origin cannot also sit on 0.5 mm, and the headers have to line up with the
  pads rather than with the grid. Two footprints, both correct.

### What building it changed in the toolkit

* **The pinout is read out of the symbol, not typed.** Forty pins typed a second
  time is forty chances to swap two of them, and nothing downstream would
  notice — the board would simply be a different, self-consistent board.
* **Stacked pins are drawn once.** The Pico symbol brings its seven grounds out
  at one point; seven wires and seven ground symbols on that point read as one
  and review as seven.
* **The reference and value now go above and below the symbol**, measured from
  its pins. A fixed offset is right for a two-pin part and lands in the middle
  of the pin labels of a forty-pin one.
* **A power symbol turns to face the way its pin leaves.** KiCad draws them
  pointing down and puts the rail name underneath, which on a twenty-pin header
  is the next pin's label.
* **Footprint uuids are remapped as a set, references included.** KiCad's
  `(group ...)` lists its members by uuid, and the Pico footprint has six of
  them; replacing pad uuids one at a time leaves the groups naming things that
  are no longer there.

## opamp-filter — 1 kHz Sallen-Key low pass, single 5 V

Two MCP6001 singles: one is the filter, the other buffers the half-rail the
filter is referenced to.

| | verdict | schematic (e/w/i) | board (e/w/i) |
| --- | --- | --- | --- |
| `reviewed` | **PASS**, 4 findings waived | 0 / 0 / 0 | 0 / 11 / 6 |
| `as-generated` | **FAIL**, 38 blocking | — | — |

`reviewed` passes KiCad's own DRC with two silkscreen warnings — no
errors, no unconnected items, no parity findings.

| as-generated | reviewed |
| --- | --- |
| ![schematic, as generated](opamp-filter/images/schematic-as-generated.jpg) | ![schematic, reviewed](opamp-filter/images/schematic-reviewed.jpg) |
| ![board front, as generated](opamp-filter/images/board-front-as-generated.jpg) | ![board front, reviewed](opamp-filter/images/board-front-reviewed.jpg) |
| ![board back, as generated](opamp-filter/images/board-back-as-generated.jpg) | ![board back, reviewed](opamp-filter/images/board-back-reviewed.jpg) |

### What this one is honest about

* **`layout.decoupling_distance` on VREF (U2.1 and U2.4)** — VREF is an op-amp
  *output*, the half-rail the signal sits on. It is not a supply and there is
  nothing to decouple it to; C2 is a filter capacitor that happens to land on
  that net. This is the same blind spot as VBUS on the Pico carrier, from the
  other direction.
* **`layout.decoupling_distance` on U1.2 and U2.2** — 7.3 mm, and a SOT-23-5
  is the reason: three pads at 0.95 mm with the supply in the middle, so the
  row has to be walked out before anything can be placed against it. Exactly
  the motor driver's problem two package sizes down, which is what makes it
  worth reporting as a pattern rather than as four separate boards' bad luck.
* **`analog.missing_decoupling` on VREF** — same net, same reason.

The most recognisable generated-schematic trait — **every connection a label,
not a wire** — is now both measured and fixed. `readability.label_only`
counts it (this sheet was 96% labels; KiCad's own demo sheets pass), and the
generator routes the sheet: every one of this design's eight signal nets is a
drawn wire tree, from the jack through the filter to the jack, with junction
dots where the trees branch, and the rule no longer fires at all. One label
per net survives, because the label is what names the net. This round also
added R6 and R7 — the coupling caps' far sides previously floated, which the
new `analog.no_dc_path` rule now catches from the netlist alone.

## fpga-audio — iCE40UP5K to PCM5102A, I2S out

An FPGA, an I2S DAC, the SPI flash the FPGA boots from, a 12 MHz oscillator and
a 1.2 V regulator for the core — on two layers.

| | verdict | schematic (e/w/i) | board (e/w/i) |
| --- | --- | --- | --- |
| `reviewed` | **PASS**, 6 findings waived | 0 / 2 / 0 | 0 / 6 / 9 |
| `as-generated` | **FAIL**, 26 blocking | — | — |

Under KiCad's own checks `reviewed` has no DRC errors, nothing unconnected and
no schematic-parity findings; silkscreen warnings between the fans are all
that is left. The engineering pass in [REVIEW.md](REVIEW.md) found and fixed
four real electrical faults here: the PCM5102A's charge pump was miswired
(flying cap to ground instead of CAPP-CAPM — the DAC had no negative rail),
VCCPLL was tied straight to the core rail instead of RC-filtered from it, the
boot flash had no chip-select pull-up, and the LDO reservoir was undersized.
The first of these is the humbling one: `analog.missing_decoupling` had been
firing on VNEG all along, and the earlier write-up called it a false positive.

| as-generated | reviewed |
| --- | --- |
| ![schematic, as generated](fpga-audio/images/schematic-as-generated.jpg) | ![schematic, reviewed](fpga-audio/images/schematic-reviewed.jpg) |
| ![board front, as generated](fpga-audio/images/board-front-as-generated.jpg) | ![board front, reviewed](fpga-audio/images/board-front-reviewed.jpg) |
| ![board back, as generated](fpga-audio/images/board-back-as-generated.jpg) | ![board back, reviewed](fpga-audio/images/board-back-reviewed.jpg) |

### What this one is honest about

**A 0.5 mm pitch QFN with pads on four sides is not a two layer board.** A real
iCE40 design drops each pin straight into an inner layer; with no inner layer
all forty-eight have to fan out on the top, at 0.2 mm track and 0.2 mm
clearance, which is a fine-line process. The cost is the first thing you see in
the plot: a 7 mm chip needs a 26 mm square of board around it before anything
else can be placed, and everything that talks to it is pushed to the edges.

That is the answer to "can it be done on two layers": yes, and you would not
want to. The board is here because the answer is worth having in a form you can
open in KiCad rather than take on trust.

The findings that follow from it, at the scale a 48-pin part gives them:

* **`layout.decoupling_distance` × 16** — the escape has to walk the row out
  to a routable pitch before a capacitor can be placed against it, and that
  walk is most of the budget. The same finding as the motor driver and the
  op-amp filter, three package sizes apart, which is what makes it a pattern
  rather than three boards' bad luck. The *via* half of the same complaint —
  `layout.decoupling_via`, nine of them at first — is gone outright: every
  0603's ground via now sits anchored against its own pad, on the far side
  from the supply, with a 1.2 mm stub as the whole loop.
* **`track.thin_power` at 0.2 mm** — nothing leaves this package wider.
* **`net.single_pin` × 31** — every unused pin has a net of its own, named
  `unconnected-(U1A-IOT_36b-Pad25)` by KiCad. A no-connect flag is a decision,
  not a defect, and the rule cannot tell.
* **`erc.pin_to_pin` on VCCPLL** — KiCad's iCE40 symbol declares VCCPLL a power
  *output*, so tying it to the regulator's output is two power outputs wired
  together. The datasheet would rather see it filtered from the core rail than
  tied to it, and the review round agreed with both: the reviewed board now
  takes VCCPLL through a 100 Ω / 10 µF + 100 nF RC from the core rail, which
  retired the ERC finding and the noise path in one move.

### What building it changed in the toolkit

Five things, each of which had been quietly producing a board that was not the
board the schematic described:

* **Two nets touching on the sheet is not something an endpoint check can see.**
  A wire that ends *on* another wire joins them, and where every pin drags a
  stub behind it that is the common case. `schematic_shorts` looks for it now
  and found three here — one had merged the 1.2 V and 3.3 V rails, and the only
  visible sign was KiCad's parity check disagreeing about a net name.
* **A symbol drawn in four units has to be placed four times.** All 48 pins
  under `(unit 1)` leaves three quarters of them in units that were never
  placed: 25 parity findings, and a sheet nobody can read.
* **A pin the design does not use still has a net**, and a board that leaves the
  pad bare disagrees with the netlist about every one of them.
* **A QFN counts anticlockwise**, so its east and north rows run bottom-to-top
  and right-to-left; handing them to the fan-out in number order made every
  escape on those sides cross every other one.
* **Two rectangles that meet at a corner are further apart than growing both and
  asking whether they intersect makes them look** — which is every pair of pads
  on the corner of a QFN.

## What the five of them say together

Three findings appear on every board that has a fine-pitch part, and they all
trace to the same fact — the escape from the package eats the distance budget
before any component can be placed:

| | motor-driver | opamp-filter | fpga-audio |
| --- | --- | --- | --- |
| package | TSSOP-16, 0.65 mm | SOT-23-5, 0.95 mm | QFN-48, 0.5 mm |
| `layout.decoupling_distance` | 3 | 4 | 16 |
| `track.thin_power` | 0.3 mm | 0.2 mm | 0.2 mm |

The two blind spots that showed up from opposite directions — decoupling asked
of VBUS, which the *module* drives, and of VREF, which an op-amp *output*
makes — are closed on the schematic side: `analog.missing_decoupling` now
reads pin electrical types, asks only where a `power_in` pin is, and never
asks on a net an output pin drives. The board file carries no pin types, so
the board-side rule keeps its blindness and the waivers say so.

And the two things no rule caught at all became rules in the review round:

* **Every connection a label, not a wire** → `readability.label_only`, plus a
  sheet router in the generator (straight, L-, Z- and detour-shaped wire
  trees with junction dots; 0 findings on KiCad's own demo sheets, and now 0
  on every `reviewed/` sheet — the handful of nets still labelled are the
  cross-sheet hauls a human would label too).
* **Autorouted-looking routing** → `route.detour` (routed length against the
  minimum spanning tree of the net's pads; 0 findings on the demo corpus at
  the shipped 4x) and `route.return_path` (signal over cuts in the other
  layer's ground fill — the electromagnetic cost of the two-layer choice).

[REVIEW.md](REVIEW.md) is the full pass: what was found, what each finding
became, and the calibration of every new rule against KiCad's demo corpus.
