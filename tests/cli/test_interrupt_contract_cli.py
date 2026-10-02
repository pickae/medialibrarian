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

    def stop(sig, command, *args, ready=None, env=None):
        """`ready` replaces "the scratch exists" as the moment to signal, for a
        run whose work starts well after its scratch does; `env` adds to the
        environment."""
        started = blackbox.start(command, *args, cwd=sandbox.work,
                                 path=sandbox.path,
                                 env=dict(environment, **(env or {})))
        ready = ready or (lambda: any(base.iterdir()))
        deadline = time.monotonic() + _SCRATCH_TIMEOUT
        while not ready() and time.monotonic() < deadline:
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


class TestAnInterruptedConversionLeavesNothingInTheTempFolder:
    """The logs a run makes for itself in TMPDIR - the renames it held back, the
    outputs of the wrong length - are read by the footer an interrupt still
    prints, and removed after it, just as at the end of a finished run."""

    @pytest.fixture
    def stopped(self, interrupt, tmp_path):
        source = interrupt.tree("audioIn", *_tracks(8))
        # A cover already holds the name the .jpeg would be renamed to.
        (source / "cover.jpeg").write_text("jpeg")
        (source / "cover.jpg").write_text("jpg")
        temp = tmp_path / "temp"
        temp.mkdir()
        stopped = interrupt.stop(signal.SIGTERM, "convert-audio", "-j", "1",
                                 source, interrupt.outputs,
                                 env={"TMPDIR": str(temp)})
        return stopped, temp

    def test_the_skip_is_still_reported(self, stopped):
        result, _ = stopped
        _assert_stopped_cleanly(result)
        assert "Safety: skipped 1 rename(s) to avoid overwrite" in result.log

    def test_the_temp_folder_is_empty_afterwards(self, stopped):
        result, temp = stopped
        _assert_stopped_cleanly(result)
        assert list(temp.iterdir()) == []


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


# A phone over adb, played by the local shell: `devices` lists one, and every
# device command runs here. The third `mv` hangs instead, so a run can be
# stopped with exactly two renames on the "phone" and one in flight.
_ADB = """\
case "$1" in
  devices) printf 'List of devices attached\\nfakephone\\tdevice\\n\\n' ;;
  shell|exec-out)
    case "$2" in
      mv\\ *) n=$(( $(cat '%(moves)s' 2>/dev/null || echo 0) + 1 ))
             echo "$n" > '%(moves)s'
             [ "$n" -gt 2 ] && exec sleep 60 ;;
    esac
    exec sh -c "$2" ;;
esac"""


class TestAReplayStoppedOnThePhone:
    """`clean-folder-structure-adb`, stopped part-way through replaying its
    renames: the phone then holds real, half-applied changes, and the footer is
    the only account of how many.

    Through the real adb route, quoting included, with the signal landing while
    the third rename hangs. The mirror is put in the scratch base through
    TMPDIR, so "no scratch left" is "the mirror was removed".
    """

    @pytest.fixture
    def stopped(self, interrupt, tmp_path):
        album = tmp_path / "phone" / "My_Album"
        album.mkdir(parents=True)
        names = _tracks(6, "mp3", prefix="my_")
        for name in names:
            (album / name).write_text(name)
        fragments = tmp_path / "no-fragments.txt"
        fragments.write_text("# no fragments\n", encoding="utf-8")
        moves = tmp_path / "mv-count"
        interrupt.with_tool("adb", _ADB % {"moves": moves})

        def hanging():
            return moves.is_file() and moves.read_text().strip() == "3"

        stopped = interrupt.stop(
            signal.SIGTERM, "clean-folder-structure-adb", "-f", fragments,
            album.parent, ready=hanging,
            env={"CFS_DEV_BACKEND": "adb", "ADB": str(interrupt.bin / "adb"),
                 "TMPDIR": str(tmp_path / "rambase")})
        return stopped, album.parent, names

    def test_it_exits_with_the_signals_status_and_says_it_was_interrupted(
            self, stopped):
        result, _, _ = stopped
        _assert_stopped_cleanly(result)
        assert "Interrupted" in result.log

    def test_the_footer_counts_the_renames_that_reached_the_phone(
            self, stopped):
        result, phone, _ = stopped
        assert "Done: 2 rename(s) applied on device" in result.log, result.log
        assert len(list((phone / "My Album").iterdir())) == 2

    def test_the_footer_is_printed_exactly_once(self, stopped):
        result, _, _ = stopped
        assert result.log.count("Done: ") == 1, result.log

    def test_no_file_is_lost_or_overwritten(self, stopped):
        _, phone, names = stopped
        held = sorted(path.read_text() for path in phone.rglob("*.mp3"))
        assert held == sorted(names)

    def test_the_mirror_is_removed(self, stopped):
        result, _, _ = stopped
        _assert_stopped_cleanly(result)
        assert result.leaked == []
# The narration checkout's interpreter, as little of it as a run needs: the setup
# probe's answer, and an audiobook for every book it is asked to read, with each
# request recorded beside it so a second run can say which books it read.
_CHECKOUT_PYTHON = r"""
here="$(dirname "$0")"
if [[ "$1" == "-c" ]]; then
    [[ "$2" == *sysconfig* ]] && mkdir -p "$here/site" && echo "$here/site"
    exit 0
fi
[[ "$1" == "app.py" ]] || exit 0
printf '%s\n' "$*" >> "$here/calls.log"
while [[ $# -gt 0 ]]; do
    [[ "$1" == "--output_dir" ]] && out="$2"
    shift
done
mkdir -p "$out" && printf 'audiobook' > "$out/Book.m4b"
"""


