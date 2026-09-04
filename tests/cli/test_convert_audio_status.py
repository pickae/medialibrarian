"""convertAudio's live status row and its per-file line.

What the row is DRAWN with has its own file; this is the content convertAudio
supplies, and the erase/re-pin dance around each line it prints.
"""

import pytest

from medialib.cli import convert_audio as ca
from medialib.lib import statusline

pytestmark = pytest.mark.fs

# 11647.5 audio seconds encoded in 187 wall-clock seconds -> 62.3x.
NOW = 1_800_000_000
ELAPSED = 187
ENCODED = "11647.500"


@pytest.fixture
def counters(tmp_path, monkeypatch):
    """A run 187 seconds in, 12 of 80 jobs done, on a host that has flock."""
    monkeypatch.setattr(ca.time, "time", lambda: float(NOW))
    monkeypatch.setenv("HAVE_FLOCK", "1")
    progress = tmp_path / "progress"
    duration = tmp_path / "duration"
    progress.write_text("12\n")
    duration.write_text(ENCODED + "\n")
    made = ca.Counters(str(progress), str(duration), 80, NOW - ELAPSED)
    made.progress = progress
    made.duration = duration
    return made


class TestStatusRow:

    def test_the_row_reports_the_position_the_audio_and_the_speed_up(
            self, counters):
        assert counters.status_text() == (
            "  encoding 12/80 jobs: elapsed 3:07  encoded 3:14:08"
            "  62.3x realtime")

    def test_the_row_offers_no_ETA(self, counters):
        """The remaining jobs are files of unknown length - a queue with two jobs
        left can hold ten seconds of work or ten hours - so extrapolating from
        the position in it would be inventing a number."""
        assert "ETA" not in counters.status_text()

    def test_without_flock_it_states_the_workload_and_claims_no_position(
            self, counters, monkeypatch):
        """The counter can lose increments without the lock, so the row says how
        much work there is rather than a position it cannot vouch for - as the
        counted prefix does for the file lines."""
        monkeypatch.setenv("HAVE_FLOCK", "")
        assert counters.status_text() == (
            "  encoding 80 jobs: elapsed 3:07  encoded 3:14:08"
            "  62.3x realtime")

    def test_a_duration_caught_mid_rewrite_reads_as_zero(self, counters):
        """The counters are read live, while the workers are rewriting them, so a
        value caught mid-write must read as nothing rather than break the row."""
        counters.duration.write_text("")
        assert counters.status_text() == (
            "  encoding 12/80 jobs: elapsed 3:07  encoded 0:00"
            "  0.00x realtime")

    def test_a_queue_position_caught_mid_rewrite_reads_as_zero(self, counters):
        counters.progress.write_text("")
        assert counters.status_text() == (
            "  encoding 0/80 jobs: elapsed 3:07  encoded 3:14:08"
            "  62.3x realtime")

    def test_a_row_drawn_at_the_start_divides_by_no_elapsed_time(
            self, counters):
        counters.run_start_epoch = NOW
        counters.progress.write_text("1\n")
        assert counters.status_text() == (
            "  encoding 1/80 jobs: elapsed 0:00  encoded 3:14:08"
            "  0.00x realtime")


class TestPerFileLine:
    """What keeps the row AT THE BOTTOM while the per-file reports scroll past
    above it: every worker erases the row before printing its own line and
    re-pins it underneath afterwards."""

    def test_the_line_carries_the_queue_position(self, counters, capsys):
        counters.progress.write_text("11\n")
        counters.report_progress("track12.m4a")
        assert capsys.readouterr().out == "[12/80] Converting: track12.m4a\n"

    def test_the_line_advanced_the_shared_counter(self, counters):
        counters.progress.write_text("11\n")
        counters.report_progress("track12.m4a")
        assert counters.progress.read_text() == "12\n"

    def test_the_row_is_erased_for_the_line_and_re_pinned_underneath_it(
            self, counters, capsys, monkeypatch):
        monkeypatch.setattr(statusline.state, "row", "1")
        counters.progress.write_text("11\n")
        counters.report_progress("track12.m4a")
        assert capsys.readouterr().err == (
            "\r\033[K"
            "\r  encoding 12/80 jobs: elapsed 3:07  encoded 3:14:08"
            "  62.3x realtime\033[K")

    def test_off_a_terminal_a_line_brings_no_row_with_it(self, counters,
                                                         capsys, monkeypatch):
        """Off a terminal the row is a periodic heartbeat rather than a fixed
        row, so a worker neither erases anything nor re-prints it after its own
        line: a redirected run's log gets one line per file, not two."""
        monkeypatch.setattr(statusline.state, "row", "")
        counters.progress.write_text("11\n")
        counters.report_progress("track12.m4a")
        assert capsys.readouterr().err == ""

    def test_off_a_terminal_the_line_itself_is_unchanged(self, counters,
                                                         capsys, monkeypatch):
        monkeypatch.setattr(statusline.state, "row", "")
        counters.progress.write_text("12\n")
        counters.report_progress("track13.m4a")
        assert capsys.readouterr().out == "[13/80] Converting: track13.m4a\n"
