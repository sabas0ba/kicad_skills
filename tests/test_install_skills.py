"""bin/install-skills.sh is how most people will adopt this repo - test it.

The guides are plain Markdown in `docs/guides/`, which is the source of truth.
This script renders them into the standard Agent Skills and Claude Code layouts
by default, or only a custom layout with --dest, and drops the `bin/eda.sh`
shim. Nothing here is required to *read* the guides.
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
USER_SKILL_CONTENT = "Pre-existing skill content owned by the target project.\n"
USER_NOTES_CONTENT = "Unrelated notes owned by the target project.\n"

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


def test_guides_are_rendered_into_both_standard_skill_layouts(project):
    target, submodule = project
    install(submodule, target)

    for root in (".agents", ".claude"):
        for name in GUIDES:
            skill = target / root / "skills" / name / "SKILL.md"
            assert skill.is_symlink(), f"{root}: {name}"
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
    existing_skill = target / ".claude" / "skills" / GUIDES[0]
    existing_skill.mkdir(parents=True)
    (existing_skill / "SKILL.md").write_text(USER_SKILL_CONTENT)

    install(submodule, target)
    assert (existing_skill / "SKILL.md").read_text() == USER_SKILL_CONTENT

    install(submodule, target, "--force")
    assert (existing_skill / "SKILL.md").is_symlink()


@pytest.mark.parametrize("absolute", [False, True])
def test_legacy_symlinks_gain_ownership_markers(project, absolute):
    target, submodule = project
    name = GUIDES[0]
    skill_dir = target / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True)
    guide = submodule / "docs" / "guides" / f"{name}.md"
    link_target = guide if absolute else Path(os.path.relpath(guide, skill_dir))
    (skill_dir / "SKILL.md").symlink_to(link_target)

    result = install(submodule, target)

    marker = skill_dir / ".eda-toolkit-installed"
    assert f"migrated .claude/skills/{name} (legacy symlink)" in result.stdout
    assert marker.read_text().strip() == "created by kicad_skills/bin/install-skills.sh"

    install(submodule, target, "--uninstall")
    assert not skill_dir.exists()


def test_an_unrelated_symlink_is_not_adopted_or_uninstalled(project):
    target, submodule = project
    name = GUIDES[0]
    skill_dir = target / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True)
    user_skill = target / "user-skill.md"
    user_skill.write_text(USER_SKILL_CONTENT)
    skill = skill_dir / "SKILL.md"
    skill.symlink_to(Path(os.path.relpath(user_skill, skill_dir)))

    install(submodule, target)
    assert not (skill_dir / ".eda-toolkit-installed").exists()

    install(submodule, target, "--uninstall")
    assert skill.is_symlink()
    assert skill.read_text() == USER_SKILL_CONTENT


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
    assert not (target / ".agents").exists()
    assert not (target / ".claude").exists()


def test_a_destination_with_whitespace_is_not_split(project):
    target, submodule = project
    destination = "agent skills"
    install(submodule, target, "--dest", destination)

    skill = target / destination / GUIDES[0] / "SKILL.md"
    assert skill.is_symlink()
    assert skill.read_text().startswith("---\n")
    assert not (target / "agent").exists()
    assert not (target / "skills").exists()

    install(submodule, target, "--dest", destination, "--uninstall")
    assert not (target / destination).exists()


def test_redundant_destination_separators_are_normalized(project):
    target, submodule = project
    result = install(submodule, target, "--dest", "a//b")

    skill = target / "a" / "b" / GUIDES[0] / "SKILL.md"
    assert f"installed a/b/{GUIDES[0]}/SKILL.md" in result.stdout
    assert skill.is_symlink()
    assert skill.read_text().startswith("---\n")

    install(submodule, target, "--dest", "a//b", "--uninstall")
    assert not (target / "a").exists()


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
    assert not (target / ".agents").exists()
    assert not (target / ".claude").exists()


def test_uninstall_removes_what_it_installed(project):
    target, submodule = project
    install(submodule, target)
    keep = target / "hardware.txt"
    keep.write_text("Target project content.\n")

    install(submodule, target, "--uninstall")
    assert not (target / ".agents").exists()
    assert not (target / ".claude").exists()  # including the directory it created
    assert not (target / "bin" / "eda.sh").exists()
    assert keep.exists()
    # the guides themselves are untouched - they are the source, not a copy
    assert (submodule / "docs" / "guides" / f"{GUIDES[0]}.md").exists()


def test_uninstall_preserves_a_skill_the_installer_skipped(project):
    target, submodule = project
    existing_skill = target / ".agents" / "skills" / GUIDES[0]
    existing_skill.mkdir(parents=True)
    (existing_skill / "SKILL.md").write_text(USER_SKILL_CONTENT)
    (existing_skill / "notes.txt").write_text(USER_NOTES_CONTENT)

    install(submodule, target)
    install(submodule, target, "--uninstall")

    assert (existing_skill / "SKILL.md").read_text() == USER_SKILL_CONTENT
    assert (existing_skill / "notes.txt").read_text() == USER_NOTES_CONTENT


def test_uninstall_keeps_unrelated_files_added_to_an_installed_skill(project):
    target, submodule = project
    install(submodule, target)
    skill_dir = target / ".agents" / "skills" / GUIDES[0]
    notes = skill_dir / "notes.txt"
    notes.write_text(USER_NOTES_CONTENT)

    install(submodule, target, "--uninstall")

    assert notes.read_text() == USER_NOTES_CONTENT
    assert not (skill_dir / "SKILL.md").exists()
    assert not (skill_dir / ".eda-toolkit-installed").exists()


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
    assert (checkout / ".agents" / "skills" / GUIDES[0] / "SKILL.md").is_symlink()
    assert (checkout / ".claude" / "skills" / GUIDES[0] / "SKILL.md").is_symlink()
    assert not (checkout / "bin" / "eda.sh").exists()  # it already has the real one


@pytest.mark.parametrize("directory", [".agents/skills", ".claude/skills"])
def test_the_generated_directories_are_not_tracked(directory):
    """Skill layouts are adapters; docs/guides/ is what gets reviewed."""
    ignored = subprocess.run(["git", "check-ignore", "-q", f"{directory}/x/SKILL.md"], cwd=ROOT)
    assert ignored.returncode == 0, f"{directory}/ must stay git-ignored"
