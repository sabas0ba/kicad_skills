"""bin/install-skills.sh is how most people will adopt this repo - test it.

The guides are plain Markdown in `docs/guides/`, which is the source of truth.
This script renders them into a layout a particular tool wants - Claude Code's
`.claude/skills/<name>/SKILL.md` by default, anywhere else with --dest - and
drops the `bin/eda.sh` shim. Nothing here is required to *read* the guides.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "install-skills.sh"
GUIDE_DIR = ROOT / "docs" / "guides"
GUIDES = sorted(p.stem for p in GUIDE_DIR.glob("*.md") if p.stem != "README")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def project(tmp_path):
    """A parent project with this repository vendored at tools/kicad_skills."""
    target = tmp_path / "my-board"
    submodule = target / "tools" / "kicad_skills"
    (submodule / "bin").mkdir(parents=True)
    shutil.copytree(GUIDE_DIR, submodule / "docs" / "guides")
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


def test_there_is_a_guide_for_every_area():
    assert set(GUIDES) == {
        "datasheet-analysis",
        "eda-environment",
        "kicad-design-gate",
        "kicad-fabrication-output",
        "kicad-pcb-authoring",
        "kicad-pcb-review",
        "kicad-schematic-authoring",
        "kicad-schematic-review",
        "spice-simulation",
    }


def test_every_guide_carries_the_header_a_tool_selects_on():
    """The YAML front matter is what lets a tool pick one guide out of the set."""
    for name in GUIDES:
        text = (GUIDE_DIR / f"{name}.md").read_text()
        assert text.startswith("---\n"), name
        header = text.split("---\n")[1]
        assert f"name: {name}\n" in header, f"{name}: front-matter name must match the filename"
        assert "description:" in header, name


def test_guides_are_rendered_into_the_skill_layout(project):
    target, submodule = project
    install(submodule, target)

    for name in GUIDES:
        skill = target / ".claude" / "skills" / name / "SKILL.md"
        assert skill.is_symlink(), name
        # Relative, so the project can be moved or cloned anywhere.
        assert not os.path.isabs(os.readlink(skill))
        assert skill.read_text().startswith("---\n"), os.readlink(skill)


def test_the_generated_copy_tracks_the_guide(project):
    """A symlink, not a copy - editing the guide must not need a re-install."""
    target, submodule = project
    install(submodule, target)

    guide = submodule / "docs" / "guides" / f"{GUIDES[0]}.md"
    guide.write_text(guide.read_text() + "\nAn edit made after installing.\n")
    skill = target / ".claude" / "skills" / GUIDES[0] / "SKILL.md"
    assert "An edit made after installing." in skill.read_text()


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
    mine = target / ".claude" / "skills" / GUIDES[0]
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine")

    install(submodule, target)
    assert (mine / "SKILL.md").read_text() == "mine"

    install(submodule, target, "--force")
    assert (mine / "SKILL.md").is_symlink()


def test_copy_mode_vendors_the_guides(project):
    target, submodule = project
    install(submodule, target, "--copy")
    skill = target / ".claude" / "skills" / GUIDES[0] / "SKILL.md"
    assert not skill.is_symlink()
    assert skill.read_text().startswith("---\n")


def test_guides_can_be_installed_anywhere(project):
    target, submodule = project
    install(submodule, target, "--dest", "docs/circuit-design", "--copy")

    assert (target / "docs" / "circuit-design" / GUIDES[0] / "SKILL.md").exists()
    assert not (target / ".claude").exists()


def test_a_nested_destination_still_gets_relative_symlinks(project):
    target, submodule = project
    install(submodule, target, "--dest", "a/b/c")

    skill = target / "a" / "b" / "c" / GUIDES[0] / "SKILL.md"
    assert skill.is_symlink()
    assert not os.path.isabs(os.readlink(skill))
    assert skill.read_text().startswith("---\n"), os.readlink(skill)


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
    assert not (target / ".claude").exists()  # including the directory it created
    assert not (target / "bin" / "eda.sh").exists()
    assert keep.exists()
    # the guides themselves are untouched - they are the source, not a copy
    assert (submodule / "docs" / "guides" / f"{GUIDES[0]}.md").exists()


def test_installing_into_this_checkout_renders_the_adapter_but_no_shim(tmp_path):
    """Standalone use: `make skills` targets the repository itself."""
    checkout = tmp_path / "kicad_skills"
    (checkout / "bin").mkdir(parents=True)
    shutil.copytree(GUIDE_DIR, checkout / "docs" / "guides")
    shutil.copy(SCRIPT, checkout / "bin" / "install-skills.sh")

    subprocess.run(
        ["bash", str(checkout / "bin" / "install-skills.sh")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (checkout / ".claude" / "skills" / GUIDES[0] / "SKILL.md").is_symlink()
    assert not (checkout / "bin" / "eda.sh").exists()  # it already has the real one


def test_the_generated_directory_is_not_tracked():
    """`.claude/skills/` is an adapter; docs/guides/ is what gets reviewed."""
    ignored = subprocess.run(["git", "check-ignore", "-q", ".claude/skills/x/SKILL.md"], cwd=ROOT)
    assert ignored.returncode == 0, ".claude/skills/ must stay git-ignored"
