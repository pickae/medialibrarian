"""Tests for medialib.lib.durationcheck - the question every conversion is asked
afterwards: is the output as long as the input.

Nothing here encodes. What is under test is the JUDGEMENT - what counts as the
same length, what a run does with a file that is not, and what it says at the end
- because that judgement is the whole safety net: the failure it exists for is an
encoder that stops early, finishes tidily and exits 0.
"""

import os

import pytest

from medialib.lib import durationcheck

pytestmark = pytest.mark.fs


@pytest.fixture
def measuring(monkeypatch):
    """The check switched ON, which the suite turns off everywhere else.

    Below the media tier there is nothing real to measure, so the conftest
    fixture disables it; these cases are about the judgement itself and supply
    their own durations, so they turn it back on.
    """
    monkeypatch.delenv(durationcheck.SKIP_VARIABLE, raising=False)


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setenv(durationcheck.LOG_VARIABLE,
                       str(tmp_path / "lengths.log"))
    durationcheck.init_log(str(tmp_path / "lengths.log"))
    return tmp_path / "lengths.log"


class TestWhatCountsAsTheSameLength:
    """Two kinds of slack, and which one applies is whichever is larger: a flat
    second for the rounding every container does, and a fraction for a source
    whose stated duration is an estimate rather than a measurement."""

    def test_a_short_file_gets_the_flat_second(self):
        assert durationcheck.allowed_drift(30) == 1.0

    def test_a_long_file_gets_the_fraction_instead(self):
        assert durationcheck.allowed_drift(10000) == 100.0

    def test_the_two_meet_where_the_fraction_overtakes_the_second(self):
        crossover = durationcheck.TOLERANCE_SECONDS \
            / durationcheck.TOLERANCE_FRACTION
        assert durationcheck.allowed_drift(crossover) == \
            durationcheck.TOLERANCE_SECONDS

    def test_an_exact_match_is_a_match(self):
        assert durationcheck.length_matches(3600, 3600)

    def test_a_frame_of_encoder_padding_is_still_a_match(self):
        assert durationcheck.length_matches(3600, 3600.046)

    def test_the_truncation_that_prompted_all_this_is_not(self):
        """The real case: an 83-hour book whose WAVE ran out at 4 GiB."""
        assert not durationcheck.length_matches(301309.7, 48695.8)

    def test_an_output_LONGER_than_its_source_is_caught_too(self):
        """A re-join that took a chunk twice is the same wrongness from the
        other side, and nothing else would notice it."""
        assert not durationcheck.length_matches(3600, 7200)

    def test_a_source_that_states_no_duration_is_not_evidence(self):
        assert durationcheck.length_matches(0, 120)


class TestTheSumOfAJoin:
    """A joined book is as long as the tracks that went into it, so the
    comparison for a join is against their total."""

    def test_the_tracks_are_added_up(self, monkeypatch):
        lengths = {"a": 10.0, "b": 20.5, "c": 0.5}
        monkeypatch.setattr(durationcheck, "media_duration",
                            lambda path: lengths[path])
        assert durationcheck.total_duration(["a", "b", "c"]) == 31.0

    def test_a_track_nothing_can_measure_adds_nothing(self, monkeypatch):
        """It understates the total, so the comparison errs towards accepting
        the join - which is the right way round for a check to be wrong."""
        monkeypatch.setattr(durationcheck, "media_duration",
                            lambda path: 0.0 if path == "b" else 10.0)
        assert durationcheck.total_duration(["a", "b", "c"]) == 20.0

    def test_nothing_to_add_is_nothing(self):
        assert durationcheck.total_duration([]) == 0.0


