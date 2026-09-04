"""The white box for medialib/lib/videograin.py.

The profile table, which blocks of a frame get a vote (the floors and the
quantile, against a block map the stubbed decode hands over), and what is done
WITH the samples - the median, the map onto the 0-50 scale, the rounding and the
clamps, and the callers' wording.
"""

import array
import os

import pytest

from medialib.lib import videograin

pytestmark = pytest.mark.stubbed

# The block map the stubbed decode hands the sample: GRAIN_PROBE_FRAMES frames is
# what the real decode would give, but one frame already holds 400 blocks at the
# contract's 20x20 grid, and the floors drop the rest anyway - one frame is the
# contract's own shape.
COLS, ROWS = 20, 20
BLOCKS = COLS * ROWS


def block_map(luma, residuals, frames=videograin.GRAIN_PROBE_FRAMES):
    """The rawvideo the probe expects: per frame, the luma block averages then
    the residual block averages, 16-bit little-endian."""
    assert BLOCKS % len(residuals) == 0
    per_frame = [luma] * BLOCKS + list(residuals) * (BLOCKS // len(residuals))
    values = array.array("H")
    for _ in range(frames):
        values.extend(per_frame)
    return values.tobytes()


class _StubFfmpeg:
    """An ffmpeg that hands the probe a canned block map, or fails."""

    def __init__(self, workdir):
        self.bin = os.path.join(workdir, "bin")
        os.makedirs(self.bin)
        self.payload = os.path.join(workdir, "payload")
        self.stub = os.path.join(self.bin, "ffmpeg")
        with open(self.stub, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'cat -- "${VGR_PAYLOAD:?}"\n'
                'exit "${VGR_RC:-0}"\n')
        os.chmod(self.stub, 0o755)

    def give(self, values, rc=0):
        with open(self.payload, "wb") as handle:
            handle.write(values)
        os.environ["VGR_PAYLOAD"] = self.payload
        os.environ["VGR_RC"] = str(rc)


@pytest.fixture
def ffmpeg(tmp_path, monkeypatch):
    stub = _StubFfmpeg(str(tmp_path))
    monkeypatch.setenv("PATH", stub.bin + os.pathsep + os.environ["PATH"])
    return stub


def sample(cols=COLS, rows=ROWS):
    # The block map is whatever the stubbed ffmpeg was last told to hand over.
    return videograin.grain_probe_sample("in.mkv", "0", cols, rows, "")


class TestGrainDefaultFor:
    @pytest.mark.parametrize("profile,expected", [
        ("av1Grain", "probe"),
        ("av1BluRay", "probe"),
        ("av1ConstrainedGood", "probe"),
        ("av1ConstrainedBad", "probe"),
        ("av1ConstrainedBluRay", "probe"),
        ("av1Animation", "0"),
        ("hevcNvenc", "0"),
        ("notAProfile", "0"),
    ])
    def test_table(self, profile, expected):
        assert videograin.grain_default_for(profile) == expected


class TestGrainProbeSample:
    def test_uniform_residual_scales_to_eight_bit_sigma(self, ffmpeg):
        # 257 sixteen-bit units is one 8-bit unit: a residual of 257 everywhere
        # is a mean absolute deviation of 1.0 and a sigma of 0.886227.
        ffmpeg.give(block_map(20000, [257]))
        assert sample() == "0.886227"

    def test_twice_the_residual_is_twice_the_sigma(self, ffmpeg):
        ffmpeg.give(block_map(20000, [514]))
        assert sample() == "1.772454"

    def test_the_calm_tenth_decides_not_the_busy_rest(self, ffmpeg):
        # One calm block against a busy nine: the tenth percentile is the calm
        # one, so the loud majority does not drag the reading up with it.
        ffmpeg.give(block_map(20000, [257, 9000, 9000, 9000, 9000, 9000,
                                      9000, 9000, 9000, 9000]))
        assert sample() == "0.886227"

    def test_a_frame_with_no_picture_offers_nothing(self, ffmpeg):
        # Letterbox bars and crushed blacks are exactly zero, and would
        # otherwise BE the tenth percentile.
        ffmpeg.give(block_map(0, [257]))
        assert sample() == ""

    def test_frozen_blocks_are_dropped_not_grainless(self, ffmpeg):
        # Blocks the source froze rather than coded measure no residual.
        ffmpeg.give(block_map(20000, [0]))
        assert sample() == ""

    def test_too_few_usable_blocks_is_a_skip(self, ffmpeg):
        # 201 of the 400 blocks hold no picture: 199 usable in the one frame
        # decoded, under the 200 the sample needs to have an opinion.
        residuals = [0] * 201 + [257] * 199
        ffmpeg.give(block_map(20000, residuals, frames=1))
        assert sample() == ""

    def test_what_the_decode_printed_stands_even_if_it_failed(self, ffmpeg):
        # The decode's status is not consulted, so what left it is graded even
        # when the seek ended badly.
        ffmpeg.give(block_map(20000, [257]), rc=1)
        assert sample() == "0.886227"

    def test_a_decode_that_prints_nothing_is_a_skip(self, ffmpeg):
        # A failed decode that printed nothing gives the grader an empty input,
        # which is the skipped sample the callers want.
        ffmpeg.give(b"", rc=1)
        assert sample() == ""


def probe_with(sigmas, dims="1920 1080 progressive 1:1", duration="600",
               workers=1):
    """grainProbeLevel with the sample stubbed to these per-sample sigmas, in
    the order the sample times come out."""
    import itertools

    counter = itertools.count()
    canned = list(sigmas)

    def fake(input, t, cols, rows, decode_accel_args=""):
        i = next(counter)
        return canned[i] if i < len(canned) else ""

    real = videograin.grain_probe_sample
    try:
        videograin.grain_probe_sample = fake
        return videograin.grain_probe_level(
            "in.mkv",
            media_duration=lambda i: duration,
            video_dimensions=lambda i: dims,
            jobs_per_core=lambda cores: workers,
            samples=len(canned) if canned else 3)
    finally:
        videograin.grain_probe_sample = real


class TestGrainProbeLevel:
    def test_mid_of_scale(self):
        # grainProbeMid is the middle of the scale by definition.
        assert probe_with(["0.34", "0.34", "0.34"]) == "20 0.3400"

    def test_doubling_adds_the_slope(self):
        assert probe_with(["0.68", "0.68", "0.68"]) == "29 0.6800"

    def test_halving_drops_the_slope(self):
        assert probe_with(["0.17", "0.17", "0.17"]) == "11 0.1700"

    def test_not_capped_at_the_default(self):
        assert probe_with(["1.36", "1.36", "1.36"]) == "38 1.3600"

    def test_the_median_decides(self):
        # One outlier cannot speak for the file.
        assert probe_with(["9.0", "0.34", "0.30"]) == "20 0.3400"

    def test_an_even_count_averages_the_middle_pair(self):
        assert probe_with(["0.30", "0.34"]) == "20 0.3200"

    def test_rounded_up_not_to_nearest(self):
        assert probe_with(["0.35", "0.35", "0.35"]) == "21 0.3500"

    def test_below_the_bottom_of_the_scale(self):
        assert probe_with(["0.001", "0.001", "0.001"]) == "0 0.0010"

    def test_past_the_top_of_the_scale(self):
        assert probe_with(["40", "40", "40"]) == "50 40.0000"

    def test_an_unmeasurable_sample_leaves_the_others(self):
        assert probe_with(["", "0.34", "0.34"]) == "20 0.3400"

    def test_nothing_measurable_reports_zero_and_no_sigma(self):
        assert probe_with(["", "", ""]) == "0"

    def test_a_frame_too_small_for_a_grid(self):
        assert probe_with(["0.34", "0.34", "0.34"],
                          dims="48 32 progressive 1:1") == "0"

    def test_no_duration_to_spread_over(self):
        assert probe_with(["0.34", "0.34", "0.34"], duration="") == "0"

    def test_the_sample_times_stop_short_of_both_ends(self):
        # The first and last sample sit at 4% and 96% of the duration, to
        # three decimals - the layout the encode's seeks are built from.
        seen = []

        def fake(input, t, cols, rows, decode_accel_args=""):
            seen.append(t)
            return "0.34"

        real = videograin.grain_probe_sample
        try:
            videograin.grain_probe_sample = fake
            videograin.grain_probe_level(
                "in.mkv",
                media_duration=lambda i: "600",
                video_dimensions=lambda i: "1920 1080 progressive 1:1",
                jobs_per_core=lambda cores: 1,
                samples=40)
        finally:
            videograin.grain_probe_sample = real
        assert len(seen) == 40
        assert seen[0] == "24.000"
        assert seen[-1] == "576.000"
        assert seen == sorted(seen, key=float)


def _wrapper(fn, line, label=None):
    import contextlib
    import io

    real = videograin.grain_probe_level
    captured = io.StringIO()
    try:
        videograin.grain_probe_level = lambda *a, **k: line
        with contextlib.redirect_stderr(captured):
            level = fn("in.mkv", label, None, None, None)
    finally:
        videograin.grain_probe_level = real
    return level, captured.getvalue()


class TestCallerWording:
    def test_grain_level_for_measured(self):
        level, log = _wrapper(videograin.grain_level_for,
                              "20 0.3400", "the label")
        assert level == "20"
        assert log == ("Film grain: source measured 0.3400 sigma -> "
                       "synthesising 20: the label\n")

    def test_grain_level_for_unmeasurable(self):
        level, log = _wrapper(videograin.grain_level_for, "0", "the label")
        assert level == "0"
        assert log == ("Film grain: could not measure the source, "
                       "synthesising none: the label\n")

    def test_source_grain_for_measured(self):
        level, log = _wrapper(videograin.source_grain_for,
                              "20 0.3400", "the label")
        assert level == "20"
        assert log == ("Bitrate test: source grain measured 0.3400 sigma "
                       "-> level 20: the label\n")

    def test_source_grain_for_unmeasurable_judges_clean(self):
        level, log = _wrapper(videograin.source_grain_for, "0", "the label")
        assert level == "0"
        assert log == ("Bitrate test: could not measure the source grain, "
                       "judging it as clean: the label\n")

    def test_an_empty_label_falls_back_to_the_input(self):
        level, log = _wrapper(videograin.grain_level_for,
                              "20 0.3400", None)
        assert log.endswith(": in.mkv\n")