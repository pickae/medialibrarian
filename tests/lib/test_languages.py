"""Tests for medialib.lib.languages - the supported-language table and its lookups.

What is pinned here is what the rows MEAN: which spellings a lookup has to accept,
and the two places where accepting only the obvious one would silently recognise
nothing at all.
"""

import pytest

from medialib.lib import languages as lang
from medialib.lib import settings


class TestTheLanguagesFromISO:
    def test_every_recognised_language_has_every_field(self):
        for row in lang.recognised():
            assert row.code2 and row.code3 and row.sub_word and row.code3b
            assert row.keywords

    def test_two_letter_codes_are_two_letters_and_three_are_three(self):
        for row in lang.recognised():
            assert len(row.code2) == 2
            assert len(row.code3) == 3
            assert len(row.code3b) == 3

    def test_no_code_is_claimed_by_two_languages(self):
        rows = lang.recognised()
        assert len({row.code2 for row in rows}) == len(rows)
        spellings = [code for row in rows
                     for code in {row.code2, row.code3, row.code3b}]
        assert len(spellings) == len(set(spellings))

    def test_the_languages_with_a_second_three_letter_code(self):
        """639-2/B differs from 639-2/T for these of the named ones; the rest
        repeat their one code."""
        differing = {row.code2 for row in lang.recognised()
                     if row.code3 != row.code3b}
        assert differing == {"de", "fr", "nl", "cs", "ro", "el", "zh"}

    @pytest.mark.parametrize("code, code3, code3b, name", [
        ("pt", "por", "por", "Portuguese"), ("no", "nor", "nor", "Norwegian"),
        ("cs", "ces", "cze", "Czech"), ("ro", "ron", "rum", "Romanian"),
        ("el", "ell", "gre", "Greek"), ("zh", "zho", "chi", "Chinese"),
        ("ja", "jpn", "jpn", "Japanese"), ("de", "deu", "ger", "German"),
    ])
    def test_codes_and_the_name_whisper_spells(self, code, code3, code3b, name):
        row = lang.language(code)
        assert (row.code3, row.code3b, row.sub_word) == (code3, code3b, name)

    def test_a_language_outside_the_named_ones_is_still_a_language(self):
        row = lang.language("sk")
        assert (row.code3, row.sub_word) == ("slk", "Slovak")
        assert lang.same_language_tag("slo", "sk")
        assert lang.code3_from_label("Slovak") == "slk"

    def test_a_tag_is_a_language_only_where_the_collection_recognises_it(self):
        """The tail of a file name is asked, and "II" is a roman numeral
        there before it is Sichuan Yi."""
        assert lang.code_from_tag("slo") == ""
        assert lang.code_from_tag("ii") == ""
        assert lang.code_from_tag("iii") == ""

    def test_a_code_iso_does_not_have_is_no_language(self):
        assert lang.language("xx") is None

    @pytest.mark.parametrize("code, words", [
        ("en", {"english", "englisch", "anglais", "ingles"}),
        ("de", {"german", "deutsch", "allemand"}),
        ("fr", {"french", "français", "francais", "französisch"}),
        ("nl", {"dutch", "nederlands"}),
        ("es", {"spanish", "espagnol"}),
        ("it", {"italian", "italien"}),
        ("pt", {"portuguese", "português", "portugues"}),
        ("pl", {"polish", "polski"}), ("sv", {"swedish", "svenska"}),
        ("da", {"danish", "dansk"}), ("no", {"norwegian", "norsk"}),
        ("fi", {"finnish", "suomi"}), ("cs", {"czech", "cesky"}),
        ("hu", {"hungarian", "magyar"}), ("ro", {"romanian", "romana"}),
        ("el", {"greek", "ellinika"}), ("tr", {"turkish", "turkce"}),
        ("ru", {"russian", "русский"}),
        ("ja", {"japanese", "nihongo", "日本語"}),
        ("ko", {"korean", "hangugeo", "한국어"}),
        ("zh", {"chinese", "zhongwen", "中文"}),
    ])
    def test_the_names_a_track_has_always_been_read_by_are_still_read(
            self, code, words):
        assert words <= set(lang.language(code).keywords)

    def test_no_name_is_two_languages(self):
        seen = {}
        for row in lang.recognised():
            for keyword in row.keywords:
                assert seen.setdefault(keyword, row.code2) == row.code2, keyword

    def test_no_keyword_is_a_commentary_marker(self):
        """Keywords say what a track is spoken IN, not what it is. A
        "commentary" keyword would stamp a language over every foreign
        commentary and rob the transcription of its one hint about it."""
        for row in lang.recognised():
            for keyword in row.keywords:
                assert not any(marker in keyword
                               for marker in lang.COMMENTARY_KEYWORDS)


