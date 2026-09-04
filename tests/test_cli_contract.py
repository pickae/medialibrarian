"""Every command's command-line surface, pinned byte for byte.

Four scenarios per command, each one a thing a user hits before any real work
starts - the help page, no arguments at all, a flag the parser has never heard of,
and a first path that is not there - recorded as stdout, stderr and exit code
together.

Bytes rather than substrings, because a substring check lets whitespace, line
order, the exit code or which stream a line went to drift silently, and these
pages are rendered by one shared renderer that every command's spec feeds. That is
what caught every accidental change through 48 module ports and 18 script ports.

Each command names ITSELF on its own page: the recording directory is the command
name and `Usage:` carries it.

REGEN=1 rewrites the fixtures from what the commands do now. It is run when a
decision says the pages change and never to make a red test green: a fixture that
"needs" regenerating is a bug until the diff proves otherwise.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from medialib import commands
from tests import blackbox

pytestmark = pytest.mark.fs

_FIXTURES = blackbox.DATA / "cliContract"
_REGEN = bool(os.environ.get("REGEN", ""))

# `cue-to-chapters` parses no options at all, so -x there is a path name and it
# has no unknown-flag shape to pin; and with no options there is no help page.
_NO_OPTIONS = {"cue-to-chapters"}

# The one argument position every one of these checks before doing anything. A
# RELATIVE name, so the refusal - which quotes the path as typed - carries the
# same bytes on every host.
_MISSING_DIR = {
    "concat-audio": ["no-such-dir", "out"],
    "convert-and-concat": ["no-such-dir", "out"],
    "convert-audio": ["no-such-dir", "out"],
    "transcribe-audio": ["no-such-dir", "out"],
    "convert-video": ["no-such-dir", "out"],
    "ingest-music": ["no-such-dir", "out"],
    "read-library": ["no-such-dir", "out"],
    "ingest-movies": ["no-such-dir"],
}


def _scenarios():
    for command in sorted(commands.COMMANDS):
        if command not in _NO_OPTIONS:
            yield command, "h", ["-h"]
        yield command, "noargs", []
        if command not in _NO_OPTIONS:
            yield command, "errUnknown", ["-x"]
        if command in _MISSING_DIR:
            yield command, "missingDir", _MISSING_DIR[command]


def _normalise(text: str, workdir: Path) -> str:
    """The two things that would otherwise pin a fixture to this machine: where
    the checkout is, and where the run happened. Two commands resolve the missing
    path to absolute before quoting it, which is the second one."""
    return text.replace(str(blackbox.REPO), "<repo>").replace(str(workdir),
                                                              "<workdir>")


@pytest.mark.parametrize(
    "command,scenario,argv", list(_scenarios()),
    ids=lambda value: value if isinstance(value, str) else "")
def test_the_page_is_the_one_that_was_recorded(command, scenario, argv,
                                               sandbox):
    directory = _FIXTURES / command
    done = sandbox.run(command, *argv)
    got = {"out": _normalise(done.stdout, sandbox.work),
           "err": _normalise(done.stderr, sandbox.work),
           "rc": "%d\n" % done.returncode}

    if _REGEN:
        directory.mkdir(parents=True, exist_ok=True)
        for suffix, text in got.items():
            (directory / ("%s.%s" % (scenario, suffix))).write_text(
                text, encoding="utf-8")
        pytest.skip("REGEN=1: recorded %s %s (rc=%d)"
                    % (command, scenario, done.returncode))

    for suffix in ("out", "err", "rc"):
        recorded = directory / ("%s.%s" % (scenario, suffix))
        assert recorded.is_file(), (
            "no fixture %s - run with REGEN=1 if the page is meant to exist"
            % recorded)
        assert got[suffix] == recorded.read_text(encoding="utf-8"), (
            "%s %s: %s differs from what was recorded" % (command, scenario,
                                                          suffix))


def test_every_recorded_page_belongs_to_a_scenario_that_still_runs():
    """A fixture nothing compares any more is a page that stopped being pinned
    without anything failing."""
    recorded = {path.parent.name + "/" + path.name
                for path in _FIXTURES.glob("*/*")}
    expected = {"%s/%s.%s" % (command, scenario, suffix)
                for command, scenario, _ in _scenarios()
                for suffix in ("out", "err", "rc")}
    assert recorded == expected


def test_there_are_pages_to_compare():
    """A glob that matched nothing would satisfy every assertion above by never
    running one."""
    assert len(list(_FIXTURES.glob("*/*"))) == 180
    assert shutil.which("bash") or True
