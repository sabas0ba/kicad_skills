---
name: eda-environment
description: Set up, pin, verify and troubleshoot the containerised EDA toolchain (KiCad + ngspice + PDF tooling) used by the circuit design skills. Use when `./bin/eda` fails, when the KiCad version has to be changed or pinned, when the container needs network or API keys, or when a circuit-design skill reports that a tool is unavailable.
---

# EDA environment

Every circuit design skill in this repository runs its work **inside a container**.
Nothing (KiCad, ngspice, python packages) is installed on the host.

## The one entry point

```bash
./bin/eda <command> [...]      # builds the image on first use, then runs it
./bin/eda doctor               # prints the versions actually available
```

`bin/eda` mounts the git repository root at `/work`, runs as your uid/gid (so
generated files are not root owned), and keeps the datasheet cache in a docker
volume. Paths passed to it are interpreted **inside** `/work`, so use paths that
are relative to the repository (absolute paths under the repo root are rewritten
automatically).

## Choosing / pinning the KiCad version

The image is derived from the official `kicad/kicad:<version>` image, so the
KiCad release is a build argument:

```bash
KICAD_VERSION=10.0.4 make build      # default
KICAD_VERSION=9.0.9  make build      # older project? build a second image
KICAD_VERSION=9.0.9 ./bin/eda pcb review hardware/board.kicad_pcb
```

Each version gets its own image tag (`eda-toolkit:<version>`), so several KiCad
releases can coexist. Available tags: https://hub.docker.com/r/kicad/kicad/tags.

**The base image is always pinned by manifest digest**, never by tag alone. A
version can only be built once its digest is listed in
`docker/kicad-digests.txt`; otherwise the build stops with an error. To add one:

```bash
docker buildx imagetools inspect kicad/kicad:9.0.10 --format '{{.Manifest.Digest}}'
# or, without docker:
curl -s https://hub.docker.com/v2/repositories/kicad/kicad/tags/9.0.10 | jq -r .digest
# then append "9.0.10 sha256:..." to docker/kicad-digests.txt
```

The same applies to everything else in the image: pip is fetched by wheel
SHA-256, and the python packages come from the hash-pinned `requirements.txt`
(regenerate it with `make lock` after editing `requirements.in`).
`tests/test_pinning.py` enforces all of this.
Never open a project with an older KiCad than it was saved with — KiCad upgrades
files in place and that is not reversible. Check first:

```bash
head -1 board.kicad_pcb     # (kicad_pcb (version 20240108) ...)
```

## Network policy

The container runs with `--network none` by default. Only
`datasheet search` and `datasheet fetch` get network access automatically.

```bash
EDA_NETWORK=1 ./bin/eda <command>    # force network on
EDA_NETWORK=0 ./bin/eda <command>    # force it off
```

Distributor API keys are forwarded from the host environment when set:
`MOUSER_API_KEY`, `DIGIKEY_CLIENT_ID` + `DIGIKEY_CLIENT_SECRET`, `NEXAR_TOKEN`,
`SEARXNG_URL`. Forward extra variables with
`EDA_ENV_PASSTHROUGH=VAR1,VAR2`.

## Verifying the environment

```bash
./bin/eda doctor        # {"kicad_cli": "10.0.4", "ngspice": "...", "ok": true}
make test               # full test-suite inside the container
make smoke              # end to end run against tests/fixtures/example_project
```

`doctor` exits non-zero when something essential is missing. `tesseract: false`
only disables OCR of scanned datasheets; everything else still works.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `required tool 'kicad-cli' is not available` | The command ran on the host. Prefix it with `./bin/eda`. |
| `docker is required but not installed` | Install docker, or run the CLI inside an already-provisioned container. |
| Build fails on `apt-get` | Only the optional OCR package needs a Debian mirror; the build continues without it. Set `--build-arg WITH_OCR=0` to skip it entirely. |
| TLS errors while building or fetching | Corporate proxy: drop the CA certificate into `docker/certs/*.crt` and rebuild. Export `HTTPS_PROXY` for the runtime. |
| `datasheet search` returns no candidates | Network is blocked, or the search engine refused the request. Read the `hint` field, then either supply an API key or pass a known URL to `datasheet fetch --url`. |
| kicad-cli complains about missing libraries | The entrypoint seeds `~/.config/kicad/<ver>/fp-lib-table` from the KiCad template. If a project uses custom libraries, mount them into `/work` and use project-relative paths. |
| Files created by the container are root owned | You bypassed `bin/eda`. Always pass `--user "$(id -u):$(id -g)"` when calling docker directly. |

## Running a raw shell in the container

```bash
make shell                                     # bash inside the image
./bin/eda doctor                               # sanity check afterwards
docker run --rm -v "$PWD:/work" -w /work eda-toolkit:10.0.4 kicad-cli --help
```
