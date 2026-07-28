# kicad_skills — containerised circuit design skills

A set of Claude Code skills, and the toolkit behind them, for doing real circuit
design work: finding and reading datasheets, simulating analog circuits, and
reviewing KiCad schematics and PCB artwork.

**Everything runs inside a container.** KiCad, ngspice and the Python
dependencies are never installed on the host — the only host requirements are
`docker` and `bash`. The KiCad release is a build argument, so it can be pinned
per project.

```bash
./bin/eda.sh doctor                                   # builds the image on first use
./bin/eda.sh sch review  hardware/ --text
./bin/eda.sh pcb review  hardware/ --text
./bin/eda.sh pcb render  hardware/ -o /tmp/art
./bin/eda.sh sim run     sim/filter.cir -o sim/out
./bin/eda.sh datasheet parse docs/datasheets/lm321.pdf -o /tmp/ds
```

## Skills

Skills live in `.claude/skills/` and are picked up automatically when Claude
Code runs in this repository. Copy the directory into another project (together
with `bin/`, `docker/`, `src/` and `Makefile`) to use them there.

| Skill | What it does |
| --- | --- |
| [`datasheet-analysis`](.claude/skills/datasheet-analysis/SKILL.md) | Extract text, parameter tables, embedded figures and rendered page images from a datasheet PDF |
| [`spice-simulation`](.claude/skills/spice-simulation/SKILL.md) | Run ngspice (op / dc / ac / tran / noise), Monte Carlo tolerance analysis and temperature sweeps, with measurements and plots |
| [`kicad-schematic-review`](.claude/skills/kicad-schematic-review/SKILL.md) | Read `.kicad_sch`, extract components/nets/hierarchy, run ERC plus design heuristics |
| [`kicad-pcb-review`](.claude/skills/kicad-pcb-review/SKILL.md) | Read `.kicad_pcb`, run DRC and layout heuristics, render layers and 3D views as PNG |
| [`kicad-fabrication-output`](.claude/skills/kicad-fabrication-output/SKILL.md) | Gerbers, drill, pick-and-place, BOM, STEP/IPC-2581, zipped with a manifest |
| [`eda-environment`](.claude/skills/eda-environment/SKILL.md) | Build, pin, verify and troubleshoot the container |

## Quick start

```bash
git clone <this repo> && cd kicad_skills
make build                     # ~5 min, downloads the KiCad image (about 4 GB)
./bin/eda.sh doctor               # {"kicad_cli": "10.0.4", "ngspice": "...", "ok": true}
make test                      # full test-suite inside the container
make smoke                     # end-to-end run against the example project
```

Then point the commands at a real project:

```bash
cd ~/projects/my-board
~/kicad_skills/bin/eda.sh sch review . --text
```

`bin/eda.sh` mounts the git repository root that contains the current directory
at `/work`, runs as your uid/gid (no root-owned files), and gives the container
no network.

## Pinning the KiCad version

```bash
KICAD_VERSION=10.0.4 make build     # default, current stable
KICAD_VERSION=9.0.9  make build     # a second image for older projects
KICAD_VERSION=9.0.9 ./bin/eda.sh pcb review board.kicad_pcb
```

Each version produces its own image tag (`eda-toolkit:<version>`) so several can
coexist. Tags: <https://hub.docker.com/r/kicad/kicad/tags>. KiCad upgrades
project files in place when it opens something older than itself, so match the
version to the project.

## Command reference

