"""The white box for medialib/cli/convert_video.py.

The helpers that belong to libraries - the resolution ladder, the chunk
directory, the grain measurement, the clock formatting - have white boxes of
their own, so what is here is what this script owns: the profile tables, the
quality level and its resolution bias, the chunk plan, the argument string, and
the -t decision.
"""

import sys

import pytest

from medialib.cli import convert_video as cv

pytestmark = pytest.mark.pure

# A table of the script's own shape, so the lookup is tested rather than the rows.
TABLE = """
av1Grain|-c:v libsvtav1 -crf 24
passthrough|-c:v copy
"""


class TestProfileArgs:

    def test_a_named_row_gives_its_arguments(self):
        assert cv.profile_args(TABLE, "av1Grain") == "-c:v libsvtav1 -crf 24"
        assert cv.profile_args(TABLE, "passthrough") == "-c:v copy"

    def test_an_unknown_name_refuses_and_lists_what_it_knows(self):
        """A typo in -p or -a has to stop the run before a file is touched, and
        the message that stops it is the list."""
        with pytest.raises(cv.UnknownProfile) as raised:
            cv.profile_args(TABLE, "nope")
        text = raised.value.text()
        assert 'Unknown profile "nope"' in text
        assert "av1Grain" in text and "passthrough" in text

    def test_every_shipped_profile_is_a_row_that_names_an_encoder(self):
        """A row whose arguments name no -c:v would encode with whatever ffmpeg
        defaults to."""
        for name, _sep, args in cv._rows(cv.VIDEO_PROFILES):
            assert cv.encoder_of(args), name


class TestQualityBias:
    """Signed, and to be ADDED, so positive means a higher CRF/CQ: softer and
    smaller. The tier comes from the shared ladder, which is why a non-16:9 source
    that fills only one axis is still biased as the tier it does the work of."""

    @pytest.mark.parametrize("width,height,expected", [
        (7680, 4320, 3), (3840, 2160, 2), (2560, 1440, 1),
        (1920, 1080, 0), (1280, 720, -1), (854, 480, -2),
    ])
    def test_each_tier_moves_the_level_by_its_row(self, width, height,
                                                  expected):
        assert cv.quality_bias_for(width, height) == expected

    @pytest.mark.parametrize("width,height", [(2880, 2160), (3840, 1600)])
    def test_a_non_16_9_source_is_biased_as_the_tier_it_reaches(self, width,
                                                               height):
        assert cv.quality_bias_for(width, height) == 2

    def test_an_unreadable_size_is_not_biased_in_either_direction(self):
        """The profile's own level stands rather than a guess."""
        assert cv.quality_bias_for("", "") == 0


class TestQualityBiasSpellings:
    """Generated from the table, so the run cannot advertise a bias it will not
    apply."""

    def test_it_leads_with_the_top_tier(self):
        assert cv.quality_bias_spellings().startswith("4320p +3, 2160p +2")

    def test_the_neutral_rung_is_not_written_as_plus_zero(self):
        """Which would only look like a decision that was made."""
        assert "1080p 0," in cv.quality_bias_spellings()

    def test_every_table_row_is_spelled_out(self):
        assert len(cv.quality_bias_spellings().split(",")) == 7


