"""Tests for medialib.lib.subtitletracks - a subtitle's form and role."""

import pytest

from medialib.lib import enums
from medialib.lib import subtitletracks as st

pytestmark = pytest.mark.pure


class TestForm:
    @pytest.mark.parametrize("codec", ["S_HDMV/PGS", "S_VOBSUB", "S_DVBSUB",
                                       "s_hdmv/pgs", "hdmv_pgs_subtitle",
                                       "dvd_subtitle", "dvb_subtitle"])
    def test_a_picture_codec_in_either_spelling(self, codec):
        assert st.subtitle_form(codec) == "image"

    @pytest.mark.parametrize("codec", ["S_TEXT/UTF8", "S_TEXT/ASS",
                                       "S_TEXT/WEBVTT", "subrip", "ass",
                                       "webvtt", "mov_text"])
    def test_a_text_codec_in_either_spelling(self, codec):
        assert st.subtitle_form(codec) == "text"

    @pytest.mark.parametrize("codec", ["A_AC3", "V_MPEGH/ISO/HEVC", "", None])
    def test_anything_else_is_no_subtitle(self, codec):
        assert st.subtitle_form(codec) == ""

    def test_both_spellings_of_the_picture_codecs_line_up(self):
        assert len(enums.IMAGE_SUBTITLE_CODECS) \
            == len(enums.IMAGE_SUBTITLE_TRACK_CODECS)


class TestRole:
    def test_a_plain_track_is_the_full_subtitle(self):
        assert st.subtitle_role("English") == "full"
        assert st.subtitle_role("") == "full"

    @pytest.mark.parametrize("flag, role", [("commentary", "commentary"),
                                            ("forced", "forced"),
                                            ("hearing_impaired", "sdh")])
    def test_a_flag_says_it(self, flag, role):
        assert st.subtitle_role("English", **{flag: True}) == role

    @pytest.mark.parametrize("name, role", [
        ("Director's Commentary", "commentary"),
        ("Audiokommentar", "commentary"),
        ("English Forced", "forced"),
        ("Français (forcés)", "forced"),
        ("English SDH", "sdh"),
        ("English (CC)", "sdh"),
        ("Slechthorenden", "sdh"),
    ])
    def test_a_name_says_it_where_no_flag_does(self, name, role):
        assert st.subtitle_role(name) == role

    def test_the_roles_are_asked_in_their_order(self):
        assert st.subtitle_role("Commentary", forced=True) == "commentary"
        assert st.subtitle_role("Forced SDH") == "forced"
        assert st.subtitle_role("SDH", forced=True) == "forced"

    def test_every_answer_is_a_role(self):
        for kwargs in ({}, {"forced": True}, {"commentary": True},
                       {"hearing_impaired": True}):
            assert st.subtitle_role("x", **kwargs) in enums.SUBTITLE_ROLES

    def test_cc_inside_a_word_is_not_closed_captions(self):
        assert st.subtitle_role("Occitan") == "full"


class TestSidecarName:
    def test_a_full_subtitle_carries_only_its_language(self):
        assert st.sidecar_name("/f/Film", "en") == "/f/Film.en.srt"

    @pytest.mark.parametrize("role", ["forced", "sdh"])
    def test_a_forced_or_sdh_one_says_so_after_it(self, role):
        assert st.sidecar_name("Film", "de", role) == "Film.de.%s.srt" % role

    def test_every_role_but_commentary_has_a_suffix(self):
        assert set(enums.SUBTITLE_ROLE_SUFFIXES) \
            == set(enums.SUBTITLE_ROLES) - {"commentary"}


class TestWhatTheTextSays:
    def test_bracketed_sounds_notes_and_speakers_are_sdh(self):
        cues = ["[door slams]", "Who's there?", "♪ soft music ♪",
                "DOCTOR: Sit down.", "(gasps)", "Fine."]
        assert st.is_sdh_text(cues)

    def test_plain_dialogue_with_the_odd_sign_is_not(self):
        cues = ["Who's there?", "Nobody.", "[HARBOUR, 1950]"] + ["Fine."] * 17
        assert not st.is_sdh_text(cues)

    def test_no_text_says_nothing(self):
        assert not st.is_sdh_text([])
        assert not st.is_sdh_text(["", "  "])


class TestRefiningARole:
    def test_a_forced_track_as_long_as_the_dialogue_is_the_whole_of_it(self):
        """A film in a foreign language flags its one subtitle forced."""
        assert st.refine_role("forced", 1000, 1100) == "full"

    def test_a_forced_track_of_signs_stays_forced(self):
        assert st.refine_role("forced", 57, 1082) == "forced"

    def test_an_unflagged_track_of_few_lines_is_forced(self):
        assert st.refine_role("full", 57, 1082) == "forced"

    def test_one_of_the_dialogue_s_length_stays_full(self):
        assert st.refine_role("full", 1082, 1342) == "full"

    @pytest.mark.parametrize("cues,reference", [(57, 0), (0, 1082),
                                                (20, 99), (1115, 10470)])
    def test_nothing_long_enough_to_measure_by_changes_nothing(
            self, cues, reference):
        assert st.refine_role("full", cues, reference) == "full"
        assert st.refine_role("forced", cues, reference) == "forced"

    def test_a_plain_track_whose_text_is_sdh_is_sdh(self):
        texts = ["[thunder]", "MAN: Run!"] * 5
        assert st.refine_role("full", texts=texts) == "sdh"

    def test_a_commentary_is_what_it_says(self):
        assert st.refine_role("commentary", 10, 1000,
                              texts=["[laughs]"]) == "commentary"

    def test_a_short_sdh_track_is_forced(self):
        """An SDH track of the signs alone is shown without being chosen."""
        assert st.refine_role("sdh", 30, 1000) == "forced"


@pytest.mark.fs
class TestReadingCues:
    def test_each_cue_s_lines_joined(self, tmp_path):
        srt = tmp_path / "a.srt"
        srt.write_text("﻿1\r\n00:00:01,000 --> 00:00:02,000\r\n"
                       "[thunder]\r\nRun!\r\n\r\n"
                       "2\r\n00:00:03,000 --> 00:00:04,000\r\nFine.\r\n",
                       encoding="utf-8", newline="")
        assert st.cue_texts(str(srt)) == ["[thunder]\nRun!", "Fine."]

    def test_a_missing_file_has_none(self, tmp_path):
        assert st.cue_texts(str(tmp_path / "missing.srt")) == []
