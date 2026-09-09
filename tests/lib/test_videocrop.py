"""The white box for medialib/lib/videocrop.py.

What one moment of a source is read as (against a stubbed cropdetect), and what is
done WITH the moments: the smallest common band, the symmetry, the rounding and the
floors, the minimum sample count, and the callers' wording.
"""

import os

import pytest

from medialib.lib import videocrop

pytestmark = pytest.mark.stubbed


class _StubFfmpeg:
    """An ffmpeg that writes canned cropdetect lines to stderr, or fails."""

    def __init__(self, workdir):
        self.bin = os.path.join(workdir, "bin")
        os.makedirs(self.bin)
        self.payload = os.path.join(workdir, "payload")
        self.stub = os.path.join(self.bin, "ffmpeg")
        with open(self.stub, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'cat -- "${VCR_PAYLOAD:?}" >&2\n'
                'exit "${VCR_RC:-0}"\n')
        os.chmod(self.stub, 0o755)

    def give(self, text, rc=0):
        with open(self.payload, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.environ["VCR_PAYLOAD"] = self.payload
        os.environ["VCR_RC"] = str(rc)


@pytest.fixture
def ffmpeg(tmp_path, monkeypatch):
    stub = _StubFfmpeg(str(tmp_path))
    monkeypatch.setenv("PATH", stub.bin + os.pathsep + os.environ["PATH"])
    return stub


def detect_line(width, height, x, y, t="1.0"):
    """One of the lines cropdetect prints, as the filter prints it."""
    return ("[Parsed_cropdetect_0 @ 0x0] x1:%d x2:%d y1:%d y2:%d w:%d h:%d "
            "x:%d y:%d pts:1 t:%s limit:0.094118 crop=%d:%d:%d:%d\n"
            % (x, x + width - 1, y, y + height - 1, width, height, x, y, t,
               width, height, x, y))


class TestCrop:

    def test_the_spec_is_what_the_crop_filter_takes(self):
        assert videocrop.Crop(1920, 800, 0, 140).spec == "1920:800:0:140"


class TestCropProbeSample:

    def test_the_last_line_is_the_answer(self, ffmpeg):
        """cropdetect accumulates rather than answering per frame, so what it
        last printed is the smallest crop that fits every frame it was given."""
        ffmpeg.give(detect_line(1920, 800, 0, 140)
                    + detect_line(1920, 900, 0, 90))
        assert videocrop.crop_probe_sample("in.mkv", "0") == videocrop.Crop(
            1920, 900, 0, 90)

    def test_a_moment_with_no_picture_in_it_is_skipped(self, ffmpeg):
        """A fade to black has nothing for cropdetect to keep and it reports a
        negative rectangle - which is a skipped sample, not a reason to crop the
        whole frame away."""
        ffmpeg.give("[Parsed_cropdetect_0 @ 0x0] x1:319 x2:0 y1:239 y2:0 "
                    "w:-318 h:-238 x:320 y:240 pts:1 t:0.2 crop=-318:-238:320:240\n")
        assert videocrop.crop_probe_sample("in.mkv", "0") is None

    def test_a_decode_that_printed_nothing_is_skipped(self, ffmpeg):
        ffmpeg.give("", rc=1)
        assert videocrop.crop_probe_sample("in.mkv", "0") is None

    def test_what_was_printed_stands_even_when_the_decode_ended_badly(self,
                                                                     ffmpeg):
        """A seek that ran off the end still measured the frames it reached."""
        ffmpeg.give(detect_line(1920, 800, 0, 140), rc=1)
        assert videocrop.crop_probe_sample("in.mkv", "0") == videocrop.Crop(
            1920, 800, 0, 140)

    def test_only_the_filters_own_lines_are_read(self, ffmpeg):
        """At this log level ffmpeg names the input as well, and a file may be
        called anything at all."""
        ffmpeg.give("[in#0 @ 0x0] Opening 'crop=1:2:3:4.mkv' for reading\n"
                    + detect_line(1920, 800, 0, 140))
        assert videocrop.crop_probe_sample("in.mkv", "0") == videocrop.Crop(
            1920, 800, 0, 140)

    def test_no_ffmpeg_at_all_is_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        assert videocrop.crop_probe_sample("in.mkv", "0") is None


class TestCommonCrop:
    """The whole point of the module: only the band EVERY sampled moment agreed
    on comes off."""

    def test_one_letterboxed_shape_crops_to_it(self):
        rects = [videocrop.Crop(1920, 800, 0, 140)] * 4
        assert videocrop.common_crop(rects, 1920, 1080) == videocrop.Crop(
            1920, 800, 0, 140)

    def test_the_widest_moment_holds_the_crop_back(self):
        """An IMAX sequence in a scope feature: the film opens up to the full
        frame for it, so the scope scenes keep their bands rather than the IMAX
        ones losing their top and bottom."""
        rects = [videocrop.Crop(1920, 800, 0, 140)] * 8 + [
            videocrop.Crop(1920, 1080, 0, 0)]
        assert videocrop.common_crop(rects, 1920, 1080) is None

    def test_a_partly_taller_sequence_crops_to_the_taller_shape(self):
        """Two shapes and neither fills the frame: what comes off is the band
        the taller of them still has."""
        rects = [videocrop.Crop(1920, 800, 0, 140)] * 8 + [
            videocrop.Crop(1920, 1000, 0, 40)]
        assert videocrop.common_crop(rects, 1920, 1080) == videocrop.Crop(
            1920, 1000, 0, 40)

    def test_an_off_centre_measurement_is_levelled_to_the_smaller_band(self):
        """More off one side than the other would re-centre the picture, and
        "this master is off-centre" is not a claim a measurement can make."""
        rects = [videocrop.Crop(1920, 900, 0, 40)]
        assert videocrop.common_crop(rects, 1920, 1080) == videocrop.Crop(
            1920, 1000, 0, 40)

    def test_bands_on_all_four_sides_come_off_as_two_pairs(self):
        rects = [videocrop.Crop(1400, 800, 260, 140)]
        assert videocrop.common_crop(rects, 1920, 1080) == videocrop.Crop(
            1400, 800, 260, 140)

    def test_pillars_alone_leave_the_height_whole(self):
        rects = [videocrop.Crop(1440, 1080, 240, 0)]
        assert videocrop.common_crop(rects, 1920, 1080) == videocrop.Crop(
            1440, 1080, 240, 0)

    @pytest.mark.parametrize("band", [1, 2, 6, videocrop.CROP_PROBE_MIN_BAND - 1])
    def test_a_band_too_thin_to_be_letterboxing_is_left_on(self, band):
        """The edge of the picture rather than a band around it - a mastering
        artefact, a dead column, a codec's ringing."""
        rects = [videocrop.Crop(1920, 1080 - 2 * band, 0, band)]
        assert videocrop.common_crop(rects, 1920, 1080) is None

    def test_a_band_is_rounded_down_to_an_even_number_of_lines(self):
        """Down, because rounding up would cut a line of picture; even, because
        the 10-bit 4:2:0 pixel formats reject an odd side."""
        rects = [videocrop.Crop(1920, 798, 0, 141)]
        assert videocrop.common_crop(rects, 1920, 1080) == videocrop.Crop(
            1920, 800, 0, 140)

    def test_no_samples_at_all_is_no_crop(self):
        assert videocrop.common_crop([], 1920, 1080) is None

    def test_a_rectangle_reaching_outside_the_frame_reads_as_no_band(self):
        """A sample of a stream whose size changed mid-file is not a measurement
        of this frame."""
        rects = [videocrop.Crop(3840, 2160, 0, 0)]
        assert videocrop.common_crop(rects, 1920, 1080) is None

    def test_a_crop_that_would_leave_no_picture_at_all_is_refused(self):
        """Unreachable from a measured rectangle, which always leaves the picture
        it kept - and the one thing that must not reach an ffmpeg command line."""
        rects = [videocrop.Crop(1920, 0, 0, 540)]
        assert videocrop.common_crop(rects, 1920, 1080) is None


class _Probes:
    """The caller's own probes, as videocrop takes them."""

    def __init__(self, duration=7200.0, size="1920 1080"):
        self.duration, self.size = duration, size
        self.asked = []

    def media_duration(self, path):
        return self.duration

    def dimensions(self, path):
        return self.size

    def jobs_per_core(self, divisor=1):
        return 4


class TestCropProbe:

    def test_the_moments_are_spread_across_the_whole_running_time(self,
                                                                  monkeypatch):
        seen = []
        monkeypatch.setattr(videocrop, "crop_probe_sample",
                            lambda path, t, accel="": seen.append(t)
                            or videocrop.Crop(1920, 800, 0, 140))
        probes = _Probes()
        videocrop.crop_probe("in.mkv", probes.media_duration, probes.dimensions,
                             probes.jobs_per_core)
        times = sorted(float(value) for value in seen)
        assert len(times) == videocrop.CROP_PROBE_SAMPLES
        # Short of both ends, where a film keeps its logos, fades and credits.
        assert times[0] == pytest.approx(7200 * 0.04)
        assert times[-1] == pytest.approx(7200 * 0.96)

    def test_too_few_readable_moments_is_no_crop(self, monkeypatch):
        """A file whose moments mostly could not be read has not been measured,
        and a crop from the handful that did is a guess at the shape of a film."""
        answers = ([videocrop.Crop(1920, 800, 0, 140)]
                   * (videocrop.CROP_PROBE_MIN_SAMPLES - 1))
        monkeypatch.setattr(videocrop, "crop_probe_sample",
                            lambda path, t, accel="":
                            answers.pop() if answers else None)
        probes = _Probes()
        crop, measured, size = videocrop.crop_probe(
            "in.mkv", probes.media_duration, probes.dimensions,
            probes.jobs_per_core)
        assert crop is None
        assert measured == videocrop.CROP_PROBE_MIN_SAMPLES - 1
        assert size == (1920, 1080)

    def test_a_worker_that_dies_is_a_skipped_moment(self, monkeypatch):
        def explode(path, t, accel=""):
            if float(t) > 3600:
                raise RuntimeError("decode fell over")
            return videocrop.Crop(1920, 800, 0, 140)

        monkeypatch.setattr(videocrop, "crop_probe_sample", explode)
        probes = _Probes()
        crop, measured, _size = videocrop.crop_probe(
            "in.mkv", probes.media_duration, probes.dimensions,
            probes.jobs_per_core)
        assert crop == videocrop.Crop(1920, 800, 0, 140)
        assert 0 < measured < videocrop.CROP_PROBE_SAMPLES

    @pytest.mark.parametrize("duration,size", [
        (0, "1920 1080"), (7200.0, "0 0"), (7200.0, ""), (7200.0, "1920"),
    ])
    def test_a_source_with_nothing_to_measure_is_not_measured(self, duration,
                                                              size,
                                                              monkeypatch):
        monkeypatch.setattr(videocrop, "crop_probe_sample",
                            lambda *a, **k: pytest.fail("nothing to sample"))
        probes = _Probes(duration=duration, size=size)
        crop, measured, _size = videocrop.crop_probe(
            "in.mkv", probes.media_duration, probes.dimensions,
            probes.jobs_per_core)
        assert crop is None and measured == 0


class TestCropFor:
    """The line the run reads: a crop is the one decision here that cannot be
    seen in the output afterwards."""

    def _run(self, monkeypatch, capsys, answer, **kwargs):
        monkeypatch.setattr(videocrop, "crop_probe_sample",
                            lambda path, t, accel="": answer)
        probes = _Probes(**kwargs)
        got = videocrop.crop_for("in.mkv", "A Film.mkv", probes.media_duration,
                                 probes.dimensions, probes.jobs_per_core)
        return got, capsys.readouterr().err

    def test_a_crop_reports_both_shapes_and_both_bands(self, monkeypatch,
                                                       capsys):
        got, said = self._run(monkeypatch, capsys,
                              videocrop.Crop(1920, 800, 0, 140))
        assert got == "1920:800:0:140"
        assert "1920x1080 (1.78:1) -> 1920x800 (2.39:1)" in said
        assert "140 line(s) off the top and the bottom" in said
        assert "0 column(s) off each side" in said
        assert "A Film.mkv" in said

    def test_a_frame_with_no_bands_says_so_and_crops_nothing(self, monkeypatch,
                                                             capsys):
        got, said = self._run(monkeypatch, capsys,
                              videocrop.Crop(1920, 1080, 0, 0))
        assert got == ""
        assert "no black bands worth removing" in said
        assert "the whole 1920x1080 frame (1.78:1)" in said

    def test_an_unmeasurable_source_says_so_and_crops_nothing(self,
                                                              monkeypatch,
                                                              capsys):
        got, said = self._run(monkeypatch, capsys, None)
        assert got == ""
        assert "too few to say what shape this film is" in said
        assert "0 of %d moments" % videocrop.CROP_PROBE_SAMPLES in said
