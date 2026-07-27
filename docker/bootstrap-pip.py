#!/usr/bin/env python3
"""Install one exact, hash-verified pip into a virtualenv.

The KiCad base image ships a Python interpreter but neither ``pip`` nor
``ensurepip``.  Rather than depending on a Debian mirror at build time (or on
whatever pip version happens to be bundled), this fetches a pinned pip wheel
from PyPI, verifies its SHA-256, and installs it by running the wheel in place
(a wheel is a zip, so it can bootstrap itself).

Usage:
    bootstrap-pip.py <venv-python> --url <wheel-url> --sha256 <digest>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("python", help="interpreter of the target virtualenv")
    parser.add_argument("--url", required=True, help="pinned pip wheel URL")
    parser.add_argument("--sha256", required=True, help="expected wheel digest")
    args = parser.parse_args()

    filename = os.path.basename(urllib.parse.urlparse(args.url).path)
    if not filename.endswith(".whl"):
        print(f"bootstrap-pip: {args.url} is not a wheel", file=sys.stderr)
        return 2

    with urllib.request.urlopen(args.url, timeout=180) as response:
        payload = response.read()

    digest = hashlib.sha256(payload).hexdigest()
    if digest != args.sha256:
        print(
            "bootstrap-pip: hash mismatch for the pip wheel\n"
            f"  expected {args.sha256}\n  got      {digest}",
            file=sys.stderr,
        )
        return 1

    tmp = tempfile.mkdtemp(prefix="pip-bootstrap-")
    path = os.path.join(tmp, filename)
    with open(path, "wb") as fh:
        fh.write(payload)
    print(f"bootstrap-pip: verified {filename} ({len(payload)} bytes)")

    # The wheel on sys.path exposes the pip package, which installs itself.
    subprocess.check_call(
        [args.python, os.path.join(path, "pip"), "install", "--no-index",
         "--find-links", tmp, "--no-cache-dir", "pip"]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
