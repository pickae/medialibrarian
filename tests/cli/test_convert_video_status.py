"""convertVideo's live status row, and the progress it is read from.

What the row is DRAWN with has its own file; this is the content convertVideo
supplies for one file's video pass, aggregated across however many chunks are
encoding it.
"""

import pytest

from medialib.cli import convert_video as cv

pytestmark = pytest.mark.fs

NOW = 1_800_000_000


@pytest.fixture
def progress(tmp_path):
    """Two chunks mid-encode plus one that has not reported yet.

    Each key is re-appended on every ffmpeg update, so the LAST of each per file
    is that chunk's position and the chunks sum to the file's.
    """
    (tmp_path / "prog.0000").write_text(
        "frame=10\nout_time_us=1000000\nframe=25\nout_time_us=2500000\n")
    (tmp_path / "prog.0001").write_text("frame=7\nout_time_us=700000\n")
    (tmp_path / "prog.0002").write_text("")
    return str(tmp_path)


class TestSumEncodeProgress:

    def test_the_chunks_latest_positions_are_summed(self, progress):
        assert cv.sum_encode_progress(progress) == (32, 3200000)

    def test_a_file_whose_encode_has_not_started_reads_as_nothing(self,
                                                                  tmp_path):
        assert cv.sum_encode_progress(str(tmp_path)) == (0, 0)

    def test_a_directory_that_is_not_there_reads_as_nothing(self, tmp_path):
        assert cv.sum_encode_progress(str(tmp_path / "gone")) == (0, 0)

    def test_the_duplicate_and_dropped_counters_are_not_frames(self, progress,
                                                               tmp_path):
        """ffmpeg reports dup_frames= and drop_frames= beside frame=, and a
        substring match would add them to the count."""
        (tmp_path / "prog.0003").write_text(
            "frame=4\ndup_frames=99\ndrop_frames=98\nout_time_us=400000\n")
        assert cv.sum_encode_progress(progress) == (36, 3600000)


def _row(directory, total, label, start, paused_at_start=0, cols=110,
         paused=False, paused_now=0):
    return cv.video_status_text(directory, total, label, start,
                                paused_at_start, cols=cols, paused=paused,
                                paused_now=paused_now, now=NOW)


class TestTheRow:
    """3.2 of 10 video seconds in 8 wall-clock seconds: 32%, 4 fps, 0.40x."""

    def test_it_reports_percent_elapsed_eta_fps_and_the_speed_up(self,
                                                                 progress):
        assert _row(progress, 10, "show/ep1.mkv", NOW - 8) == (
            "  encoding show/ep1.mkv: 32.0%  elapsed 0:08  ETA 0:17  "
            "4.0 fps  0.40x realtime")

    def test_too_little_encoded_to_project_from_withholds_the_eta(self,
                                                                  tmp_path):
        assert _row(str(tmp_path), 10, "show/ep1.mkv", NOW - 8) == (
            "  encoding show/ep1.mkv: 0.0%  elapsed 0:08  ETA --:--  "
            "0.0 fps  0.00x realtime")

    def test_an_unknown_duration_leaves_the_percentage_at_zero(self, progress):
        """Rather than dividing by it."""
        assert _row(progress, 0, "show/ep1.mkv", NOW - 8) == (
            "  encoding show/ep1.mkv: 0.0%  elapsed 0:08  ETA --:--  "
            "4.0 fps  0.40x realtime")

    def test_a_row_drawn_the_instant_the_encode_starts_divides_by_no_time(
            self, progress):
        assert _row(progress, 10, "show/ep1.mkv", NOW) == (
            "  encoding show/ep1.mkv: 32.0%  elapsed 0:00  ETA 0:00  "
            "0.0 fps  0.00x realtime")


class TestANarrowTerminal:
    """The columns come out of the FILE NAME, never out of the numbers - down to
    the floor of twelve characters the shortening is never asked to go below,
    which is what an 80-column terminal is left with once the figures have
    theirs."""

    def test_the_name_is_shortened_and_the_figures_are_not(self, progress):
        assert _row(progress, 10,
                    "Some/Deep/Folder/Tree/A Long Episode Title.mkv", NOW - 8,
                    cols=80) == (
            "  encoding ...Title.mkv: 32.0%  elapsed 0:08  ETA 0:17  "
            "4.0 fps  0.40x realtime")

    def test_the_shortened_row_stays_inside_the_terminal(self, progress):
        row = _row(progress, 10,
                   "Some/Deep/Folder/Tree/A Long Episode Title.mkv", NOW - 8,
                   cols=80)
        assert len(row) < 80


class TestWhileTheRunIsPaused:
    """Pausing stops the encoders where they are, so the row says so and the clock
    stands still: the seconds spent paused are taken out of the ones the run has
    been going, leaving the actual encoding for every figure to be read from."""

    def test_the_paused_row_says_so_and_leaves_the_pause_out(self, progress):
        """Five of the eight seconds were a pause, so three were encoding."""
        assert _row(progress, 10, "show/ep1.mkv", NOW - 8, paused=True,
                    paused_now=5) == (
            "  paused   show/ep1.mkv: 32.0%  elapsed 0:03  ETA 0:06  "
            "10.7 fps  1.07x realtime")

    def test_the_two_spellings_are_the_same_width(self, progress):
        """So pausing does not shift the rest of the row sideways."""
        encoding = _row(progress, 10, "x", NOW - 8)
        paused = _row(progress, 10, "x", NOW - 8, paused=True)
        assert len(encoding) == len(paused)

    def test_a_pause_from_earlier_in_the_run_is_not_deducted_from_a_later_file(
            self, progress):
        """Which is what the baseline is for: the counter is the whole run's, and
        the figure it is subtracted from is this file's."""
        assert _row(progress, 10, "show/ep1.mkv", NOW - 8, paused_at_start=300,
                    paused_now=300) == (
            "  encoding show/ep1.mkv: 32.0%  elapsed 0:08  ETA 0:17  "
            "4.0 fps  0.40x realtime")

    def test_a_banked_pause_still_comes_off_once_the_run_resumed(self,
                                                                progress):
        """Resuming banks what the pause lasted; the row keeps leaving it out and
        goes back to its ordinary spelling."""
        assert _row(progress, 10, "show/ep1.mkv", NOW - 8, paused_now=5) == (
            "  encoding show/ep1.mkv: 32.0%  elapsed 0:03  ETA 0:06  "
            "10.7 fps  1.07x realtime")
