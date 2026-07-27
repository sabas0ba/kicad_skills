# kicad_skills — containerised circuit design skills

A set of Claude Code skills, and the toolkit behind them, for doing real circuit
design work: finding and reading datasheets, simulating analog circuits, and
reviewing KiCad schematics and PCB artwork.

**Everything runs inside a container.** KiCad, ngspice and the Python
dependencies are never installed on the host — the only host requirements are
`docker` and `bash`. The KiCad release is a build argument, so it can be pinned
per project.

```bash
./bin/eda doctor                                   # builds the image on first use
./bin/eda sch review  hardware/ --text
./bin/eda pcb review  hardware/ --text
./bin/eda pcb render  hardware/ -o /tmp/art
./bin/eda sim run     sim/filter.cir -o sim/out
./bin/eda datasheet fetch LM321 -o docs/datasheets/
```

## Skills

Skills live in `.claude/skills/` and are picked up automatically when Claude
Code runs in this repository. Copy the directory into another project (together
with `bin/`, `docker/`, `src/` and `Makefile`) to use them there.

| Skill | What it does |
| --- | --- |
| [`datasheet-lookup`](.claude/skills/datasheet-lookup/SKILL.md) | Search distributor APIs / the web for a part number and download the datasheet PDF, with a cache |
| [`datasheet-analysis`](.claude/skills/datasheet-analysis/SKILL.md) | Extract text, parameter tables, embedded figures and rendered page images from a datasheet |
| [`spice-simulation`](.claude/skills/spice-simulation/SKILL.md) | Run ngspice (op / dc / ac / tran / noise), measure gain, bandwidth, phase margin, rise time, overshoot, THD, and plot |
| [`kicad-schematic-review`](.claude/skills/kicad-schematic-review/SKILL.md) | Read `.kicad_sch`, extract components/nets/hierarchy, run ERC plus design heuristics |
| [`kicad-pcb-review`](.claude/skills/kicad-pcb-review/SKILL.md) | Read `.kicad_pcb`, run DRC and layout heuristics, render layers and 3D views as PNG |
| [`eda-environment`](.claude/skills/eda-environment/SKILL.md) | Build, pin, verify and troubleshoot the container |

## Quick start

```bash
git clone <this repo> && cd kicad_skills
make build                     # ~5 min, downloads the KiCad image (about 4 GB)
./bin/eda doctor               # {"kicad_cli": "10.0.4", "ngspice": "...", "ok": true}
make test                      # full test-suite inside the container
make smoke                     # end-to-end run against the example project
```

Then point the commands at a real project:

```bash
cd ~/projects/my-board
~/kicad_skills/bin/eda sch review . --text
```

`bin/eda` mounts the git repository root that contains the current directory at
`/work`, runs as your uid/gid (no root-owned files), and keeps the datasheet
cache in a docker volume.

## Pinning the KiCad version

```bash
KICAD_VERSION=10.0.4 make build     # default, current stable
KICAD_VERSION=9.0.9  make build     # a second image for older projects
KICAD_VERSION=9.0.9 ./bin/eda pcb review board.kicad_pcb
```

Each version produces its own image tag (`eda-toolkit:<version>`) so several can
coexist. Tags: <https://hub.docker.com/r/kicad/kicad/tags>. KiCad upgrades
project files in place when it opens something older than itself, so match the
version to the project.

## Command reference