class TestChunkCount:
    """Driven by resolution rather than length: the smaller the frames, the less
    work a single encoder spreads across cores."""

    @pytest.mark.parametrize("width,height,expected", [
        (3840, 2160, 1), (2560, 1440, 2), (1920, 1080, 3),
        (1280, 720, 4), (854, 480, 6),
    ])
    def test_each_tier_at_the_reference_core_count(self, width, height,
                                                   expected):
        assert cv.chunk_count_for(width, height, 32) == expected

    @pytest.mark.parametrize("width,height,expected", [
        (2880, 2160, 1), (3840, 1600, 1), (1440, 1080, 3), (1920, 800, 3),
    ])
    def test_either_dimension_reaches_the_tier(self, width, height, expected):
        assert cv.chunk_count_for(width, height, 32) == expected

    @pytest.mark.parametrize("cores,width,height,expected", [
        (16, 1920, 1080, 2), (16, 3840, 2160, 1),
        (64, 1920, 1080, 6), (64, 3840, 2160, 2),
    ])
    def test_it_scales_linearly_with_the_machines_cores(self, cores, width,
                                                        height, expected):
        assert cv.chunk_count_for(width, height, cores) == expected

    def test_it_never_drops_below_one(self):
        assert cv.chunk_count_for(3840, 2160, 1) == 1

    def test_an_unreadable_size_takes_the_middle_rung(self):
        """Its own tier rather than a guessed number of pixels: safe in both
        directions."""
        assert cv.chunk_count_for("", "", 32) == 3

    def test_a_half_read_size_is_classified_by_the_axis_that_came_through(self):
        assert cv.chunk_count_for("", 2160, 32) == 1


class TestDownscaleArgs:
    """The ceiling only ever scales DOWN, and only when the source is really above
    it: a filter that resamples the picture is not free."""

    def test_without_a_ceiling_nothing_is_scaled(self):
        assert cv.downscale_args(3840, 2160, "") == ""

    @pytest.mark.parametrize("width,height,expected", [
        (3840, 2160, " -vf scale=1920:1080"),
        (3840, 1600, " -vf scale=1920:800"),
        (2880, 2160, " -vf scale=1440:1080"),
    ])
    def test_a_larger_source_is_capped_with_its_aspect_kept(self, width,
                                                            height, expected):
        assert cv.downscale_args(width, height, "1080p") == expected

    @pytest.mark.parametrize("width,height", [(1920, 1080), (1280, 720)])
    def test_a_source_at_or_below_the_ceiling_is_left_alone(self, width,
                                                            height):
        """Nothing is ever scaled UP: the pixels to fill a bigger frame do not
        exist."""
        assert cv.downscale_args(width, height, "1080p") == ""

    def test_a_size_that_could_not_be_read_is_left_alone(self):
        """Rather than guessed at, since a wrong guess would resample the
        picture."""
        assert cv.downscale_args("", "", "1080p") == ""

    def test_the_scaled_size_is_even_on_both_sides(self):
        """An odd size would be rejected by the 10-bit 4:2:0 pixel formats these
        profiles encode in."""
        assert cv.downscale_args(2048, 858, "1080p") == " -vf scale=1920:804"

    def test_an_alias_names_the_same_ceiling(self):
        assert cv.downscale_args(7680, 4320, "4k") == " -vf scale=3840:2160"


class TestEqualBoundaries:

    def test_three_chunks_are_two_equal_cuts(self):
        assert cv.equal_boundaries(120, 3) == ["40.000", "80.000"]

    def test_four_chunks_are_three(self):
        assert cv.equal_boundaries(120, 4) == ["30.000", "60.000", "90.000"]

    @pytest.mark.parametrize("count", [1, 0, -1])
    def test_one_chunk_has_no_interior_boundaries(self, count):
        assert cv.equal_boundaries(120, count) == []


class TestPixFmt:
    """10-bit for every source, SDR or HDR, because it avoids banding at no
    meaningful cost."""

    @pytest.mark.parametrize("args", ["-c:v libsvtav1 -crf 24",
                                      "-c:v libx265 -crf 20"])
    def test_a_software_encoder_takes_the_planar_format(self, args):
        assert cv.pix_fmt_for(args) == "yuv420p10le"

    @pytest.mark.parametrize("args", ["-c:v hevc_nvenc -preset p6",
                                      "-c:v h264_vaapi"])
    def test_hardware_frames_take_the_semi_planar_one(self, args):
        assert cv.pix_fmt_for(args) == "p010le"


