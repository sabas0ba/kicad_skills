"""Shared pytest fixtures and capability based skipping."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_PROJECT = FIXTURES / "example_project"


def pytest_collection_modifyitems(config, items):
    """Skip container-only tests when the corresponding tool is missing."""
    missing = {
        "kicad": shutil.which("kicad-cli") is None,
        "ngspice": shutil.which("ngspice") is None,
    }
    reasons = {
        "kicad": "kicad-cli not available (run the suite inside the container: make test)",
        "ngspice": "ngspice not available (run the suite inside the container: make test)",
    }
    for item in items:
        for marker, is_missing in missing.items():
            if marker in item.keywords and is_missing:
                item.add_marker(pytest.mark.skip(reason=reasons[marker]))


@pytest.fixture(scope="session")
def example_project() -> Path:
    return EXAMPLE_PROJECT


@pytest.fixture(scope="session")
def example_sch() -> Path:
    return EXAMPLE_PROJECT / "example.kicad_sch"


@pytest.fixture(scope="session")
def example_pcb() -> Path:
    return EXAMPLE_PROJECT / "example.kicad_pcb"


@pytest.fixture()
def project_copy(tmp_path, example_project) -> Path:
    """A writable copy of the example project (kicad-cli writes lock/backup files)."""
    dest = tmp_path / "project"
    shutil.copytree(example_project, dest)
    return dest


@pytest.fixture(scope="session")
def rc_netlist() -> Path:
    return FIXTURES / "spice" / "rc_lowpass.cir"
