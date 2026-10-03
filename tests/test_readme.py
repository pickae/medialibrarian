"""The README and docs/ against the package they describe.

Item 8.1's verify, kept as a test because the three things it asks are the three
that rot silently: a command that is renamed, a link that stops resolving, and
a requirements list that disagrees with the install.
"""

import re
from pathlib import Path

import pytest

from medialib import commands
from tests import blackbox

pytestmark = pytest.mark.pure

_README = (blackbox.REPO / "README.md").read_text(encoding="utf-8")
_PAGES = [blackbox.REPO / "README.md", *sorted((blackbox.REPO / "docs").rglob("*.md"))]
_COMMAND_PAGES = sorted((blackbox.REPO / "docs" / "commands").glob("*.md"))


def _text(page: Path) -> str:
    return page.read_text(encoding="utf-8")


def _headings(page: Path) -> list[str]:
    return re.findall(r"^#+ (.+)$", _text(page), re.MULTILINE)


def _anchor(heading: str) -> str:
    """GitHub's rule: lower-cased, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\s-]", "", heading.replace("`", "").lower())
    return re.sub(r"\s+", "-", text.strip())


def test_no_entry_script_is_named():
    """The eighteen .sh files were deleted at item 6.3; a page that still names
    one is telling a reader to run something that is not there."""
    assert [p.name for p in _PAGES if ".sh" in _text(p)] == []


def test_every_command_page_is_a_command_the_package_installs():
    assert _COMMAND_PAGES, "the per-command pages are gone"
    assert [p.stem for p in _COMMAND_PAGES if p.stem not in commands.COMMANDS] == []
    assert [p.name for p in _COMMAND_PAGES if _headings(p)[0] != f"`{p.stem}`"] == []


def test_every_link_reaches_a_file_and_heading():
    broken = []
    for page in _PAGES:
        for target in re.findall(r"\]\(([^)]+)\)", _text(page)):
            if target.startswith(("http://", "https://")):
                continue
            path, _, fragment = target.partition("#")
            dest = (page.parent / path).resolve() if path else page
            if not dest.exists() or (fragment and fragment not in
                                     {_anchor(h) for h in _headings(dest)}):
                broken.append(f"{page.name}: {target}")
    assert broken == []


def test_the_requirements_agree_with_the_install():
    """`mutagen` is the one third-party import, and the README used to say there
    was nothing to install three bullets above listing it."""
    requirements = _README.split("## Requirements", 1)[1].split("\n##", 1)[0]
    assert "`mutagen`" in requirements
    assert "nothing to install" not in _README
