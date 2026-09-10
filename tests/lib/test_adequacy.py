"""The white box for medialib/lib/adequacy.py.

What is pinned here is the vocabulary three models share: which side of each
boundary a figure falls on, that a missing figure is never an extreme, and that
the lossless floor lifts exactly one verdict and leaves the other two alone.
"""

import pytest

from medialib.lib import adequacy

pytestmark = pytest.mark.pure


class TestTheVerdict:
    @pytest.mark.parametrize("measured,expected", [
        # the requirement is 100 throughout
        ("1", adequacy.STARVED),
        ("99", adequacy.STARVED),
        ("99.999", adequacy.STARVED),
        ("100", adequacy.ADEQUATE),
        ("101", adequacy.ADEQUATE),
        ("199.999", adequacy.ADEQUATE),
        ("200", adequacy.GENEROUS),
        ("100000", adequacy.GENEROUS),
    ])
    def test_the_two_boundaries_are_inclusive_from_below(self, measured,
                                                         expected):
        """Exactly adequate is adequate and exactly twice it is generous: a
        figure sitting on a boundary belongs to the better verdict, which is what
        keeps a file encoded at precisely the target from being called short of
        it."""
        assert adequacy.verdict(measured, "100") == expected

    @pytest.mark.parametrize("measured,adequate", [
        ("", "100"), ("100", ""), ("", ""),
        ("0", "100"), ("100", "0"),
        ("-5", "100"), ("100", "-5"),
        ("not a number", "100"), ("100", "not a number"),
        (None, "100"), ("100", None),
    ])
    def test_a_figure_that_is_not_a_positive_number_cannot_be_judged(
            self, measured, adequate):
        """Unknown and not an extreme. A file nobody could measure must not read
        as starved, because every caller here skips a starved file and none of
        them means to skip an unreadable one."""
        assert adequacy.verdict(measured, adequate) == adequacy.UNKNOWN

    def test_the_generous_factor_can_be_moved(self):
        assert adequacy.verdict("300", "100", 4) == adequacy.ADEQUATE
        assert adequacy.verdict("400", "100", 4) == adequacy.GENEROUS

    def test_a_factor_of_nothing_leaves_generous_unreachable(self):
        """Rather than making every adequate file generous, which is what a
        multiplication by zero would do."""
        assert adequacy.verdict("10000", "100", 0) == adequacy.ADEQUATE

    def test_the_numbers_are_read_the_way_awk_reads_them(self):
        """They arrive as text from probes and tables alike, and a trailing unit
        is a thing a probe prints."""
        assert adequacy.verdict(" 250 ", "100") == adequacy.GENEROUS


class TestTheLosslessFloor:
    def test_starved_becomes_adequate(self):
        assert adequacy.at_least_adequate(adequacy.STARVED) == adequacy.ADEQUATE

    @pytest.mark.parametrize("name", [adequacy.ADEQUATE, adequacy.GENEROUS,
                                      adequacy.UNKNOWN])
    def test_and_nothing_else_moves(self, name):
        """Including unknown: a lossless file nobody could measure is still a
        file nobody could measure, and claiming it is adequate would be a
        judgement made on no reading at all."""
        assert adequacy.at_least_adequate(name) == name


class TestIsStarved:
    def test_only_the_word_itself(self):
        assert adequacy.is_starved(adequacy.STARVED)

    @pytest.mark.parametrize("name", [adequacy.ADEQUATE, adequacy.GENEROUS,
                                      adequacy.UNKNOWN, ""])
    def test_and_never_the_others(self, name):
        assert not adequacy.is_starved(name)


class TestTheVocabularyItself:
    def test_the_three_verdicts_are_worst_first(self):
        assert adequacy.VERDICTS == (adequacy.STARVED, adequacy.ADEQUATE,
                                     adequacy.GENEROUS)

    def test_unknown_is_not_one_of_them(self):
        """It is the absence of a verdict, not a fourth one, so a caller
        iterating the real ones never has to filter it out."""
        assert adequacy.UNKNOWN not in adequacy.VERDICTS
