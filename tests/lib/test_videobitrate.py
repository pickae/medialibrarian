"""The white box for medialib/lib/videobitrate.py.

What is pinned here is the model
that answers "is this video worth re-encoding at all", which convert-video's
-t asks per file.

The model is one anchor (5000 kbit/s of H.264 at 1920x1080, up to 30 fps,
clean) scaled along five axes, and the axes are the whole point: each is a
TREND that has to keep pointing the right way, and a table of expected numbers
would only restate the arithmetic. So what is asserted here is the shape of
the model -
  * the anchor itself, which every other figure is a multiple of;
  * resolution scaling that is SUB-linear in the pixel count, and more so the
    newer the codec is (which is what makes a 4K source the one worth
    re-encoding);
  * codec efficiency that grows with the frame size for the same reason;
  * aspect ratio, which is linear WITHIN a tier - a scope frame is a 2160p
    picture that simply has fewer pixels to pay for;
  * frame rate, where twice the frames cost at most half as much again;
  * grain, the axis that inverts the others: at full heaviness the codecs
    converge and the resolution curve straightens, unless the output
    synthesises the grain instead of coding it - and, since it is the one axis
    that has to be MEASURED, whether a run pays for that measurement at all
    (a conversion does, a census does not);
  * the hardware encode penalty, which applies to an output and never to a
    source;
- plus the verdicts (starved / adequate / generous) and the measurement that
feeds them, ``video_bitrate_stats``, whose job is to find the VIDEO stream's
own bitrate in a container that may state it per stream, as an mkvmerge tag,
or not at all.

The measurement runs against canned ffprobe JSON through a stubbed ffprobe,
so no media is needed.
"""

import inspect
import json
import os

import pytest

from medialib.lib import videobitrate

pytestmark = pytest.mark.stubbed

# The model, with the defaults the assertions below share - 24 fps, no grain,
# coded rather than synthesised, and a software encoder - so each case names
# only the axis it is about.
def adequate(codec, width, height, fps="24", grain="0", synth="0", hw="0"):
    return videobitrate.adequate_video_bitrate(codec, width, height, fps, grain,
                                               synth, hw)


def ratio(a, b):
    return float(a) / float(b)


# --- the anchor ----------------------------------------------------------------
# Everything else in the model is this number scaled, so it is the one figure
# worth pinning exactly: 5 Mbit/s of H.264 at 1080p is the top of the
# streaming services' own 1080p ladders, i.e. where a careful viewer stops
# noticing the encode.

class TestAnchor:
    def test_the_anchor_is_5000_kbit(self):
        assert adequate("h264", "1920", "1080") == "5000"

    def test_24_and_30_fps_are_the_anchor_own_case(self):
        assert adequate("h264", "1920", "1080", "24") == \
            adequate("h264", "1920", "1080", "30")

    def test_anything_below_is_too(self):
        # 25 fps PAL is not cheaper.
        assert adequate("h264", "1920", "1080", "24") == \
            adequate("h264", "1920", "1080", "25")


# --- resolution: sub-linear in the pixel count ----------------------------------
# Four times the pixels is not four times the DETAIL, and a modern codec's
# larger blocks suit a big frame better than a small one, so 2160p costs about
# 3x 1080p in H.264 rather than 4x - and the same curve runs downwards, so
# 720p costs MORE than its 44% of the pixels would say.

class TestResolution:
    def test_2160p_costs_about_3x_1080p(self):
        assert ratio(adequate("h264", "3840", "2160"),
                     adequate("h264", "1920", "1080")) == pytest.approx(3, abs=0.15)

    def test_and_that_is_under_the_pixel_counts_own_4x(self):
        assert float(adequate("h264", "3840", "2160")) < \
            float(adequate("h264", "1920", "1080")) * 4

    def test_720p_costs_more_than_its_44_percent_of_the_pixels(self):
        assert ratio(adequate("h264", "1280", "720"),
                     adequate("h264", "1920", "1080")) == pytest.approx(0.52, abs=0.05)

    def test_an_sd_frame_is_cheaper_than_a_720p_one(self):
        assert float(adequate("h264", "720", "576")) < \
            float(adequate("h264", "1280", "720"))


