"""What a run does when it is STOPPED - by Ctrl+C, by a kill, or by the terminal
window being closed under it.

Two promises, both of which a run gets wrong by default and has to be written for:

  1. **It still prints its closing report.** A run stopped halfway did work, and
     the report is the only account of how much of it survived, so the same
     figures a finished run prints have to come out for the part that got done.
  2. **It hands its RAM scratch back.** These commands work in a tmpfs, and a
     process killed by a signal it does not handle never runs its cleanup - so
     the scratch stays resident until the machine is rebooted. A few interrupted
     runs of anything that holds whole books or video chunks in there fills the
     tmpfs and makes the NEXT run fail for a reason nothing points at.

SIGTERM and SIGHUP rather than SIGINT, and only because of how the signal is
delivered here; all three go through the same handler, which is the thing under
test. SIGHUP gets a case of its own because it is the closed-terminal window - the
interruption nobody is watching the output of.

The scratch base is this test's own directory rather than the shared tmpfs root.
Reading the shared root cannot answer the question while anything else is running:
another suite, or a real conversion, creates entries with exactly the names these
commands use and they read as this run's leak. A private base makes the assertion
simply "is this directory empty", with no list of scratch names to keep in step.
"""

from __future__ import annotations

import os
import signal
import time

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

# Long enough that the run is certainly still inside its first probe when the
# signal lands, so it is stopped mid-queue rather than after finishing - and no
# longer than that, because `convert-and-concat`'s delegated child leaves its own
# `ffprobe` sleeping when the wrapper goes, and that grandchild holds the output
# pipe open until it wakes. Reading the pipe therefore costs this delay once per
# interrupted wrapper run, and the wrapper's own two promises are kept
# either way.
_SLOW = "sleep 5"

# The signal is sent once the run has actually claimed its scratch, not after a
# fixed wait. A fixed wait has to be picked for the slowest command on the most
# loaded machine and is then dead time for every other case - and `ingest-music`
# scans before it allocates, so the wait that suited `convert-audio` was too short
# for it. Waiting for the thing the case is about to exist removes the guess.
_SCRATCH_TIMEOUT = 30.0

# Once it has, a moment more so the signal lands with the queue under way rather
# than during setup - being stopped mid-work is what the report is an account of.
_GRACE = 0.4


class _Stopped:
    """One interrupted run: its status, everything it printed, and what it was
    holding in the scratch base at the moment it was signalled."""

    def __init__(self, status, log, during, base):
        self.status = status
        self.log = log
        self.during = during
        self.leaked = sorted(p.name for p in base.iterdir())