class TestWhatHappensToAShortOutput:

    def test_it_is_removed_named_and_recorded(self, tmp_path, measuring, log,
                                              monkeypatch):
        output = tmp_path / "book.m4a"
        output.write_text("x")
        monkeypatch.setattr(durationcheck, "media_duration", lambda p: 100.0)
        said = []

        assert not durationcheck.verify("book.m4b", "in/book.m4b", str(output),
                                        source_seconds=3600,
                                        log=lambda *m: said.append(" ".join(
                                            str(p) for p in m)))
        assert not output.exists()
        assert durationcheck.failures() == [("book.m4b", 3600.0, 100.0)]
        assert "1:00:00" in said[0] and "0:01:40" in said[0]

    def test_a_whole_output_is_left_alone_and_recorded_nowhere(
            self, tmp_path, measuring, log, monkeypatch):
        output = tmp_path / "book.m4a"
        output.write_text("x")
        monkeypatch.setattr(durationcheck, "media_duration", lambda p: 3600.0)

        assert durationcheck.verify("book.m4b", "in/book.m4b", str(output),
                                    source_seconds=3600, log=lambda *m: None)
        assert output.exists()
        assert durationcheck.failures() == []

    def test_an_output_that_is_not_there_fails_without_a_probe(
            self, tmp_path, measuring, log):
        probed = []

        def never(path):
            probed.append(path)
            return 0.0

        assert not durationcheck.verify("book.m4b", "in/book.m4b",
                                        str(tmp_path / "gone.m4a"),
                                        source_seconds=3600,
                                        log=lambda *m: None)
        assert probed == []
        assert len(durationcheck.failures()) == 1

    def test_an_empty_output_is_as_short_as_one_that_stopped_early(
            self, tmp_path, measuring, log):
        output = tmp_path / "book.m4a"
        output.touch()

        assert not durationcheck.verify("book.m4b", "in/book.m4b", str(output),
                                        source_seconds=3600,
                                        log=lambda *m: None)

    def test_remove_false_leaves_an_output_the_caller_owns(
            self, tmp_path, measuring, log, monkeypatch):
        output = tmp_path / "book.m4a"
        output.write_text("x")
        monkeypatch.setattr(durationcheck, "media_duration", lambda p: 1.0)

        assert not durationcheck.verify("book.m4b", "in/book.m4b", str(output),
                                        source_seconds=3600, remove=False,
                                        log=lambda *m: None)
        assert output.exists()


class TestTheSwitch:
    """A run whose tools do not make media does not measure what they made."""

    def test_nothing_is_measured_or_removed_while_it_is_set(
            self, tmp_path, log, monkeypatch):
        monkeypatch.setenv(durationcheck.SKIP_VARIABLE, "1")
        output = tmp_path / "book.m4a"
        output.write_text("x")

        assert durationcheck.verify("book.m4b", "in/book.m4b", str(output),
                                    source_seconds=3600, log=lambda *m: None)
        assert output.exists()
        assert durationcheck.failures() == []

    def test_an_empty_value_is_not_set(self, monkeypatch):
        monkeypatch.setenv(durationcheck.SKIP_VARIABLE, "")
        assert durationcheck.checking()


class TestTheRecord:
    """A FILE, because the checks run in worker processes: a counter raised in
    one of them is invisible to the run that started it."""

    def test_a_run_with_no_log_records_nothing_rather_than_making_one(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv(durationcheck.LOG_VARIABLE, raising=False)
        durationcheck.record("book.m4b", 3600, 100)
        assert durationcheck.failures() == []
        assert list(tmp_path.iterdir()) == []

    def test_every_worker_appends_rather_than_replacing(self, log):
        durationcheck.record("one.m4b", 3600, 100)
        durationcheck.record("two.m4b", 60, 30)
        assert [name for name, _s, _o in durationcheck.failures()] \
            == ["one.m4b", "two.m4b"]

    def test_a_separator_in_a_path_cannot_break_a_record(self, log):
        durationcheck.record("a\tb\nc.m4b", 3600, 100)
        assert durationcheck.failures() == [("a b c.m4b", 3600.0, 100.0)]

    def test_the_recap_is_silent_when_there_is_nothing_to_recap(self, log):
        class Stream:
            written = ""

            def write(self, text):
                self.written += text

        stream = Stream()
        assert durationcheck.report(stream) == 0
        assert stream.written == ""

    def test_the_recap_names_each_file_and_both_lengths(self, log):
        class Stream:
            written = ""

            def write(self, text):
                self.written += text

        durationcheck.record("book.m4b", 3600, 100)
        stream = Stream()
        assert durationcheck.report(stream) == 1
        assert "book.m4b" in stream.written
        assert "1:00:00" in stream.written and "0:01:40" in stream.written

    def test_a_log_path_that_is_a_symlink_is_refused(self, tmp_path,
                                                     monkeypatch, capsys):
        real = tmp_path / "real"
        real.write_text("")
        link = tmp_path / "link"
        link.symlink_to(real)
        monkeypatch.setenv("TMPDIR", str(tmp_path))

        settled = durationcheck.init_log(str(link))
        assert settled != str(link)
        assert os.path.basename(settled).startswith("lengthMismatch.")
        assert "symlink" in capsys.readouterr().err

    def test_a_log_a_parent_opened_is_appended_to_rather_than_emptied(
            self, tmp_path, monkeypatch):
        inherited = tmp_path / "parent.log"
        durationcheck.init_log(str(inherited))
        durationcheck.record("one.m4b", 3600, 100)

        assert durationcheck.init_log() == str(inherited)
        durationcheck.record("two.m4b", 60, 30)
        assert len(durationcheck.failures()) == 2
