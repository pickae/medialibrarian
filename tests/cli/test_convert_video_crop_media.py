"""Tier D for the black-band measurement: the probe against real ffmpeg output.

Two claims can only be made against real video. The first is the PARSE - what
cropdetect prints, on this build, for a frame with bands around it, which no
hand-written stub can stand in for: the unit tests use one, so this is the single
place the shape of that output is checked against the filter itself.

The second is the reason the module exists. A film does not have one shape, and a
crop measured at any one place cuts the picture somewhere else - so a clip that is
letterboxed for half its length and full-frame for the other half must come back
with NO crop, and the only way to be sure of that is to hand the probe a file that
really changes shape partway through and let it seek around inside it.
"""

import shutil
import subprocess

import pytest

from medialib.cli import convert_video_run as run
from medialib.lib import videocrop

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]

# Flat colour rather than a test pattern: the measurement is about where the
# picture ENDS, so a source whose own content is partly black would be asking the
# filter a different question than the one under test.
FRAME = (320, 240)
PICTURE_HEIGHT = 120
BAND = (FRAME[1] - PICTURE_HEIGHT) // 2

# Long enough that the samples, which stop short of both ends, still land in both
# halves of the shape-changing clip.
SECONDS = 20


def _write(path, argv):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    *argv, "-c:v", "ffv1", str(path)], check=True)
    return str(path)


@pytest.fixture(scope="module")
def letterboxed(tmp_path_factory):
    """One shape all the way through: a 320x120 picture centred in a 320x240
    frame."""
    return _write(tmp_path_factory.mktemp("crop") / "letterboxed.mkv", [
        "-f", "lavfi",
        "-i", "color=c=red:s=%dx%d:r=24:d=%d" % (FRAME[0], PICTURE_HEIGHT,
                                                 SECONDS),
        "-vf", "pad=%d:%d:0:%d" % (FRAME[0], FRAME[1], BAND)])


@pytest.fixture(scope="module")
def changing_shape(tmp_path_factory):
    """The IMAX case in miniature: letterboxed for the first half, opening up to
    the whole frame for the second."""
    half = SECONDS // 2
    return _write(tmp_path_factory.mktemp("crop") / "changing.mkv", [
        "-f", "lavfi",
        "-i", "color=c=red:s=%dx%d:r=24:d=%d" % (FRAME[0], PICTURE_HEIGHT,
                                                 half),
        "-f", "lavfi",
        "-i", "color=c=green:s=%dx%d:r=24:d=%d" % (FRAME[0], FRAME[1], half),
        "-filter_complex",
        "[0:v]pad=%d:%d:0:%d[boxed];[boxed][1:v]concat=n=2:v=1:a=0[out]"
        % (FRAME[0], FRAME[1], BAND),
        "-map", "[out]"])


def _probe(path):
    """The probe wired exactly as the run wires it, so what is exercised is the
    path a conversion takes and not a second arrangement of the same parts."""
    return videocrop.crop_probe(path, run._media_duration, run._dimensions_line,
                                lambda divisor=1: 2)


class TestTheFixturesReallyAreWhatTheyClaim:

    def test_the_letterboxed_clip_is_stored_in_the_whole_frame(self,
                                                               letterboxed):
        assert run._dimensions_line(letterboxed).split()[:2] == [
            str(FRAME[0]), str(FRAME[1])]

    def test_and_is_long_enough_to_sample(self, letterboxed):
        assert run._media_duration(letterboxed) >= SECONDS - 1


class TestOneShapeAllTheWayThrough:

    def test_the_bands_are_measured_off_the_real_filter_output(self,
                                                               letterboxed):
        crop, measured, size = _probe(letterboxed)
        assert size == FRAME
        assert measured == videocrop.CROP_PROBE_SAMPLES
        assert crop == videocrop.Crop(FRAME[0], PICTURE_HEIGHT, 0, BAND)

    def test_and_the_run_encodes_through_that_rectangle(self, letterboxed):
        crop = videocrop.crop_for(letterboxed, "letterboxed.mkv",
                                  run._media_duration, run._dimensions_line,
                                  lambda divisor=1: 2)
        assert crop == "%d:%d:0:%d" % (FRAME[0], PICTURE_HEIGHT, BAND)


class TestAFilmThatChangesShapePartwayThrough:
    """The whole reason the probe seeks all over the file instead of reading the
    head of it."""

    def test_nothing_is_cropped_at_all(self, changing_shape):
        crop, measured, _size = _probe(changing_shape)
        assert measured == videocrop.CROP_PROBE_SAMPLES
        assert crop is None

    def test_and_the_run_says_so_rather_than_cutting_the_wider_scenes(
            self, changing_shape, capsys):
        assert videocrop.crop_for(changing_shape, "changing.mkv",
                                  run._media_duration, run._dimensions_line,
                                  lambda divisor=1: 2) == ""
        assert "no black bands worth removing" in capsys.readouterr().err