@pytest.fixture
def interrupt(sandbox, tmp_path):
    """Slow media stubs, a private scratch base, and a way to stop a run.

    `ffprobe` is what these commands call first and once per item, so making IT
    the slow one is what reliably leaves a run still mid-queue. The encoders still
    produce their outputs, so the items that DID finish are real.

    No stub of `python3`: a command IS the interpreter, and a stub by that name
    makes the whole run exit 0 having done nothing - which reads exactly like a
    command that ignored its signal.
    """
    sandbox.with_media_stubs()
    sandbox.with_tool("ffprobe", 'echo "123.456"\n' + _SLOW)
    sandbox.with_tool("convert",
                      'out="${!#}"; out="${out%\\>}"\n' + _SLOW + '\n: > "$out"')
    sandbox.with_tool("identify", 'echo "100 100"')
    sandbox.with_tools("mkvpropedit", "mkvextract", "fdupes", "rsync", "jq")

    base = tmp_path / "rambase"
    base.mkdir()
    # Every knob, not just `ramScratchBase`. The per-command ones reach
    # `init_ram_base` as its OVERRIDE argument and so win over the general one -
    # and conftest points them at each test's own workspace, so setting only
    # `ramScratchBase` leaves `ingest-music` and `convert-comics` allocating
    # somewhere this fixture is not watching.
    environment = dict(os.environ,
                       **{knob: str(base) for knob in
                          ("ramScratchBase", "ramBase", "censusRamBase",
                           "comicsRamBase", "musicRamBase",
                           "readLibraryRamBase")})

    def stop(sig, command, *args):
        started = blackbox.start(command, *args, cwd=sandbox.work,
                                 path=sandbox.path, env=environment)
        deadline = time.monotonic() + _SCRATCH_TIMEOUT
        while not any(base.iterdir()) and time.monotonic() < deadline:
            if started.poll() is not None:
                break
            time.sleep(0.05)
        time.sleep(_GRACE)
        during = sorted(p.name for p in base.iterdir())
        started.send_signal(sig)
        log, _ = started.communicate(timeout=180)
        return _Stopped(started.returncode, log, during, base)

    def finish(command, *args):
        """A run allowed to complete, for the one case that needs a control.

        With the waiting taken out: a run to completion should not have to sit
        through delays that exist only to keep an interrupted run still working.
        """
        sandbox.with_media_stubs()
        sandbox.with_tool("identify", 'echo "100 100"')
        sandbox.with_tools("mkvpropedit", "mkvextract", "fdupes", "rsync", "jq")
        done = sandbox.run(command, *args, env=environment, timeout=180)
        return done, sorted(p.name for p in base.iterdir())

    def fixture(name, *files, nested=""):
        folder = tmp_path / name
        (folder / nested if nested else folder).mkdir(parents=True)
        for entry in files:
            (folder / entry).write_text("x")
        return folder

    sandbox.stop = stop
    sandbox.finish = finish
    sandbox.tree = fixture
    sandbox.outputs = tmp_path / "out"
    return sandbox


def _tracks(count, extension="flac", prefix=""):
    return ["%strack%d.%s" % (prefix, n, extension) for n in range(1, count + 1)]


def _assert_stopped_cleanly(stopped, *, expected=130):
    """The two things every case below asserts about the stop itself.

    A wrong status on its own says nothing about WHY, and these runs are the ones
    that cannot simply be repeated to find out - what they are stopped in the
    middle of depends on how loaded the machine is. So the run's own output goes
    with the failure.
    """
    assert stopped.status == expected, stopped.log
    assert stopped.during, \
        "the run had claimed no scratch yet, so releasing it proves nothing"


