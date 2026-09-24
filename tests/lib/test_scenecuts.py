"""Tests for medialib.lib.scenecuts - which picture change a chunk cut moves to.

The frames here are handed straight to the choice, so what is pinned is the
choice itself; what ffmpeg reports about a real picture is
tests/lib/test_scenecuts_media.py.
"""

import pytest

from medialib.lib import scenecuts
from medialib.lib.scenecuts import Frame

pytestmark = pytest.mark.pure

STEP = 1 / 24


def _frames(start, count, cuts=(), blacks=()):
    """``count`` frames at 24 fps from ``start``; the indexes in ``cuts`` score
    as a hard cut and those in ``blacks`` are black."""
    return [Frame(start + index * STEP, 40.0 if index in cuts else 0.5,
                  index in blacks) for index in range(count)]


class TestPickCut:
    def test_nothing_changing_is_no_cut(self):
        assert scenecuts.pick_cut(_frames(90, 480), 100) is None

    def test_a_hard_cut_is_found(self):
        frames = _frames(90, 480, cuts={100})
        seconds, kind = scenecuts.pick_cut(frames, 100)
        assert kind == "scene"
        assert frames[99].time < seconds < frames[100].time

    def test_black_is_found(self):
        _seconds, kind = scenecuts.pick_cut(_frames(90, 480, blacks={300}), 100)
        assert kind == "black"

    def test_the_nearest_change_wins_whatever_it_is(self):
        frames = _frames(90, 480, cuts={20}, blacks={250})
        seconds, kind = scenecuts.pick_cut(frames, 100)
        assert kind == "black"
        assert frames[249].time < seconds < frames[250].time

    def test_a_score_under_the_threshold_is_motion_not_a_cut(self):
        frames = [f._replace(score=scenecuts.SCENE_SCORE - 0.1)
                  for f in _frames(90, 480)]
        assert scenecuts.pick_cut(frames, 100) is None

    def test_the_first_frame_of_the_window_is_never_the_cut(self):
        """It scores against nothing, so a high score there says nothing."""
        assert scenecuts.pick_cut(_frames(90, 480, cuts={0}), 90) is None

    def test_the_cut_falls_between_frames_so_rounding_keeps_the_frame(self):
        frames = _frames(5025.0, 10, cuts={1})
        seconds, _kind = scenecuts.pick_cut(frames, 5025.0)
        written = float("%.3f" % seconds)
        assert frames[0].time < written < frames[1].time

    def test_no_frames_is_no_cut(self):
        assert scenecuts.pick_cut([], 100) is None


class TestAlignedBounds:
    @pytest.fixture
    def probed(self):
        calls = []

        def frames_of(changes):
            def probe(source, start, span, accel):
                calls.append((start, span))
                return [Frame(start + i * STEP, 0.5, False)
                        if start + i * STEP not in changes
                        else Frame(start + i * STEP, 40.0, False)
                        for i in range(int(span * 24) + 1)]
            return probe
        return calls, frames_of

    def test_every_interior_cut_is_searched_around_its_own_target(self, probed):
        calls, frames_of = probed
        scenecuts.aligned_bounds("s", ["0", "1200.000", "2400.000", "3600.000"],
                                 frames_of=frames_of(set()))
        assert calls == [(1190.0, 20.0), (2390.0, 20.0)]

    def test_the_window_shrinks_to_a_quarter_of_a_short_chunk(self, probed):
        calls, frames_of = probed
        scenecuts.aligned_bounds("s", ["0", "8.000", "16.000"],
                                 frames_of=frames_of(set()))
        assert calls == [(6.0, 4.0)]

    def test_a_cut_with_nothing_near_stays_even(self, probed):
        _calls, frames_of = probed
        bounds = ["0", "1200.000", "2400.000", "3600.000"]
        assert scenecuts.aligned_bounds("s", bounds, frames_of=frames_of(set())) == (
            bounds, ["even", "even"])

    def test_the_ends_are_never_moved(self, probed):
        _calls, frames_of = probed
        change = 1190.0 + 100 * STEP
        out, kinds = scenecuts.aligned_bounds(
            "s", ["0", "1200.000", "2401.250"], frames_of=frames_of({change}))
        assert out[0] == "0" and out[-1] == "2401.250"
        assert kinds == ["scene"]
        assert 1190.0 + 99 * STEP < float(out[1]) < change

    def test_a_file_encoded_whole_is_not_probed(self, probed):
        calls, frames_of = probed
        assert scenecuts.aligned_bounds("s", [], frames_of=frames_of(set())) == (
            [], [])
        assert calls == []

    def test_an_unreadable_source_is_chunked_as_before(self):
        bounds = ["0", "20.000", "40.000"]
        assert scenecuts.aligned_bounds(
            "s", bounds, frames_of=lambda *a: []) == (bounds, ["even"])


class TestParse:
    def test_the_printed_frames_are_read(self):
        text = ("frame:0    pts:0       pts_time:0\n"
                "lavfi.scd.mafd=0.000\nlavfi.scd.score=0.000\n"
                "frame:1    pts:42      pts_time:0.0417\n"
                "lavfi.scd.mafd=31.0\nlavfi.scd.score=36.423\n"
                "lavfi.blackframe.pblack=100\n")
        assert scenecuts._parse(text, 10.0) == [
            Frame(10.0, 0.0, False), Frame(10.0417, 36.423, True)]

    def test_nothing_printed_is_no_frames(self):
        assert scenecuts._parse("", 10.0) == []


class TestDescribe:
    def test_each_kind_is_counted(self):
        assert scenecuts.describe(["scene", "black", "scene"]) == (
            "2 on a scene change, 1 on black")

    def test_an_even_cut_says_so(self):
        assert scenecuts.describe(["even"]) == "1 with no change nearby"

    def test_no_cuts_is_nothing_to_say(self):
        assert scenecuts.describe([]) == ""