# --- codec efficiency, and how it GROWS with the frame --------------------------

class TestCodec:
    def test_hevc_needs_less_than_h264_at_1080p(self):
        assert float(adequate("hevc", "1920", "1080")) < \
            float(adequate("h264", "1920", "1080"))

    def test_and_av1_less_than_hevc(self):
        assert float(adequate("av1", "1920", "1080")) < \
            float(adequate("hevc", "1920", "1080"))

    def test_while_mpeg2_needs_far_more_than_h264(self):
        assert float(adequate("h264", "1920", "1080")) < \
            float(adequate("mpeg2", "1920", "1080"))

    def test_mpeg2_needs_twice_what_h264_would(self):
        assert ratio(adequate("mpeg2", "1920", "1080"),
                     adequate("h264", "1920", "1080")) == pytest.approx(2, abs=0.01)

    def test_and_mpeg4_asp_half_again(self):
        assert ratio(adequate("mpeg4", "1920", "1080"),
                     adequate("h264", "1920", "1080")) == pytest.approx(1.5, abs=0.01)

    # The pre-H.264 eras are judged as H.264 and then charged one flat penalty,
    # so the ratio is the SAME whatever the frame size and grain do to the
    # figure - which is the whole point of not giving those eras curves of
    # their own.
    def test_the_mpeg2_penalty_is_the_same_at_2160p(self):
        assert ratio(adequate("mpeg2", "3840", "2160"),
                     adequate("h264", "3840", "2160")) == pytest.approx(2, abs=0.01)

    def test_and_the_same_again_on_a_very_grainy_source(self):
        assert ratio(adequate("mpeg2", "1920", "1080", "24", "45"),
                     adequate("h264", "1920", "1080", "24", "45")) == \
            pytest.approx(2, abs=0.01)

    def test_the_mpeg4_penalty_does_not_drift_with_the_frame_size(self):
        assert ratio(adequate("mpeg4", "3840", "2160"),
                     adequate("h264", "3840", "2160")) == pytest.approx(1.5, abs=0.01)

    def test_mpeg4_asp_is_still_the_cheaper_of_the_two_eras(self):
        assert float(adequate("mpeg4", "1920", "1080")) < \
            float(adequate("mpeg2", "1920", "1080"))

    def test_an_intra_only_master_is_charged_the_mpeg2_eras_penalty(self):
        assert adequate("mpeg2", "1920", "1080") == adequate("prores", "1920", "1080")

    # The gain growing with resolution is the reason a 4K source is the one
    # worth re-encoding, so it is asserted as a comparison of the two ratios
    # rather than as either one on its own.
    def test_av1s_advantage_is_bigger_at_2160p_than_at_1080p(self):
        at_1080 = ratio(adequate("av1", "1920", "1080"), adequate("h264", "1920", "1080"))
        at_2160 = ratio(adequate("av1", "3840", "2160"), adequate("h264", "3840", "2160"))
        assert at_2160 < at_1080

    def test_an_hevc_4k_master_needs_about_11_12_mbit(self):
        assert float(adequate("hevc", "3840", "2160")) == \
            pytest.approx(11500, abs=1000)

    def test_and_an_av1_one_about_8(self):
        assert float(adequate("av1", "3840", "2160")) == pytest.approx(8000, abs=1000)


# --- aspect ratio: linear within the tier ---------------------------------------
# A 2.39:1 scope frame is a 2160p picture - it gets that tier's sub-linear
# scaling - but it has only 1600 of the tier's 2160 lines to spend bits on,
# in exact proportion.

class TestAspect:
    def test_a_scope_4k_frame_costs_its_share_of_a_16_9_one(self):
        assert ratio(adequate("h264", "3840", "1600"),
                     adequate("h264", "3840", "2160")) == \
            pytest.approx(1600 / 2160, abs=0.02)

    def test_and_so_does_a_4_3_4k_frame(self):
        assert ratio(adequate("h264", "2880", "2160"),
                     adequate("h264", "3840", "2160")) == \
            pytest.approx(2880 / 3840, abs=0.02)


