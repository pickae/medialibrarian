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
    """A scripted model: ``outline``, ``refine``, ``sections`` and ``review``
    are each a function from the prompt to the answer's text; the review
    changes nothing unless told to."""

    def __init__(self, outline=None, refine=None, sections=None,
                 context_tokens=200000, review=None):
        self.context_tokens = context_tokens
        self.jobs = 2
        self.instructions = ""
        self.asked: list[str] = []
        self._outline = outline
        self._refine = refine or (lambda prompt: json.dumps(
            {"start": _first_line(prompt), "title": _placed_title(prompt)}))
        self._sections = sections
        self._review = review or (lambda prompt: '{"titles": []}')

    def ask(self, system, prompt):
        self.asked.append(prompt)
        if prompt.startswith("Below is part"):
            return self._sections(prompt)
        if "Find the line where" in prompt:
            return self._refine(prompt)
        if "correct the titles" in prompt:
            return self._review(prompt)
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
        assert len(model.asked) == 4
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


def test_a_half_minute_shortest_chapter_is_allowed():
    assert not aichapters.too_short(2 * 60_000, min_minutes=0.5)
    assert aichapters.too_short(50_000, min_minutes=0.5)


@pytest.mark.parametrize("minutes, words", [
    (2, "2 minutes"), (1, "1 minute"), (0.5, "30 seconds"),
    (1.5, "1.5 minutes"), (0.25, "15 seconds"), (0.01, "1 second")])
def test_the_shortest_chapter_is_told_to_the_model_in_its_own_units(minutes, words):
    assert aichapters.span_label(minutes) == words


def test_the_settle_step_keeps_a_chapter_the_floor_lets_through():
    # An episode's opening, then its first speaker 40 s later, then the next part.
    cues = [aichapters.Cue(0, 5_000, "opening"),
            aichapters.Cue(40_000, 45_000, "first speaker"),
            aichapters.Cue(400_000, 405_000, "next part")]
    chosen = [(0, "Episode", "Episode"), (1, "First speaker", "First speaker"),
              (2, "Next", "Next")]
    kept = {floor: [at for at, *_ in aichapters._settle(cues, chosen, 900_000, floor)]
            for floor in (60_000, 30_000)}
    assert len(kept[60_000]) == 2
    assert len(kept[30_000]) == 3


class TestTheNeighboursOfASecondLook:
    """A second look is told where the chapters either side of it start - at a
    line of its own transcript, or so far outside it - so it can tell what its
    chapter is from what surrounds it, whatever the chapters are called."""

    def _looks(self, *starts, cues=30):
        model = Model(outline=_outline(*starts))
        aichapters.generate(_cues(cues), model)
        return [prompt for prompt in model.asked if "Find the line where" in prompt]

    def test_a_neighbour_far_off_is_said_to_be_outside_with_its_distance(self):
        first, last = self._looks(1, 21, 41)
        assert 'The chapter before it, "Topic 1", starts about 10 minutes ' \
               'before the start of this one, outside this transcript.' in first
        assert 'The chapter after it, "Topic 41", starts about 10 minutes ' \
               'after the start of this one, outside this transcript.' in first
        assert 'The chapter before it, "Topic 21"' in last

    def test_a_neighbour_in_the_window_is_said_to_be_at_a_line(self):
        (look,) = self._looks(1, 7, 9, cues=10)[:1]
        assert re.search(r'The chapter after it, "Topic 9", starts at about line '
                         r'L\d+ \(0:\d\d:\d\d\), inside this transcript\.', look)

    def test_the_last_chapter_has_no_chapter_after_it(self):
        *_, last = self._looks(1, 21, 41)
        assert "There is no chapter after it." in last

    def test_the_neighbours_are_settled(self):
        (look, *_) = self._looks(1, 21)
        assert "Those neighbours are settled" in look

    def test_nothing_in_the_words_is_about_episodes_or_speakers(self):
        (look, *_) = self._looks(1, 21)
        sentence = look[look.index("The chapter before it"):look.index("Find the line")]
        assert not re.search(r"episode|speaker|season|header", sentence, re.IGNORECASE)

    def test_a_start_at_or_before_the_chapter_before_it_is_not_taken(self):
        marks = aichapters.generate(_cues(30), Model(
            outline=_outline(1, 21, 41),
            refine=lambda p: ('{"start": "L1", "title": "T"}' if '"Topic 21" is placed' in p
                              else json.dumps({"start": _first_line(p),
                                               "title": _placed_title(p)}))))
        assert marks[1][0] == 600000 - 500

    def test_a_start_at_or_after_the_chapter_after_it_is_not_taken(self):
        def refine(prompt):
            if '"Topic 21" is placed' in prompt:
                after = re.search(r"\[(L\d+) (0:20:\d\d)\]", prompt)
                return json.dumps({"start": after.group(1), "title": "T"}) if after \
                    else json.dumps({"start": _first_line(prompt), "title": "T"})
            return json.dumps({"start": _first_line(prompt), "title": _placed_title(prompt)})

        marks = aichapters.generate(_cues(60), Model(outline=_outline(1, 21, 23, 41),
                                                     refine=refine))
        assert [at for at, _ in marks] == sorted(at for at, _ in marks)