class TestWhetherATagSaysAnything:
    @pytest.mark.parametrize("tag", ["eng", "de", "zz", "por", "x"])
    def test_a_tag_with_content_is_real(self, tag):
        assert lang.is_real_language_tag(tag)

    @pytest.mark.parametrize("tag", ["", "null", "und", "mis", "qaa", "zxx"])
    def test_the_six_that_name_no_language(self, tag):
        """mkvmerge reports an unset language as "und" and a missing property
        renders as the literal "null"; the other three are uncoded, reserved and
        "no linguistic content"."""
        assert not lang.is_real_language_tag(tag)

    @pytest.mark.parametrize("tag", ["UND", "Null", "ZXX", "Mis"])
    def test_the_case_of_those_does_not_save_them(self, tag):
        assert not lang.is_real_language_tag(tag)

    def test_a_space_is_content_as_far_as_this_is_concerned(self):
        assert lang.is_real_language_tag(" ")


class TestACodeFromATag:
    @pytest.mark.parametrize("tag", ["de", "deu", "ger", "DE", "Deu", "GER"])
    def test_every_spelling_of_german_finds_german(self, tag):
        """A track muxed as "deu" reads back from mkvmerge as "ger", so a lookup
        on the fed code alone would recognise no German track at all."""
        assert lang.code_from_tag(tag) == "de"

    @pytest.mark.parametrize("tag,expected", [
        ("en", "en"), ("eng", "en"), ("fr", "fr"), ("fra", "fr"), ("fre", "fr"),
        ("nl", "nl"), ("nld", "nl"), ("dut", "nl"), ("es", "es"), ("spa", "es"),
        ("it", "it"), ("ita", "it"),
        ("pt", "pt"), ("por", "pt"), ("pl", "pl"), ("pol", "pl"),
        ("sv", "sv"), ("swe", "sv"), ("da", "da"), ("dan", "da"),
        ("no", "no"), ("nor", "no"), ("fi", "fi"), ("fin", "fi"),
        ("cs", "cs"), ("ces", "cs"), ("hu", "hu"), ("hun", "hu"),
        ("ro", "ro"), ("ron", "ro"), ("el", "el"), ("ell", "el"),
        ("tr", "tr"), ("tur", "tr"), ("ru", "ru"), ("rus", "ru"),
        ("ja", "ja"), ("jpn", "ja"), ("ko", "ko"), ("kor", "ko"),
        ("zh", "zh"), ("chi", "zh"), ("zho", "zh"),
    ])
    def test_every_row_answers_by_all_three_of_its_codes(self, tag, expected):
        assert lang.code_from_tag(tag) == expected

    @pytest.mark.parametrize("tag", ["yue", "xx", "zzz"])
    def test_a_tag_with_no_two_letter_language_answers_nothing(self, tag):
        assert lang.code_from_tag(tag) == ""

    @pytest.mark.parametrize("tag", ["und", "null", "zxx", "", "mis", "qaa"])
    def test_and_neither_does_a_tag_that_names_no_language(self, tag):
        assert lang.code_from_tag(tag) == ""

    @pytest.mark.parametrize("tag", ["engx", "en-GB", "ge", "du", " en"])
    def test_a_near_miss_is_a_miss(self, tag):
        """Matched whole, so a code with anything on either end is not that code."""
        assert lang.code_from_tag(tag) == ""


class TestACodeFromAName:
    @pytest.mark.parametrize("name,expected", [
        ("English", "en"), ("german", "de"), ("FRENCH", "fr"),
        ("Dutch", "nl"), ("spanish", "es"), ("Italian", "it"),
        ("Portuguese", "pt"), ("japanese", "ja"), ("Czech", "cs"),
        ("Hungarian", "hu"), ("Korean", "ko"),
    ])
    def test_the_english_name_in_any_case(self, name, expected):
        """Exactly the spelling whisper-ctranslate2 prints - "Detected language
        'Dutch'" - because it title-cases the same English names."""
        assert lang.code_from_name(name) == expected

    @pytest.mark.parametrize("name", ["Deutsch", "Nederlands", "Magyar", "",
                                      "english ", " Dutch"])
    def test_anything_that_is_not_that_name_answers_nothing(self, name):
        """The native name is not the English one: "Magyar" is Hungarian, and
        the lookup answers by the name whisper prints, not by the keywords."""
        assert lang.code_from_name(name) == ""

    def test_a_two_letter_code_is_not_a_name(self):
        assert lang.code_from_name("en") == ""


