# Example output

Everything here was produced by this toolkit and is committed so the README can
show real output rather than describe it. Regenerate any of it with the commands
below; nothing is hand-edited beyond downscaling for the repository.

## Open Air Max (KiCad demo project)

`openair-max-*.jpg` are derived from the **Open Air Max** demo project by
[AirGradient](https://www.airgradient.com/), which ships with KiCad in
`/usr/share/kicad/demos/openair-max` and is licensed
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). These rendered
images are adaptations of that work and are therefore **also CC BY-SA 4.0** —
unlike the rest of this repository, which is Apache-2.0.

| File | What it is |
| --- | --- |
| `openair-max-layers.jpg` | the board contact sheet: 6 composite views, 4 copper layers, 3 renders |
| `openair-max-3d.jpg` | the isometric 3D render |
| `openair-max-schematic.jpg` | sheet 1 of 3 of the schematic |
| `openair-max-crosstalk.jpg` | `eda pcb crosstalk` — the ten worst coupled runs struck through in magenta where they sit, numbered by each pair's `index` in the JSON. Pair 1 is the I2C pair: `/SCL` beside `/SDA` for 65.5 mm at a 0.198 mm gap on `F.Cu`, which the solver prices at 94 mV of near-end noise on a 3.3 V, 1 ns edge — the number the `emc.parallel_run` warning could only gesture at, at the place a reviewer can walk to |

```bash
# inside the container, because the demos live in the image and /usr/share is read-only
docker run --rm -v "$PWD:/work" -w /work -e PYTHONPATH=/work/src \
  --entrypoint bash eda-toolkit:10.0.4 -c '
    cp -r /usr/share/kicad/demos/openair-max /tmp/proj
    python3 -m eda_toolkit.cli report /tmp/proj -o /work/build/openair-max \
      --dpi 150 --glb --title "Open Air Max (KiCad demo)"
    python3 -m eda_toolkit.cli pcb crosstalk /tmp/proj -o /work/build/openair-max'
```

## Thermal map and heating curve (buck-5v example)

`buck-5v-thermal.jpg` and `buck-5v-heating.jpg` are `eda pcb thermal` over
[`examples/buck-5v/reviewed`](https://github.com/sabas0ba/kicad_skills/tree/main/examples/buck-5v)
with the LM2596 stated at 1.5 W: the steady map marks the hottest cell (28.8 K
over ambient, at x=160.3 y=70.3 on the board), and the curve is `--transient
300` from power-on — 63% of the steady rise at 41 s, 95% of it reached by five
minutes. The transient's energy books balance to the solver's own stopping
tolerance, which the JSON carries as `transient.balance.residual`.

| File | What it is |
| --- | --- |
| `buck-5v-thermal.jpg` | the steady temperature map, hotspot marked |
| `buck-5v-heating.jpg` | the heating curve, the 63% clock marked where it landed |

```bash
./bin/eda.sh pcb thermal examples/buck-5v/reviewed --power U1=1.5   --transient 300 -o build/thermal
```

## Revision diff

`diff-schematic.jpg` and `diff-board.jpg` are `eda diff` run over two revisions of
the test fixture, with C2 moved on the sheet, R1 moved on the board and its value
changed from 10k to 4k7. Red is what the old revision had and the new one does
not; green is the other way round, so a part that moved is both. Both are the
zoomed detail crop the command writes alongside the full page — on an A4 sheet the
change is a few hundred pixels of two megapixels.

| File | What it is |
| --- | --- |
| `diff-schematic.jpg` | C2 red where it was, green where it is now; R1's value change marked |
| `diff-board.jpg` | the same R1 move on the front copper |

```bash
git worktree add /tmp/base <ref>
./bin/eda.sh diff /tmp/base/hardware hardware/ -o build/diff
```

Both come from this repository's own fixture, so they are Apache-2.0 like the
rest of it.

## RC low-pass reference circuit

`rc-lowpass-*.jpg` come from [`tests/fixtures/spice/rc_lowpass.cir`](https://github.com/sabas0ba/kicad_skills/blob/main/tests/fixtures/spice/rc_lowpass.cir),
which is part of this repository (Apache-2.0). Its corner frequency is
1/(2πRC) = 1000 Hz by construction, which is what the test-suite checks against.

```bash
./bin/eda.sh sim run tests/fixtures/spice/rc_lowpass.cir -o build/sim
./bin/eda.sh sim montecarlo tests/fixtures/spice/rc_lowpass.cir -o build/mc \
  --vary R1=1% --vary C1=10% --metric 'ac.v(out).f_minus_3db_hz' --trials 200
```
