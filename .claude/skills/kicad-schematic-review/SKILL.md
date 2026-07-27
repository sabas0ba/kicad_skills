---
name: kicad-schematic-review
description: Read a KiCad schematic (.kicad_sch) to extract components, nets and hierarchy, and review it - ERC plus design checks for decoupling, floating inputs, single-pin nets, annotation, missing footprints, I2C pull-ups and LED series resistors. Use when asked to review, check, understand or summarise a schematic or circuit design in a KiCad project.
---

# KiCad schematic review

Reads `.kicad_sch` files and reviews them. Runs in the container
(`eda-environment` skill); pass paths relative to the repository root.

## The three commands

```bash
./bin/eda sch info    hardware/               # structure: components, nets, hierarchy
./bin/eda sch review  hardware/ --text        # ERC + design heuristics
./bin/eda sch render  hardware/ -o /tmp/sch   # PNG of each sheet, to look at
```

The target may be a `.kicad_sch`, a `.kicad_pro`, or the project directory
(the root sheet is found automatically, sub-sheets are followed).

## How to actually review a schematic

1. **`sch info`** — get the parts list, the net list and the sheet hierarchy.
   Note the supply rails, the ICs and anything unfamiliar.
2. **`sch review --text`** — machine findings. Every `error` must be explained
   or fixed; every `warning` must be judged, not blindly reported.
3. **`sch render` + Read the PNG** — the machine cannot see intent. Look at the
   drawing to check signal flow, that the topology is what the user described,
   and that nothing important is drawn but disconnected.
4. **Datasheets** — for each IC, use the `datasheet-lookup` /
   `datasheet-analysis` skills to check the actual part against its ratings:
   supply range, input common-mode range, required external components,
   pins that must not float.
5. Report findings grouped by severity, each with the reference designator or
   net name, why it matters, and the concrete fix.

## What `sch review` checks

**From KiCad's own ERC** (`erc.*` rules — authoritative): unconnected pins and
wire endpoints, conflicting drivers, power pins not driven, duplicate
references, library symbol mismatches, off-grid endpoints, bus errors.

**Design heuristics on top of the netlist:**

| Rule | Meaning |
| --- | --- |
| `net.single_pin` | net reaches exactly one pin — usually a wiring mistake |
| `net.no_driver` | a net with only input pins, nothing drives it |
| `analog.missing_decoupling` | an IC supply net with no capacitor to ground |
| `analog.i2c_pullup` | a net named SDA/SCL with no resistor on it |
| `analog.led_no_series_resistor` | LED with no current limiting on either terminal |
| `power.no_ground` / `power.no_supply` / `power.many_supplies` | rail sanity |
| `schematic.duplicate_reference` / `schematic.unannotated` | annotation problems |
| `schematic.missing_footprint` / `missing_value` / `missing_datasheet` | field completeness |
| `schematic.dnp` | DNP parts, listed so they are not forgotten in a BOM |

Exit code is `2` when there is at least one error, `0` otherwise — usable in CI.

## Things the tool cannot check (do these by hand)

* Whether the *topology* implements the intended function.
* Component values: gain, cut-off frequency, current limits, divider ratios,
  time constants. Compute them, or verify with the `spice-simulation` skill.
* Power budget and thermal dissipation.
* Whether a part's operating conditions are respected (needs the datasheet).
* Reset/boot strapping, protection against reverse polarity and ESD,
  connector pinout against the mating part.

## Notes

* With `--no-cli` the review runs without KiCad, using a pure-python
  connectivity extractor (`netlist_source: geometry-fallback`). It agrees with
  KiCad on ordinary sheets but resolves cross-sheet connections only through
  power symbols and global labels, and it cannot run ERC. Prefer the container.
* `./bin/eda sch netlist <target> --format kicadxml -o out.net` exports the
  netlist for other tools; `--format json` gives the normalised structure the
  review uses.
* `./bin/eda sch erc <target>` returns KiCad's raw ERC JSON when the details of
  a violation are needed.
