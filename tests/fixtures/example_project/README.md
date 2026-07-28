# Example project

A tiny but complete KiCad project used by the test-suite and by the skill
documentation: a passive RC low pass (R1/C1, fc ≈ 1.6 kHz) followed by an LM321
unity gain buffer, with a local decoupling capacitor (C2) and a 3-pin connector.

* `example.kicad_sch` — schematic. The symbols are the real ones from the KiCad
  standard libraries, embedded in `lib_symbols` as KiCad itself does. It is
  written in the **oldest format the CI matrix covers**, with no KiCad 10 only
  tokens (`duplicate_pin_numbers_are_jumpers`, `in_pos_files`, `(power global)`),
  because KiCad never reads a file newer than itself — one such token and
  KiCad 9 refuses the whole document.
* `example.kicad_pcb` — 40 × 30 mm two layer board, routed, with a **filled**
  ground pour on `B.Cu` (saved by KiCad 9's pcbnew, so both matrix versions read
  it). The fill is committed on purpose: KiCad 9's `pcb drc` has no
  `--refill-zones`, so an unfilled pour there means GND reads as unconnected —
  which is exactly what a real project would suffer. The footprints are
  **simplified copies** of the standard ones (pads and reference text only), so
  DRC reports a `lib_footprint_mismatch` warning for each of them. That is
  expected: it keeps the fixture small and readable while still exercising every
  code path.
* `example.kicad_pro` — project settings (net classes, design rules).

The board is DRC clean apart from those library mismatches, so a new error in
`eda pcb review tests/fixtures/example_project` means the toolkit changed
behaviour, not that the board is broken.