class TestAnInterruptedLibraryReading:
    """`read-library`, whose books take hours each, stopped with one of them in
    hand. The lossless library only (`-b 0`), so the slow probe after each
    narration is the one place the run can be waiting when the signal lands."""

    @pytest.fixture
    def checkout(self, tmp_path):
        checkout = tmp_path / "e2a"
        (checkout / "python_env" / "bin").mkdir(parents=True)
        (checkout / "app.py").touch()
        interpreter = checkout / "python_env" / "bin" / "python"
        interpreter.write_text("#!/usr/bin/env bash\n%s\n" % _CHECKOUT_PYTHON)
        interpreter.chmod(0o755)
        return checkout

    @pytest.fixture
    def stopped(self, interrupt, checkout):
        interrupt.with_tool("ebook-convert", 'cat -- "$1" > "$2"')
        source = interrupt.tree("booksIn", *_tracks(3, "epub"))
        arguments = ("-c", checkout, "-d", "cpu", "-l", "deu", "-b", "0",
                     source, interrupt.outputs)
        stopped = interrupt.stop(signal.SIGTERM, "read-library", *arguments)
        stopped.arguments = arguments
        return stopped

    def test_it_exits_with_the_signals_status_and_says_it_was_interrupted(
            self, stopped):
        _assert_stopped_cleanly(stopped)
        assert "Interrupted" in stopped.log

    def test_it_prints_its_closing_stats_exactly_once(self, stopped):
        assert stopped.log.count("\nStats\n") == 1, stopped.log
        assert "Books found:       3" in stopped.log, stopped.log

    def test_it_leaves_no_scratch_behind(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert stopped.leaked == []

    def test_a_second_run_reads_the_books_it_did_not_finish(self, stopped,
                                                           interrupt,
                                                           checkout):
        _assert_stopped_cleanly(stopped)
        library = interrupt.outputs / "m4b"
        finished = sorted(p.name for p in library.glob("*.m4b")) \
            if library.is_dir() else []
        assert len(finished) < 3, stopped.log
        calls = checkout / "python_env" / "bin" / "calls.log"
        calls.write_text("")
        done, leaked = interrupt.finish("read-library", *stopped.arguments)
        assert done.returncode == 0, done.stdout + done.stderr
        assert sorted(p.name for p in library.glob("*.m4b")) == [
            "track1.m4b", "track2.m4b", "track3.m4b"]
        assert len(calls.read_text().splitlines()) == 3 - len(finished)
        assert leaked == []


class TestAnInterruptedCensus:
    """`content-census -b`, stopped while a probe is in flight: the report of
    the part it read is kept, and the cubes are not built from it, since a cube
    cannot say it was built from half a library."""

    @pytest.fixture
    def stopped(self, interrupt):
        built = interrupt.work / "duckdbWasRun"
        interrupt.with_tool("duckdb", ': > "%s"' % built)
        library = interrupt.tree("Shelf", "a.txt", *_tracks(4, "mp3"))
        stopped = interrupt.stop(signal.SIGTERM, "content-census", "-b",
                                 library)
        stopped.library = library
        stopped.built = built
        return stopped

    def test_it_exits_with_the_signals_status(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert "Interrupted" in stopped.log

    def test_the_report_of_what_was_read_is_kept(self, stopped):
        assert (stopped.library / "booksShelf.csv").is_file(), stopped.log

    def test_the_cubes_are_not_built_from_it(self, stopped):
        assert "the cubes were NOT built" in stopped.log, stopped.log
        assert not stopped.built.exists()
        assert not list(stopped.library.glob("*.duckdb"))

    def test_it_leaves_no_scratch_behind(self, stopped):
        _assert_stopped_cleanly(stopped)
        assert stopped.leaked == []


class TestAVideoRunWaitingForItsDolbyVisionSlot:
    """`convert-video` prepares the next file while the current one encodes, and
    a Dolby Vision profile 7 file prepared ahead waits for the run's one DV slot.
    An interrupt has to reach that waiter too, or the run's exit waits on a
    thread that waits on a slot the stopped encode never hands back.

    In-process rather than a whole run: the waiter is a thread inside the run,
    and how long it takes to let go is the thing pinned."""

    def test_the_waiter_lets_go_within_about_a_second(self, tmp_path,
                                                      monkeypatch):
        import threading

        from medialib.cli import convert_video as rules
        from medialib.cli import convert_video_run as run_module
        from medialib.lib import safety

        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "abortRequested"))
        monkeypatch.setattr(run_module, "log", lambda *a, **k: None)
        monkeypatch.setattr(rules, "dolby_vision_profile",
                            lambda path: ("7", "1"))
        converted = []
        monkeypatch.setattr(run_module, "normalise_dolby_vision",
                            lambda *a: converted.append(a) or ("", ""))
        settings = rules.Settings(input_dir=str(tmp_path / "in"),
                                  chunk_root=str(tmp_path / "work"),
                                  dv_encoder_support=True)
        state = run_module.Run(settings)
        # The file encoding holds the slot, and is stopped holding it.
        assert state.dv_slot.acquire(timeout=1)
        ahead = run_module.Plan("b.mkv", rules.Settings(**settings.__dict__))
        waiter = threading.Thread(target=state._settle_dolby_vision,
                                  args=(ahead,), daemon=True)
        waiter.start()
        waiter.join(0.3)
        assert waiter.is_alive()

        stopped_at = time.monotonic()
        safety.request_abort()
        waiter.join(5)

        assert not waiter.is_alive()
        assert time.monotonic() - stopped_at < 2.0
        assert converted == []
        assert not ahead.holds_dv_slot
        assert ahead.settings.dolby_vision_mode == "0"
