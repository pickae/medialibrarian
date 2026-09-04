"""Tests for medialib.lib.languages - the supported-language table and its lookups.

What is pinned here is what the rows MEAN: which spellings a lookup has to accept,
and the two places where accepting only the obvious one would silently recognise
nothing at all.
"""

import pytest

from medialib.lib import languages as lang


class TestTheTableItself:
    def test_every_row_has_every_field(self):
        for row in lang.LANGUAGES:
            assert row.code2 and row.code3 and row.sub_word and row.code3b
            assert row.keywords

    def test_two_letter_codes_are_two_letters_and_three_are_three(self):
        for row in lang.LANGUAGES:
            assert len(row.code2) == 2
            assert len(row.code3) == 3
            assert len(row.code3b) == 3

    def test_no_code_is_claimed_by_two_languages(self):
        """The lookups walk the table in order and take the first match, so a code
        in two rows would make the order load-bearing without saying so."""
        seen = []
        for row in lang.LANGUAGES:
            seen += [row.code2, row.code3, row.code3b]
        assert len(set(seen)) == len(set(seen))
        for row in lang.LANGUAGES:
            for other in lang.LANGUAGES:
                if other is row:
                    continue
                assert row.code2 not in (other.code2, other.code3, other.code3b)

    def test_only_three_languages_have_a_second_three_letter_code(self):
        """639-2/B differs from 639-2/T for exactly these; the rest repeat."""
        differing = {row.code2 for row in lang.LANGUAGES if row.code3 != row.code3b}
        assert differing == {"de", "fr", "nl"}

    def test_no_keyword_is_a_commentary_marker(self):
        """Field 4 says what a track is spoken IN, not what it is. A "commentary"
        keyword in the English row would stamp "eng" over every foreign
        commentary and rob the transcription of its one hint about the language."""
        for row in lang.LANGUAGES:
            for keyword in row.keywords:
                assert not any(marker in keyword for marker in lang.COMMENTARY_KEYWORDS)


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
    ])
    def test_every_row_answers_by_all_three_of_its_codes(self, tag, expected):
        assert lang.code_from_tag(tag) == expected

    @pytest.mark.parametrize("tag", ["pt", "por", "ja", "jpn", "zh", "sv"])
    def test_a_language_with_no_row_answers_nothing(self, tag):
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
    ])
    def test_the_english_name_in_any_case(self, name, expected):
        """Exactly the spelling whisper-ctranslate2 prints - "Detected language
        'Dutch'" - because it title-cases the same English names."""
        assert lang.code_from_name(name) == expected

    @pytest.mark.parametrize("name", ["Deutsch", "Nederlands", "Portuguese", "",
                                      "english ", " Dutch"])
    def test_anything_that_is_not_that_name_answers_nothing(self, name):
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
        for row in lang.LANGUAGES:
            codes |= {row.code2, row.code3, row.code3b}
        assert codes.isdisjoint(lang._NOT_A_LANGUAGE)

    @pytest.mark.parametrize("tag", ["", "null", "und", "mis", "qaa", "zxx"])
    def test_and_so_the_walk_would_answer_nothing_for_them_by_itself(self, tag):
        assert not any(tag in (row.code2, row.code3, row.code3b)
                       for row in lang.LANGUAGES)
