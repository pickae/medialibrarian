"""Tier D for medialib.lib.durationcheck: the check against real files.

tests/lib/test_durationcheck.py settles the judgement with durations handed
straight to it. What only real tools can answer is the measuring - that ffprobe
is asked a question it answers for every container this library writes, and that
two files really are told apart by it. A check that agreed with itself but
misread an .m4a would pass every test there and protect nothing.
"""

import shutil
import subprocess

import pytest

from medialib.lib import durationcheck

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]

SECONDS = 20


def _noise(path, seconds, codec_args=()):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anoisesrc=d=%d:c=pink:r=48000" % seconds,
         "-map", "0:a", "-ac", "2", *codec_args, str(path)],
        check=True, stdin=subprocess.DEVNULL)
    return path


@pytest.fixture(scope="module")
def whole(tmp_path_factory):
    return _noise(tmp_path_factory.mktemp("lengths") / "whole.flac", SECONDS)


class TestMeasuringARealFile:

    @pytest.mark.parametrize("name,codec_args", [
        ("a.flac", []),
        ("a.opus", ["-c:a", "libopus", "-b:a", "48k"]),
        ("a.m4a", ["-c:a", "aac", "-b:a", "64k"]),
        ("a.mp3", ["-c:a", "libmp3lame", "-b:a", "64k"]),
        ("a.mkv", ["-c:a", "libopus", "-b:a", "48k"]),
    ])
    def test_every_container_this_library_writes_answers(self, tmp_path, name,
                                                         codec_args):
        """Each of them, because the figure comes from the CONTAINER: one that
        stated nothing would read as zero, and zero is 'no comparison' - the
        check would go quiet for that format alone."""
        made = _noise(tmp_path / name, SECONDS, codec_args)
        assert durationcheck.media_duration(made) == pytest.approx(SECONDS,
                                                                   abs=0.5)

    def test_a_file_that_is_not_there_measures_as_nothing(self, tmp_path):
        assert durationcheck.media_duration(str(tmp_path / "gone.flac")) == 0.0

    def test_and_so_does_a_file_that_is_not_media(self, tmp_path):
        junk = tmp_path / "notmedia.m4a"
        junk.write_text("this is not an mp4")
        assert durationcheck.media_duration(str(junk)) == 0.0


class TestTellingAWholeConversionFromAShortOne:
    """The one thing the whole safety net rests on: a real encode that stopped
    early is not mistaken for a real encode that did not."""

    @pytest.fixture(autouse=True)
    def measuring(self, monkeypatch, tmp_path):
        monkeypatch.delenv(durationcheck.SKIP_VARIABLE, raising=False)
        durationcheck.init_log(str(tmp_path / "lengths.log"))

    def test_a_complete_encode_passes_and_is_kept(self, tmp_path, whole):
        made = tmp_path / "out.opus"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(whole),
                        "-c:a", "libopus", "-b:a", "48k", str(made)],
                       check=True, stdin=subprocess.DEVNULL)

        assert durationcheck.verify("whole.flac", str(whole), str(made),
                                    log=lambda *m: None)
        assert made.exists()
        assert durationcheck.failures() == []

    def test_an_encode_that_stopped_early_is_caught_and_removed(self, tmp_path,
                                                               whole):
        """`-t 5` is the shape of the real failure: a finished, playable file
        that exits 0 and holds a quarter of the recording."""
        made = tmp_path / "short.opus"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-t", "5",
                        "-i", str(whole), "-c:a", "libopus", "-b:a", "48k",
                        str(made)], check=True, stdin=subprocess.DEVNULL)
        assert durationcheck.media_duration(made) == pytest.approx(5, abs=0.5)

        assert not durationcheck.verify("whole.flac", str(whole), str(made),
                                        log=lambda *m: None)
        assert not made.exists()
        assert [name for name, _s, _o in durationcheck.failures()] \
            == ["whole.flac"]

    def test_a_re_encode_into_another_codec_is_still_the_same_length(
            self, tmp_path, whole):
        """Opus pads to its own frame and MP4 carries an edit list, so the two
        sides of the comparison are never bit-identical figures - the tolerance
        has to cover that without covering a truncation."""
        made = tmp_path / "out.m4a"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(whole),
                        "-c:a", "aac", "-b:a", "64k", str(made)],
                       check=True, stdin=subprocess.DEVNULL)

        assert durationcheck.verify("whole.flac", str(whole), str(made),
                                    log=lambda *m: None)
