"""Tests for medialib.lib.subtitletracks - a subtitle's form and role."""

import pytest

from medialib.lib import enums, subtitletracks as st

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