# --- frame rate ------------------------------------------------------------------

class TestFrameRate:
    def test_doubling_the_frame_rate_costs_half_as_much_again(self):
        assert ratio(adequate("h264", "1920", "1080", "60"),
                     adequate("h264", "1920", "1080", "30")) == pytest.approx(1.5, abs=0.02)

    def test_48_fps_film_lands_on_the_same_curve(self):
        assert ratio(adequate("h264", "1920", "1080", "48"),
                     adequate("h264", "1920", "1080", "30")) == pytest.approx(1.32, abs=0.05)

    def test_and_the_curve_is_capped(self):
        # A 240 fps clip cannot run away with it.
        assert ratio(adequate("h264", "1920", "1080", "240"),
                     adequate("h264", "1920", "1080", "30")) == pytest.approx(2, abs=0.01)


# --- grain: the axis that inverts the others -------------------------------------
# Grain is noise: incompressible, unpredictable between frames, and it defeats
# exactly the tools a modern codec is ahead by. So a very grainy source costs
# more AND flattens the difference between codecs.

class TestGrain:
    def test_ordinary_grain_is_picture_detail(self):
        # Below the knee it costs nothing extra.
        assert adequate("h264", "1920", "1080", "24", "0") == \
            adequate("h264", "1920", "1080", "24", "15")

    def test_a_very_grainy_source_costs_more_than_a_clean_one(self):
        assert float(adequate("h264", "1920", "1080", "24", "0")) < \
            float(adequate("h264", "1920", "1080", "24", "30"))

    def test_at_full_heaviness_the_codecs_have_converged(self):
        assert adequate("h264", "1920", "1080", "24", "45") == \
            adequate("av1", "1920", "1080", "24", "45")

    def test_and_the_resolution_curve_has_straightened_to_the_pixel_count(self):
        assert ratio(adequate("av1", "3840", "2160", "24", "45"),
                     adequate("av1", "1920", "1080", "24", "45")) == \
            pytest.approx(4, abs=0.05)

    # Synthesised instead of coded, the grain never reaches the encoder: it is
    # denoised out, carried as a parameter set, and what is left to code is
    # easier than the same film without grain ever was.
    def test_a_synthesising_output_is_judged_below_the_clean_requirement(self):
        assert float(adequate("av1", "1920", "1080", "24", "40", "1")) < \
            float(adequate("av1", "1920", "1080", "24", "0", "1"))

    def test_which_is_far_below_coding_the_same_grain(self):
        assert float(adequate("av1", "1920", "1080", "24", "40", "1")) < \
            float(adequate("av1", "1920", "1080", "24", "40", "0"))

    def test_a_clean_source_is_not_discounted_for_synthesis(self):
        assert adequate("av1", "1920", "1080", "24", "0", "0") == \
            adequate("av1", "1920", "1080", "24", "0", "1")


# The weight on its own: 0 below the knee, 1 at the top, and nothing at all for
# a level that was never measured.

class TestGrainHeaviness:
    def test_an_unmeasured_grain_level_weighs_nothing(self):
        assert videobitrate.bitrate_grain_heaviness("") == "0.000"

    def test_nor_does_one_below_the_knee(self):
        assert videobitrate.bitrate_grain_heaviness("10") == "0.000"

    def test_25_is_a_quarter_of_the_way_up(self):
        assert videobitrate.bitrate_grain_heaviness("25") == "0.250"

    def test_and_the_weight_is_capped_at_1(self):
        assert videobitrate.bitrate_grain_heaviness("50") == "1.000"


# --- the hardware encode penalty ---------------------------------------------------

class TestHardware:
    def test_an_nvenc_output_needs_somewhat_more_bitrate(self):
        assert ratio(adequate("av1", "3840", "2160", "24", "0", "0", "1"),
                     adequate("av1", "3840", "2160", "24", "0", "0", "0")) == \
            pytest.approx(1.15, abs=0.02)


