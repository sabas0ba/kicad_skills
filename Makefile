KICAD_VERSION ?= 10.0.4
IMAGE         ?= eda-toolkit:$(KICAD_VERSION)
DOCKER        ?= docker
# PYTHONPATH puts the working tree ahead of the copy baked into the image, so
# the tests always exercise the code you are editing.
RUN_IN_IMAGE   = $(DOCKER) run --rm -u $(shell id -u):$(shell id -g) \
                   -v "$(CURDIR):/work" -w /work -e HOME=/tmp/eda-home \
                   -e PYTHONPATH=/work/src --network none $(IMAGE)

.PHONY: help build rebuild test test-host test-docker smoke shell doctor lint clean fixtures

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

build:  ## build the container image (KICAD_VERSION=x.y.z to pin a release)
	$(DOCKER) build --build-arg KICAD_VERSION=$(KICAD_VERSION) \
	  -f docker/Dockerfile -t $(IMAGE) .

rebuild:  ## rebuild without the layer cache
	$(DOCKER) build --no-cache --build-arg KICAD_VERSION=$(KICAD_VERSION) \
	  -f docker/Dockerfile -t $(IMAGE) .

doctor:  ## report tool versions inside the image
	./bin/eda doctor

test: test-docker  ## default test target: the full suite inside the container

test-docker: build  ## run the whole suite (unit + kicad + ngspice) in the container
	$(RUN_IN_IMAGE) pytest -q -p no:cacheprovider tests

test-host:  ## run only the pure-python unit tests on the host (needs a local venv)
	python -m pytest -q -m "not kicad and not ngspice and not network" tests

smoke: build  ## end-to-end check against the example project
	$(RUN_IN_IMAGE) bash tests/smoke.sh

shell: build  ## interactive shell inside the container
	$(DOCKER) run --rm -it -u $(shell id -u):$(shell id -g) \
	  -v "$(CURDIR):/work" -w /work -e HOME=/tmp/eda-home $(IMAGE) bash

clean:  ## remove generated artefacts
	rm -rf build dist .pytest_cache **/__pycache__ tests/_out
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
