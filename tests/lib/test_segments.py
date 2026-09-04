"""Tests for medialib.lib.segments - planning where a long file is cut.

The hashes below were taken from the shell (``printf '%s' ... | md5sum``) rather
than from Python, so a change to how the path is encoded on the way into the
digest fails here instead of quietly moving every scratch directory.
"""

import pytest

from medialib.lib import segments


class TestTheScratchPaths:
    def test_the_hash_is_the_shells_hash(self):
        assert segments.chunk_dir_for(
            "/run/chunks", "Artist/Album/01 Track.m4b"
        ) == "/run/chunks/835a05453c8e6d15331392d671ff2e9f"

    def test_an_empty_path_still_hashes(self):
        assert segments.chunk_dir_for("/r", "") == "/r/d41d8cd98f00b204e9800998ecf8427e"

    def test_the_path_is_hashed_as_bytes(self):
        """md5sum sees the bytes the shell wrote, so the encoding is not a choice."""
        assert segments.plan_file_for(
            "/run/plans", "Björk/Homogénic.flac"
        ) == "/run/plans/e8aca0a622ae2663cff08835e9ffa217"

    def test_both_roots_use_one_hash(self):
        """The planner and the stitcher never speak; the hash is the whole protocol."""
        track = "Some/Track.m4b"
        assert (segments.chunk_dir_for("/a", track).rpartition("/")[2]
                == segments.plan_file_for("/b", track).rpartition("/")[2])


class TestTheChunkPlan:
    def test_a_plain_split(self):
        assert segments.seg_plan("3600", "8") == "8 450.000000 225.000000"

    @pytest.mark.parametrize("jobs", ["1", "0", "", "-4"])
    def test_fewer_than_two_leaves_the_file_whole(self, jobs):
        assert segments.seg_plan("3600", jobs) is None

    def test_a_duration_that_is_not_a_number_is_zero(self):
        """A missing probe result plans a file of length nothing, and does not stop
        the run. That is the shell's behaviour and it is deliberate."""
        assert segments.seg_plan("later", "4") == "4 0.000000 0.000000"

    def test_the_count_is_truncated_but_the_division_is_not(self):
        """%d truncates the printed count while the segment divides by the value
        itself - two readings of one variable, both the shell's."""
        assert segments.seg_plan("100", "2.9") == "2 34.482759 17.241379"

    def test_a_leading_zero_is_decimal_and_not_octal(self):
        assert segments.seg_plan("3600", "07") == "7 514.285714 257.142857"


class TestChoosingTheBoundaries:
    def test_a_boundary_moves_to_the_nearest_silence(self):
        assert segments.select_boundaries(["905.5", "890.0"], "3600", "4")[0] == "905.500"

    def test_the_nearest_wins_even_from_the_far_side(self):
        assert segments.select_boundaries(["880.0", "902.0"], "3600", "4")[0] == "902.000"

    def test_with_no_silence_the_arithmetic_boundary_stands(self):
        """A file with no quiet moment is still cut - just not at one."""
        assert segments.select_boundaries([], "3600", "4") == [
            "900.000", "1800.000", "2700.000"]

    def test_a_silence_further_than_half_a_segment_is_ignored(self):
        assert segments.select_boundaries(["300.0"], "3600", "4")[0] == "900.000"

    def test_a_silence_too_near_the_start_is_not_eligible(self):
        """Within a fifth of a segment of the previous cut, and the first cut's
        "previous" is zero - so an early silence cannot make a sliver."""
        assert segments.select_boundaries(["179.0"], "3600", "4")[0] == "900.000"

    def test_a_silence_too_near_the_end_is_not_eligible(self):
        assert segments.select_boundaries(["3421.0"], "3600", "4")[-1] == "2700.000"

    def test_a_boundary_within_a_second_of_the_last_is_dropped(self):
        """Dropped, not moved: the piece before it simply runs on, so the count of
        pieces falls rather than a one-second piece existing."""
        got = segments.select_boundaries(["901.0", "901.4"], "20", "10")
        assert len(got) == len(set(got))

    def test_a_dropped_boundary_does_not_advance_the_mark(self):
        """The "previous" only moves when something was printed, so the next
        boundary is measured from the last real cut and not from the near-miss."""
        assert segments.select_boundaries([], "10", "10") == [
            "2.000", "4.000", "6.000", "8.000"]

    def test_the_order_the_midpoints_arrive_in_does_not_matter(self):
        forwards = segments.select_boundaries(["905.5", "1795.0"], "3600", "4")
        backwards = segments.select_boundaries(["1795.0", "905.5"], "3600", "4")
        assert forwards == backwards

    def test_a_blank_line_is_a_midpoint_of_zero(self):
        """awk reads every line, and an empty $1 is zero - which is then never
        eligible, being at the very start. It must not become an error."""
        assert segments.select_boundaries(["", "  ", "905.5"], "3600", "4")[0] == "905.500"

    def test_only_the_first_field_of_a_line_is_read(self):
        assert segments.select_boundaries(["905.5 ignored"], "3600", "4")[0] == "905.500"

    @pytest.mark.parametrize("jobs", ["1", "0", ""])
    def test_fewer_than_two_chooses_nothing(self, jobs):
        assert segments.select_boundaries(["100", "200"], "3600", jobs) == []


class TestTheTwoGuards:
    """Where a candidate is refused for being too close to something.

    Both are constructed here rather than left to the corpus, which reaches them
    a handful of times in six hundred: each needs a candidate inside a band a
    fifth of a segment wide that is also the nearest one to its boundary.
    """

    def test_a_cut_is_measured_from_where_the_last_one_ENDED_UP(self):
        """1350 is exactly half a segment past the first boundary, so it is taken
        and the mark moves there. 1500 is then inside a fifth of a segment of the
        mark, and the second boundary stays where the arithmetic put it."""
        assert segments.select_boundaries(["1350", "1500"], "3600", "4") == [
            "1350.000", "1800.000", "2700.000"]

    def test_and_a_candidate_past_that_fifth_is_taken(self):
        assert segments.select_boundaries(["1350", "1600"], "3600", "4") == [
            "1350.000", "1600.000", "2700.000"]

    def test_the_guard_at_the_end_needs_a_fractional_core_count(self):
        """The loop runs while k < n, so the last boundary sits at ceil(n)-1
        segments. For a whole n that is n-1, whose window reaches n-0.5 segments
        and stops short of the guard at n-0.2 - the guard cannot fire at all. A
        fractional n pushes the last boundary further out and the window past it,
        and 3450 is then refused for being too near the end."""
        assert segments.select_boundaries(["3450"], "3600", "3.5") == [
            "1028.571", "2057.143", "3085.714"]

    def test_and_a_whole_core_count_never_reaches_it(self):
        """cpuCount only ever produces a whole number, so in a real run the guard
        at the end is unreachable. Kept because it is what the shell does."""
        assert segments.select_boundaries(["3450"], "3600", "4") == [
            "900.000", "1800.000", "2700.000"]
