"""Tests for medialib.lib.prefixes - the ports of the group prefix passes.

These pin the contract in its own right - what the behaviour is FOR, and the
padding-insensitive comparison in particular, because that is the rule the pass
exists to survive.
"""

import pytest

from medialib.lib.prefixes import normalize_prefix_padding as pad
from medialib.lib.prefixes import wipe_doubled_prefixes as wipe
from medialib.lib.prefixes import wipe_uniform_prefixes as uniform

pytestmark = pytest.mark.pure


def wipe_all(prefixes, cores):
    """The common case: every sibling is in the group."""
    return wipe(prefixes, cores, list(range(len(prefixes))))


def pad_all(prefixes, cores):
    return pad(prefixes, cores, list(range(len(prefixes))))


def uniform_all(prefixes, cores=None):
    """Cores default to something distinct and unlike the prefix, so the
    bare-core guard never trips and the identical-prefix rule is tested alone."""
    cores = cores if cores is not None else [f"name{i}" for i in range(len(prefixes))]
    return uniform(prefixes, cores, list(range(len(prefixes))))


class TestWipeWhenTheWholeGroupIsDoubled:
    """The outer copy says nothing the core does not, so it goes."""

    def test_a_numbered_run(self):
        assert wipe_all(["1", "2", "3"], ["1 A", "2 B", "3 test"]) == ["", "", ""]

    def test_the_core_can_be_the_bare_number(self):
        assert wipe_all(["1", "2"], ["1", "2"]) == ["", ""]

    def test_a_single_member_group(self):
        assert wipe_all(["5"], ["5 solo"]) == [""]

    def test_a_non_numeric_prefix_matches_as_text(self):
        assert wipe_all(["CD", "CD"], ["CD one", "CD two"]) == ["", ""]


class TestWipeIgnoresPaddingForNumbers:
    """Our own padding pass widens the outer prefix; this pass must survive it."""

    def test_a_widened_outer_prefix_is_still_doubled(self):
        assert wipe_all(["01", "02"], ["1 A", "2 B"]) == ["", ""]

    def test_padding_may_differ_per_side_and_per_member(self):
        assert wipe_all(["1", "02"], ["01 A", "2 B"]) == ["", ""]

    def test_zero_pads_to_zero(self):
        assert wipe_all(["00"], ["0 A"]) == [""]

    def test_a_longer_number_is_a_different_number(self):
        assert wipe_all(["1", "1"], ["12 A", "1 B"]) == ["1", "1"]


class TestWipeIsAllOrNothing:
    """A partial strip would desynchronise the group's numbering."""

    def test_one_undoubled_member_spares_the_whole_group(self):
        assert wipe_all(["1", "2"], ["1 A", "B"]) == ["1", "2"]

    def test_an_empty_prefix_anywhere_blocks_the_wipe(self):
        assert wipe_all(["", "1"], ["A", "1 B"]) == ["", "1"]

    def test_nothing_is_doubled(self):
        assert wipe_all(["1", "2", "3"], ["A", "B", "test"]) == ["1", "2", "3"]


class TestWipeOverASubsetOfTheSiblings:
    """Only the plurality filetype takes part; the rest must come back untouched."""

    def test_members_outside_the_group_are_left_alone(self):
        prefixes = ["1", "2", "9"]
        cores = ["1 A", "2 B", "unrelated"]
        assert wipe(prefixes, cores, [0, 1]) == ["", "", "9"]

    def test_a_member_outside_the_group_cannot_block_the_wipe(self):
        assert wipe(["1", "2", ""], ["1 A", "2 B", "C"], [0, 1]) == ["", "", ""]

    def test_an_empty_group_changes_nothing(self):
        assert wipe(["1", "2"], ["1 A", "2 B"], []) == ["1", "2"]


class TestWipeDoesNotModifyItsInput:
    def test_the_caller_s_list_survives(self):
        prefixes = ["1", "2"]
        wipe_all(prefixes, ["1 A", "2 B"])
        assert prefixes == ["1", "2"]


class TestPaddingToTheLargestValue:
    """One rule, both directions: the width is the digit count of the largest."""

    def test_under_padded_prefixes_are_widened(self):
        assert pad_all(["8", "9", "10"], ["A", "B", "C"])[0] == ["08", "09", "10"]

    def test_excess_padding_is_stripped(self):
        assert pad_all(["007", "008"], ["A", "B"])[0] == ["7", "8"]

    def test_a_three_digit_run(self):
        assert pad_all(["99", "100"], ["A", "B"])[0] == ["099", "100"]

    def test_a_run_need_not_start_at_one(self):
        assert pad_all(["8", "9"], ["A", "B"])[0] == ["8", "9"]

    def test_a_run_may_start_at_zero(self):
        assert pad_all(["0", "1"], ["A", "B"])[0] == ["0", "1"]

    def test_padding_that_differs_per_member_is_normalised(self):
        # the series is read by VALUE, so how each member was padded before does
        # not decide whether it is one
        assert pad_all(["0030", "31", "032", "33"],
                       ["A", "B", "C", "D"])[0] == ["30", "31", "32", "33"]

    def test_a_series_already_at_the_right_width_is_a_no_op(self):
        # consecutive and same-width: nothing to widen and nothing to strip
        assert pad_all(["2019", "2020", "2021"],
                       ["A", "B", "C"])[0] == ["2019", "2020", "2021"]


