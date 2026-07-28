"""bin/install-skills.sh is how most people will adopt this repo - test it.

The guides ship under `.claude/skills/` because that is where Claude Code finds
them, but nothing about the script assumes that: --dest puts them anywhere and
--no-guides skips them entirely, for people who only want the CLI.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "install-skills.sh"
SKILLS = sorted(p.name for p in (ROOT / ".claude" / "skills").iterdir() if p.is_dir())

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def project(tmp_path):
    """A parent project with this repository vendored at tools/kicad_skills."""
    target = tmp_path / "my-board"
    submodule = target / "tools" / "kicad_skills"
    (submodule / "bin").mkdir(parents=True)
    shutil.copytree(ROOT / ".claude", submodule / ".claude")
    shutil.copy(SCRIPT, submodule / "bin" / "install-skills.sh")
    (submodule / "bin" / "eda.sh").write_text("#!/usr/bin/env bash\necho stub\n")
    (submodule / "bin" / "eda.sh").chmod(0o755)
    return target, submodule


def install(submodule, target, *args):
    return subprocess.run(
        ["bash", str(submodule / "bin" / "install-skills.sh"), "--target", str(target), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def test_skills_are_symlinked_relative_to_the_project(project):
    target, submodule = project
    install(submodule, target)

    for name in SKILLS:
        link = target / ".claude" / "skills" / name
        assert link.is_symlink(), name
        # Relative, so the project can be moved or cloned anywhere.
        assert not os.path.isabs(os.readlink(link))
        assert (link / "SKILL.md").exists()


def test_the_shim_forwards_to_the_submodule_wrapper(project):
    target, submodule = project
    install(submodule, target)

    shim = target / "bin" / "eda.sh"
    assert os.access(shim, os.X_OK)
    result = subprocess.run([str(shim), "doctor"], capture_output=True, text=True, cwd=target)
    assert result.stdout.strip() == "stub", result.stderr


def test_the_shim_still_works_from_another_directory(project):
    target, submodule = project
    install(submodule, target)
    result = subprocess.run(
        [str(target / "bin" / "eda.sh")], capture_output=True, text=True, cwd=target.parent
    )
    assert result.stdout.strip() == "stub", result.stderr


def test_existing_entries_are_kept_unless_forced(project):
    target, submodule = project
    mine = target / ".claude" / "skills" / SKILLS[0]
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine")

    install(submodule, target)
    assert (mine / "SKILL.md").read_text() == "mine"

    install(submodule, target, "--force")
    assert (target / ".claude" / "skills" / SKILLS[0]).is_symlink()


def test_copy_mode_vendors_the_skills(project):
    target, submodule = project
    install(submodule, target, "--copy")
    link = target / ".claude" / "skills" / SKILLS[0]
    assert not link.is_symlink()
    assert (link / "SKILL.md").exists()


def test_guides_can_be_installed_anywhere(project):
    target, submodule = project
    install(submodule, target, "--dest", "docs/circuit-design", "--copy")

    guide = target / "docs" / "circuit-design" / SKILLS[0] / "SKILL.md"
    assert guide.exists()
    assert not (target / ".claude").exists()


def test_a_nested_destination_still_gets_relative_symlinks(project):
    target, submodule = project
    install(submodule, target, "--dest", "a/b/c")

    link = target / "a" / "b" / "c" / SKILLS[0]
    assert link.is_symlink()
    assert not os.path.isabs(os.readlink(link))
    assert (link / "SKILL.md").exists(), os.readlink(link)


def test_the_cli_can_be_installed_without_any_guides(project):
    target, submodule = project
    install(submodule, target, "--no-guides")

    assert (target / "bin" / "eda.sh").exists()
    assert not (target / ".claude").exists()


def test_uninstall_removes_what_it_installed(project):
    target, submodule = project
    install(submodule, target)
    keep = target / "hardware.txt"
    keep.write_text("mine")

    install(submodule, target, "--uninstall")
    assert not (target / ".claude" / "skills").exists()
    assert not (target / ".claude").exists()  # and the directory it created
    assert not (target / "bin" / "eda.sh").exists()
    assert keep.exists()
    assert (submodule / ".claude" / "skills" / SKILLS[0] / "SKILL.md").exists()


def test_installing_into_this_repository_is_refused(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT), "--target", str(ROOT)], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "this repository itself" in result.stderr
