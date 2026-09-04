"""Tier D for the ffsubsync interface ingestMovies depends on.

**The subject here is ANOTHER PROJECT'S surface, not this port's.** ingestMovies
does not merely call ffsubsync, it READS it: the sync decides whether a subtitle
is kept or deleted by finding "low-quality alignment" in the log, because
ffsubsync exits 0 whether it applied an alignment or refused one. That makes
four pieces of somebody else's CLI load-bearing, and no stub can vouch for one
of them.

That surface has already moved once - none of the flags below exist before
ffsubsync 0.5.0, which is why the run probes for them at startup - so it will
move again, and a suite that only ever meets a stubbed ffsubsync would not
notice. Both outcomes matter equally: a version that stopped refusing would burn
wrong subtitles into improved copies, and one that started refusing good
alignments would delete every subtitle an ingest downloads.

The fixtures are two real pairings, one of each verdict: a feature film's
subtitles against a commentary from a different, shorter film (the pairing this
whole check was written for), and the right film 0.670 s out of step.
"""

import filecmp
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from medialib.lib import subtitlefiles
from tests import blackbox

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffsubsync") is None,
                       reason="tier D needs the real ffsubsync"),
]

_FIXTURES = blackbox.DATA / "subtitleSync"

FLAGS = ["--skip-sync-on-low-quality", "--quality-max-offset-seconds",
         "--max-offset-seconds", "--log-dir-path"]