class TestPaddingOnlyAnActualNumberingSeries:
    """Unrelated numbers are not chapters, and must come back untouched."""

    def test_years_are_left_alone(self):
        assert pad_all(["2019", "2021", "2024"], ["A", "B", "C"])[0] == ["2019", "2021", "2024"]

    def test_a_gap_disqualifies_the_group(self):
        assert pad_all(["1", "2", "4"], ["A", "B", "C"])[0] == ["1", "2", "4"]

    def test_a_repeated_number_disqualifies_the_group(self):
        assert pad_all(["1", "01"], ["A", "B"])[0] == ["1", "01"]

    def test_a_non_numeric_prefix_disqualifies_the_group(self):
        assert pad_all(["05-12", "2"], ["A", "B"])[0] == ["05-12", "2"]

    def test_an_empty_prefix_disqualifies_the_group(self):
        assert pad_all(["", "1"], ["A", "B"])[0] == ["", "1"]

    def test_a_lone_member_is_not_a_series(self):
        assert pad_all(["007"], ["A"])[0] == ["007"]


class TestPaddingACoreThatIsNothingButItsPrefix:
    """It is repadded in lockstep, so the caller's later collapse still fires."""

    def test_the_core_follows_the_prefix(self):
        prefixes, cores = pad_all(["8", "9", "10"], ["8", "B", "C"])
        assert prefixes == ["08", "09", "10"]
        assert cores == ["08", "B", "C"]

    def test_a_core_that_merely_starts_with_the_prefix_does_not(self):
        prefixes, cores = pad_all(["8", "9", "10"], ["8 A", "B", "C"])
        assert prefixes == ["08", "09", "10"]
        assert cores == ["8 A", "B", "C"]


class TestPaddingOverASubsetOfTheSiblings:
    """Only the plurality filetype takes part; the rest must come back untouched."""

    def test_members_outside_the_group_are_left_alone(self):
        prefixes, cores = pad(["8", "9", "10", "007"], ["A", "B", "C", "D"], [0, 1, 2])
        assert prefixes == ["08", "09", "10", "007"]
        assert cores == ["A", "B", "C", "D"]

    def test_a_member_outside_the_group_cannot_disqualify_it(self):
        prefixes, _ = pad(["8", "9", "not a number"], ["A", "B", "C"], [0, 1])
        assert prefixes == ["8", "9", "not a number"]

    def test_an_empty_group_changes_nothing(self):
        assert pad(["8", "9"], ["A", "B"], [])[0] == ["8", "9"]


class TestPaddingDoesNotModifyItsInput:
    def test_the_caller_s_lists_survive(self):
        prefixes, cores = ["8", "9", "10"], ["8", "B", "C"]
        pad_all(prefixes, cores)
        assert prefixes == ["8", "9", "10"]
        assert cores == ["8", "B", "C"]


class TestUniformWipeWhenThePrefixDistinguishesNothing:
    def test_an_identical_year_goes(self):
        assert uniform_all(["2024", "2024", "2024"]) == ["", "", ""]

    def test_an_identical_padded_number_goes(self):
        assert uniform_all(["01", "01", "01"]) == ["", "", ""]

    def test_one_differing_prefix_keeps_them_all(self):
        assert uniform_all(["05", "05", "06"]) == ["05", "05", "06"]

    def test_a_lone_member_keeps_its_only_identifying_text(self):
        assert uniform_all(["2024"]) == ["2024"]

    def test_already_empty_is_a_no_op(self):
        assert uniform_all(["", "", ""]) == ["", "", ""]


class TestUniformWipeKeepsADate:
    """A date says when the item is FROM, which is about the item, not about the
    group - and small groups share one legitimately."""

    def test_an_identical_date_is_kept(self):
        assert uniform_all(["20260728", "20260728"]) == ["20260728", "20260728"]

    def test_it_is_the_date_shape_that_is_protected_not_the_length(self):
        # eight digits, but no year starts with a 3
        assert uniform_all(["30260728", "30260728"]) == ["", ""]

    def test_a_bare_year_is_a_plain_number_and_goes(self):
        assert uniform_all(["2026", "2026"]) == ["", ""]

    def test_six_digits_is_a_plain_number_and_goes(self):
        assert uniform_all(["202607", "202607"]) == ["", ""]


class TestUniformWipeKeepsAMemberThatIsNothingButItsPrefix:
    def test_an_empty_core_anywhere_blocks_the_wipe(self):
        # "10", "10 A", "10 B": wiping would strand the first with no name at all
        assert uniform_all(["10", "10", "10"], ["", "A", "B"]) == ["10", "10", "10"]

    def test_a_core_that_merely_equals_the_prefix_does_not(self):
        # that member keeps "10" as its name, so the group collapses consistently
        assert uniform_all(["10", "10", "10"], ["10", "A", "B"]) == ["", "", ""]


class TestUniformWipeOverASubsetOfTheSiblings:
    def test_members_outside_the_group_are_left_alone(self):
        assert uniform(["7", "7", "9"], ["A", "B", "C"], [0, 1]) == ["", "", "9"]

    def test_a_member_outside_the_group_cannot_block_the_wipe(self):
        assert uniform(["7", "7", "8"], ["A", "B", ""], [0, 1]) == ["", "", "8"]

    def test_a_group_of_one_is_not_a_group(self):
        assert uniform(["7", "7"], ["A", "B"], [0]) == ["7", "7"]


class TestUniformWipeDoesNotModifyItsInput:
    def test_the_caller_s_list_survives(self):
        prefixes = ["2024", "2024"]
        uniform_all(prefixes)
        assert prefixes == ["2024", "2024"]
