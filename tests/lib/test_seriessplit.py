"""medialib/lib/seriessplit.py: finding where one episode ends and the next
begins, without a decoder or a model - each stood in for."""

import pytest

from medialib.lib import scenecuts, seriessplit

pytestmark = pytest.mark.pure


def _frames(*blacks, start=0.0, end=600.0):
    times = [start + step * 0.5 for step in range(int((end - start) * 2))]
    return [scenecuts.Frame(time, 0.0, any(abs(time - black) < 0.3
                                           for black in blacks))
            for time in times]


class TestWhereToLook:
    def test_the_runtimes_are_scaled_to_the_file(self):
        assert seriessplit.expected_seams([1500, 1500], 2800) == [1400]
        assert seriessplit.expected_seams([600, 1200, 600], 2400) \
            == [600, 1800]

    def test_nothing_to_go_by(self):
        assert seriessplit.expected_seams([], 2800) == []


class TestTheCandidates:
    def test_black_meeting_silence_comes_first(self):
        found = seriessplit.candidates_near(
            1400.0, [], _frames(1380.0, 1410.0, start=1250, end=1550),
            [1410.4, 1300.0])
        assert found[0] == seriessplit.Candidate(1410.0, "black+silence")

    def test_a_chapter_mark_near_the_seam_is_a_candidate(self):
        found = seriessplit.candidates_near(1400.0, [1395.0, 200.0],
                                            _frames(start=1250, end=1550), [])
        assert seriessplit.Candidate(1395.0, "chapter") in found


class TestThePlan:
    def test_every_seam_settled_is_cut(self):
        plan = seriessplit.plan_split(
            2800, [1500, 1500], [],
            lambda start, span: (_frames(1410.0, start=start,
                                         end=start + span), [1410.2]),
            lambda expected, found: found[0])
        assert plan.cuts == (1410.0,)

    def test_an_unsure_seam_leaves_the_file_whole(self):
        plan = seriessplit.plan_split(
            2800, [1500, 1500], [],
            lambda start, span: (_frames(1410.0, start=start,
                                         end=start + span), [1410.2]),
            lambda expected, found: None)
        assert plan.cuts == ()
        assert "could not be told" in plan.reason


class TestTheModelsAnswer:
    @pytest.mark.parametrize("answer,pick", [
        ('{"pick": 2}', 1), ('{"pick": "1"}', 0), ('{"pick": "unsure"}', None),
        ('{"pick": 9}', None), ("no idea", None),
    ])
    def test_only_a_candidate_it_was_shown(self, answer, pick):
        assert seriessplit.parse_pick(answer, 2) == pick

    def test_the_prompt_shows_what_is_said_either_side(self):
        text = seriessplit.prompt(
            1400.0, [seriessplit.Candidate(1410.0, "black+silence")],
            lambda start, end: "before" if end <= 1410.0 else "after")
        assert "Before: before" in text and "After: after" in text
