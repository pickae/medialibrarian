"""Tier D for the lossless file readLibrary keeps beside the audiobook.

The engine leaves a lossless master of every book behind - the raw synthesised
audio - and exports the audiobook from it through one filter pass: loudness
normalisation and a light denoise. Only the filtered version is ever listened
to, so a "lossless" file that skipped that pass would not be the audiobook's
master but a curiosity that sounds different from every other copy of the book.

None of this can be checked with a stub, because all of it is about the SAMPLES:
whether the filters really ran, and whether the result really is the audiobook's
own shape - its sample rate and channel count read off the audiobook rather than
assumed. So this tier encodes real audio and measures it.
"""

import shutil
import subprocess

import pytest

from medialib.lib import bookpublishing as bp

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *map(str, args)],
                   check=True, stdin=subprocess.DEVNULL)


def _property(path, entry):
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a:0", "-show_entries",
         "stream=" + entry, "-of", "default=nk=1:nw=1", "--", str(path)],
        capture_output=True, text=True)
    return done.stdout.splitlines()[0] if done.stdout.strip() else ""


def _duration(path):
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", "--", str(path)],
        capture_output=True, text=True)
    return float(done.stdout.strip() or 0)


def _mean_volume(path):
    """The mean level in dBFS, which is how "did the filters run" is asked."""
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
         "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    for line in done.stderr.splitlines():
        if "mean_volume" in line:
            return float(line.split()[-2])
    raise AssertionError("ffmpeg reported no mean_volume for %s" % path)


@pytest.fixture
def book(tmp_path):
    """The master as the engine leaves it - the model's own rate, and QUIET,
    which is exactly why the export normalises it - and the audiobook exported
    from it at 44.1 kHz."""
    master = tmp_path / "Some Title.flac"
    _ffmpeg("-f", "lavfi", "-i",
            "sine=frequency=220:duration=20:sample_rate=24000",
            "-af", "volume=0.03", "-ac", "1", "-c:a", "flac", master)
    tagged = tmp_path / "Some Title.m4b"
    _ffmpeg("-i", master, "-af", bp.LOSSLESS_FILTERS, "-ar", "44100",
            "-ac", "1", "-c:a", "aac", "-b:a", "192k", tagged)
    return master, tagged


class TestTheFixturesAreWhatTheyClaim:
    def test_the_master_is_at_the_models_rate_and_the_audiobook_at_the_exports(
            self, book):
        master, tagged = book
        assert _property(master, "sample_rate") == "24000"
        assert _property(tagged, "sample_rate") == "44100"

    def test_the_audiobook_is_much_louder_than_the_raw_master(self, book):
        master, tagged = book
        assert _mean_volume(tagged) - _mean_volume(master) > 10


class TestTheFileThatGetsKept:
    def test_it_is_written_into_the_workspace_not_over_the_master(self, book,
                                                                  tmp_path):
        master, tagged = book
        kept = bp.audiobook_lossless(str(master), str(tagged),
                                     str(tmp_path / "book"))
        assert kept == str(tmp_path / "book" / "lossless" / "Some Title.flac")
        assert (tmp_path / "book" / "lossless" / "Some Title.flac").is_file()

    def test_it_takes_the_audiobooks_shape_and_not_the_masters(self, book,
                                                              tmp_path):
        master, tagged = book
        kept = bp.audiobook_lossless(str(master), str(tagged),
                                     str(tmp_path / "book"))
        assert _property(kept, "codec_name") == "flac"
        assert _property(kept, "sample_rate") == "44100"
        assert _property(kept, "channels") == "1"

    def test_and_the_same_length_as_the_master(self, book, tmp_path):
        """The filters are sample-for-sample, which is what lets the m4b's
        chapter timeline be written into this file unchanged."""
        master, tagged = book
        kept = bp.audiobook_lossless(str(master), str(tagged),
                                     str(tmp_path / "book"))
        assert abs(_duration(master) - _duration(kept)) < 0.2

    def test_it_is_levelled_like_the_audiobook(self, book, tmp_path):
        """The point of the whole step: it sounds like the audiobook, not like
        the raw master it was made from."""
        master, tagged = book
        kept = bp.audiobook_lossless(str(master), str(tagged),
                                     str(tmp_path / "book"))
        assert abs(_mean_volume(kept) - _mean_volume(tagged)) < 1.5


class TestARunThatWantsTheRawModelOutputInstead:
    def test_emptying_the_filters_leaves_it_as_quiet_as_the_master(
            self, book, tmp_path, monkeypatch):
        """Which turns the step into a plain re-encode, so what is kept is what
        the model produced."""
        master, tagged = book
        monkeypatch.setenv("audiobookLosslessFilters", "")
        kept = bp.audiobook_lossless(str(master), str(tagged),
                                     str(tmp_path / "raw"))
        assert abs(_mean_volume(kept) - _mean_volume(master)) < 1.5


class TestAMasterThatIsNotThere:
    def test_it_is_refused(self, book, tmp_path):
        _master, tagged = book
        assert bp.audiobook_lossless(str(tmp_path / "missing.flac"),
                                     str(tagged),
                                     str(tmp_path / "none")) is None
