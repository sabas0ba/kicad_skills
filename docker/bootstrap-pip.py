#!/usr/bin/env python3
"""Bootstrap pip into a virtualenv without relying on distribution packages.

The KiCad base image ships a Python interpreter but no ``ensurepip`` and no
``python3-pip``.  Rather than depending on a Debian mirror being reachable at
build time, this script tries ``ensurepip`` first and falls back to fetching the
pip wheel straight from PyPI (a wheel is a zip, so it can be executed in place).

Usage: bootstrap-pip.py /path/to/venv/bin/python
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request

PYPI_JSON = "https://pypi.org/pypi/pip/json"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: bootstrap-pip.py <venv-python>", file=sys.stderr)
        return 2
    python = sys.argv[1]

    if subprocess.call([python, "-m", "ensurepip", "--upgrade"]) == 0:
        print("bootstrap-pip: installed via ensurepip")
        return 0

    print("bootstrap-pip: ensurepip unavailable, fetching the pip wheel from PyPI")
    with urllib.request.urlopen(PYPI_JSON, timeout=120) as response:
        release = json.load(response)
    wheels = [u for u in release["urls"] if u["packagetype"] == "bdist_wheel"]
    if not wheels:
        print("bootstrap-pip: no pip wheel on PyPI?!", file=sys.stderr)
        return 1
    wheel = wheels[0]

    tmp = tempfile.mkdtemp(prefix="pip-bootstrap-")
    path = os.path.join(tmp, wheel["filename"])
    urllib.request.urlretrieve(wheel["url"], path)
    print(f"bootstrap-pip: downloaded {wheel['filename']}")

    # A wheel on sys.path exposes the pip package, which can install itself.
    subprocess.check_call(
        [python, os.path.join(path, "pip"), "install", "--no-index",
         "--find-links", tmp, "pip"]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