class TestMergeParam:

    def test_it_prepends_into_an_existing_value(self):
        """Prepending rather than appending is what lets a caller inject a key the
        row also carries: duplicate keys have no defined precedence."""
        assert cv.merge_param("-c:v libx265 -x265-params profile=main10",
                              "-x265-params", "master-display=G(1,2)") == \
            "-c:v libx265 -x265-params master-display=G(1,2):profile=main10"

    def test_it_appends_the_flag_when_it_is_not_there(self):
        assert cv.merge_param("-c:v libsvtav1", "-svtav1-params",
                              "content-light=1,2") == \
            "-c:v libsvtav1 -svtav1-params content-light=1,2"


class TestNvencEngines:
    """A guess from the model, because the real count needs the Video Codec SDK -
    and always overridable with -e."""

    def test_the_5090_exposes_three(self):
        assert cv.nvenc_engines_for("NVIDIA GeForce RTX 5090") == 3

    @pytest.mark.parametrize("name", ["NVIDIA GeForce RTX 4090",
                                      "NVIDIA RTX A1000 Laptop GPU", ""])
    def test_every_other_card_defaults_to_two(self, name):
        """A safe value: an extra session on a one-engine card time-slices, it is
        not an error."""
        assert cv.nvenc_engines_for(name) == 2


class TestDolbyVisionArgs:
    """Mode 1 carries the source's RPU through, 0 explicitly strips it so ffmpeg's
    auto default cannot turn it on behind the script's back, and an empty mode - a
    source that is not Dolby Vision at all - must change nothing."""

    X265 = "-c:v libx265 -crf 20 -preset slow -x265-params profile=main10"
    AV1 = "-c:v libsvtav1 -crf 30 -preset 6 -svtav1-params tune=0"
    NVENC = "-c:v hevc_nvenc -preset p6 -cq 24 -profile:v main10"

    @pytest.mark.parametrize("args", [X265, AV1, NVENC])
    def test_a_non_dolby_vision_source_leaves_the_arguments_untouched(self,
                                                                      args):
        assert cv.dolby_vision_args(args, "") == args

    def test_x265_with_it_on_merges_the_VBV_settings(self):
        """x265 refuses to code an RPU without them."""
        assert cv.dolby_vision_args(self.X265, "1") == (
            "-c:v libx265 -crf 20 -preset slow -x265-params "
            "vbv-maxrate=160000:vbv-bufsize=160000:profile=main10 "
            "-dolbyvision 1")

    def test_x265_with_it_off_adds_no_VBV_settings(self):
        assert cv.dolby_vision_args(self.X265, "0") == self.X265 + \
            " -dolbyvision 0"

    @pytest.mark.parametrize("mode", ["1", "0"])
    def test_svtav1_only_gets_the_switch(self, mode):
        """It has no such requirement."""
        assert cv.dolby_vision_args(self.AV1, mode) == \
            "%s -dolbyvision %s" % (self.AV1, mode)

    @pytest.mark.parametrize("mode", ["1", "0"])
    def test_nvenc_is_untouched_either_way(self, mode):
        """It cannot code an RPU at all and has no option to set."""
        assert cv.dolby_vision_args(self.NVENC, mode) == self.NVENC


class TestVideoSourceFor:
    """The source itself, unless a dual-layer profile 7 file was normalised to 8.1
    first - in which case every video encode has to read THAT. Audio, subtitles
    and the mux never come through here: the intermediate has none of them."""

    @pytest.mark.skipif(sys.platform == "win32",
                     reason="POSIX path layout; os.path.join uses the "
                            "platform's separator")
    def test_an_ordinary_source_is_encoded_from_itself(self):
        assert cv.video_source_for("sub/film.mkv", "/lib/in") == \
            "/lib/in/sub/film.mkv"

    def test_a_normalised_source_is_encoded_from_the_prepared_file(self):
        assert cv.video_source_for("sub/film.mkv", "/lib/in",
                                   "/dev/shm/scratch/dv81.mkv") == \
            "/dev/shm/scratch/dv81.mkv"