class TestTheReviewOfTheTitles:
    """The finished list is read once more as a whole, with the user's
    instructions, and may reword titles - never move, add or drop a chapter."""

    def _run(self, review, *starts, instructions=""):
        model = Model(outline=_outline(*(starts or (1, 21, 41))), review=review)
        model.instructions = instructions
        return aichapters.generate(_cues(60), model), model

    def _reviews(self, model):
        return [p for p in model.asked if "correct the titles" in p]

    def test_it_sees_the_list_and_the_instructions_and_no_transcript(self):
        _, model = self._run(None, instructions="Name each speaker.")
        (prompt,) = self._reviews(model)
        assert "[C1 0:00:00] Topic 1" in prompt
        assert re.search(r"\[C3 0:\d\d:\d\d\] Topic 41", prompt)
        assert "Name each speaker." in prompt
        assert "line at" not in prompt

    def test_it_runs_without_instructions_too(self):
        _, model = self._run(None)
        (prompt,) = self._reviews(model)
        assert "The person these chapters are for" not in prompt

    def test_a_title_it_gives_replaces_the_old_one_and_nothing_else_moves(self):
        before, _ = self._run(None)
        marks, _ = self._run(lambda p: '{"titles": [{"id": "C2", "title": "Ep. 1: Fixed"}]}')
        assert [at for at, _ in marks] == [at for at, _ in before]
        assert [title for _, title in marks] == ["Topic 1", "Ep. 1: Fixed", "Topic 41"]

    def test_an_id_that_names_no_chapter_is_ignored(self):
        marks, _ = self._run(lambda p: json.dumps({"titles": [
            {"id": "C9", "title": "X"}, {"id": "nope", "title": "Y"},
            {"id": "C1", "title": ""}, "junk"]}))
        assert [title for _, title in marks] == ["Topic 1", "Topic 21", "Topic 41"]

    def test_a_bare_list_is_taken_as_the_titles(self):
        marks, _ = self._run(lambda p: '[{"id": "3", "title": "Last"}]')
        assert marks[2][1] == "Last"

    def test_a_review_that_fails_keeps_every_title(self):
        marks, _ = self._run(lambda p: "no json at all")
        assert [title for _, title in marks] == ["Topic 1", "Topic 21", "Topic 41"]

    def test_a_list_of_two_is_not_worth_a_review(self):
        _, model = self._run(None, 1, 21)
        assert not self._reviews(model)

    def test_a_title_the_second_look_changed_is_given_after_the_list(self):
        def refine(prompt):
            title = _placed_title(prompt)
            return json.dumps({"start": _first_line(prompt),
                               "title": "Renamed" if title == "Topic 21" else title})

        model = Model(outline=_outline(1, 21, 41), refine=refine)
        aichapters.generate(_cues(60), model)
        (prompt,) = self._reviews(model)
        assert re.search(r"\[C2 0:\d\d:\d\d\] Renamed\n", prompt)
        drafts = prompt[prompt.index("Titles changed after the first draft"):]
        assert "\nC2: Topic 21" in drafts
        assert "\nC1:" not in drafts and "\nC3:" not in drafts

    def test_with_no_title_changed_there_are_no_first_drafts(self):
        _, model = self._run(None)
        (prompt,) = self._reviews(model)
        assert "Titles changed after the first draft" not in prompt

    def test_and_the_review_may_give_the_first_title_back(self):
        def refine(prompt):
            title = _placed_title(prompt)
            return json.dumps({"start": _first_line(prompt),
                               "title": "Renamed" if title == "Topic 21" else title})

        model = Model(outline=_outline(1, 21, 41), refine=refine,
                      review=lambda p: '{"titles": [{"id": "C2", "title": "Topic 21"}]}')
        marks = aichapters.generate(_cues(60), model)
        assert [title for _, title in marks] == ["Topic 1", "Topic 21", "Topic 41"]

    def test_it_tells_the_model_the_starts_are_final(self):
        _, model = self._run(None)
        (prompt,) = self._reviews(model)
        assert "starts are final" in prompt
