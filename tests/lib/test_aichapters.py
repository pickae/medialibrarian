"""The chapter generator against a scripted model: what it reads, what it asks,
and how the answers become marks.

The model is a function here. Its answers are keyed on which of the three
requests it is handed - the summary of a window, the outline, the second look
at one start - so each case says what the model would answer and checks what
the generator made of it.
"""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest

from medialib.lib import aichapters, chapterllm

pytestmark = pytest.mark.pure


def _cues(minutes: int, every: int = 10) -> list[aichapters.Cue]:
    """A cue every ``every`` seconds for ``minutes``, each one second short of
    the next so there is a pause to place a mark in."""
    return [aichapters.Cue(second * 1000, (second + every - 1) * 1000,
                           "line at %d" % second)
            for second in range(0, minutes * 60, every)]


class Model:
    """A scripted model: ``outline``, ``refine`` and ``sections`` are each a
    function from the prompt to the answer's text."""

    def __init__(self, outline=None, refine=None, sections=None,
                 context_tokens=200000):
        self.context_tokens = context_tokens
        self.jobs = 2
        self.instructions = ""
        self.asked: list[str] = []
        self._outline = outline
        self._refine = refine or (lambda prompt: json.dumps(
            {"start": _first_line(prompt), "title": _placed_title(prompt)}))
        self._sections = sections

    def ask(self, system, prompt):
        self.asked.append(prompt)
        if prompt.startswith("Below is part"):
            return self._sections(prompt)
        if "Find the line where" in prompt:
            return self._refine(prompt)
        return self._outline(prompt)


def _first_line(prompt: str) -> str:
    """The id of the line the window centres on, by the time the prompt names."""
    placed = re.search(r"placed to start \((\d+:\d\d:\d\d)\)", prompt).group(1)
    return re.search(r"\[(L\d+) %s\]" % placed, prompt).group(1)


def _placed_title(prompt: str) -> str:
    return re.search(r'the chapter "([^"]+)" is placed', prompt).group(1)


def _outline(*starts):
    """An outline answer: chapters at these passage numbers, titled after them."""
    return lambda prompt: json.dumps({"summary": "A test.", "chapters": [
        {"start": "P%d" % start, "title": "Topic %d" % start} for start in starts]})


class TestReadingTranscripts:
    def test_srt(self, tmp_path):
        path = tmp_path / "a.srt"
        path.write_text("1\n00:00:01,500 --> 00:00:03,000\nHello\nthere\n\n"
                        "2\n01:02:03,040 --> 01:02:04,000\n<i>Later</i>\n\n")
        assert aichapters.read_transcript(str(path)) == [
            aichapters.Cue(1500, 3000, "Hello there"),
            aichapters.Cue(3723040, 3724000, "Later")]

    def test_vtt_with_short_times(self, tmp_path):
        path = tmp_path / "a.vtt"
        path.write_text("WEBVTT\n\n00:01.000 --> 00:02.500\nOne\n")
        assert aichapters.read_transcript(str(path)) == [
            aichapters.Cue(1000, 2500, "One")]

    def test_whisper_tsv(self, tmp_path):
        path = tmp_path / "a.tsv"
        path.write_text("start\tend\ttext\n0\t1200\t Hi\n1300\t2000\tYou\n")
        assert [cue.text for cue in aichapters.read_transcript(str(path))] == ["Hi", "You"]

    def test_json_segments(self, tmp_path):
        path = tmp_path / "a.json"
        path.write_text(json.dumps({"segments": [{"start": 1.25, "end": 2, "text": " A"}]}))
        assert aichapters.read_transcript(str(path)) == [aichapters.Cue(1250, 2000, "A")]

    def test_lrc_lines_end_where_the_next_begins(self, tmp_path):
        path = tmp_path / "a.lrc"
        path.write_text("[ar:Someone]\n[00:01.50]One\n[123:00.00]Two\n")
        assert aichapters.read_transcript(str(path)) == [
            aichapters.Cue(1500, 7380000, "One"), aichapters.Cue(7380000, 7385000, "Two")]

    def test_empty_cues_are_dropped_and_the_rest_sorted(self, tmp_path):
        path = tmp_path / "a.srt"
        path.write_text("00:00:05,000 --> 00:00:06,000\nB\n\n"
                        "00:00:01,000 --> 00:00:02,000\n\n\n"
                        "00:00:00,000 --> 00:00:01,000\nA\n")
        assert [cue.text for cue in aichapters.read_transcript(str(path))] == ["A", "B"]

    def test_the_first_format_found_wins(self, tmp_path):
        (tmp_path / "show.vtt").write_text("x")
        (tmp_path / "show.srt").write_text("x")
        assert aichapters.find_transcript(str(tmp_path / "show")).endswith(".srt")
        assert aichapters.find_transcript(str(tmp_path / "other")) == ""


