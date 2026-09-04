"""The white box for medialib/lib/runlog.py - what a run says while it works.

``log`` writes one line to stderr, timestamped only when LOG_TIMESTAMPS is set,
and ``counted_prefix`` prints its "[n/total] " only when flock is there to keep
the count honest.
"""

import io
import re
import sys

import pytest

from medialib.lib import runlog

try:
    import fcntl
except ImportError:      # a platform without POSIX file locks
    fcntl = None

pytestmark = pytest.mark.pure

needs_fcntl = pytest.mark.skipif(fcntl is None,
                                 reason="no fcntl on this platform")


class TestLog:
    def test_without_timestamps_the_line_is_the_arrow_form(self, monkeypatch):
        monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)
        out = io.StringIO()
        runlog.log("Censusing 3 file(s)", stream=out)
        assert out.getvalue() == "==> Censusing 3 file(s)\n"

    def test_with_timestamps_the_line_carries_a_clock(self, monkeypatch):
        monkeypatch.setenv("LOG_TIMESTAMPS", "1")
        out = io.StringIO()
        runlog.log("Wrote 2 rows", stream=out)
        assert re.fullmatch(r"\[\d\d:\d\d:\d\d\] Wrote 2 rows\n",
                            out.getvalue())

    def test_the_arguments_are_joined_with_spaces(self, monkeypatch):
        """`log a b c` is `"$*"`, one line, single-spaced."""
        monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)
        out = io.StringIO()
        runlog.log("one", 2, "three", stream=out)
        assert out.getvalue() == "==> one 2 three\n"

    def test_an_empty_message_is_still_a_line(self, monkeypatch):
        monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)
        out = io.StringIO()
        runlog.log(stream=out)
        assert out.getvalue() == "==> \n"


class TestCountedPrefix:
    def test_with_flock_it_is_the_position(self, monkeypatch):
        monkeypatch.setenv("HAVE_FLOCK", "1")
        assert runlog.counted_prefix(812, 40000) == "[812/40000] "

    def test_without_flock_it_is_nothing_at_all(self, monkeypatch):
        """Not "[1/40000] " per worker: without the lock there is no shared
        count, and a number each worker made up on its own is worse than none."""
        monkeypatch.setenv("HAVE_FLOCK", "")
        assert runlog.counted_prefix(812, 40000) == ""


class TestCpuCount:
    def test_it_is_the_cores(self, monkeypatch):
        monkeypatch.setattr(runlog.os, "cpu_count", lambda: 8)
        assert runlog.cpu_count() == 8

    @pytest.mark.parametrize("answer", [None, 0])
    def test_an_unreadable_count_runs_serially_rather_than_not_at_all(
            self, monkeypatch, answer):
        monkeypatch.setattr(runlog.os, "cpu_count", lambda: answer)
        assert runlog.cpu_count() == 1


class TestFlock:
    def test_a_host_with_flock_settles_the_flag(self, monkeypatch):
        monkeypatch.setattr(runlog.shutil, "which", lambda name: "/usr/bin/" + name)
        assert runlog.settle_flock() == "1"
        assert runlog.counted_prefix(1, 2) == "[1/2] "

    def test_a_host_without_it_settles_the_flag_empty(self, monkeypatch):
        monkeypatch.setattr(runlog.shutil, "which", lambda name: None)
        assert runlog.settle_flock() == ""
        assert runlog.counted_prefix(1, 2) == ""

    @needs_fcntl
    def test_the_lock_is_taken_when_the_flag_says_so(self, tmp_path,
                                                     monkeypatch):
        monkeypatch.setenv("HAVE_FLOCK", "1")
        taken = []
        monkeypatch.setattr("fcntl.flock",
                            lambda fd, how: taken.append(how))
        with open(tmp_path / "console.lock", "w") as handle, runlog.take_lock(handle):
            pass
        assert taken == [fcntl.LOCK_EX, fcntl.LOCK_UN]

    @needs_fcntl
    def test_without_the_flag_no_lock_is_taken_at_all(self, tmp_path,
                                                      monkeypatch):
        """Not "lock anyway because fcntl is always there": the shell takes no
        lock on such a host, and the two halves of one run must agree."""
        monkeypatch.setenv("HAVE_FLOCK", "")
        taken = []
        monkeypatch.setattr("fcntl.flock",
                            lambda fd, how: taken.append(how))
        with open(tmp_path / "console.lock", "w") as handle, runlog.take_lock(handle):
            pass
        assert taken == []

    @needs_fcntl
    def test_a_lock_that_cannot_be_taken_still_runs_the_block(self, tmp_path,
                                                              monkeypatch):
        monkeypatch.setenv("HAVE_FLOCK", "1")

        def refuse(fd, how):
            raise OSError("no locks available")

        monkeypatch.setattr("fcntl.flock", refuse)
        ran = []
        with open(tmp_path / "console.lock", "w") as handle, runlog.take_lock(handle):
            ran.append(True)
        assert ran == [True]


