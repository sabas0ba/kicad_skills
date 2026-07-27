# Everything the circuit-design skills need, pinned to one KiCad release.
#
#   docker build -f docker/Dockerfile --build-arg KICAD_VERSION=10.0.4 -t eda-toolkit:10.0.4 .
#
# The KiCad images (kicad/kicad:<version>, Debian based) already ship kicad-cli,
# the standard symbol/footprint/3D libraries and ngspice, so KiCad and the
# stand-alone simulator always agree on the SPICE engine.
#
# Build arguments:
#   KICAD_VERSION   KiCad release to base the image on (see hub.docker.com/r/kicad/kicad/tags)
#   WITH_OCR        1 (default) = try to apt-get tesseract for scanned datasheets.
#                   The build does not fail when no Debian mirror is reachable.
#
# Corporate proxies: drop your CA certificate(s) into docker/certs/*.crt and they
# are added to the image trust store before anything is downloaded.
ARG KICAD_VERSION=10.0.4
FROM kicad/kicad:${KICAD_VERSION}

ARG KICAD_VERSION
ARG WITH_OCR=1
LABEL org.opencontainers.image.title="eda-toolkit" \
      org.opencontainers.image.description="Datasheet, SPICE and KiCad review toolkit" \
      eda.kicad_version="${KICAD_VERSION}"

USER root
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Optional extra trust anchors (empty by default - the directory only holds a .gitkeep).
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

# Isolated virtualenv: the distribution's python installation is never touched.
# --system-site-packages keeps KiCad's own bindings (pcbnew) importable.
ENV VIRTUAL_ENV=/opt/eda-venv
COPY docker/bootstrap-pip.py /tmp/bootstrap-pip.py
RUN set -eu; \
    python3 -m venv --without-pip --system-site-packages "$VIRTUAL_ENV"; \
    python3 /tmp/bootstrap-pip.py "$VIRTUAL_ENV/bin/python"; \
    rm -f /tmp/bootstrap-pip.py
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# The toolkit itself lives at /opt/eda so the image works even when the
# workspace mount is read-only or holds a different checkout.
COPY pyproject.toml /opt/eda/pyproject.toml
COPY src /opt/eda/src
RUN pip install --no-deps /opt/eda

COPY docker/entrypoint.sh /usr/local/bin/eda-entrypoint
RUN chmod +x /usr/local/bin/eda-entrypoint

# Writable defaults for callers that run with an arbitrary --user.
ENV HOME=/tmp/eda-home \
    EDA_CACHE_DIR=/cache \
    MPLCONFIGDIR=/tmp/eda-home/matplotlib \
    KICAD_VERSION=${KICAD_VERSION}
RUN mkdir -p /cache /work /tmp/eda-home && chmod 0777 /cache /work /tmp/eda-home

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/eda-entrypoint"]
CMD ["doctor"]
