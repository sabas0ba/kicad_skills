KICAD_VERSION ?= 10.0.4
IMAGE         ?= eda-toolkit:$(KICAD_VERSION)
DOCKER        ?= docker
UV            ?= uv

# The base image is pinned by manifest digest, looked up per version.
KICAD_DIGEST   = $(shell awk '$$1 == "$(KICAD_VERSION)" {print $$2; exit}' docker/kicad-digests.txt)
BUILD_ARGS     = --build-arg KICAD_VERSION=$(KICAD_VERSION) --build-arg KICAD_DIGEST=$(KICAD_DIGEST)

# PYTHONPATH puts the working tree ahead of the copy baked into the image, so
# the tests always exercise the code you are editing.
RUN_IN_IMAGE   = $(DOCKER) run --rm -u $(shell id -u):$(shell id -g) \
                   -v "$(CURDIR):/work" -w /work -e HOME=/tmp/eda-home \
                   -e PYTHONPATH=/work/src --network none $(IMAGE)

# The GitHub Pages gem set, pinned like everything else. Only used by `make
# site`, which renders the docs exactly as Pages will so link, title and Liquid
# problems surface before pushing rather than after.
PAGES_IMAGE = jekyll/jekyll@sha256:b49c58a6b9b6490eba9016f0ce9d965f2583d62af7191a4d3f3855b1c2cceb99

.PHONY: help build rebuild lock lint test test-host test-docker test-coverage smoke shell doctor clean \
        check-digest check-pins refresh-pins skills site

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

check-digest:
	@test -n "$(KICAD_DIGEST)" || { \
	  echo "error: no pinned digest for KiCad $(KICAD_VERSION)."; \
	  echo "       Add it to docker/kicad-digests.txt (the file explains how)."; \
	  exit 1; }

build: check-digest  ## build the container image (KICAD_VERSION=x.y.z to pin a release)
	$(DOCKER) build $(BUILD_ARGS) -f docker/Dockerfile -t $(IMAGE) .

rebuild: check-digest  ## rebuild without the layer cache
	$(DOCKER) build --no-cache $(BUILD_ARGS) -f docker/Dockerfile -t $(IMAGE) .

lock:  ## regenerate uv.lock from pyproject.toml (exact versions + hashes)
	$(UV) lock

check-pins:  ## report container pins that upstream has moved past
	python3 tools/refresh_pins.py

refresh-pins:  ## rewrite the container pins (add --set-default-kicad by hand to bump KiCad)
	python3 tools/refresh_pins.py --write

site:  ## build the GitHub Pages site into _site/ (needs network on first run)
	@rm -rf _site && mkdir -p _site && chmod 777 _site
	docker run --rm -v "$(PWD):/srv/jekyll" -v "$(PWD)/_site:/out" \
	    $(if $(wildcard /root/.ccr/ca-bundle.crt),-v /root/.ccr/ca-bundle.crt:/ca.crt:ro -e SSL_CERT_FILE=/ca.crt,) \
	    $(PAGES_IMAGE) jekyll build --destination /out
	@echo "==> open _site/index.html, or: python3 -m http.server -d _site"

skills:  ## mirror docs/guides/ into .claude/skills (generated, git-ignored)
	./bin/install-skills.sh --force

lint:  ## ruff (lint + format check) over the whole tree
	$(UV) run --frozen --extra test ruff check .
	$(UV) run --frozen --extra test ruff format --check .

doctor:  ## report tool versions inside the image
	./bin/eda.sh doctor

test: test-docker  ## default test target: the full suite inside the container

test-docker: build  ## run the whole suite (unit + kicad + ngspice) in the container
	$(RUN_IN_IMAGE) pytest -q -p no:cacheprovider tests

test-coverage: build  ## the full suite plus a coverage report
	$(RUN_IN_IMAGE) pytest -q -p no:cacheprovider \
	  --cov --cov-report=term --cov-report=xml:coverage.xml tests

test-host:  ## run only the pure-python unit tests on the host (needs a local venv)
	python -m pytest -q -m "not kicad and not ngspice" tests

smoke: build  ## end-to-end check against the example project
	$(RUN_IN_IMAGE) bash tests/smoke.sh

shell: build  ## interactive shell inside the container
	$(DOCKER) run --rm -it -u $(shell id -u):$(shell id -g) \
	  -v "$(CURDIR):/work" -w /work -e HOME=/tmp/eda-home $(IMAGE) bash

clean:  ## remove generated artefacts
	rm -rf build dist .pytest_cache tests/_out
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