```
eda doctor                                    tool versions in the environment

eda datasheet info   PDF
eda datasheet find   PDF QUERY... [--regex]
eda datasheet text   PDF [--pages 1-5] [--layout] [--ocr]
eda datasheet tables PDF [--pages 5]
eda datasheet images PDF -o DIR [--pages]
eda datasheet pages  PDF -o DIR [--pages] [--dpi 200]
eda datasheet parse  PDF -o DIR [--renders] [--ocr]

eda sim lint     NETLIST
eda sim run      NETLIST -o DIR [--no-plots] [--timeout S]
eda sim montecarlo NETLIST -o DIR --vary R1=1% --metric ac.v(out).f_minus_3db_hz [--trials N]
eda sim temperature NETLIST -o DIR [--temperatures -40 25 85] [--metric ...]
eda sim measure  RAW [--thd SIGNAL --fundamental HZ] [--skip S]
eda sim plot     RAW -o DIR [--signals ...]
eda sim netlist  SCHEMATIC -o FILE           export a SPICE deck from KiCad

eda sch info     TARGET [--no-cli]
eda sch review   TARGET [--text] [--collapse N] [-o report.json] [--no-cli]
eda sch bom      TARGET -o bom.csv [--group-by ...] [--fields ...]
eda sch erc      TARGET                      raw KiCad ERC JSON
eda sch netlist  TARGET [--format json|kicadxml|spice|...] [-o FILE]
eda sch render   TARGET -o DIR [--dpi 200]

eda pcb info     TARGET
eda pcb review   TARGET [--text] [--collapse N] [--threshold KEY=VALUE] [-o report.json]
eda pcb fab      TARGET -o DIR [--step] [--ipc2581] [--pos-format csv]
eda pcb drc      TARGET [--no-parity]        raw KiCad DRC JSON
eda pcb render   TARGET -o DIR [--views ...] [--per-layer] [--no-3d] [--dpi 300]
eda pcb stats    TARGET
```

`TARGET` accepts a `.kicad_sch`/`.kicad_pcb` file, a `.kicad_pro`, or a project
directory. All commands print JSON by default (the review commands also take
`--text` for a human readable digest) and exit `2` when a review found errors,
so they drop straight into CI.

### Environment variables

| Variable | Effect |
| --- | --- |
| `KICAD_VERSION` | KiCad release / image tag to use (default `10.0.4`) |
| `EDA_IMAGE` | override the image name entirely |
| `EDA_NETWORK` | `1` gives the container network access (default: offline) |
| `EDA_MOUNT` | host directory to mount at `/work` (default: git root or `$PWD`) |
| `EDA_ENV_PASSTHROUGH` | extra environment variable names to forward |
| `EDA_DOCKER_ARGS` | extra arguments for `docker run` |

## How it fits together

```
bin/eda.sh         host wrapper: docker run, uid mapping, network policy, path rewriting
docker/Dockerfile  kicad/kicad:<version> + ngspice + an isolated virtualenv
src/eda_toolkit/
├── cli.py                 the `eda` command
├── datasheet/             PDF text, table, image and page extraction
├── spice/                 ngspice runner, raw-file parser, measurements, plots
└── kicad/                 s-expression parser, schematic/board models,
                           kicad-cli wrapper, review rules, renderers
tests/                     pytest suite + fixtures + smoke test
.claude/skills/            the skills themselves
```

Design notes:

* **KiCad is the source of truth** where it can be: ERC, DRC, netlist export
  and plotting all go through `kicad-cli`. The hand-written parsers add what
  the CLI does not expose (geometry, stackup, pad positions, properties) and
  provide a pure-python fallback so the library, and the test-suite, still work
  without KiCad installed.
* **Review = rules + pictures.** The rule engine produces structured findings;
  the renderers produce PNGs so the parts a rule cannot judge (placement,
  routing quality, silkscreen legibility, signal flow) can be looked at.
* **Findings are typed** (`rule`, `severity`, `message`, `location`, `details`)
  and every rule is a small function registered in a list — adding a check is a
  dozen lines plus a test.

## Review quality

The rules were tuned by running them over the 18 KiCad demo projects that ship
with the image (`/usr/share/kicad/demos`), which is a far harsher corpus than
the test fixture. That pass produced three fixes:

* KiCad's global library tables are now seeded from Python, not only by the
  container entrypoint. Without them every symbol reports "the current
  configuration does not include the library ...", which was 1965 findings of
  pure noise across the corpus.