class TestPassages:
    def test_cut_only_between_cues_once_long_enough(self):
        found = aichapters.passages(_cues(2), seconds=20)
        assert [(p.first, p.last) for p in found] == [
            (0, 2), (3, 5), (6, 8), (9, 11)]
        assert found[1].start == 30000

    def test_a_short_tail_is_a_passage_of_its_own(self):
        found = aichapters.passages(_cues(1)[:4], seconds=20)
        assert [(p.first, p.last) for p in found] == [(0, 2), (3, 3)]


class TestTitles:
    @pytest.mark.parametrize("raw,clean", [
        ('"The Big Question"', "The Big Question"),
        ("Chapter 3: Rivers", "Rivers"),
        ("2. Bridges", "Bridges"),
        ("1815: the year", "1815: the year"),
        ("  two\nlines ", "two lines"),
        (None, ""),
    ])
    def test_cleaned(self, raw, clean):
        assert aichapters.clean_title(raw) == clean

    def test_a_long_title_is_cut_at_a_word(self):
        title = aichapters.clean_title("word " * 40)
        assert len(title) <= aichapters.MAX_TITLE + 1 and title.endswith("word…")


class TestGenerate:
    def test_marks_are_the_transcripts_times_not_the_models(self):
        """The model names passages; each mark is the time of a cue."""
        cues = _cues(30)
        model = Model(outline=_outline(1, 21, 41))
        marks = aichapters.generate(cues, model)
        assert [title for _, title in marks] == ["Topic 1", "Topic 21", "Topic 41"]
        # A passage is three cues here, so passage 21 starts on cue 60 (second
        # 600), and the mark goes into the pause before it.
        assert marks[0][0] == 0
        assert marks[1][0] == 600000 - 500
        assert marks[2][0] == 1200000 - 500

    def test_the_second_look_moves_a_start_and_retitles_it(self):
        cues = _cues(30)

        def refine(prompt):
            return json.dumps({"start": "L63", "title": "Better title"})

        marks = aichapters.generate(cues, Model(outline=_outline(1, 21), refine=refine))
        assert marks[1] == (620000 - 500, "Better title")

    def test_a_start_the_second_look_rejects_is_dropped(self):
        def refine(prompt):
            if "Topic 21" in _placed_title(prompt):
                return json.dumps({"start": None, "title": "x"})
            return json.dumps({"start": _first_line(prompt), "title": _placed_title(prompt)})

        marks = aichapters.generate(_cues(30), Model(outline=_outline(1, 21, 41),
                                                     refine=refine))
        assert [title for _, title in marks] == ["Topic 1", "Topic 41"]

    def test_a_start_outside_the_window_keeps_the_outlines_place(self):
        marks = aichapters.generate(_cues(30), Model(
            outline=_outline(1, 21), refine=lambda p: '{"start": "L1", "title": "T"}'))
        assert marks[1] == (600000 - 500, "T")

    def test_the_whole_outline_is_beside_every_second_look(self):
        model = Model(outline=_outline(1, 21, 41))
        aichapters.generate(_cues(30), model)
        looks = [prompt for prompt in model.asked if "Find the line where" in prompt]
        assert len(looks) == 2
        assert all("Topic 1" in look and "Topic 41" in look for look in looks)
        assert all("What it is: A test." in look for look in looks)

    def test_the_users_instructions_are_in_every_request(self):
        model = Model(outline=_outline(1, 21, 41))
        model.instructions = "A chapter per speaker, named."
        aichapters.generate(_cues(30), model)
        assert len(model.asked) == 3
        assert all("<<<\nA chapter per speaker, named.\n>>>" in prompt
                   for prompt in model.asked)

    def test_and_in_the_summaries_of_a_long_transcript(self):
        def sections(prompt):
            first = re.search(r"\[(P\d+) ", prompt).group(1)
            return json.dumps({"sections": [{"start": first, "summary": "talk"}]})

        model = Model(outline=lambda prompt: json.dumps(
            {"chapters": [{"start": re.search(r"\[(P\d+) ", prompt).group(1),
                           "title": "All"}]}),
            sections=sections, context_tokens=3000)
        model.instructions = "Name the speakers."
        aichapters.generate(_cues(60), model)
        summaries = [prompt for prompt in model.asked if prompt.startswith("Below is part")]
        assert summaries and all("Name the speakers." in prompt for prompt in summaries)

    def test_without_instructions_nothing_changes(self):
        model = Model(outline=_outline(1, 21))
        aichapters.generate(_cues(30), model)
        assert not any("<<<" in prompt for prompt in model.asked)

    def test_chapters_shorter_than_the_minimum_are_merged_away(self):
        marks = aichapters.generate(_cues(30), Model(outline=_outline(1, 4, 21, 59)),
                                    min_minutes=3)
        # Passage 4 is 90 s in, passage 59 is 60 s from the end.
        assert [title for _, title in marks] == ["Topic 1", "Topic 21"]

    def test_the_first_chapter_starts_at_zero_whatever_the_outline_says(self):
        marks = aichapters.generate(_cues(30), Model(outline=_outline(3, 21)))
        assert marks[0] == (0, "Topic 3")

    def test_ids_outside_the_transcript_are_ignored(self):
        marks = aichapters.generate(_cues(30), Model(outline=_outline(1, 21, 999)))
        assert len(marks) == 2

    def test_an_unusable_outline_is_asked_again_then_given_up(self):
        answers = iter(["no json here", "still none"])
        model = Model(outline=lambda prompt: next(answers))
        assert aichapters.generate(_cues(30), model) == []
        assert len(model.asked) == 2

    def test_json_inside_prose_is_read(self):
        marks = aichapters.generate(_cues(30), Model(outline=lambda p: (
            "Sure! Here it is:\n```json\n"
            + _outline(1, 21)(p) + "\n```")))
        assert len(marks) == 2

    def test_a_recording_too_short_to_divide_asks_nothing(self):
        model = Model(outline=_outline(1))
        assert aichapters.generate(_cues(3), model) == []
        assert model.asked == []

    def test_a_long_transcript_is_summarised_before_the_outline(self):
        """With a window far too small for the transcript, the outline is drawn
        from the summaries, and the summaries' starts are passage ids."""
        seen = {}

        def sections(prompt):
            ids = re.findall(r"\[(P\d+) ", prompt)
            return json.dumps({"sections": [
                {"start": ids[0], "summary": "about %s" % ids[0]},
                {"start": ids[len(ids) // 2], "summary": "about %s" % ids[len(ids) // 2]}]})

        def outline(prompt):
            seen["outline"] = prompt
            ids = re.findall(r"\[(P\d+) ", prompt)
            return json.dumps({"summary": "", "chapters": [
                {"start": ids[0], "title": "First"}, {"start": ids[-1], "title": "Last"}]})

        marks = aichapters.generate(_cues(60, every=2), Model(
            outline=outline, sections=sections, context_tokens=6000))
        assert "already summarised" in seen["outline"]
        assert "about P" in seen["outline"]
        assert [title for _, title in marks] == ["First", "Last"]

    def test_the_model_failing_outright_is_no_chapters(self):
        def broken(prompt):
            raise chapterllm.LlmError("down")

        assert aichapters.generate(_cues(30), Model(outline=broken)) == []


def test_rows_are_ogm():
    assert aichapters.chapter_rows([(0, "Intro"), (3723004, "Next")]) == [
        "CHAPTER01=00:00:00.000", "CHAPTER01NAME=Intro",
        "CHAPTER02=01:02:03.004", "CHAPTER02NAME=Next"]


def test_time_label():
    assert aichapters.time_label(3723999) == "1:02:03"


@pytest.mark.parametrize("duration, short", [
    (0, False),                # unknown: asked about, not turned away
    (3 * 60_000, True),        # under two shortest chapters
    (4 * 60_000, False),
    (60 * 60_000, False),
])
def test_too_short_is_known_before_any_transcript(duration, short):
    assert aichapters.too_short(duration) is short


def test_too_short_follows_the_shortest_chapter():
    assert aichapters.too_short(9 * 60_000, min_minutes=5)


class TestAsReady:
    def test_a_local_model_waits_for_every_transcript_then_goes_in_order(self):
        made: dict[str, str] = {}
        events = []

        def transcribe():
            events.append("transcribed")
            made.update(a="a.srt", b="b.srt")

        def chapter(item, source):
            events.append((item, source))
            return item.upper()

        results = aichapters.as_ready(["a", "b"], lambda item: made.get(item, ""),
                                      transcribe, SimpleNamespace(local=True), chapter,
                                      begin=lambda: events.append("begin"))
        assert results == ["A", "B"]
        assert events == ["transcribed", "begin", ("a", "a.srt"), ("b", "b.srt")]

    def test_a_hosted_one_starts_each_file_as_its_transcript_appears(self, monkeypatch):
        monkeypatch.setattr(aichapters, "POLL_SECONDS", 0.01)
        made = {"ready": "ready.srt"}
        first_done = threading.Event()

        def transcribe():
            # Only finishes the second transcript once the first file has been
            # chaptered: a run that waited for transcription would hang here.
            assert first_done.wait(timeout=5)
            made["later"] = "later.srt"

        def chapter(item, source):
            if item == "ready":
                first_done.set()
            return (item, source)

        results = aichapters.as_ready(["ready", "later", "never"],
                                      lambda item: made.get(item, ""), transcribe,
                                      SimpleNamespace(local=False), chapter)
        assert results == [("ready", "ready.srt"), ("later", "later.srt"),
                           ("never", "")]

    def test_and_runs_them_side_by_side(self):
        together = threading.Barrier(3)

        def chapter(item, source):
            together.wait(timeout=5)
            return item

        assert aichapters.as_ready(["a", "b", "c"], lambda item: item + ".srt", None,
                                   SimpleNamespace(local=False), chapter) == ["a", "b", "c"]
