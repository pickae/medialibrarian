"""The README against the package it describes.

Item 8.1's verify, kept as a test because the three things it asks are the three
that rot silently: a command that is renamed, an anchor that stops resolving, and
a requirements list that disagrees with the install.
"""

import re

import pytest

from medialib import commands
from tests import blackbox

pytestmark = pytest.mark.pure

_README = (blackbox.REPO / "README.md").read_text(encoding="utf-8")


def _headings() -> list[str]:
    return re.findall(r"^#+ (.+)$", _README, re.MULTILINE)


def _anchor(heading: str) -> str:
    """GitHub's rule: lower-cased, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\s-]", "", heading.replace("`", "").lower())
    return re.sub(r"\s+", "-", text.strip())


def test_no_entry_script_is_named():
    """The eighteen .sh files were deleted at item 6.3; a README that still names
    one is telling a reader to run something that is not there."""
    assert ".sh" not in _README


def test_every_command_heading_is_a_command_the_package_installs():
    named = [h.strip("`") for h in _headings() if h.startswith("`")]
    assert named, "the per-command sections are gone"
    assert [n for n in named if n not in commands.COMMANDS] == []


def test_every_in_page_link_reaches_a_heading():
    anchors = {_anchor(h) for h in _headings()}
    assert [a for a in re.findall(r"\]\(#([^)]+)\)", _README) if a not in anchors] == []


def test_the_requirements_agree_with_the_install():
    """`mutagen` is the one third-party import, and the README used to say there
    was nothing to install three bullets above listing it."""
    requirements = _README.split("## Requirements", 1)[1].split("\n##", 1)[0]
    assert "`mutagen`" in requirements
    assert "nothing to install" not in _README