# --- a size that could not be read ---------------------------------------------------
# Every axis is a multiple of the frame size, so a guessed size would be a
# guessed verdict; the model says nothing instead, and the caller converts
# rather than judging.

class TestUnreadableSize:
    def test_an_unreadable_frame_size_gets_no_figure_at_all(self):
        assert adequate("h264", "", "") == ""

    def test_nor_does_a_zero_one(self):
        assert adequate("h264", "0", "0") == ""


# --- the codec tuning tables ----------------------------------------------------------
# WHICH codec a name means is codecs' job (and its own test file); what is
# asserted here is only that this file's two tables are read against that
# answer - the family's factor and exponent, and its generation's flat
# penalty.

class TestCodecTuning:
    @pytest.mark.parametrize("codec,expected", [
        ("hevc", "0.78 0.77 1.00 hevc"),
        ("H265", "0.78 0.77 1.00 hevc"),
        ("notACodec", "1.00 0.80 1.00 unknown"),
        ("", "1.00 0.80 1.00 unknown"),
        ("mpeg2video", "1.00 0.80 2.00 mpeg2"),
        ("msmpeg4v3", "1.00 0.80 1.50 mpeg4"),
        ("prores", "1.00 0.80 2.00 intra"),
    ])
    def test_tuning(self, codec, expected):
        assert videobitrate.bitrate_codec_tuning(codec) == expected


# --- the tier's nominal size ---------------------------------------------------------

class TestTierPixels:
    def test_a_1080p_frame_is_scaled_against_1920x1080(self):
        assert videobitrate.bitrate_tier_pixels("1920", "1080") == "2073600"

    def test_and_a_scope_4k_frame_against_the_whole_16_9_2160p_frame(self):
        assert videobitrate.bitrate_tier_pixels("3840", "1600") == "8294400"

    def test_sd_has_no_nominal_size(self):
        # It is scaled against its own.
        assert videobitrate.bitrate_tier_pixels("640", "480") == "307200"

    def test_and_an_unreadable_size_has_nothing_to_scale_at_all(self):
        assert videobitrate.bitrate_tier_pixels("", "") == "0"


# --- the verdicts ----------------------------------------------------------------------
# "Generous" is twice adequate: a source with a whole adequate encode's worth
# of bitrate to spare, which is the same thing as one that can be halved and
# stay adequate - the default the -t decision applies.

class TestVerdict:
    @pytest.mark.parametrize("kbit,adequate_kbit,expected", [
        ("4999", "5000", "starved"),
        ("5000", "5000", "adequate"),
        ("9999", "5000", "adequate"),
        ("10000", "5000", "generous"),
        ("", "5000", "unknown"),
        ("5000", "", "unknown"),
    ])
    def test_verdict(self, kbit, adequate_kbit, expected):
        assert videobitrate.bitrate_verdict(kbit, adequate_kbit) == expected


# --- who pays for the grain measurement --------------------------------------------------
# Grain is the one axis of this model that is not in a container: it has to be
# measured off the pixels, which is a decode of samples per file. That is
# nothing against an encode measured in hours and everything against a census,
# which is one metadata read per file - so whether the SOURCE side measures at
# all is a flag, and the model is asked for the level rather than told it.
#
# The probe itself belongs to videograin and is not imported here (it needs a
# caller's own mediaDuration and videoDimensions), so it is handed in as a
# function - which is also the point of the third case: a caller that never
# has it must not fall over.