class TestTheQualityKnob:
    """The same knob under two names: -crf for the software encoders, -cq for the
    NVENC ones. The constrained rows have neither - they target an average bitrate,
    so there is no level for -q to override."""

    @pytest.mark.parametrize("args,expected", [
        ("-c:v libsvtav1 -crf 30", "-crf"),
        ("-c:v hevc_nvenc -cq 24", "-cq"),
        ("-c:v libsvtav1 -b:v 3000k -qmin 30", ""),
    ])
    def test_the_flag_a_row_sets_its_quality_with(self, args, expected):
        assert cv.video_quality_flag(args) == expected

    def test_qmin_is_not_mistaken_for_a_quality_level(self):
        """The constrained rows use it as a floor under an average bitrate."""
        assert cv.video_quality_flag("-c:v libsvtav1 -b:v 600k -qmin 35") == ""

    @pytest.mark.parametrize("args,expected", [
        ("-c:v libsvtav1 -crf 30", 63),
        ("-c:v libx265 -crf 20", 51),
        ("-c:v hevc_nvenc -cq 24", 51),
    ])
    def test_the_top_of_each_encoders_scale(self, args, expected):
        assert cv.video_quality_max(args) == expected


class TestApplyVideoQuality:

    AV1 = "-c:v libsvtav1 -crf 30 -preset 6"
    NVENC = "-c:v hevc_nvenc -preset p7 -cq 24 -b:v 0"

    def test_a_given_level_replaces_the_rows_own(self):
        assert cv.apply_video_quality(self.AV1, 1920, 1080, given=True,
                                      quality="18") == \
            "-c:v libsvtav1 -crf 18 -preset 6"

    def test_and_is_used_for_every_file_whatever_its_tier(self):
        """-q is what turns the bias off."""
        for width, height in ((3840, 2160), (854, 480)):
            assert cv.apply_video_quality(self.AV1, width, height, given=True,
                                          quality="18") == \
                "-c:v libsvtav1 -crf 18 -preset 6"

    @pytest.mark.parametrize("width,height,expected", [
        (3840, 2160, 32), (1920, 1080, 30), (854, 480, 28),
    ])
    def test_without_one_the_rows_level_moves_with_the_tier(self, width,
                                                            height, expected):
        assert cv.apply_video_quality(self.AV1, width, height) == \
            "-c:v libsvtav1 -crf %d -preset 6" % expected

    def test_the_nvenc_flag_is_moved_the_same_way(self):
        assert cv.apply_video_quality(self.NVENC, 3840, 2160) == \
            "-c:v hevc_nvenc -preset p7 -cq 26 -b:v 0"

    def test_the_bias_is_clamped_to_the_encoders_scale(self):
        """So the clamp and the -q validation cannot disagree about what the
        encoder will accept."""
        assert cv.apply_video_quality("-c:v libx265 -crf 50", 7680, 4320) == \
            "-c:v libx265 -crf 51"
        assert cv.apply_video_quality("-c:v libx265 -crf 1", 854, 480) == \
            "-c:v libx265 -crf 0"

    def test_a_row_with_no_quality_flag_comes_back_untouched(self):
        """Safe to apply to every profile, which is what lets the validation be
        the one place a mismatch is reported."""
        row = "-c:v libsvtav1 -b:v 3000k -qmin 30"
        assert cv.apply_video_quality(row, 3840, 2160) == row

    def test_a_flag_with_no_number_is_left_exactly_as_written(self):
        """Rather than having a level invented for it."""
        row = "-c:v libsvtav1 -crf -preset 6"
        assert cv.apply_video_quality(row, 3840, 2160) == row

    def test_an_unreadable_size_leaves_the_rows_own_level(self):
        assert cv.apply_video_quality(self.AV1, "", "") == self.AV1