class TestJobsPerCore:
    """How many jobs run at once when one job wants several cores."""

    def test_the_cores_divided_by_what_one_job_wants(self, monkeypatch):
        monkeypatch.setattr(runlog, "cpu_count", lambda: 16)
        assert runlog.jobs_per_core(4) == 4

    def test_a_machine_smaller_than_the_divisor_still_runs(self, monkeypatch):
        """Floored at 1: a two-core host runs serially rather than not at
        all."""
        monkeypatch.setattr(runlog, "cpu_count", lambda: 2)
        assert runlog.jobs_per_core(4) == 1

    def test_no_divisor_is_one_job_per_core(self, monkeypatch):
        monkeypatch.setattr(runlog, "cpu_count", lambda: 8)
        assert runlog.jobs_per_core() == 8

    @pytest.mark.parametrize("divisor", [0, -1])
    def test_a_divisor_that_would_not_divide_is_treated_as_one(
            self, monkeypatch, divisor):
        """The shell's ``$((total / 0))`` would kill the run; the floor on the
        divisor is what keeps it a job count."""
        monkeypatch.setattr(runlog, "cpu_count", lambda: 8)
        assert runlog.jobs_per_core(divisor) == 8



class TestTheMissingFlockWarning:
    """Said once, at startup, and only when a missing flock will actually cost
    the run its progress positions."""

    @pytest.fixture(autouse=True)
    def a_clean_latch(self, monkeypatch):
        for name in ("SKIP_TOOL_PREFLIGHT", "UNCOUNTED_PROGRESS_WARNED"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HAVE_FLOCK", "")

    def _said(self, monkeypatch):
        lines = []
        monkeypatch.setattr(runlog, "log", lines.append)
        runlog.warn_uncounted_progress()
        return "\n".join(lines)

    def test_without_flock_it_says_what_is_lost_and_what_is_not(self,
                                                               monkeypatch):
        said = self._said(monkeypatch)
        assert "flock not installed" in said
        # the half that stops it reading as a data-loss warning
        assert "nothing is skipped" in said

    def test_with_flock_there_is_nothing_to_warn_about(self, monkeypatch):
        monkeypatch.setenv("HAVE_FLOCK", "1")
        assert self._said(monkeypatch) == ""

    def test_skipping_the_preflight_silences_it_too(self, monkeypatch):
        monkeypatch.setenv("SKIP_TOOL_PREFLIGHT", "1")
        monkeypatch.setenv("HAVE_FLOCK", "")
        assert self._said(monkeypatch) == ""

    def test_it_is_said_once_per_run_not_once_per_caller(self, monkeypatch):
        """Several of these scripts reach it twice over, and a child has nothing
        to add to something the run has already said."""
        assert self._said(monkeypatch) != ""
        assert self._said(monkeypatch) == ""


class TestWhichInterpreterAHelperIsRunWith:
    """It answers for a helper SCRIPT that is shelled out to, and not for the
    package, which is already running on the interpreter that imported it.

    PATH stays authoritative, which is what keeps a fixture's stub interpreter
    in charge of what a run shells out to: reaching for sys.executable here ran
    the real one against a stub's fixtures, and cost an afternoon.
    """

    def test_the_environment_wins(self, monkeypatch):
        monkeypatch.setenv("PYTHON_BIN", "/opt/stub/python3")
        assert runlog.python_bin() == "/opt/stub/python3"

    def test_without_one_the_running_interpreter_is_the_answer(self,
                                                               monkeypatch):
        monkeypatch.delenv("PYTHON_BIN", raising=False)
        assert runlog.python_bin() == sys.executable

    def test_an_empty_setting_is_no_setting(self, monkeypatch):
        monkeypatch.setenv("PYTHON_BIN", "")
        assert runlog.python_bin() == sys.executable


class TestEveryHelperGoesThroughIt:
    """A module that resolves the interpreter for itself opts out of the above
    silently, which is how one of them came to ignore PYTHON_BIN altogether."""

    @pytest.mark.parametrize("module", ["bookpublishing", "thumbnails"])
    def test_the_modules_that_run_a_helper_script(self, module, monkeypatch):
        import importlib
        loaded = importlib.import_module("medialib.lib." + module)
        monkeypatch.setenv("PYTHON_BIN", "/opt/stub/python3")
        assert loaded._python_bin() == "/opt/stub/python3"
