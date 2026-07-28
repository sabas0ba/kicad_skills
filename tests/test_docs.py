"""The published documentation is the Markdown in the repository.

GitHub Pages serves main / (root) with the settings in `_config.yml`, so a link
that resolves on github.com can still 404 on the site: anything under an
excluded path is simply not there. These checks keep the two in agreement, and
keep the guides reachable from an index rather than only by knowing the path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "_config.yml"
GUIDE_DIR = ROOT / "docs" / "guides"

# Every Markdown file the site publishes.
PUBLISHED = sorted(
    [ROOT / "README.md", ROOT / "AGENTS.md", *(ROOT / "docs").rglob("*.md")],
    key=str,
)

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _config_text() -> str:
    return CONFIG.read_text(encoding="utf-8")


def _excluded_prefixes() -> list[str]:
    """The `exclude:` list, read without a YAML parser (none is a test dependency)."""
    lines = _config_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "exclude:")
    out = []
    for line in lines[start + 1 :]:
        if not line.startswith("  - "):
            break
        out.append(line[4:].strip())
    return out


def _is_excluded(relative: str) -> bool:
    for prefix in _excluded_prefixes():
        if relative == prefix.rstrip("/") or relative.startswith(prefix.rstrip("/") + "/"):
            return True
    # Jekyll never publishes dot files or dot directories.
    return any(part.startswith(".") for part in Path(relative).parts)


def _links(md: Path) -> list[str]:
    return [
        target
        for target in LINK.findall(md.read_text(encoding="utf-8"))
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    ]


@pytest.mark.parametrize("md", PUBLISHED, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_link_from_a_published_page_resolves(md: Path):
    for target in _links(md):
        resolved = (md.parent / target.split("#")[0]).resolve()
        assert resolved.exists(), f"{md.relative_to(ROOT)} -> {target} does not exist"


@pytest.mark.parametrize("md", PUBLISHED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_published_page_links_into_an_excluded_path(md: Path):
    """A link into src/ or docker/ works on github.com and 404s on the site."""
    for target in _links(md):
        resolved = (md.parent / target.split("#")[0]).resolve()
        relative = resolved.relative_to(ROOT).as_posix()
        assert not _is_excluded(relative), (
            f"{md.relative_to(ROOT)} -> {target} is excluded from the site; "
            f"link to it on github.com instead"
        )


@pytest.mark.parametrize("md", PUBLISHED, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_published_page_opens_with_its_title(md: Path):
    """jekyll-titles-from-headings only reads a heading that comes first.

    Put anything above it - a note, a badge row, a blockquote - and the page is
    published titled after the site instead of after itself.
    """
    body = md.read_text(encoding="utf-8")
    if body.startswith("---\n"):  # skip the front matter the guides carry
        body = body.split("---\n", 2)[2]
    first = next(line for line in body.splitlines() if line.strip())
    assert first.startswith("# "), (
        f"{md.relative_to(ROOT)} starts with {first[:40]!r}, so it has no title on the site"
    )


@pytest.mark.parametrize("md", PUBLISHED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_published_page_contains_liquid_delimiters(md: Path):
    """Jekyll expands Liquid everywhere, fenced code blocks included.

    `--format '{{.Manifest.Digest}}'` was silently deleted from the published
    page, leaving a command that prints the whole manifest instead of a digest.
    There is no per-file opt-out on the Jekyll that GitHub Pages runs, and
    `{% raw %}` would show up verbatim on github.com, so the rule is simply not
    to write the delimiters - every case so far has had a clean alternative.
    """
    body = md.read_text(encoding="utf-8")
    for delimiter in ("{{", "{%"):
        assert delimiter not in body, (
            f"{md.relative_to(ROOT)} contains {delimiter!r}, which Jekyll will expand"
        )


def test_the_site_is_served_from_the_repository_root():
    text = _config_text()
    assert "theme:" in text, "the site needs a theme to render as HTML"
    assert re.search(r"^baseurl: /kicad_skills$", text, re.MULTILINE), (
        "a project site lives under /<repo>/, so baseurl must match the repository name"
    )


def test_the_toolkit_itself_is_not_published_as_a_website():
    excluded = _excluded_prefixes()
    for directory in ("src/", "tests/", "tools/", "docker/", "bin/"):
        assert directory in excluded, f"{directory} would be copied into the site"


def test_the_guides_index_links_every_guide():
    index = (GUIDE_DIR / "README.md").read_text(encoding="utf-8")
    for guide in sorted(GUIDE_DIR.glob("*.md")):
        if guide.name == "README.md":
            continue
        assert f"({guide.name})" in index, f"{guide.name} is not linked from the guides index"


def test_the_readme_links_the_guides_index():
    assert "docs/guides/README.md" in (ROOT / "README.md").read_text(encoding="utf-8")
