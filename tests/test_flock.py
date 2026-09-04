"""Whether this run locks, and who is allowed to ask.

``HAVE_FLOCK`` is a decision with three states and an environment variable that
can only hold two: set to "1", set to "" and not there at all. The third is
"nobody has asked yet", and a command that read the variable directly could not
tell it from "asked, and the answer is no" - so it took the no-flock path on a
host that has flock, printing progress lines with no position and closing counts
that read low. Six of the eighteen commands were in that state, kept right only
by the shim that ran ahead of them.

One reader resolves it, and these are the cases that keep it the only one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from medialib import commands
from medialib.lib import runlog
from tests import blackbox

_REPO = blackbox.REPO
_TOOLSTUB = blackbox.TOOLSTUB


def _without_fcntl(monkeypatch) -> None:
    """A host whose interpreter has no ``fcntl`` - Windows, from anywhere.

    A None in ``sys.modules`` is what CPython turns into the ImportError the
    probe catches, so the case reads the same on a host that really has the
    module and one that does not.
    """
    monkeypatch.setitem(sys.modules, "fcntl", None)


class TestHaveFlock:
    pytestmark = pytest.mark.pure

    def test_an_answer_in_the_environment_stands(self, monkeypatch):
        monkeypatch.setenv("HAVE_FLOCK", "1")
        assert runlog.have_flock() is True

    def test_and_an_empty_answer_is_an_answer(self, monkeypatch):
        """A parent that probed and found none, or a case asking for the
        countless path - not an unsettled variable to probe again."""
        monkeypatch.setenv("HAVE_FLOCK", "")
        assert runlog.have_flock() is False

    def test_nothing_there_at_all_is_probed(self, monkeypatch):
        monkeypatch.delenv("HAVE_FLOCK", raising=False)
        monkeypatch.setattr(runlog.shutil, "which", lambda tool: "/usr/bin/flock")
        assert runlog.have_flock() is True

    def test_no_tool_but_a_locking_c_library_still_counts(self, monkeypatch):
        """The macOS rung. ``flock(1)`` is util-linux and has never shipped on
        a Mac; the system call the lock is actually taken with has. Answering
        no there would have been the port declining a facility it was already
        using."""
        pytest.importorskip("fcntl")
        monkeypatch.delenv("HAVE_FLOCK", raising=False)
        monkeypatch.setattr(runlog.shutil, "which", lambda tool: None)
        assert runlog.have_flock() is True

    def test_and_a_probe_that_finds_neither_answers_no(self, monkeypatch):
        """Windows: no tool on PATH, and ``fcntl`` is a POSIX module that
        interpreter does not build."""
        monkeypatch.delenv("HAVE_FLOCK", raising=False)
        monkeypatch.setattr(runlog.shutil, "which", lambda tool: None)
        _without_fcntl(monkeypatch)
        assert runlog.have_flock() is False

    def test_the_probe_is_left_in_the_environment_for_the_children(
            self, monkeypatch):
        """A worker fanned out to inherits the parent's answer rather than
        probing for its own; two halves of one run that disagreed would print
        two different kinds of progress line."""
        monkeypatch.delenv("HAVE_FLOCK", raising=False)
        monkeypatch.setattr(runlog.shutil, "which", lambda tool: None)
        _without_fcntl(monkeypatch)
        runlog.have_flock()
        assert os.environ["HAVE_FLOCK"] == ""

    def test_the_probe_happens_once(self, monkeypatch):
        monkeypatch.delenv("HAVE_FLOCK", raising=False)
        probes = []
        monkeypatch.setattr(runlog.shutil, "which",
                            lambda tool: probes.append(tool) or "/usr/bin/flock")
        runlog.have_flock()
        runlog.have_flock()
        assert probes == ["flock"]


def test_one_module_reads_the_variable_and_it_is_runlog():
    """The structural half of the rule. A command cannot read it unsettled if it
    cannot read it at all, which is what makes all eighteen right at once rather
    than eighteen call sites that each have to remember."""
    named = sorted(
        path.relative_to(_REPO).as_posix()
        for path in _production_modules()
        if "HAVE_FLOCK" in path.read_text(encoding="utf-8"))
    assert named == ["medialib/lib/runlog.py"]


def _production_modules():
    """Everything under the package that is not itself a test. A case may say
    what a host answers - that is how the branches are reached."""
    return [path for path in _REPO.glob("medialib/**/*.py")
            if not path.name.startswith("test_") and path.name != "conftest.py"]


def test_the_walk_finds_the_package():
    """A glob that matched nothing would satisfy the assertion above by never
    reading a file."""
    assert len(_production_modules()) > 70


@pytest.mark.stubbed
@pytest.mark.skipif(not shutil.which("flock"),
                    reason="the claim is about a host that HAS flock")
def test_a_command_run_with_the_variable_unset_still_locks(tmp_path):
    """The whole point, end to end: a command that takes no step of its own to
    settle the variable, run with nothing having settled it, does not claim the
    host has no flock while standing on a host that has one.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("zip", "unzip", "convert", "identify"):
        shutil.copy(_TOOLSTUB, bin_dir / tool)
    (tmp_path / "in").mkdir()

    env = {k: v for k, v in os.environ.items() if k != "HAVE_FLOCK"}
    env["PATH"] = "%s:%s" % (bin_dir, os.path.dirname(shutil.which("flock")))
    env["TOOLSTUB_LOG"] = str(tmp_path / "calls")
    done = subprocess.run(
        [sys.executable, "-m", commands.module_for("convert-comics"),
         str(tmp_path / "in"), str(tmp_path / "out")],
        env=env, capture_output=True, text=True)

    assert "flock not installed" not in done.stderr, done.stderr
