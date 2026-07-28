---
name: spice-simulation
description: Simulate analog circuits with ngspice - DC operating point, DC sweep, AC/Bode, transient, noise, THD - plus Monte Carlo component tolerance analysis and temperature sweeps, with measurements (gain, -3 dB bandwidth, phase margin, rise time, overshoot, spread) and plots. Use when a filter, amplifier, regulator, bias network or driver has to be verified, dimensioned, toleranced or debugged, or when a KiCad schematic needs simulating.
---

# Analog simulation (ngspice)

> One of the [kicad_skills](https://github.com/sabas0ba/kicad_skills) usage guides for the
> `eda` CLI — [all six](README.md). Plain Markdown: read it directly, or hand it to
> whatever assistant you use.

ngspice runs inside the container (see the `eda-environment` guide), the same engine
KiCad's built-in simulator uses, so results agree with what the user sees in
Eeschema.

## Workflow

```bash
# 1. write the deck (plain SPICE, any editor/tool)
# 2. cheap syntax check before burning a run
./bin/eda.sh sim lint sim/rc.cir
# 3. run it: writes raw + CSV + measurements + PNG plots
./bin/eda.sh sim run sim/rc.cir -o sim/out
```

`sim run` returns JSON: `ok`, `errors` (parsed from the ngspice log), and one
entry per analysis with `csv`, `plot` (PNG) and `measurements`. Read the PNG
with the Read tool when the shape of the response matters — the numbers alone
hide ringing, clipping and convergence artefacts.

## Deck conventions that avoid the usual failures

```spice
* first line is ALWAYS the title - it is discarded
V1 in 0 DC 0 AC 1 SIN(0 1 1k)      * AC 1 = 1 V stimulus for .ac
R1 in out 1k
C1 out 0 159.155n
.ac dec 200 10 100k                * decade sweep, 200 points/decade
.tran 10u 5m
.end
```

* Node `0` is ground and must exist, otherwise ngspice reports a singular matrix.
* Give the AC source `AC 1` so gains come out in dB directly.
* `.include`/`.lib` paths are resolved relative to the deck; model files next to
  the deck (`*.lib`, `*.mod`, `*.sub`) are copied into the work directory.
* Sweep at least a decade beyond the region of interest; use `dec` (log) for AC.
* For transient runs, make the step small enough (≥20 points per period of the
  fastest signal) or the measurements will be wrong rather than noisy.

## What is measured automatically

| Analysis | Measurements returned |
| --- | --- |
| `.ac` | max gain (dB) and its frequency, gain/phase at both ends, **-3 dB corner(s)**, bandwidth, unity gain frequency, **phase margin** |
| `.tran` | min/max/peak-to-peak/mean/RMS/final, plus either step metrics (**10-90 % rise time, overshoot %, 2 % settling time**) or periodic metrics (**amplitude, estimated frequency**) depending on the waveform |
| `.dc` | min/max, endpoints, maximum slope (small-signal gain), monotonicity |
| `.op` | every node voltage and branch current |

THD needs the fundamental, so it is a separate call:

```bash
./bin/eda.sh sim measure sim/out/work/sim.raw --thd "v(out)" --fundamental 1000 --skip 1m
```

## Will it still work with real parts? (tolerance analysis)

A nominal simulation is the one circuit you will never build. Vary the parts
inside their tolerance and look at the spread of the number you actually care
about:

```bash
./bin/eda.sh sim montecarlo sim/rc.cir -o sim/mc \
    --vary R1=1% --vary C1=10% \
    --metric ac.v(out).f_minus_3db_hz \
    --trials 200
```

Returns `statistics` (mean, stdev, min/max, p05/median/p95, `spread_pct`),
`nominal_metric` for reference, a `histogram.png` and `trials.csv`. The metric
is a path into the usual measurements: `<analysis>.<signal>.<key>` — e.g.
`ac.v(out).gain_db_max`, `tran.v(out).overshoot_pct`, or `op.v(out)`.

* `--distribution normal` (default) treats the tolerance as ±3σ, clipped to the
  band — how reels of parts actually behave.
* `--distribution uniform` is the honest choice when you know nothing about the
  distribution.
* `--distribution worst` samples only the two extremes, which finds the corners
  fastest.
* `--vary` also accepts `.param` names, so anything parameterised in the deck
  can be swept, not just R/C/L.
* `--seed` makes a run reproducible; quote the seed when you quote the result.

Judge the result against the requirement, not against the nominal: "fc =
1000 Hz nominal, 5th–95th percentile 968–1035 Hz with 1 % parts, spec is
±5 % → passes with margin".

## Does it survive the temperature range?

```bash
./bin/eda.sh sim temperature sim/bias.cir -o sim/temp \
    --temperatures -40 0 25 85 125 --metric op.v(out)
```

Runs the deck at each temperature (`.temp`) and reports the metric per point
plus `drift_per_celsius`. Only models with temperature coefficients will move —
ideal R and C do not, so a flat result means the models are ideal, not that the
circuit is stable.

## Simulating a KiCad schematic

```bash
./bin/eda.sh sim netlist hardware/amp.kicad_sch -o sim/amp.cir   # kicad-cli export
./bin/eda.sh sim run sim/amp.cir -o sim/out
```

The export only produces a usable deck when the symbols carry Spice model
fields (`Spice_Primitive`, `Spice_Model`, `.model`/`.subckt` includes). For a
plain schematic it is usually faster and more honest to hand-write a deck for
the sub-circuit under investigation and say so in the report.

## Interpreting the result honestly

* State the operating point first — a simulation of a circuit biased into the
  rail is meaningless however pretty the AC plot is.
* Compare against the closed-form expectation (`fc = 1/(2πRC)`, `A = -Rf/Rin`,
  …). If simulation and theory disagree, the deck is usually wrong.
* Convergence failures, `singular matrix`, `timestep too small` are reported in
  `errors`; fix the circuit (add a DC path to ground, `.options` relaxation)
  rather than ignoring them.
* Ideal parts are ideal: op-amp models without supply pins never clip, real
  regulators have ESR-dependent stability. Say which effects the model omits.
* Report `value (analysis, conditions)` and keep the generated PNG/CSV in the
  repository next to the design when the result matters.
