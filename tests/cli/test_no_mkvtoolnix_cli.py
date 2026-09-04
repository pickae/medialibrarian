"""The audio commands when mkvtoolnix is absent.

`mkvmerge` and its siblings are not requirements of `convert-audio`,
`concat-audio` or `convert-and-concat`. A run can do everything but a few things
without them - the MP3/m4b chapter-and-title detour, which routes those two
formats through Matroska, the opus-source cover extraction, and pulling cover art
out of a Matroska source - so their absence is a startup warning rather than a
refusal.

The state is settled once at startup and travels in the environment, which is what
makes both halves of this testable on any host:

  * the GUARDS are asserted with the state forced, so they behave exactly as if
    the tools were gone whatever the host has installed - the MP3/m4b step skips
    with a visible line instead of killing the worker, and `mkvmerge` is never
    executed;
  * the SETTLEMENT is asserted on a PATH that genuinely cannot find any of the
    trio, which is what the `sandbox` fixture's `narrow()` is for. The warning has
    to be said exactly once per run, however many commands that run reaches.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.stubbed

_TRIO = ("mkvmerge", "mkvpropedit", "mkvextract")

# Appends its argv to a log as well as doing what the stub does, so a call that
# must NOT happen is asserted from the record rather than from the output it fakes.
_RECORDING_MKVMERGE = r"""
printf '%s\0' "$@" >> "${MKV_LOG:-/dev/null}"
prev=""; for a in "$@"; do [[ "$prev" == "-o" ]] && : > "$a"; prev="$a"; done
"""

# The ordinary tools these commands reach for. A miss fails loudly rather than
# quietly narrowing what the run can do: on a narrow PATH a missing `mktemp` is an
# unhandled error in code no case here is about.
_ORDINARY = ("bash", "env", "xargs", "find", "sort", "sed", "awk", "grep", "head",
             "tail", "wc", "cat", "cut", "tr", "uniq", "mktemp", "stat", "touch",
             "date", "basename", "dirname", "realpath", "mkdir", "rmdir", "rm",
             "mv", "cp", "ln", "jq", "rsync", "md5sum", "flock", "nproc")


def _album(root, *names):
    album = root / "Album"
    album.mkdir(parents=True)
    for name in names:
        (album / name).touch()
    return root


@pytest.fixture
def forced(sandbox, tmp_path):
    """The tools present but the state saying otherwise, so only the guards run.

    Pre-setting it means the commands neither settle nor warn: that is the
    settlement's job and is asserted separately below.
    """
    sandbox.with_media_stubs()
    sandbox.with_tool("mkvmerge", _RECORDING_MKVMERGE)
    calls = tmp_path / "mkvmerge.calls"
    calls.write_text("")

    def run(command, *args, expect=0):
        done = sandbox.run(command, *args,
                           env=dict(os.environ, HAVE_MKVTOOLNIX="",
                                    MKV_LOG=str(calls)))
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.calls = calls
    sandbox.forced = run
    return sandbox


class TestTheGuardsWithTheStateForced:
    """What each guard does when the answer is "no tools", whatever the host
    actually has."""

    def test_an_mp3_run_loses_its_chapters_visibly_and_not_fatally(self, forced,
                                                                  tmp_path):
        """The skip is a line the worker prints, not a death under `set -e`."""
        source = _album(tmp_path / "mp3in", "01 - first.mp3", "02 - second.mp3")
        outputs = tmp_path / "mp3out"
        log = forced.forced("concat-audio", "-v", source, outputs)
        assert (outputs / "Album.mp3").is_file()
        assert log.count("chapters and title not embedded") == 1, log
        assert forced.calls.read_text() == "", "mkvmerge was executed"

    def test_a_forced_state_does_not_also_warn_at_startup(self, forced,
                                                          tmp_path):
        """The warning belongs to the settlement, not to the guard."""
        source = _album(tmp_path / "mp3in2", "01 - first.mp3", "02 - second.mp3")
        log = forced.forced("concat-audio", "-v", source, tmp_path / "mp3out2")
        assert "mkvtoolnix not found" not in log, log

    def test_an_opus_run_is_untouched(self, forced, tmp_path):
        """Its chapters go through mutagen, which never sees mkvtoolnix, so the
        run is exactly as it was - output included. That the chapters really go
        out that way is `tests/lib/test_chapters.py`'s: the write is a function
        call, not a process this tier can put a recorder in front of."""
        source = _album(tmp_path / "opusin", "01 - first.opus",
                        "02 - second.opus")
        outputs = tmp_path / "opusout"
        forced.forced("concat-audio", source, outputs)
        assert (outputs / "Album.opus").is_file()
        assert forced.calls.read_text() == "", "mkvmerge was executed"


@pytest.fixture
def absent(sandbox, tmp_path):
    """A PATH that finds no mkvtool at all, and every ordinary tool the commands
    need.

    Built as an allow-list rather than by dropping directories from the host's
    PATH: on this host mkvtoolnix lives in the same directory as the coreutils, so
    dropping it would take those with it.

    The suite-wide preflight switch is off for these runs, because it would
    silence the very warning under test - the stubs satisfy the preflight the same
    way they satisfy the runs, and the trio is not among what it asks for.
    """
    sandbox.with_media_stubs(exclude=_TRIO)
    sandbox.linking(*_ORDINARY).narrow()

    def run(command, *args, expect=0):
        done = sandbox.run(command, *args,
                           env=dict(os.environ, SKIP_TOOL_PREFLIGHT=""))
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.absent = run
    return sandbox


class TestTheSettlementOnAPathWithoutThem:
    def test_the_path_really_finds_no_mkvtool(self, absent):
        """The scenario before the claims that rest on it."""
        import shutil
        for tool in _TRIO:
            assert shutil.which(tool, path=absent.path) is None, tool

    def test_concat_audio_warns_once_and_names_the_chapter_loss(self, absent,
                                                               tmp_path):
        source = _album(tmp_path / "sin", "01 - first.mp3", "02 - second.mp3")
        outputs = tmp_path / "sout"
        log = absent.absent("concat-audio", "-v", source, outputs)
        assert (outputs / "Album.mp3").is_file()
        assert log.count("mkvtoolnix not found") == 1, log
        assert log.count("chapters and titles cannot be embedded") == 1, log
        assert log.count("chapters and title not embedded") == 1, log

    def test_convert_audio_warns_once_and_the_run_goes_on(self, absent,
                                                          tmp_path):
        """`-c`, because without it a source the encoder would not improve is
        deliberately left where it is and there is no output to count - and with
        it the mp3 is carried over verbatim, which is what the wrapper's
        transcoding phase does."""
        source = tmp_path / "cin"
        source.mkdir()
        (source / "01 - first.mp3").touch()
        (source / "02 - second.mp3").touch()
        outputs = tmp_path / "cout"
        log = absent.absent("convert-audio", "-c", source, outputs)
        assert len(list(outputs.rglob("*.mp3"))) == 2
        assert log.count("mkvtoolnix not found") == 1, log
        assert "sidecar images" in log, log

    def test_the_wrapper_warns_once_for_the_whole_run(self, absent, tmp_path):
        """Not once per command it drives. Its own settlement runs first and sets
        the state the commands it starts inherit, so those stay quiet."""
        source = tmp_path / "win"
        book = source / "Book"
        book.mkdir(parents=True)
        (book / "01 - first.mp3").touch()
        (book / "02 - second.mp3").touch()
        outputs = tmp_path / "wout"
        log = absent.absent("convert-and-concat", source, outputs)
        # The wrapper always passes -c, so the mp3 is carried over verbatim and
        # the finished book keeps its source extension.
        assert (outputs / "Book.mp3").is_file()
        assert log.count("mkvtoolnix not found") == 1, log
        assert log.count(
            "The encoding and concatenation themselves are unaffected") == 1, log
