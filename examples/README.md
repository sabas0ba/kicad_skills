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

## Still to come

`motor-driver`, `pico-carrier`, `opamp-filter`, `fpga-audio` — in that order.
