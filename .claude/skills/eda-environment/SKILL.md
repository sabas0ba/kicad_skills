---
name: eda-environment
description: Set up, pin, verify and troubleshoot the containerised EDA toolchain (KiCad + ngspice + PDF tooling) behind the `eda` CLI. Use when `./bin/eda.sh` fails, when the KiCad version has to be changed or pinned, or when a circuit-design command reports that a tool is unavailable.
---

> Part of [kicad_skills](https://github.com/sabas0ba/kicad_skills) — plain-Markdown usage
> guides for the `eda` CLI. Readable on its own; Claude Code loads it as a skill.

# EDA environment

Every command in this toolkit does its work **inside a container**.
Nothing (KiCad, ngspice, python packages) is installed on the host.

## The one entry point

```bash
./bin/eda.sh <command> [...]      # builds the image on first use, then runs it
./bin/eda.sh doctor               # prints the versions actually available
```

In a project that uses this repository as a **submodule**, `bin/eda.sh` is the
one-line shim written by `bin/install-skills.sh`; it behaves identically. If it
is missing, re-run `./<submodule>/bin/install-skills.sh` from the project root.

`bin/eda.sh` mounts the git repository root at `/work` and runs as your uid/gid,
so generated files are not root owned. Paths passed to it are interpreted
**inside** `/work`, so use paths that are relative to the repository (absolute
paths under the repo root are rewritten automatically).

## Choosing / pinning the KiCad version

The image is derived from the official `kicad/kicad:<version>` image, so the
KiCad release is a build argument:

```bash
KICAD_VERSION=10.0.4 make build      # default
KICAD_VERSION=9.0.9  make build      # older project? build a second image
KICAD_VERSION=9.0.9 ./bin/eda.sh pcb review hardware/board.kicad_pcb
```

Each version gets its own image tag (`eda-toolkit:<version>`), so several KiCad
releases can coexist. Available tags: https://hub.docker.com/r/kicad/kicad/tags.
CI runs the full suite against both 10.0.4 and 9.0.9.

Not every `kicad-cli` flag exists in every release, so the wrapper asks the
binary (`kicad_cli.supports`) rather than assuming. On KiCad 9 that means DRC
does not refill zones (`layout.unfilled_zone` becomes load-bearing - fill and
commit your pours), and `pcb export stats` returns nothing.

**The base image is always pinned by manifest digest**, never by tag alone. A
version can only be built once its digest is listed in
`docker/kicad-digests.txt`; otherwise the build stops with an error. To add one:

```bash
docker buildx imagetools inspect kicad/kicad:9.0.10 --format '{{.Manifest.Digest}}'
# or, without docker:
curl -s https://hub.docker.com/v2/repositories/kicad/kicad/tags/9.0.10 | jq -r .digest
# then append "9.0.10 sha256:..." to docker/kicad-digests.txt
```

The same applies to everything else in the image: pip and uv are fetched by
wheel SHA-256, and the python packages come from `uv.lock`, installed with
`uv sync --frozen` (regenerate it with `make lock` after editing the
dependencies in `pyproject.toml`). `tests/test_pinning.py` enforces all of this.
Never open a project with an older KiCad than it was saved with — KiCad upgrades
files in place and that is not reversible. Check first:

```bash
head -1 board.kicad_pcb     # (kicad_pcb (version 20240108) ...)
```

## Network policy

Nothing the toolkit does needs the internet, so the container always runs with
`--network none`. If you ever need to lift that (custom tooling inside the
image, a proxy-only registry):

```bash
EDA_NETWORK=1 ./bin/eda.sh <command>       # allow network for this run
EDA_ENV_PASSTHROUGH=VAR1,VAR2 ./bin/eda.sh <command>   # forward extra env vars
```

Proxy variables (`HTTPS_PROXY`, `NO_PROXY`, ...) are forwarded automatically.

## Verifying the environment

```bash
./bin/eda.sh doctor        # {"kicad_cli": "10.0.4", "ngspice": "...", "ok": true}
make test               # full test-suite inside the container
make smoke              # end to end run against tests/fixtures/example_project
```

`doctor` exits non-zero when something essential is missing. `tesseract: false`
only disables OCR of scanned datasheets; everything else still works.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `required tool 'kicad-cli' is not available` | The command ran on the host. Prefix it with `./bin/eda.sh`. |
| `docker is required but not installed` | Install docker, or run the CLI inside an already-provisioned container. |
| Build fails on `apt-get` | Only the optional OCR package needs a Debian mirror; the build continues without it. Set `--build-arg WITH_OCR=0` to skip it entirely. |
| TLS errors while building or fetching | Corporate proxy: drop the CA certificate into `docker/certs/*.crt` and rebuild. Export `HTTPS_PROXY` for the runtime. |
| kicad-cli complains about missing libraries | The entrypoint seeds `~/.config/kicad/<ver>/fp-lib-table` from the KiCad template. If a project uses custom libraries, mount them into `/work` and use project-relative paths. |
| Files created by the container are root owned | You bypassed `bin/eda.sh`. Always pass `--user "$(id -u):$(id -g)"` when calling docker directly. |

## Running a raw shell in the container

```bash
make shell                                     # bash inside the image
./bin/eda.sh doctor                               # sanity check afterwards
docker run --rm -v "$PWD:/work" -w /work eda-toolkit:10.0.4 kicad-cli --help
```
