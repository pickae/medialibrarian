"""The white box for medialib/lib/objectaudio.py.

What is pinned here is the shape of the two decisions - which sources a flag may
come from, and the one rung of the ladder whose order is not obvious.
"""

import pytest

from medialib.lib import objectaudio as oa

pytestmark = pytest.mark.pure


class TestObjectFlag:
    """Three sources, in order of how much they know."""

    @pytest.mark.parametrize("commercial", [
        "E-AC-3 JOC with Dolby Atmos",
        "Dolby Digital Plus with Dolby Atmos",
        "WITH DOLBY ATMOS",
    ])
    def test_the_commercial_name_is_what_mediainfo_writes_for_JOC(
            self, commercial):
        assert oa.audio_object_flag(commercial, "", "") == "1"

    @pytest.mark.parametrize("commercial", [
        "DTS-HD Master Audio", "Dolby TrueHD", "E-AC-3", "",
    ])
    def test_a_plain_lossless_track_is_not_object_audio(self, commercial):
        """Only the JOC variants are: TrueHD and DTS-HD MA carry a bed."""
        assert oa.audio_object_flag(commercial, "", "") == ""

    @pytest.mark.parametrize("count,expected", [
        ("1", "1"), ("11", "1"), ("128", "1"),
        ("0", ""), ("00", ""), ("", ""), ("two", ""), ("-1", ""), ("1.5", ""),
    ])
    def test_the_object_count_is_what_the_JOC_header_states(self, count,
                                                            expected):
        assert oa.audio_object_flag("", count, "") == expected

    @pytest.mark.parametrize("name", [
        "Dolby Atmos 7.1", "DTS:X", "DTSX", "atmos", "dts:x 7.1", "ATMOS",
    ])
    def test_the_track_name_is_the_last_resort(self, name):
        """A track that advertises itself although mediainfo read no metadata."""
        assert oa.audio_object_flag("", "", name) == "1"

    @pytest.mark.parametrize("name", ["Surround 5.1", "English", "Commentary"])
    def test_a_name_that_claims_nothing_scores_nothing(self, name):
        assert oa.audio_object_flag("", "", name) == ""


class TestExFlag:
    """Dolby Surround EX: a 5.1 bed with a matrixed back surround."""

    @pytest.mark.parametrize("mode", [
        "Dolby Surround EX", "DOLBY SURROUND EX", "Surround EX",
    ])
    def test_the_settings_mode_is_what_mediainfo_writes_for_the_flag(self,
                                                                     mode):
        assert oa.audio_ex_flag(mode, "") == "1"

    @pytest.mark.parametrize("mode", ["Dolby Surround", "Dolby Digital", "-",
                                      ""])
    def test_the_plain_surround_mode_is_the_downmix_flag_and_not_EX(self, mode):
        """Every commentary track of a film carries "Dolby Surround"."""
        assert oa.audio_ex_flag(mode, "") == ""

    @pytest.mark.parametrize("name", [
        "AC-3 5.1-EX", "Dolby Digital EX", "DD-EX", "ddex", "Surround EX",
        "English 5.1 EX", "E-AC-3 EX",
    ])
    def test_the_track_name_is_the_last_resort(self, name):
        assert oa.audio_ex_flag("", name) == "1"

    @pytest.mark.parametrize("name", [
        "Commentary by Alex Doe", "Extended Edition", "Surround 5.1",
        "Exclusive", "Texas", "", "Dolby Atmos 7.1",
    ])
    def test_a_syllable_is_not_a_format(self, name):
        """"ex" is a syllable of too many other words - and of too many names,
        a commentary track being a list of them - to read on its own."""
        assert oa.audio_ex_flag("", name) == ""


