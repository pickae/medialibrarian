"""Tests for medialib.lib.modelmeter - a model's wall-clock and tokens.

What is pinned: stretches of work that overlap count the time they share
once, a pause between them not at all, and the line says the tokens written,
their pace and, where a server said, the tokens read.
"""

import pytest

from medialib.lib import modelmeter


class TestTheWallClock:
    @pytest.mark.parametrize("spans,seconds", [
        ([], 0.0),
        ([(0, 10)], 10.0),
        # Side by side: the shared five seconds once.
        ([(0, 10), (5, 15)], 15.0),
        # One inside another.
        ([(0, 10), (2, 4)], 10.0),
        # One after another, touching.
        ([(0, 10), (10, 12)], 12.0),
        # A pause between them is not counted.
        ([(0, 10), (100, 105)], 15.0),
        # Out of order, three at once.
        ([(20, 30), (0, 25), (1, 2)], 30.0)])
    def test_overlaps_count_once(self, spans, seconds):
        assert modelmeter.joined(spans) == seconds

    def test_merged_spans_keep_their_overlap(self):
        """Two films read at once, added into the run's account, are the
        time they took together, not its sum."""
        one, two, run = (modelmeter.Spans() for _ in range(3))
        one.add(0.0, 10.0)
        two.add(4.0, 12.0)
        run.merge(one)
        run.merge(two)
        assert run.seconds == 12.0

    def test_timing_a_block(self):
        spans = modelmeter.Spans()
        with spans.timing():
            pass
        assert len(spans.all()) == 1 and spans.seconds >= 0.0


class TestTheLine:
    def test_written_read_and_pace(self):
        tally = modelmeter.Tally()
        tally.add(2.0, 20, read=300)
        tally.add(2.0, 20, read=300)
        assert modelmeter.rate_line("ocr", 2.0, tally) == (
            "ocr:        2 request(s) in     2.0s, 40 token(s) written, "
            "20.0 tok/s (10.0 tok/s a request), 600 read, 640 in all")

    def test_nothing_read_says_nothing_of_it(self):
        tally = modelmeter.Tally()
        tally.add(1.0, 10)
        assert "read" not in modelmeter.rate_line("outline", 1.0, tally)

    def test_none_reported(self):
        tally = modelmeter.Tally()
        tally.add(1.0, None)
        assert modelmeter.rate_line("ocr", 1.0, tally).endswith(
            "tokens not reported")


class TestTheRecord:
    """What worker processes record, read back as one run."""

    def test_records_add_up_and_their_stretches_join(self, tmp_path):
        path = str(tmp_path / "meter")
        for start, end, tokens in ((0.0, 10.0, 30), (5.0, 12.0, 20)):
            spans, tally = modelmeter.Spans(), modelmeter.Tally()
            spans.add(start, end)
            tally.add(end - start, tokens, read=100)
            modelmeter.record(path, spans, tally)
        spans, tally = modelmeter.gathered(path)
        assert spans.seconds == 12.0
        assert (tally.requests, tally.tokens, tally.read, tally.counted) == (2, 50, 200, 2)

    def test_no_record_is_nothing(self, tmp_path):
        spans, tally = modelmeter.gathered(str(tmp_path / "none"))
        assert (spans.seconds, tally.requests) == (0.0, 0)

    def test_a_torn_line_is_skipped(self, tmp_path):
        path = tmp_path / "meter"
        path.write_text('{"spans": [[0, 1]], "tally": {"requests": 1}}\nnot json\n')
        assert modelmeter.gathered(str(path))[1].requests == 0