* Power symbols are recognised by KiCad's real invariant - the `#` reference
  prefix - not just by a `power:` library id. Older projects were reporting
  every GND symbol as a component with no footprint.
* `net.single_pin` is graded by whether the net was named by the designer, and
  any rule that fires more than six times is folded into one finding with a
  count. On `interf_u` the review went from 113 warnings to 2.

Re-run it after changing a rule:

```bash
docker run --rm -v "$PWD:/work" -w /work -e PYTHONPATH=/work/src \
  --entrypoint python3 eda-toolkit:10.0.4 tools/review_demos.py /tmp/out
```

## Reproducibility

Every external input is pinned, and the pins are enforced by
`tests/test_pinning.py` (which runs in CI):

| Input | Pin |
| --- | --- |
| KiCad base image | manifest digest, per version, in [`docker/kicad-digests.txt`](docker/kicad-digests.txt) |
| pip / uv | exact version + wheel SHA-256 (`ARG PIP_*`, [`docker/uv-bootstrap.txt`](docker/uv-bootstrap.txt)) |
| Python packages | [`uv.lock`](uv.lock) - exact versions + artifact hashes for the whole tree, installed with `uv sync --frozen` |
| GitHub Actions | 40 character commit SHA, with the tag in a trailing comment |
| CI runner | `ubuntu-24.04`, never `-latest`; Python `3.13.5` |

Keeping them current:

* Dependabot ([`.github/dependabot.yml`](.github/dependabot.yml)) opens weekly
  PRs for `uv.lock` and the actions.
* `make check-pins` (and the weekly [`pins.yml`](.github/workflows/pins.yml)
  workflow) covers what Dependabot cannot parse: the KiCad image digest and the
  pip/uv bootstrap wheels. It opens a PR when upstream moved. The default KiCad
  release is never bumped automatically.

To change a dependency, edit `pyproject.toml` and regenerate the lock:

```bash
make lock          # uv lock
make rebuild
```

To use another KiCad release, add its digest to `docker/kicad-digests.txt`
(the file documents the one-liner) and build with `KICAD_VERSION=<version>`.
The build fails loudly if a version has no pinned digest.

## Testing

```bash
make test          # everything, inside the container
make test-coverage # the same, with a coverage report
make test-host     # pure-python subset on the host (needs a local venv)
make lint          # ruff
make smoke         # end-to-end: every skill's main command
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs two jobs on
every push and pull request: the pure-python suite against the hash-pinned
dependency set, and the full suite plus the smoke test inside the freshly built
container (which also uploads the rendered example artwork as an artifact).
Nothing in CI touches the network beyond pulling the pinned base image and the
locked wheels.

The suite covers the s-expression parser, schematic and board models, every
review rule, the ngspice raw-file parser (ASCII/binary, real/complex), the
measurement maths (checked against analytically known circuits), the datasheet
PDF extraction (against a generated PDF), and the CLI. Tests that
need `kicad-cli` or `ngspice` are marked and skipped automatically outside the
container.

Integration tests run the real tools: KiCad ERC and DRC on the example project,
a deliberately introduced short to prove DRC catches it, netlist comparison
between `kicad-cli` and the fallback extractor, layer/3D rendering, and an RC
filter whose simulated −3 dB corner is compared against `1/(2πRC)`.

## Limitations

* Getting datasheet PDFs is out of scope - the container has no network. Save
  the PDF into the repository first, then parse it.
* Embedded-image extraction only recovers raster images. Most datasheet figures
  are vector art — render the page instead (`datasheet pages`).
* `sim netlist` (KiCad → SPICE) only produces a usable deck when the schematic
  carries Spice model fields.
* The review rules encode common practice, not your fab's or your project's
  rules. Thresholds are adjustable (`--threshold`); the defaults are
  conservative low-cost-fab values.
* OCR of scanned datasheets needs `tesseract`, installed only if a Debian mirror
  is reachable at build time (`WITH_OCR=0` to skip). Everything else works
  without it.