class TestApplyNvencTune:

    ROW = "-c:v hevc_nvenc -preset p7 -tune uhq -rc vbr"

    def test_the_wanted_tuning_leaves_the_row_alone(self):
        assert cv.apply_nvenc_tune(self.ROW, "uhq") == self.ROW

    def test_a_build_that_cannot_take_it_gets_the_fallback(self):
        assert cv.apply_nvenc_tune(self.ROW, "hq") == \
            "-c:v hevc_nvenc -preset p7 -tune hq -rc vbr"

    def test_a_row_with_no_tuning_is_untouched(self):
        """So it is safe to apply to everything, software rows included."""
        row = "-c:v libsvtav1 -crf 30"
        assert cv.apply_nvenc_tune(row, "hq") == row


class TestBuildVideoArgs:
    """The assembly: which encoder-specific parameter the HDR fragment is merged
    into, where the pixel format, the colour arguments and the scale filter land,
    and that the Dolby Vision switch is applied last.

    The three readers that would need a real file are stubbed, so what is under
    test is purely the assembly.
    """

    HDR10 = " -color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc"
    HLG = " -color_primaries bt2020 -color_trc arib-std-b67 -colorspace bt2020nc"
    DISPLAY = ("G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)"
               "L(10000000,1) 1000,400")

    @pytest.fixture
    def build(self, monkeypatch):
        def run(base, colour="", display="", dimensions=("1920", "1080"),
                **settings):
            monkeypatch.setattr(cv, "video_color_args", lambda path: colour)
            monkeypatch.setattr(cv, "hdr_master_display", lambda path: display)
            monkeypatch.setattr(cv, "video_dimensions",
                                lambda path: dimensions + ("", ""))
            return cv.build_video_args(base, "in.mkv", cv.Settings(**settings))
        return run

    def test_an_SDR_source_gets_the_pixel_format_and_the_colour_tags(self,
                                                                     build):
        assert build("-c:v libx265 -crf 20",
                     colour=" -color_primaries bt709 -color_trc bt709") == (
            "-c:v libx265 -crf 20 -pix_fmt yuv420p10le "
            "-color_primaries bt709 -color_trc bt709")

    def test_a_source_with_no_colour_tags_has_none_invented(self, build):
        assert build("-c:v libx265 -crf 20") == \
            "-c:v libx265 -crf 20 -pix_fmt yuv420p10le"

    def test_HDR10_into_x265_merges_the_metadata_and_the_PQ_optimisation(
            self, build):
        """hdr-opt reasons about where the PQ curve puts its steps."""
        built = build("-c:v libx265 -crf 20", colour=self.HDR10,
                      display=self.DISPLAY)
        assert built.startswith(
            "-c:v libx265 -crf 20 -x265-params hdr-opt=1:repeat-headers=1:"
            "master-display=G(8500,39850)B(6550,2300)R(35400,14600)"
            "WP(15635,16450)L(10000000,1):max-cll=1000,400 ")
        assert built.endswith("-pix_fmt yuv420p10le" + self.HDR10)

    def test_an_HLG_source_keeps_the_metadata_but_drops_the_PQ_only_switch(
            self, build):
        """BT.2100 permits an HLG stream to carry the same static metadata and
        some graders write it, so it is copied - but hdr-opt has no business on
        one."""
        built = build("-c:v libx265 -crf 20", colour=self.HLG,
                      display=self.DISPLAY)
        assert "hdr-opt" not in built
        assert "master-display=G(8500,39850)" in built

    def test_the_same_source_into_svtav1_uses_the_AV1_spelling(self, build):
        """Transfer-agnostic: there is no PQ-only switch on that side to
        withhold."""
        assert build("-c:v libsvtav1 -crf 30", colour=self.HLG,
                     display=self.DISPLAY) == (
            "-c:v libsvtav1 -crf 30 -svtav1-params mastering-display="
            "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)"
            "L(10000000,1):content-light=1000,400 -pix_fmt yuv420p10le"
            + self.HLG)

    @pytest.mark.parametrize("mode,switch", [
        ("1", " -dolbyvision 1"), ("0", " -dolbyvision 0"), ("", ""),
    ])
    def test_the_dolby_vision_switch_joins_the_encoder_arguments(
            self, build, mode, switch):
        """Before the pixel format and colour tags, which are appended after the
        whole encoder block."""
        assert build("-c:v libsvtav1 -crf 30", colour=" -colorspace bt2020nc",
                     dolby_vision_mode=mode) == (
            "-c:v libsvtav1 -crf 30%s -pix_fmt yuv420p10le -colorspace bt2020nc"
            % switch)

    def test_x265_with_HDR_and_DV_carries_all_three(self, build):
        built = build("-c:v libx265 -crf 20", colour=self.HDR10,
                      display="G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10) 100,50",
                      dolby_vision_mode="1")
        assert "vbv-maxrate=160000" in built
        assert "master-display=G(1,2)" in built
        assert "-dolbyvision 1" in built

    def test_a_hardware_encoder_gets_the_hardware_pixel_format(self, build):
        assert build("-c:v hevc_nvenc -cq 24") == \
            "-c:v hevc_nvenc -cq 24 -pix_fmt p010le"

    def test_the_quality_override_is_applied_here(self, build):
        """So every caller encodes at the level the run settled on."""
        assert build("-c:v libsvtav1 -crf 30", quality_given=True,
                     quality="18") == \
            "-c:v libsvtav1 -crf 18 -pix_fmt yuv420p10le"

    def test_and_the_bias_when_there_is_none(self, build):
        assert build("-c:v libsvtav1 -crf 30",
                     dimensions=("3840", "2160")) == \
            "-c:v libsvtav1 -crf 32 -pix_fmt yuv420p10le"

    def test_the_grain_level_and_fast_decode_are_merged_for_svtav1(self,
                                                                   build):
        """The two settings the row deliberately leaves out, because they are
        decided per file and per run."""
        built = build("-c:v libsvtav1 -crf 30 -svtav1-params tune=0",
                      grain_level="12", fast_decode="2")
        assert "-svtav1-params film-grain=12:film-grain-denoise=1:" \
            "fast-decode=2:tune=0" in built

    def test_the_denoise_accompanies_the_synthesis(self, build):
        """Asking for synthesis without it would store the source's grain AND
        re-generate more on top of it."""
        built = build("-c:v libsvtav1 -crf 30", grain_level="12")
        assert "film-grain=12:film-grain-denoise=1" in built

    def test_neither_is_merged_for_an_encoder_that_has_no_such_knob(self,
                                                                    build):
        built = build("-c:v hevc_nvenc -cq 24", grain_level="12",
                      fast_decode="2")
        assert "film-grain" not in built and "fast-decode" not in built

    def test_the_downscale_filter_is_appended_last(self, build):
        assert build("-c:v libsvtav1 -crf 30", dimensions=("3840", "2160"),
                     max_resolution="1080p").endswith(" -vf scale=1920:1080")

    def test_the_bias_follows_the_ENCODED_size_not_the_source(self, build):
        """With -r in play those differ, and it is the encoded frame that decides
        whether a level is generous or mean."""
        built = build("-c:v libsvtav1 -crf 30", dimensions=("3840", "2160"),
                      max_resolution="1080p")
        assert built.startswith("-c:v libsvtav1 -crf 30 ")