class TestSourceBitrateGrain:
    def test_the_probe_defaults_on(self):
        # A caller that never sets the flag gets the measurement, the way the
        # shell's ${bitrateGrainProbe:-1} does.
        params = inspect.signature(videobitrate.source_bitrate_grain).parameters
        assert params["probe_enabled"].default == 1

    def test_with_it_on_the_measurement_is_what_the_model_is_given(self):
        calls = []

        def probe(file, label):
            calls.append((file, label))
            return "40"

        assert videobitrate.source_bitrate_grain("/in/aFilm.mkv", "aFilm.mkv",
                                                 1, probe) == "40"
        assert calls == [("/in/aFilm.mkv", "aFilm.mkv")]

    def test_and_the_file_stands_in_for_a_label_nobody_passed(self):
        calls = []

        def probe(file, label):
            calls.append((file, label))
            return "40"

        assert videobitrate.source_bitrate_grain("/in/aFilm.mkv", "", 1,
                                                 probe) == "40"
        assert calls == [("/in/aFilm.mkv", "/in/aFilm.mkv")]

    def test_with_it_off_no_level_is_stated_at_all(self):
        calls = []

        def probe(file, label):
            calls.append((file, label))

        assert videobitrate.source_bitrate_grain("/in/aFilm.mkv", "", 0,
                                                 probe) == ""
        assert calls == []

    def test_a_caller_without_the_probe_library_asks_for_nothing(self):
        assert videobitrate.source_bitrate_grain("/in/aFilm.mkv", "", 1,
                                                 None) == ""

    def test_the_unprobed_requirement_is_never_the_harsher_of_the_two(self):
        # An unstated level weighs nothing, and coded grain only ever RAISES
        # what a stream needs, so the verdict taken without the probe can only
        # be the same or more generous.
        assert videobitrate.bitrate_grain_heaviness("") == "0.000"
        assert float(adequate("hevc", "1920", "1080", "24", "40")) > \
            float(adequate("hevc", "1920", "1080", "24", ""))


# --- videoBitrateStats: finding the VIDEO stream's own bitrate ---------------------------
# ffprobe is stubbed with canned JSON per case; the filter is real, because the
# three fallbacks, the audio subtraction and the rational frame rate are the
# part with somewhere to hide.