class TestRecognisingACommentary:
    @pytest.mark.parametrize("name", [
        "Commentary", "commentary", "COMMENTARY", "Audio Commentary",
        "Director's Commentary", "Commentary with the cast",
    ])
    def test_the_english_spellings(self, name):
        assert lang.is_commentary_name(name)

    @pytest.mark.parametrize("name", [
        "Audiokommentar", "Kommentar von Regisseur", "Commentaar",
        "Commentaire audio", "Comentario del director", "Commento del regista",
    ])
    def test_a_name_says_it_in_the_language_of_its_disc(self, name):
        """A Matroska should say this with the commentary FLAG, and many do, but
        plenty only say it in the name. Anything not recognised here gets no flag
        and no transcript, so this list is what "every commentary track" means."""
        assert lang.is_commentary_name(name)

    def test_matched_as_a_substring_and_not_as_a_word(self):
        assert lang.is_commentary_name("Director's Commentary Track 2")
        assert lang.is_commentary_name("xxcommentxx")

    @pytest.mark.parametrize("name", [
        "Audiosommentary 2", "Audiolkommentar", "Comentary", "Commmentary 3",
        "Kommentra 1", "Commentray", "Audio Comentario", "Comento",
    ])
    def test_a_long_word_spelled_a_letter_wrong_still_says_it(self, name):
        """A rip's track names are typed by hand, and a commentary missed here
        is one never transcribed. The slip is the one a film's title may
        have."""
        assert lang.is_commentary_name(name)

    @pytest.mark.parametrize("name", ["Documentary", "Momentary",
                                      "Elementary", "Monetary", "Cmmntry"])
    def test_a_word_two_letters_off_is_another_word(self, name):
        assert not lang.is_commentary_name(name)

    @pytest.mark.parametrize("name", ["English", "Deutsch 5.1", "Original", "",
                                      "Isolated Score", "coment"])
    def test_a_name_that_marks_nothing(self, name):
        assert not lang.is_commentary_name(name)

    def test_the_shortest_keyword_is_what_makes_the_longer_ones_redundant(self):
        """"comment" is inside "commentary" and "commentaire", so those two are
        found by the English keyword alone - the list is longer than it strictly
        needs to be because the foreign words that are NOT are the point."""
        assert lang.is_commentary_name("comment")


class TestTheCaseFolding:
    def test_the_one_codepoint_the_two_languages_disagree_about(self):
        """U+0130, the dotted capital I. A track name is arbitrary text off a disc,
        so it can hold anything - and Python's own lower() leaves a combining dot
        behind, which none of the keywords can match."""
        assert lang.is_commentary_name("KOMMENTAR İ")
        assert not lang.is_commentary_name("MÜZİK")


class TestTheGuardThatDecidesNothing:
    """``code_from_tag`` asks ``is_real_language_tag`` before it walks the table,
    and removing that check would change no answer at all: none of the six tags
    that name no language is a code in any row, so the walk returns "" for them
    whether the guard ran or not.

    The check is there to say what it means, not to decide anything. The assertion
    below is what keeps that true - a language whose code collided with a sentinel
    would make the guard load-bearing without anybody noticing.
    """

    def test_no_tag_that_names_no_language_is_also_somebodys_code(self):
        codes = set()
        for row in lang.recognised():
            codes |= {row.code2, row.code3, row.code3b}
        assert codes.isdisjoint(lang._NOT_A_LANGUAGE)

    @pytest.mark.parametrize("tag", ["", "null", "und", "mis", "qaa", "zxx"])
    def test_and_so_the_walk_would_answer_nothing_for_them_by_itself(self, tag):
        assert not any(tag in (row.code2, row.code3, row.code3b)
                       for row in lang.recognised())