class TestLadder:
    """One winner per language, the best available tier."""

    @pytest.mark.parametrize("codec,channels,objects,score", [
        ("A_EAC3", "8", "1", "100"),
        ("A_EAC3", "8", "", "90"),
        ("A_EAC3", "6", "1", "80"),
        ("A_AC3", "8", "", "70"),
        ("A_EAC3", "6", "", "60"),
        ("A_AC3", "6", "", "50"),
    ])
    def test_the_six_rungs(self, codec, channels, objects, score):
        assert oa.audio_ladder_score(codec, channels, objects) == score

    @pytest.mark.parametrize("codec,score", [
        ("A_AC3", "62"), ("A_EAC3", "65"),
    ])
    def test_the_EX_rungs_sit_between_the_two_bed_widths(self, codec, score):
        assert oa.audio_ladder_score(codec, "6", "", "1") == score

    def test_EX_beats_the_5_1_it_is_encoded_as_and_loses_to_everything_above(
            self):
        """Above 5.1, because of the channel it carries that a 5.1 does not;
        below a discrete 7.1 and below every Atmos tier, which carry more."""
        ex = int(oa.audio_ladder_score("A_AC3", "6", "", "1"))
        assert ex > int(oa.audio_ladder_score("A_AC3", "6", "", ""))
        assert ex < int(oa.audio_ladder_score("A_EAC3", "6", "", "1"))
        assert ex < int(oa.audio_ladder_score("A_AC3", "8", "", ""))
        assert (int(oa.audio_ladder_score("A_EAC3", "6", "", "1"))
                < int(oa.audio_ladder_score("A_AC3", "8", "", "")))
        for atmos in (oa.audio_ladder_score("A_EAC3", "6", "1", ""),
                      oa.audio_ladder_score("A_EAC3", "8", "1", "")):
            assert int(oa.audio_ladder_score("A_EAC3", "6", "", "1")) \
                < int(atmos)

    def test_objects_outrank_an_EX_flag_on_the_same_track(self):
        """A track that is both is an Atmos track: the objects are what the
        matrixed channel only approximates."""
        assert oa.audio_ladder_score("A_EAC3", "6", "1", "1") == "80"

    @pytest.mark.parametrize("codec,score", [
        ("A_AC3", "70"), ("A_EAC3", "90"),
    ])
    def test_an_EX_flag_on_a_7_1_bed_changes_nothing(self, codec, score):
        """EX is 5.1 and a matrix, so the flag is only read on that bed - and a
        7.1 that claims it stays on its own rung rather than falling off the
        ladder."""
        assert oa.audio_ladder_score(codec, "8", "", "1") == score

    def test_objects_on_a_narrow_bed_outrank_a_wider_plain_one(self):
        """The one order that is not obvious: an E-AC-3 5.1 WITH objects sits
        above a plain AC-3 7.1, because the objects are the only thing the
        narrower track carries that no other tier does."""
        assert (int(oa.audio_ladder_score("A_EAC3", "6", "1"))
                > int(oa.audio_ladder_score("A_AC3", "8", "")))

    @pytest.mark.parametrize("objects", ["1", "", "0"])
    def test_an_object_flag_on_AC_3_changes_nothing(self, objects):
        """Plain AC-3 carries no JOC, so a flag on one says nothing - and must
        not move it up the ladder or off it."""
        assert oa.audio_ladder_score("A_AC3", "8", objects) == "70"

    @pytest.mark.parametrize("codec", [
        "A_TRUEHD", "A_DTS", "A_OPUS", "A_FLAC", "A_AAC", "",
    ])
    def test_a_codec_that_is_not_on_the_ladder_scores_nothing(self, codec):
        """The lossless tracks are consumed into their opus bed before the
        ladder runs, and opus is separate from it."""
        assert oa.audio_ladder_score(codec, "8", "1") == ""

    @pytest.mark.parametrize("channels", ["1", "2", "3", "4", "5", "7", "16",
                                          # 9 is a 3D bed, above libopus's ceiling
                                          "9", "", "08"])
    def test_a_channel_count_with_no_rung_scores_nothing(self, channels):
        assert oa.audio_ladder_score("A_EAC3", channels, "1") == ""

    def test_a_codec_id_with_a_suffix_still_lands_on_its_rung(self):
        """mkvmerge writes A_AC3/BSID9 for some streams."""
        assert oa.audio_ladder_score("A_AC3/BSID9", "6", "") == "50"


class TestOpusLayout:
    """The layouts ffmpeg's libopus wrapper has to be told."""

    @pytest.mark.parametrize("channels,expected", [
        ("3", "3.0"), ("4", "quad"),
    ])
    def test_the_two_counts_that_need_one(self, channels, expected):
        """`-ac` alone lands 3 and 4 on ffmpeg's defaults - 2.1 and 4.0 - and
        the encoder accepts neither."""
        from medialib.lib import bitrates
        assert bitrates.audio_opus_layout(channels) == expected

    @pytest.mark.parametrize("channels", ["1", "2", "5", "6", "7", "8", "9",
                                          "0", ""])
    def test_every_other_count_needs_nothing(self, channels):
        """Their defaults are exactly the layout the encoder wants."""
        from medialib.lib import bitrates
        assert bitrates.audio_opus_layout(channels) is None
