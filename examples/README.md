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
./bin/eda.sh gate examples/buck-5v/reviewed     --policy ai-generated --text
./bin/eda.sh gate examples/buck-5v/as-generated --policy ai-generated --text
```

Regenerate them with:

```bash
docker run --rm -u $(id -u):$(id -g) -v "$PWD:/work" -w /work \
  -e PYTHONPATH=/work/src -e HOME=/tmp/eda-home \
  --entrypoint python3 eda-toolkit:9.0.9 tools/make_examples.py examples/
```

The generator reads KiCad's own symbol and footprint libraries, so these are the
real parts, not simplified copies — which is why it runs inside the container.

## buck-5v — 12 V to 5 V at 2 A

LM2596S-5, catch diode, output inductor, screw terminals in and out.

**Schematic — done.** Under KiCad's own ERC:

| | error | warning | info |
| --- | --- | --- | --- |
| `reviewed` | **0** | **0** | **0** |
| `as-generated` | 3 | 3 | 14 |

What separates them, and which check finds it:

| in `as-generated` | found by |
| --- | --- |
| symbols and wires off the 1.27 mm grid | `readability.off_grid_pin` / `_wire` / `_label`, and KiCad's own `erc.endpoint_off_grid` |
| no PWR_FLAG on the externally supplied rails | `erc.power_pin_not_driven` |
| two symbols dropped on the same spot | `readability.overlapping_symbols` |
| no title block, no design notes | `readability.title_block`, `spec.no_design_notes` |
| no tolerance / voltage / current rating, no MPN | `spec.missing_rating`, `spec.missing_part_number` |
| capacitors chosen without derating the rail | `spec.voltage_derating` |

**Board — not finished.** The floorplan and the ground pour are in place, but the
routing is still being worked: `reviewed` does not pass DRC yet. Do not read the
board half of this example as a reference until this note says otherwise.

## Still to come

`motor-driver`, `pico-carrier`, `opamp-filter`, `fpga-audio` — in that order.
