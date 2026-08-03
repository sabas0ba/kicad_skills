# Everything the circuit-design skills need, pinned to one KiCad release.
#
#   make build                       # uses the default version + its digest
#   KICAD_VERSION=9.0.9 make build   # digest looked up in docker/kicad-digests.txt
#
# Every input is version pinned:
#   * the base image by manifest digest (KICAD_DIGEST)
#   * pip and uv by version and wheel SHA-256
#   * every python package by uv.lock, installed with `uv sync --frozen`
# The KiCad images (kicad/kicad:<version>, Debian based) already ship kicad-cli,
# the standard symbol/footprint/3D libraries and ngspice, so KiCad and the
# stand-alone simulator always agree on the SPICE engine.
#
# Corporate proxies: drop CA certificate(s) into docker/certs/*.crt and they are
# added to the image trust store before anything is downloaded.
ARG KICAD_VERSION=10.0.4
ARG KICAD_DIGEST=sha256:770e2fa006695077b5ba218742d5c3137896af91257b2ca971243fa56b01bb86
FROM kicad/kicad:${KICAD_VERSION}@${KICAD_DIGEST}

ARG KICAD_VERSION
ARG WITH_OCR=1
# pip exists only to install uv; both are fetched by digest.
ARG PIP_VERSION=26.2
ARG PIP_URL=https://files.pythonhosted.org/packages/62/36/a3aed958d60531cb442b7ab4596cda7b3621cfb916f8ae1d6769795c7dc1/pip-26.2-py3-none-any.whl
ARG PIP_SHA256=931c303696af6fa3417112103b1cad26890e5a07eccb5b99783700e33f2b8aad

LABEL org.opencontainers.image.title="eda-toolkit" \
      org.opencontainers.image.description="Datasheet, SPICE and KiCad review toolkit" \
      eda.kicad_version="${KICAD_VERSION}"

USER root
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Optional extra trust anchors (empty by default - the directory only holds docs).
COPY docker/certs/ /usr/local/share/eda-certs/
RUN set -eu; \
    if ls /usr/local/share/eda-certs/*.crt >/dev/null 2>&1; then \
        cat /usr/local/share/eda-certs/*.crt >> /etc/ssl/certs/ca-certificates.crt; \
        echo "added $(ls /usr/local/share/eda-certs/*.crt | wc -l) extra CA certificate(s)"; \
    fi

# OCR is the only feature that needs distribution packages. Keep it optional so
# the image still builds on networks without a Debian mirror.
RUN set -eu; \
    if [ "$WITH_OCR" = "1" ]; then \
        if apt-get update && apt-get install -y --no-install-recommends tesseract-ocr; then \
            rm -rf /var/lib/apt/lists/*; \
        else \
            echo "WARNING: could not install tesseract-ocr; OCR of scanned datasheets is disabled"; \
            rm -rf /var/lib/apt/lists/*; \
        fi; \
    fi

# uv drives the real installation, but the base image has neither pip nor
# ensurepip, so a hash-verified pip is bootstrapped into a throwaway venv first
# and used for nothing except installing the pinned uv.
COPY docker/bootstrap-pip.py /tmp/bootstrap-pip.py
COPY docker/uv-bootstrap.txt /tmp/uv-bootstrap.txt
RUN set -eu; \
    python3 -m venv --without-pip /opt/uv-boot; \
    python3 /tmp/bootstrap-pip.py /opt/uv-boot/bin/python \
        --url "$PIP_URL" --sha256 "$PIP_SHA256"; \
    /opt/uv-boot/bin/pip --version | grep -q "$PIP_VERSION"; \
    /opt/uv-boot/bin/pip install --require-hashes -r /tmp/uv-bootstrap.txt; \
    ln -s /opt/uv-boot/bin/uv /usr/local/bin/uv; \
    rm -f /tmp/bootstrap-pip.py /tmp/uv-bootstrap.txt; \
    uv --version

# The toolkit and its environment. uv.lock is the single source of truth for
# versions and hashes; --frozen refuses to re-resolve, and never letting uv
# download an interpreter keeps the image's python the one that is used.
ENV VIRTUAL_ENV=/opt/eda-venv \
    UV_PROJECT_ENVIRONMENT=/opt/eda-venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
COPY pyproject.toml uv.lock /opt/eda/
COPY docker/build-backend.txt /tmp/build-backend.txt
COPY src /opt/eda/src
# --system-certs makes uv use the OS trust store (rustls bundles its own by
# default), which is what picks up any certificate added from docker/certs.
#
# The build backend goes in first, by hash: uv.lock cannot cover
# [build-system] requires, so without this uv would fetch an unpinned
# setuptools to build the project. --no-build-isolation-package then makes it
# use this one, and --inexact stops the sync from pruning it mid-build.
RUN set -eu; \
    uv venv "$VIRTUAL_ENV"; \
    uv pip install --python "$VIRTUAL_ENV/bin/python" --system-certs \
        --require-hashes -r /tmp/build-backend.txt; \
    uv sync --frozen --system-certs --extra test --inexact \
        --no-build-isolation-package eda-toolkit --project /opt/eda; \
    rm -f /tmp/build-backend.txt
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY docker/entrypoint.sh /usr/local/bin/eda-entrypoint
RUN chmod +x /usr/local/bin/eda-entrypoint

# Writable defaults for callers that run with an arbitrary --user.
ENV HOME=/tmp/eda-home \
    MPLCONFIGDIR=/tmp/eda-home/matplotlib \
    KICAD_VERSION=${KICAD_VERSION}
RUN mkdir -p /work /tmp/eda-home && chmod 0777 /work /tmp/eda-home

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/eda-entrypoint"]
CMD ["doctor"]
