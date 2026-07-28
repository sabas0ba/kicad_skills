# Working in this repository

This repo provides circuit-design skills (`.claude/skills/`) backed by the
`eda_toolkit` Python package, all executed inside a container.

## Ground rules

* **Never run `kicad-cli`, `ngspice` or the `eda` CLI on the host.** Use
  `./bin/eda.sh <command>` — it builds and runs the container, maps your uid, and
  keeps the host clean. Anything else pollutes the host or fails outright.
* Paths passed to `./bin/eda.sh` are relative to the repository root (mounted at
  `/work` in the container).
* `KICAD_VERSION` selects the KiCad release; the default is in `bin/eda.sh` and
  the `Makefile`. Keep the two in sync when changing it.

## Development

```bash
make build        # build (or rebuild) the image
make test         # full suite in the container - this is the gate
make test-host    # fast pure-python subset (needs .venv with the test extra)
make smoke        # end-to-end run of every skill's main command
```

`make test` mounts the working tree and puts `/work/src` on `PYTHONPATH`, so it
tests the code you are editing, not the copy baked into the image. Rebuild the
image only when the Dockerfile or the dependencies change.

## Adding a review rule

Rules are functions registered with the `@rule` decorator in
`src/eda_toolkit/kicad/sch_review.py` or `pcb_review.py`. They receive a context
(parsed design + netlist/board + ERC/DRC output) and return `Finding` objects.
Add the rule, then add a test in `tests/test_sch_review.py` /
`tests/test_pcb_review.py` using the in-memory `ReviewContext.from_netlist` /
`PcbContext.from_board` constructors — no filesystem needed.

Keep severities honest: `error` = the design is broken, `warning` = a human must
judge it, `info` = context.

## Pinning rules (enforced by tests/test_pinning.py)

* Python dependencies: edit `pyproject.toml`, then `make lock` (`uv lock`).
  `uv.lock` is the only lock file — it carries the hashes and the image installs
  it with `uv sync --frozen`. Do not add a second requirements file.
* KiCad releases: add `<version> <sha256 digest>` to `docker/kicad-digests.txt`
  before building with a new `KICAD_VERSION`. Keep the Dockerfile's default
  `KICAD_VERSION`/`KICAD_DIGEST`, the `Makefile` and `bin/eda.sh` in agreement.
* GitHub Actions: `uses: owner/action@<40-char-sha> # vX.Y.Z`. No tag-only refs,
  no `ubuntu-latest`.

## Test fixtures

`tests/fixtures/example_project` is a small, DRC-clean KiCad project (RC filter
+ LM321 buffer). Its symbols come from the real KiCad libraries; its footprints
are simplified, which is why DRC reports `lib_footprint_mismatch` for each of
them. Keep the project clean: a new error there means the toolkit changed
behaviour.