class TestTheVideoOnlyFailsafe:
    """The intermediate is kept under the source's name plus a marker, and only
    when the video pass really produced a COMPLETE encode."""

    def test_the_path_is_marked_and_forced_to_matroska(self):
        assert cv.video_only_path_for("A Movie.mp4", "/out") == \
            "/out/A Movie (video only).mkv"

    def test_it_keeps_the_sources_sub_folder(self):
        assert cv.video_only_path_for("sub/A Movie.mkv", "/out") == \
            "/out/sub/A Movie (video only).mkv"


class TestVideoIntermediateComplete:
    """A missing, empty or short video.mkv means the video pass itself failed - a
    chunk that died leaves a short re-join, and half a video is not worth
    keeping."""

    @pytest.fixture
    def probe(self, tmp_path, monkeypatch):
        def run(duration, content="x"):
            if content is not None:
                (tmp_path / "video.mkv").write_text(content)
            monkeypatch.setattr(cv, "_probe", lambda argv: str(duration))
            return cv.video_intermediate_complete(str(tmp_path), 100)
        return run

    def test_a_missing_intermediate_is_not_complete(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.setattr(cv, "_probe", lambda argv: "100")
        assert cv.video_intermediate_complete(str(tmp_path), 100) is False

    def test_an_empty_one_is_not_either(self, probe):
        """Caught before probing."""
        assert probe(100, content="") is False

    @pytest.mark.parametrize("duration", [100, 101, 99.5, 99])
    def test_as_long_or_longer_is_complete(self, probe, duration):
        """The one-second tolerance absorbs the rounding between a container's
        duration and the sum of the chunks."""
        assert probe(duration) is True

    @pytest.mark.parametrize("duration", [98.9, 50, 0])
    def test_more_than_a_second_short_is_not(self, probe, duration):
        assert probe(duration) is False


class TestTheAudioTracks:
    """What each track is encoded with: a track above stereo is downmixed -
    libopus refuses a 5.1(side) layout outright - and is then priced as the stereo
    track it has become."""

    def _args(self, channels, profile="opus", custom=""):
        return cv.audio_track_args(channels, cv.Settings(
            audio_profile=profile, custom_audio_bitrate=custom))

    @pytest.mark.parametrize("channels", ["6", "8"])
    def test_a_surround_track_is_downmixed_to_stereo(self, channels):
        args, bitrate = self._args(channels)
        assert args == "-ac 2 -c:a libopus"
        # And priced as stereo, not as the surround track it was.
        assert bitrate == "120"

    @pytest.mark.parametrize("channels,bitrate", [("2", "120"), ("1", "100")])
    def test_stereo_and_mono_keep_their_channels_and_their_own_bitrate(
            self, channels, bitrate):
        args, found = self._args(channels)
        assert "-ac " not in args
        assert found == bitrate

    def test_an_unreadable_channel_count_is_not_downmixed(self):
        """Left as it is rather than guessed at, and priced from the stereo
        fallback the lookup already had."""
        args, bitrate = self._args("N/A")
        assert "-ac " not in args
        assert bitrate == "120"

    def test_a_pinned_bitrate_still_downmixes(self):
        """-b says nothing about channels, and the downmix is what makes the
        encode possible at all."""
        args, bitrate = self._args("6", profile="opusCustom", custom="96")
        assert args == "-ac 2 -c:a libopus"
        assert bitrate == "96"

    def test_the_passthrough_profile_copies_rather_than_encoding(self):
        """Which is what makes the run skip the audio pass entirely."""
        args, _bitrate = self._args("6", profile="passthrough")
        assert "-c:a copy" in args


class TestWarnSourceGeometry:
    """Detected and reported, never corrected: deinterlacing picks a field order,
    a cadence and an output frame rate, un-squeezing picks a target pixel grid,
    and each is a judgement some material defeats and nothing can undo."""

    def _lines(self, verdict, sar):
        found = []
        cv.warn_source_geometry("a.mkv", verdict, sar, log=found.append)
        return found

    def test_an_interlaced_source_is_warned_about(self):
        assert any("is interlaced" in line
                   for line in self._lines("interlaced", "1:1"))

    @pytest.mark.parametrize("verdict", ["", "unknown", "N/A", "progressive"])
    def test_everything_else_is_not(self, verdict):
        assert not any("interlaced" in line
                       for line in self._lines(verdict, "1:1"))

    def test_a_non_square_pixel_aspect_is_warned_about(self):
        assert any("non-square pixels" in line
                   for line in self._lines("progressive", "16:15"))

    @pytest.mark.parametrize("sar", ["", "unknown", "N/A", "0:1", "1:1"])
    def test_a_square_or_unrecorded_one_is_not(self, sar):
        """0:1 is ffprobe for "no pixel aspect recorded", which is not the same
        claim as a non-square one."""
        assert not any("non-square" in line
                       for line in self._lines("progressive", sar))

    def test_a_source_that_is_both_gets_both(self):
        assert len(self._lines("interlaced", "16:15")) == 2


class TestConversionWorthwhile:
    """The three steps a file has to get past, on canned measurements. The MODEL
    is the bitrate library's and is tested there; what is asserted here is the
    DECISION taken with it - which sources are skipped, which are converted, and
    that each answer says what it was judged on."""

    @pytest.fixture
    def decide(self, monkeypatch):
        def run(stats, width=1920, height=1080, enc_width=1920,
                enc_height=1080, grain="0", saving="50", grain_level="0"):
            monkeypatch.setattr(cv.videobitrate, "video_bitrate_stats",
                                lambda path: stats)
            lines = []
            settings = cv.Settings(input_dir="/in", encoder="libsvtav1",
                                   hardware_encode=False,
                                   grain_level=grain_level,
                                   required_saving=saving)
            verdict = cv.conversion_worthwhile(
                "a.mkv", width, height, enc_width, enc_height, grain,
                settings, log=lines.append)
            return verdict, "\n".join(lines)
        return run

    def test_a_starved_source_is_skipped(self, decide):
        """It cannot be improved by encoding it again, only degraded a generation
        further."""
        worth, why = decide("h264 24 3000 stated")
        assert worth is False
        assert "is already starved - about 5000 kbit/s would be adequate" in why

    def test_an_adequate_source_with_no_room_to_save_is_skipped_too(self,
                                                                    decide):
        """Adequate is not enough on its own: the question is whether the OUTPUT
        would still be adequate on what is left after the saving."""
        worth, why = decide("h264 24 5500 stated")
        assert worth is False
        assert "only 2750 would be left after saving 50%" in why

    def test_the_same_source_converts_when_less_is_demanded(self, decide):
        """Which is the whole point of the number being an option rather than a
        constant."""
        worth, _why = decide("h264 24 5500 stated", saving="30")
        assert worth is True

    def test_a_generous_source_is_converted(self, decide):
        worth, why = decide("h264 24 12000 stated")
        assert worth is True
        assert "so 50% can be saved and it stays adequate" in why

    def test_the_output_is_judged_at_the_size_it_is_ENCODED_at(self, decide):
        """So a 4K source with nothing to give at 4K may still be worth
        converting to 1080p."""
        assert decide("h264 24 15500 stated", width=3840, height=2160,
                      enc_width=3840, enc_height=2160)[0] is False
        assert decide("h264 24 15500 stated", width=3840, height=2160,
                      enc_width=1920, enc_height=1080)[0] is True

    def test_grain_cuts_both_ways(self, decide):
        """Coded, it is noise that costs the new codec nearly as much as it cost
        the old one; synthesised, it is denoised out and the output needs far
        less."""
        assert decide("h264 24 9000 stated", grain="40")[0] is False
        assert decide("h264 24 9000 stated", grain="40",
                      grain_level="12")[0] is True

    def test_an_unreadable_measurement_converts_anyway_and_says_so(self,
                                                                   decide):
        """The test exists to avoid pointless work, and refusing a file over a
        missing measurement would lose a conversion that may well be worth
        doing."""
        worth, why = decide("h264 24  ")
        assert worth is True
        assert "could not measure the source video bitrate" in why

    def test_an_unreadable_frame_size_converts_anyway(self, decide):
        assert decide("h264 24 6000 stated", width="", height="",
                      enc_width="", enc_height="")[0] is True