class TestSpeaks:
    """Whether a track is spoken in the language a catalogue names by its
    ISO 639-1 code - the film's original language, as TMDb gives it."""

    def test_the_bcp47_tag_places_any_language(self):
        assert lang.speaks("jpn", "ja", "ja")

    def test_a_region_on_it_changes_nothing(self):
        assert lang.speaks("ger", "de-CH", "de")

    def test_another_language_is_not_it(self):
        assert not lang.speaks("ger", "de", "ja")

    @pytest.mark.parametrize("tag", ["ger", "deu", "de"])
    def test_without_one_the_legacy_tag_is_read_by_the_table(self, tag):
        assert lang.speaks(tag, "null", "de")

    def test_an_undetermined_bcp47_tag_falls_back_to_the_legacy_one(self):
        assert lang.speaks("fre", "und", "fr")

    @pytest.mark.parametrize("code", ["cn", "zh"])
    @pytest.mark.parametrize("ietf", ["yue", "cmn", "zh", "zh-Hant-HK",
                                      "null"])
    def test_every_chinese_tag_is_chinese(self, code, ietf):
        """TMDb's "cn" is Cantonese and "zh" Mandarin; discs say "chi" for
        both, or "yue" and "cmn" when they are precise."""
        assert lang.speaks("chi", ietf, code)

    @pytest.mark.parametrize("language, ietf", [("nob", "nb"), ("nno", "nn"),
                                                ("nor", "no")])
    def test_tmdb_s_norwegian_is_every_written_norwegian(self, language,
                                                         ietf):
        assert lang.speaks(language, ietf, "no")

    def test_a_language_with_no_family_is_only_itself(self):
        assert not lang.speaks("jpn", "ja", "ko")

    def test_no_code_is_no_language(self):
        assert not lang.speaks("eng", "en", "")


class TestWantedSubtitles:
    """The ``spokenLanguages`` setting read by
    :func:`medialib.lib.languages.spoken_languages`: the languages a run
    fetches and tests subtitles for, in the order the setting names them."""

    SHIPPED = ["en", "de", "fr", "nl", "es", "it"]

    def test_the_shipped_settings_file_names_the_languages_of_the_owner(self):
        assert settings.shipped()["spokenLanguages"] == ",".join(self.SHIPPED)

    def test_the_setting_unset_is_what_the_shipped_file_says(
            self, monkeypatch):
        monkeypatch.delenv("spokenLanguages", raising=False)
        assert [row.code2 for row in lang.spoken_languages()] == self.SHIPPED

    def test_an_empty_setting_is_the_shipped_value_too(self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "")
        assert [row.code2 for row in lang.spoken_languages()] == self.SHIPPED

    def test_the_setting_s_order_is_the_order_the_sources_are_asked_in(
            self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "pt,ja")
        assert [row.code2 for row in lang.spoken_languages()] == ["pt", "ja"]

    def test_any_iso_language_may_be_named(self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "sk,af")
        assert [row.code2 for row in lang.spoken_languages()] == ["sk", "af"]

    def test_a_code_named_twice_is_one_language(self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "pt, pt ,PT,en")
        assert [row.code2 for row in lang.spoken_languages()] == ["pt", "en"]

    def test_the_case_of_a_code_does_not_matter(self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "ZH,En")
        assert [row.code2 for row in lang.spoken_languages()] == ["zh", "en"]

    def test_a_code_iso_does_not_know_is_refused(self, monkeypatch):
        from medialib.lib import clioptions
        monkeypatch.setenv("spokenLanguages", "xx")
        with pytest.raises(clioptions.UsageError) as refused:
            lang.spoken_languages()
        assert '"xx"' in refused.value.message

    def test_a_three_letter_code_is_not_a_two_letter_one(self, monkeypatch):
        from medialib.lib import clioptions
        monkeypatch.setenv("spokenLanguages", "deu")
        with pytest.raises(clioptions.UsageError):
            lang.spoken_languages()

    def test_every_unknown_code_is_named(self, monkeypatch):
        from medialib.lib import clioptions
        monkeypatch.setenv("spokenLanguages", "xx,yy")
        with pytest.raises(clioptions.UsageError) as refused:
            lang.spoken_languages()
        assert '"xx"' in refused.value.message
        assert '"yy"' in refused.value.message

    def test_a_language_the_user_speaks_is_one_a_track_name_is_read_for(
            self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "en,sk")
        assert "sk" in {row.code2 for row in lang.recognised()}
        assert "slovak" in lang.language("sk").keywords


class TestTakingTheForcedWordOut:
    @pytest.mark.parametrize("name,rest", [
        ("English (Forced)", "English"),
        ("Forced", ""),
        ("English - Forced", "English"),
        ("English [forced]", "English"),
        ("English", "English"),
    ])
    def test_the_word_and_what_was_around_it(self, name, rest):
        assert lang.without_forced_word(name) == rest
