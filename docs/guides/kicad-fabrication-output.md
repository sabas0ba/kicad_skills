---
name: kicad-fabrication-output
description: Produce the manufacturing package from a KiCad project - Gerbers, Excellon drill files, pick-and-place, BOM, optional STEP and IPC-2581, zipped and with a manifest. Use when a board has to be sent to a fab or assembly house, when gerbers/drill/BOM/pick-and-place files are requested, or when checking that a design is ready to order.
---

# Fabrication output

> One of the [kicad_skills](https://github.com/sabas0ba/kicad_skills) usage guides for the
> `eda` CLI — [all six](README.md). Plain Markdown: read it directly, or hand it to
> whatever assistant you use.

Turns a finished board into the files a manufacturer accepts. Runs in the
container (see the `eda-environment` guide), offline.

## One command for the whole package

```bash
./bin/eda.sh pcb fab hardware/ -o build/fab
```

Writes, and records in `build/fab/manifest.json`:

```
build/fab/
├── gerbers/          copper, mask, paste, silkscreen, Edge.Cuts (X2, 6 digits)
│   ├── *.gbr
│   ├── *.drl         Excellon, mm, with a PDF map
│   └── drill-report.txt
├── <board>-pos.csv   pick and place, mm, DNP excluded
├── <board>-bom.csv   grouped by value + footprint, DNP excluded
├── <board>-fab.zip   everything above, ready to upload
└── manifest.json     what was written, and what failed
```

Options: `--step` (3D model for mechanical review), `--ipc2581` (single-file
exchange format some fabs prefer), `--fab-layers` (also plot F.Fab/B.Fab for
the assembler), `--include-dnp`, `--pos-format ascii|csv|gerber`, `--no-zip`.

Each step is independent: if STEP export fails, the Gerbers are still written
and the failure is listed in `manifest.json.errors`. The command exits `2` when
anything failed.

## Before you send it

**Run the reviews first.** A fab package built from a board with DRC errors is
just a faster way to make scrap:

```bash
./bin/eda.sh pcb review hardware/ --text     # must be error free
./bin/eda.sh sch review hardware/ --text
./bin/eda.sh pcb fab    hardware/ -o build/fab
```

Then check the package itself:

* Open `manifest.json` - `board_size_mm`, `layer_count` and the step list should
  match what you ordered.
* `drill-report.txt` lists the hole count and sizes; compare against the fab's
  minimum drill.
* Render the layers (`pcb render`) and **look at them** - a missing layer in the
  Gerber set is invisible in a file listing but obvious in a plot.
* Confirm the pick and place origin and units suit the assembler. Many want the
  drill/place origin rather than the sheet origin.

## Bill of materials on its own

```bash
./bin/eda.sh sch bom hardware/ -o build/bom.csv
./bin/eda.sh sch bom hardware/ -o build/bom.csv --fields Reference,Value,Footprint,MPN,QUANTITY
```

Returns `line_items` and `total_parts` alongside the CSV, so a BOM that is
suddenly one part short is easy to spot between revisions. Grouping defaults to
`Value,Footprint`; DNP parts are excluded unless `--include-dnp` is given.

For ordering, the BOM needs manufacturer part numbers: add an `MPN` (or
`Manufacturer_Part_Number`) field to the symbols in the schematic and export it
with `--fields`. The toolkit does not invent part numbers.

## What this does not do

* No panelisation, no fab-specific stackup files, no impedance control
  documentation - talk to the fab about those.
* No design rule translation: `pcb review --threshold ...` uses your numbers,
  it does not know your fab's capability table.
* The zip layout suits most cheap fabs (JLC, PCBWay, Aisler, OSHPark all accept
  it), but some ask for a specific naming scheme. Check their guide once per
  vendor.
