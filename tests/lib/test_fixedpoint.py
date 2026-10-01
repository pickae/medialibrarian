"""Tests for medialib.lib.fixedpoint - repeating a pass until it stops
changing anything."""

import pytest

from medialib.lib.fixedpoint import until_stable

pytestmark = pytest.mark.pure


def passes(*answers):
    """A step answering <answers> in turn, and the list of calls it saw."""
    calls = []

    def step():
        calls.append(len(calls))
        return answers[len(calls) - 1]
    return step, calls


class TestUntilStable:

    def test_an_input_already_stable_is_passed_over_once(self):
        step, calls = passes(0)
        assert until_stable(step) == 0
        assert len(calls) == 1

    def test_it_stops_at_the_first_pass_that_changes_nothing(self):
        step, calls = passes(3, 1, 0, 5)
        assert until_stable(step) == 2
        assert len(calls) == 3

    def test_a_bool_answer_is_as_good_as_a_count(self):
        step, _calls = passes(True, False)
        assert until_stable(step) == 1

    def test_the_limit_stops_a_pass_that_never_settles(self):
        step, calls = passes(*[1] * 10)
        assert until_stable(step, 4) == 4
        assert len(calls) == 4

    def test_a_pass_settling_within_the_limit_is_not_cut_short(self):
        step, calls = passes(1, 0)
        assert until_stable(step, 4) == 1
        assert len(calls) == 2
