"""Tests for medialib.lib.affix - shrinking a shared affix so removing it
cannot cut a word or a number in half.

Why the rule exists is what needs saying, and the rule is subtle enough that the
examples are the documentation: an aligner that is one character out turns
"Alpha"/"Beta" into "Alph"/"Bet" and nobody notices until a library is renamed.
"""

import pytest

from medialib.lib.affix import affix_char_class as klass
from medialib.lib.affix import align_affix_to_run_boundary as align

pytestmark = pytest.mark.pure


class TestCharacterClasses:
    def test_ascii_digits_are_d(self):
        assert [klass(c) for c in "0123456789"] == ["D"] * 10

    def test_ascii_letters_are_l(self):
        assert klass("a") == "L"
        assert klass("Z") == "L"

    def test_separators_are_o(self):
        assert [klass(c) for c in " -_.,;:/\\()[]"] == ["O"] * 13

    def test_an_accented_letter_is_a_letter(self):
        assert klass("é") == "L"
        assert klass("Æ") == "L"

    def test_an_en_dash_is_a_letter_too(self):
        # Not ASCII punctuation, so bash's classifier calls it a letter. It is in
        # the separator vocabulary the name cleaners actually run over, so this is
        # a live quirk rather than a theoretical one.
        assert klass("–") == "L"


class TestNothingToProtect:
    def test_an_empty_affix_is_returned_as_is(self):
        assert align("prefix", "", ["anything"]) == ""

    def test_an_affix_ending_on_a_separator_is_already_clean(self):
        assert align("prefix", "01 ", ["01 alpha", "01 beta"]) == "01 "

    def test_a_suffix_starting_on_a_separator_is_already_clean(self):
        assert align("suffix", " - show", ["alpha - show", "beta - show"]) == " - show"

    def test_an_affix_that_splits_nothing_survives_whole(self):
        # the character past the affix is a separator in every name, so the cut is
        # already at a boundary
        assert align("prefix", "disc", ["disc 1", "disc 2"]) == "disc"


class TestTheAffixRetreats:
    def test_it_keeps_a_word_whole(self):
        assert align("suffix", "a - show", ["alpha - show", "beta - show"]) == " - show"

    def test_it_keeps_a_number_whole(self):
        assert align("prefix", "1", ["11 title", "12 title"]) == ""

    def test_a_mixed_token_is_cut_where_the_class_changes(self):
        assert align("prefix", "a1", ["a16z", "a17z"]) == "a"

    def test_and_the_letter_at_the_other_end_is_removable(self):
        assert align("suffix", "z", ["a16z", "a17z"]) == "z"

    def test_the_whole_affix_can_retreat_away(self):
        assert align("prefix", "di", ["die", "din"]) == ""

    def test_a_letter_shared_by_three_pure_words_retreats(self):
        assert align("prefix", "l", ["last", "lost", "list"]) == ""

    def test_the_letter_past_the_affix_may_be_accented(self):
        # the retreat is about what the affix ABUTS, and an accented letter is a
        # letter - so this is inside a word and goes
        assert align("prefix", "caf", ["caf\u00e9", "cafx"]) == ""


class TestWhichNamesGetAVote:
    def test_a_name_the_affix_covers_entirely_is_skipped(self):
        # "di" covers "di" completely, so it cannot be split and does not block
        # the retreat that "die" forces
        assert align("prefix", "di", ["die", "di"]) == ""

    def test_a_suffix_covering_a_whole_short_name_retreats_too(self):
        assert align("suffix", "ic", ["eic", "ic"]) == ""

    def test_one_split_name_is_enough_to_retreat(self):
        assert align("prefix", "ab", ["abc", "ab-1", "ab-2"]) == ""

    def test_no_names_at_all_means_no_split(self):
        assert align("prefix", "ab", []) == "ab"


class TestASeparatorIsAlwaysACleanEdge:
    """Every delimiter is class O, so an affix that ends on one - or ends right
    before one - cuts at a boundary and is left exactly as given."""

    @pytest.mark.parametrize("affix", ["abc-", "abc_", "abc.", "abc("])
    def test_a_prefix_ending_on_a_delimiter(self, affix):
        assert align("prefix", affix, [affix + "x", affix + "y"]) == affix

    def test_a_prefix_ending_right_before_one(self):
        assert align("prefix", "abc", ["abc-x", "abc-y"]) == "abc"

    @pytest.mark.parametrize("affix", ["-abc", "_abc", ".abc"])
    def test_a_suffix_starting_on_a_delimiter(self, affix):
        assert align("suffix", affix, ["x" + affix, "y" + affix]) == affix

    def test_a_number_against_a_delimiter_is_not_split(self):
        # what borders the cut is the separator, not the number
        assert align("prefix", "12-", ["12-x", "12-y"]) == "12-"
        assert align("suffix", "-12", ["x-12", "y-12"]) == "-12"


class TestTheAsciiOnlyRetreat:
    """The class test accepts any letter; the retreat only walks back over ASCII
    ones. So an affix ending in an accented letter finds a split and then retreats
    past nothing. bash does this, so the port does."""

    def test_an_accented_boundary_retreats_no_further(self):
        assert align("prefix", "abcé", ["abcéx", "abcéy"]) == "abcé"

    def test_where_an_ascii_boundary_would_have_gone_back(self):
        assert align("prefix", "abcd", ["abcdx", "abcdy"]) == ""


class TestTheSideArgument:
    def test_anything_that_is_not_prefix_means_suffix(self):
        # bash tests `== prefix`, so this is the contract rather than an accident
        assert align("suffix", "ow", ["show", "grow"]) == align("anything", "ow", ["show", "grow"])
