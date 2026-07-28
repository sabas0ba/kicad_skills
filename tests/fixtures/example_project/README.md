# Example project

A tiny but complete KiCad project used by the test-suite and by the skill
documentation: a passive RC low pass (R1/C1, fc ≈ 1.6 kHz) followed by an LM321
unity gain buffer, with a local decoupling capacitor (C2) and a 3-pin connector.

* `example.kicad_sch` — schematic. The symbols are the real ones from the KiCad
  standard libraries, embedded in `lib_symbols` as KiCad itself does.
* `example.kicad_pcb` — 40 × 30 mm two layer board, routed, with a ground pour
  on `B.Cu`. The footprints are **simplified copies** of the standard ones (pads
  and reference text only), so DRC reports a `lib_footprint_mismatch` warning
  for each of them. That is expected: it keeps the fixture small and readable
  while still exercising every code path.
* `example.kicad_pro` — project settings (net classes, design rules).

The board is DRC clean apart from those library mismatches, so a new error in
`eda pcb review tests/fixtures/example_project` means the toolkit changed
behaviour, not that the board is broken.