def _run_ffs(reference, subtitle, log_dir):
    """One sync with the production flags and thresholds, in place, exactly as
    the download runs it."""
    return subprocess.run(
        ["ffsubsync", str(reference), "-i", str(subtitle), "-o", str(subtitle),
         "--max-offset-seconds", "600", "--skip-sync-on-low-quality",
         "--quality-max-offset-seconds", "60",
         "--log-dir-path", str(log_dir)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)


def _logged(log_dir, record):
    """The last value ffsubsync logged for a record."""
    text = (Path(log_dir) / "ffsubsync.log").read_text()
    found = re.findall(r"^%s: (.*)$" % re.escape(record), text, re.M)
    return found[-1] if found else ""


@pytest.fixture(scope="module")
def refused(tmp_path_factory):
    """The wrong pairing, synced."""
    work = tmp_path_factory.mktemp("bad")
    subtitle = work / "bad.srt"
    shutil.copyfile(_FIXTURES / "mismatched.subtitle.srt", subtitle)
    done = _run_ffs(_FIXTURES / "mismatched.reference.srt", subtitle, work)
    return work, subtitle, done


@pytest.fixture(scope="module")
def applied(tmp_path_factory):
    """The right film, slightly out of step."""
    work = tmp_path_factory.mktemp("good")
    subtitle = work / "good.srt"
    shutil.copyfile(_FIXTURES / "matched.subtitle.srt", subtitle)
    done = _run_ffs(_FIXTURES / "matched.reference.srt", subtitle, work)
    return work, subtitle, done


class TestTheFlagsStillExist:
    @pytest.fixture(scope="class")
    def help_text(self):
        """Captured into a variable rather than piped into a matcher, for the
        reason the startup probe gives: `--help | grep -q` can SIGPIPE it."""
        done = subprocess.run(["ffsubsync", "--help"], capture_output=True,
                              text=True)
        return done.stdout + done.stderr

    @pytest.mark.parametrize("flag", FLAGS)
    def test_help_still_offers(self, flag, help_text):
        assert flag in help_text


class TestAWrongPairingIsRefused:
    def test_it_still_exits_zero(self, refused):
        """An unknown option would have ended in argparse's status 2, which is
        what the startup probe exists to prevent."""
        _work, _subtitle, done = refused
        assert done.returncode == 0, done.stderr

    def test_log_dir_path_still_writes_the_log(self, refused):
        work, _subtitle, _done = refused
        assert (work / "ffsubsync.log").is_file()

    def test_the_rejection_is_one_unwrapped_log_line(self, refused):
        """The exact phrase the sync looks for. If a future version routes the
        file handler through rich as well, the phrase wraps, the match goes
        quiet, and every wrongly-paired subtitle is kept and burned into an
        improved copy."""
        work, _subtitle, _done = refused
        text = (work / "ffsubsync.log").read_text()
        assert len(re.findall(
            r"low-quality alignment \(.*\); leaving subtitles unmodified",
            text)) == 1

    def test_the_subtitle_is_left_byte_identical(self, refused):
        """So a caller that decided to keep it would at least keep something
        coherent."""
        _work, subtitle, _done = refused
        assert filecmp.cmp(_FIXTURES / "mismatched.subtitle.srt", subtitle,
                           shallow=False)

    def test_the_two_gates_that_catch_it(self, refused):
        """Asserted by meaning rather than by the exact numbers - those are the
        alignment algorithm's business and may legitimately change. The best
        offset it could find is far outside what is believable, and the match is
        anti-correlated on top of that."""
        work, _subtitle, _done = refused
        assert abs(float(_logged(work, "offset seconds"))) > 60
        assert float(_logged(work, "score")) < 0

    def test_and_the_reason_names_the_offset_and_the_threshold(self, refused):
        work, _subtitle, _done = refused
        assert re.search(r"low-quality alignment \(.*\|offset\| [0-9.]+s > "
                         r"60\.0s", (work / "ffsubsync.log").read_text())


class TestTheRightFilmSlightlyOutOfStepIsSynced:
    """The happy flow, and the more fragile half of the contract: the same
    thresholds must let a real release difference through, or the ingest would
    delete every subtitle it downloads."""

    def test_it_exits_zero_and_is_not_refused(self, applied):
        work, _subtitle, done = applied
        assert done.returncode == 0, done.stderr
        assert "low-quality alignment" not in (work / "ffsubsync.log").read_text()

    def test_the_offset_found_is_the_real_one(self, applied):
        work, _subtitle, _done = applied
        assert _logged(work, "offset seconds") == "0.670"

    def test_no_framerate_correction_is_invented(self, applied):
        work, _subtitle, _done = applied
        assert _logged(work, "framerate scale factor") == "1.000"

    def test_the_score_is_positive(self, applied):
        work, _subtitle, _done = applied
        assert float(_logged(work, "score")) > 0

    def test_every_timestamp_moved_by_the_reported_offset(self, applied):
        """Bar one: a 13.5 s cue whose END is pulled in to start+10 s, which is
        --max-subtitle-seconds at work - worth pinning too, because it means a
        sync rewrites DURATIONS and not only offsets."""
        _work, subtitle, _done = applied

        def stamps(path):
            found = []
            for line in Path(path).read_text().splitlines():
                if "-->" in line:
                    for half in line.split("-->"):
                        hours, minutes, rest = half.strip().split(":")
                        seconds, millis = rest.split(",")
                        found.append(((int(hours) * 3600 + int(minutes) * 60
                                       + int(seconds)) * 1000) + int(millis))
            return found

        before = stamps(_FIXTURES / "matched.subtitle.srt")
        after = stamps(subtitle)
        assert len(before) == len(after)
        moved = sum(1 for a, b in zip(before, after, strict=True)
                    if b - a == 670)
        assert (moved, len(before) - moved) == (2683, 1)


class TestWhyTheFileLogAndNotStderr:
    """Not a requirement on ffsubsync, but the premise the sync is built on:
    with COLUMNS unset - every non-interactive run - rich wraps the message at
    80 columns and the same match against stderr finds nothing.

    Should this ever start FAILING, ffsubsync stopped wrapping and the note in
    the sync can be revisited; it does not mean anything is broken.
    """

    def test_the_stderr_copy_is_wrapped_hence_unmatchable(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.delenv("COLUMNS", raising=False)
        subtitle = tmp_path / "wrap.srt"
        shutil.copyfile(_FIXTURES / "mismatched.subtitle.srt", subtitle)
        done = subprocess.run(
            ["ffsubsync", str(_FIXTURES / "mismatched.reference.srt"),
             "-i", str(subtitle), "-o", str(subtitle),
             "--max-offset-seconds", "600", "--skip-sync-on-low-quality",
             "--quality-max-offset-seconds", "60"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert not re.search(
            r"low-quality alignment \(.*\); leaving subtitles unmodified",
            done.stdout + done.stderr)


class TestEndToEndThroughTheRealWrapper:
    """The port's own sync_subtitle against the real binary: the two verdicts
    that decide whether a subtitle survives the ingest."""

    def test_it_sees_the_real_refusal(self, tmp_path):
        subtitle = tmp_path / "e2e-bad.srt"
        shutil.copyfile(_FIXTURES / "mismatched.subtitle.srt", subtitle)
        assert subtitlefiles.sync_subtitle(
            str(_FIXTURES / "mismatched.reference.srt"), str(subtitle),
            "600", "60", "yes") == 2

    def test_and_the_real_good_sync(self, tmp_path):
        subtitle = tmp_path / "e2e-good.srt"
        shutil.copyfile(_FIXTURES / "matched.subtitle.srt", subtitle)
        assert subtitlefiles.sync_subtitle(
            str(_FIXTURES / "matched.reference.srt"), str(subtitle),
            "600", "60", "yes") == 0
