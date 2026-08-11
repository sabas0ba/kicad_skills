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

## When these were made, and by what

Both variants carry it in their title block, in the comment fields, on the
schematic and on the board:

```
(comment 1 "generated 2026-08-11 by Claude Code")
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
| `reviewed` | **PASS** | 0 / 0 / 0 | 0 / 1 / 7 |
| `as-generated` | **FAIL**, 6 blocking | 3 / 3 / 14 | 0 / 5 / 8 |

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
vias dropping into it. The generated variant has no pour at all:

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
| `reviewed` | **FAIL**, 7 blocking | 0 / 3 / 0 | 0 / 5 / 6 |
| `as-generated` | **FAIL**, 38 blocking | 3 / 4 / 15 | 8 / 20 / 7 |

`reviewed` does not pass, and is committed failing on purpose. Under KiCad's
own checks it is spotless — zero DRC violations, zero unconnected items, zero
schematic-parity findings, on both 9.0.9 and 10.0.4 — so everything left is
this toolkit's own opinion about a board KiCad is happy with. That is the
interesting part, and papering over it with waivers would throw it away. What
each finding turns into is decided once all five examples exist.

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

## Still to come

`pico-carrier`, `opamp-filter`, `fpga-audio` — in that order.
