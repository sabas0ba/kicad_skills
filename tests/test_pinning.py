"""Supply chain pinning is a requirement, so it is a test.

Every external input must be immutable: GitHub Actions by commit SHA, the base
image by manifest digest, pip by wheel hash and every python package by version
plus hash.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def workflows() -> list[Path]:
    return sorted((ROOT / ".github" / "workflows").glob("*.yml")) + \
           sorted((ROOT / ".github" / "workflows").glob("*.yaml"))


def test_there_is_a_workflow():
    assert workflows(), "expected at least one GitHub Actions workflow"


def test_actions_are_pinned_to_a_commit_sha():
    uses = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)\s*(#.*)?$")
    seen = 0
    for workflow in workflows():
        for lineno, line in enumerate(workflow.read_text().splitlines(), start=1):
            match = uses.match(line)
            if not match:
                continue
            seen += 1
            action, comment = match.group(1), match.group(2)
            where = f"{workflow.name}:{lineno}"
            if action.startswith("./") or action.startswith("docker://"):
                continue
            assert "@" in action, f"{where}: {action} is not pinned"
            ref = action.split("@", 1)[1]
            assert SHA1.match(ref), f"{where}: {action} must be pinned to a 40 character commit SHA"
            assert comment and re.search(r"v\d", comment), (
                f"{where}: pin {action} with a trailing '# vX.Y.Z' comment so it stays readable"
            )
    assert seen, "no 'uses:' entries found - did the workflow move?"


def test_runners_are_not_latest():
    for workflow in workflows():
        for lineno, line in enumerate(workflow.read_text().splitlines(), start=1):
            if "runs-on:" in line:
                assert "latest" not in line, (
                    f"{workflow.name}:{lineno}: pin the runner image (e.g. ubuntu-24.04)"
                )


def test_base_image_is_pinned_by_digest():
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    from_lines = [ln for ln in dockerfile.splitlines() if ln.startswith("FROM ")]
    assert from_lines
    for line in from_lines:
        assert "@" in line, f"{line}: the base image must carry a digest"
    digest_arg = re.search(r"^ARG KICAD_DIGEST=(\S+)", dockerfile, re.MULTILINE)
    assert digest_arg, "KICAD_DIGEST build argument is missing"
    assert SHA256.match(digest_arg.group(1))


def test_pip_is_pinned_by_version_and_hash():
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    version = re.search(r"^ARG PIP_VERSION=(\S+)", dockerfile, re.MULTILINE)
    url = re.search(r"^ARG PIP_URL=(\S+)", dockerfile, re.MULTILINE)
    sha = re.search(r"^ARG PIP_SHA256=([0-9a-f]{64})\s*$", dockerfile, re.MULTILINE)
    assert version and url and sha, "pip must be pinned by version, URL and SHA-256"
    assert f"pip-{version.group(1)}-" in url.group(1), "PIP_URL and PIP_VERSION disagree"


def test_requirements_are_fully_pinned_with_hashes():
    text = (ROOT / "requirements.txt").read_text()
    # join the backslash continuations so each requirement is one logical line
    logical = text.replace("\\\n", " ")
    requirements = [ln for ln in logical.splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")]
    assert len(requirements) >= 10, "the lock file looks truncated"
    for line in requirements:
        name = line.split()[0]
        assert "==" in name, f"{name} is not pinned to an exact version"
        assert "--hash=sha256:" in line, f"{name} has no hash"


def test_requirements_lock_covers_every_direct_dependency():
    locked = {
        line.split("==")[0].lower().replace("_", "-")
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line and not line.startswith((" ", "#", "\t"))
    }
    for raw in (ROOT / "requirements.in").read_text().splitlines():
        name = raw.split("#")[0].strip()
        if not name:
            continue
        assert name.lower().replace("_", "-") in locked, f"{name} is missing from requirements.txt"


def test_dockerfile_installs_with_require_hashes():
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    assert "--require-hashes" in dockerfile
    # build isolation would silently fetch an unpinned build backend
    assert "--no-build-isolation" in dockerfile


def test_kicad_version_defaults_agree():
    digests = _kicad_digests()
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    makefile = (ROOT / "Makefile").read_text()
    wrapper = (ROOT / "bin" / "eda").read_text()

    docker_version = re.search(r"^ARG KICAD_VERSION=(\S+)", dockerfile, re.MULTILINE).group(1)
    make_version = re.search(r"^KICAD_VERSION \?= (\S+)", makefile, re.MULTILINE).group(1)
    wrapper_version = re.search(r'KICAD_VERSION="\$\{KICAD_VERSION:-(\S+?)\}"', wrapper).group(1)

    assert docker_version == make_version == wrapper_version, (
        "the default KiCad version differs between the Dockerfile, the Makefile and bin/eda"
    )
    assert docker_version in digests, "the default version has no pinned digest"

    docker_digest = re.search(r"^ARG KICAD_DIGEST=(\S+)", dockerfile, re.MULTILINE).group(1)
    assert docker_digest == digests[docker_version], (
        "the Dockerfile default digest does not match docker/kicad-digests.txt"
    )

    for workflow in workflows():
        for match in re.finditer(r"KICAD_VERSION:\s*(\S+)", workflow.read_text()):
            assert match.group(1) == docker_version, (
                f"{workflow.name} pins a different KiCad version"
            )


def _kicad_digests() -> dict[str, str]:
    entries = {}
    for line in (ROOT / "docker" / "kicad-digests.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        version, digest = line.split()
        entries[version] = digest
    return entries


@pytest.mark.parametrize("version,digest", sorted(_kicad_digests().items()))
def test_every_listed_digest_is_well_formed(version, digest):
    assert re.match(r"^\d+\.\d+\.\d+$", version), f"{version} is not a full KiCad version"
    assert SHA256.match(digest), f"{version} has a malformed digest"