```
eda doctor                                    tool versions in the environment

eda datasheet search PART [--limit N] [--provider NAME]
eda datasheet fetch  PART|--url URL [-o PATH] [--force]
eda datasheet info   PDF
eda datasheet find   PDF QUERY... [--regex]
eda datasheet text   PDF [--pages 1-5] [--layout] [--ocr]
eda datasheet tables PDF [--pages 5]
eda datasheet images PDF -o DIR [--pages]
eda datasheet pages  PDF -o DIR [--pages] [--dpi 200]
eda datasheet parse  PDF -o DIR [--renders] [--ocr]

eda sim lint     NETLIST
eda sim run      NETLIST -o DIR [--no-plots] [--timeout S]
eda sim measure  RAW [--thd SIGNAL --fundamental HZ] [--skip S]
eda sim plot     RAW -o DIR [--signals ...]
eda sim netlist  SCHEMATIC -o FILE           export a SPICE deck from KiCad

eda sch info     TARGET [--no-cli]
eda sch review   TARGET [--text] [-o report.json] [--no-cli]
eda sch erc      TARGET                      raw KiCad ERC JSON
eda sch netlist  TARGET [--format json|kicadxml|spice|...] [-o FILE]
eda sch render   TARGET -o DIR [--dpi 200]

eda pcb info     TARGET
eda pcb review   TARGET [--text] [--threshold KEY=VALUE] [-o report.json]
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
| `EDA_NETWORK` | `1` always allow network, `0` never (default: only for `datasheet search`/`fetch`) |
| `EDA_MOUNT` | host directory to mount at `/work` (default: git root or `$PWD`) |
| `EDA_ENV_PASSTHROUGH` | extra environment variable names to forward |
| `EDA_DOCKER_ARGS` | extra arguments for `docker run` |
| `MOUSER_API_KEY`, `DIGIKEY_CLIENT_ID`/`_SECRET`, `NEXAR_TOKEN`, `SEARXNG_URL` | datasheet search providers (forwarded automatically) |

## How it fits together

```
bin/eda            host wrapper: docker run, uid mapping, network policy, path rewriting
docker/Dockerfile  kicad/kicad:<version> + ngspice + an isolated virtualenv
src/eda_toolkit/
├── cli.py                 the `eda` command
├── datasheet/             search providers, cached download, PDF extraction
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

## Reproducibility

Every external input is pinned, and the pins are enforced by
`tests/test_pinning.py` (which runs in CI):

| Input | Pin |
| --- | --- |
| KiCad base image | manifest digest, per version, in [`docker/kicad-digests.txt`](docker/kicad-digests.txt) |
| pip | exact version + wheel SHA-256 (`ARG PIP_*` in the Dockerfile) |
| Python packages | exact versions + hashes in [`requirements.txt`](requirements.txt), installed with `--require-hashes --no-deps --no-build-isolation` |
| GitHub Actions | 40 character commit SHA, with the tag in a trailing comment |
| CI runner | `ubuntu-24.04`, never `-latest`; Python `3.13.5` |

To change a dependency, edit `requirements.in` and regenerate the lock:

```bash
make lock          # uv pip compile --generate-hashes (python 3.13, linux)
make rebuild
```

To use another KiCad release, add its digest to `docker/kicad-digests.txt`
(the file documents the one-liner) and build with `KICAD_VERSION=<version>`.
The build fails loudly if a version has no pinned digest.

## Testing

```bash
make test          # everything, inside the container (158 tests)
make test-host     # pure-python subset on the host (needs a local venv)
make smoke         # end-to-end: every skill's main command
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs two jobs on
every push and pull request: the pure-python suite against the hash-pinned
dependency set, and the full suite plus the smoke test inside the freshly built
container (which also uploads the rendered example artwork as an artifact).
Nothing in CI touches the network beyond pulling the pinned base image and the
locked wheels — the datasheet search tests use mocked HTTP.

The suite covers the s-expression parser, schematic and board models, every
review rule, the ngspice raw-file parser (ASCII/binary, real/complex), the
measurement maths (checked against analytically known circuits), the datasheet
providers and PDF extraction (against a generated PDF), and the CLI. Tests that
need `kicad-cli` or `ngspice` are marked and skipped automatically outside the
container.

Integration tests run the real tools: KiCad ERC and DRC on the example project,
a deliberately introduced short to prove DRC catches it, netlist comparison
between `kicad-cli` and the fallback extractor, layer/3D rendering, and an RC
filter whose simulated −3 dB corner is compared against `1/(2πRC)`.

## Limitations

* Datasheet **search** depends on external services. Without a distributor API
  key it falls back to scraping DuckDuckGo, which is rate limited and may be
  blocked; `datasheet fetch --url` always works.
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