class _StubFfprobe:
    """An ffprobe that hands the stats a canned document, or fails."""

    def __init__(self, workdir):
        self.bin = os.path.join(workdir, "bin")
        os.makedirs(self.bin)
        self.payload = os.path.join(workdir, "payload")
        self.stub = os.path.join(self.bin, "ffprobe")
        with open(self.stub, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'if [[ "${VBT_RC:-0}" == 0 && -f "${VBT_PAYLOAD:-}" ]]; then\n'
                '    cat -- "${VBT_PAYLOAD:?}"\n'
                'fi\n'
                'exit "${VBT_RC:-0}"\n')
        os.chmod(self.stub, 0o755)

    def give(self, document, rc=0):
        if document is None:
            os.environ.pop("VBT_PAYLOAD", None)
        else:
            with open(self.payload, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            os.environ["VBT_PAYLOAD"] = self.payload
        os.environ["VBT_RC"] = str(rc)


@pytest.fixture
def ffprobe(tmp_path, monkeypatch):
    stub = _StubFfprobe(str(tmp_path))
    monkeypatch.setenv("PATH", stub.bin + os.pathsep + os.environ["PATH"])
    return stub


class TestVideoBitrateStats:
    def test_a_stated_stream_bitrate_is_used_as_it_stands(self, ffprobe):
        # MP4 and friends state a per-stream bitrate, which is the figure
        # wanted.
        ffprobe.give({"streams": [
            {"codec_type": "video", "codec_name": "h264",
             "avg_frame_rate": "24000/1001", "bit_rate": "6000000"},
            {"codec_type": "audio", "codec_name": "aac", "channels": 6,
             "bit_rate": "640000"}],
            "format": {"bit_rate": "6700000", "duration": "600",
                       "size": "502500000"}})
        assert videobitrate.video_bitrate_stats("in.mp4") == \
            "h264 23.976023976023978 6000 stated"

    def test_an_mkvmerge_bps_tag_stands_in_for_the_missing_bitrate(self, ffprobe):
        # Matroska states none; mkvmerge writes a per-track BPS tag instead,
        # whose suffix spelling is not fixed, so it is matched on the prefix
        # and case-insensitively.
        ffprobe.give({"streams": [
            {"codec_type": "video", "codec_name": "hevc",
             "avg_frame_rate": "24/1",
             "tags": {"BPS-eng": "12000000", "DURATION": "01:40:00"}},
            {"codec_type": "audio", "codec_name": "opus", "channels": 2,
             "tags": {"bps": "128000"}}],
            "format": {"bit_rate": "12200000", "duration": "6000",
                       "size": "9150000000"}})
        assert videobitrate.video_bitrate_stats("in.mkv") == "hevc 24 12000 stated"

    def test_with_neither_the_audio_is_taken_off_the_container(self, ffprobe):
        # A file that states neither is measured: the container's own bitrate
        # less what the audio tracks state, which is an estimate and says so.
        ffprobe.give({"streams": [
            {"codec_type": "video", "codec_name": "av1",
             "avg_frame_rate": "25/1"},
            {"codec_type": "audio", "codec_name": "opus", "channels": 2,
             "bit_rate": "128000"},
            {"codec_type": "subtitle", "codec_name": "subrip"}],
            "format": {"bit_rate": "5128000", "duration": "600",
                       "size": "384600000"}})
        assert videobitrate.video_bitrate_stats("in.mkv") == "av1 25 4900 estimated"

    def test_an_audio_track_that_states_nothing_is_charged_per_channel(self, ffprobe):
        # ... and an audio track that states nothing either is estimated per
        # channel rather than counted as free, so the video's figure is not
        # inflated by it.
        ffprobe.give({"streams": [
            {"codec_type": "video", "codec_name": "av1",
             "avg_frame_rate": "25/1"},
            {"codec_type": "audio", "codec_name": "opus", "channels": 2}],
            "format": {"bit_rate": "5128000", "duration": "600",
                       "size": "384600000"}})
        assert videobitrate.video_bitrate_stats("in.mkv") == "av1 25 4900 estimated"

    def test_no_container_bitrate_either(self, ffprobe):
        # A container that states no bitrate at all is measured from its size
        # and duration.
        ffprobe.give({"streams": [
            {"codec_type": "video", "codec_name": "h264",
             "r_frame_rate": "30000/1001"}],
            "format": {"duration": "100", "size": "62500000"}})
        # The fps falls back to r_frame_rate.
        assert videobitrate.video_bitrate_stats("in.mkv") == \
            "h264 29.97002997002997 4900 estimated"

    def test_an_attached_cover_picture_is_not_the_video_stream(self, ffprobe):
        # Cover art is a video stream that is not the video, so it must not be
        # mistaken for it.
        ffprobe.give({"streams": [
            {"codec_type": "video", "codec_name": "mjpeg",
             "disposition": {"attached_pic": 1}, "bit_rate": "90000"},
            {"codec_type": "video", "codec_name": "h264",
             "avg_frame_rate": "24/1", "bit_rate": "3000000"}],
            "format": {"bit_rate": "3200000", "duration": "600",
                       "size": "240000000"}})
        assert videobitrate.video_bitrate_stats("in.mkv") == "h264 24 3000 stated"

    def test_an_unreadable_file_states_nothing(self, ffprobe):
        # Nothing readable at all leaves every field empty rather than
        # guessing a number.
        ffprobe.give({"streams": [], "format": {}})
        assert videobitrate.video_bitrate_stats("broken.mkv") == "   "

    def test_a_probe_that_fails_states_nothing(self, ffprobe):
        ffprobe.give(None, rc=1)
        assert videobitrate.video_bitrate_stats("broken.mkv") == ""

    def test_a_null_top_level_is_every_field_empty(self):
        # jq indexes null's .streams as null, which settles to no streams and
        # completes: four empty fields, the same as an unreadable-but-readable
        # document.
        assert videobitrate.stats_from_json(None) == "   "

    def test_a_non_object_top_level_is_a_failed_filter(self):
        # Every other non-object top level makes jq's index error out and state
        # nothing.
        for document in ([1, 2], "a file", 42, True):
            with pytest.raises(ValueError):
                videobitrate.stats_from_json(document)