class TestAnInterruptedConversion:
    """`convert-audio`, stopped part-way through its queue."""

    @pytest.fixture
    def stopped(self, interrupt):
        source = interrupt.tree("audioIn", *_tracks(8))
        return interrupt.stop(signal.SIGTERM, "convert-audio", "-j", "1",
                              source, interrupt.outputs)

    def test_it_exits_with_the_signals_status_and_says_it_was_interrupted(
            self, stopped):
        _assert_stopped_cleanly(stopped)
        assert "Interrupted" in stopped.log

    def test_it_still_prints_its_closing_stats_and_safety_recap(self, stopped):
        assert "Stats" in stopped.log, stopped.log
        assert "Total time:" in stopped.log, stopped.log
        assert "Safety: skipped" in stopped.log, stopped.log

    def test_the_closing_report_is_printed_exactly_once(self, stopped):
        """A run that reaches its own closing line after an abort check must not
        print the whole thing twice."""
        assert stopped.log.count("\nStats\n") == 1, stopped.log

    def test_it_leaves_no_scratch_behind(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert stopped.leaked == []


class TestAClosedTerminal:
    """SIGHUP: the same handler, and the interruption nobody is watching the
    output of - so the report being written matters more here, not less."""

    @pytest.fixture
    def stopped(self, interrupt):
        source = interrupt.tree("audioInHup", *_tracks(8))
        return interrupt.stop(signal.SIGHUP, "convert-audio", "-j", "1",
                              source, interrupt.outputs)

    def test_it_ends_the_run_with_the_signals_status_and_still_reports(
            self, stopped):
        _assert_stopped_cleanly(stopped)
        assert "Stats" in stopped.log, stopped.log

    def test_it_leaves_no_scratch_behind(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert stopped.leaked == []


class TestADifferentCommandWithTheSameContract:
    """`convert-images`, so what is asserted is the repo's behaviour rather than
    one command's."""

    @pytest.fixture
    def stopped(self, interrupt):
        source = interrupt.tree("imgIn", *_tracks(8, "jpg", prefix="page"))
        return interrupt.stop(signal.SIGTERM, "convert-images", "-j", "1",
                              source, interrupt.outputs)

    def test_it_exits_with_the_signals_status_and_still_reports(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert "Converted" in stopped.log, stopped.log
        assert "seconds" in stopped.log, stopped.log

    def test_it_leaves_no_scratch_behind(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert stopped.leaked == []


class TestAWrapperThatDelegates:
    """`convert-and-concat`, the hard case: it drives `convert-audio` and then
    `concat-audio`, once per sub-folder.

    This is where "release the scratch on the way out" is easiest to get wrong in
    either direction - leaking the wrapper's tree, or deleting it while the next
    phase is still reading from it. Both are visible: a leak in the scratch count,
    a premature delete in the exit status and the missing outputs.
    """

    def _books(self, interrupt, name):
        folder = interrupt.tree(name, nested="bookA/disc1")
        (folder / "bookB" / "disc1").mkdir(parents=True)
        for book in ("bookA", "bookB"):
            for n in (1, 2, 3):
                (folder / book / "disc1" / ("t%d.flac" % n)).write_text("x")
        return folder

    def test_a_run_allowed_to_finish_writes_both_outputs_and_leaks_nothing(
            self, interrupt):
        """The control the interrupted run below is read against."""
        source = self._books(interrupt, "concatIn")
        outputs = interrupt.outputs
        done, leaked = interrupt.finish("convert-and-concat", "-s", source,
                                        outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        assert (outputs / "bookA" / "disc1.flac").is_file()
        assert (outputs / "bookB" / "disc1.flac").is_file()
        assert leaked == []

    @pytest.fixture
    def stopped(self, interrupt):
        source = self._books(interrupt, "concatInKilled")
        return interrupt.stop(signal.SIGTERM, "convert-and-concat", "-s",
                              source, interrupt.outputs)

    def test_it_exits_with_the_signals_status_and_still_reports(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert "Stats" in stopped.log, stopped.log

    def test_it_leaves_no_scratch_behind(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert stopped.leaked == []


class TestALongRunningIngest:
    """`ingest-music`: encode, tag, then a second library in Opus - the longest
    run here by nature, so the one most likely to be stopped part-way, and the one
    where losing the account of what it had already ingested costs most."""

    @pytest.fixture
    def stopped(self, interrupt, tmp_path):
        source = interrupt.tree("musicIn", nested="Album")
        for track in _tracks(8):
            (source / "Album" / track).write_text("x")
        return interrupt.stop(signal.SIGTERM, "ingest-music", "-j", "1", source,
                              tmp_path / "musicLib", tmp_path / "musicLibopus")

    def test_it_exits_with_the_signals_status_and_says_it_was_interrupted(
            self, stopped):
        _assert_stopped_cleanly(stopped)
        assert "Interrupted" in stopped.log

    def test_it_still_prints_the_stats_of_the_part_it_ingested(self, stopped):
        assert "Stats" in stopped.log, stopped.log
        assert "Lossless tracks:" in stopped.log, stopped.log
        assert "Total time:" in stopped.log, stopped.log
        assert "Safety: skipped" in stopped.log, stopped.log

    def test_it_prints_the_closing_report_exactly_once(self, stopped):
        assert stopped.log.count("Lossless tracks: ") == 1, stopped.log

    def test_it_leaves_no_scratch_behind(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert stopped.leaked == []
