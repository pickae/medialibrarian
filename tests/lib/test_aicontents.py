"""Tests for asking a language model to search the web for a book's table of
contents - medialib.lib.aicontents.

The model is a stand-in that records what it was told and answers with
titles. What is pinned: only a book whose chapter names are numbers is asked
about; the model is told the book and its chapters as they stand; its titles
are taken only when there is one for every chapter; and what it was asked is
counted.
"""

import pytest

from medialib.lib import aicontents, llmclient, modelmeter, onlinecover

pytestmark = pytest.mark.pure


class _Model:
    """A model that can search, answering ``answer`` - or raising it."""

    def __init__(self, answer, can_search=True):
        self.answer = answer
        self.can_search = can_search
        self.asked = []
        self.purpose = ""
        self.name = "stand-in"

    def searching(self, purpose=""):
        self.purpose = purpose
        return self if self.can_search else None

    def ask(self, system, prompt, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(2.0, 40, 900)
        return self.answer


def _rows(*names):
    """OGM rows, a chapter every ten minutes, named ``names``."""
    lines = []
    for number, name in enumerate(names, 1):
        lines.append("CHAPTER%02d=00:%02d:00.000" % (number, (number - 1) * 10))
        lines.append("CHAPTER%02dNAME=%s" % (number, name))
    return lines


def _names(lines):
    return [line.partition("=")[2] for line in lines if line.split("=")[0].endswith("NAME")]


def _book(tmp_path, count=3):
    folder = tmp_path / "Ann Mirelle" / "The Quiet Orchard"
    folder.mkdir(parents=True)
    tracks = []
    for number in range(1, count + 1):
        track = folder / ("%02d.mp3" % number)
        track.write_bytes(b"x")
        tracks.append(str(track))
    return folder, tracks


def _tags(_path):
    return {"album": "The Quiet Orchard", "artist": "Ann Mirelle"}


def _ask(tmp_path, model, lines, **kwargs):
    folder, tracks = _book(tmp_path, len(_names(lines)))
    subject = onlinecover.subject_for(folder.name, 1800,
                                      tag_titles=["The Quiet Orchard"],
                                      tag_creators=["Ann Mirelle"])
    return aicontents.book_titles(
        str(folder), "The Quiet Orchard", lines, tracks, "", subject=subject,
        seconds=1800.0, searchers=[model], probe=_tags, **kwargs)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aicontents.SWITCH_VARIABLE, "on")


@pytest.mark.parametrize("name", ["01", "Track 02", "CD1 - 03", "Track02",
                                  "Kapitel IV", "Chapter 3 of 40", "Teil 2", ""])
def test_a_name_that_only_numbers_says_nothing(name):
    assert aicontents.says_nothing(name)


@pytest.mark.parametrize("name", ["The Gate", "Prologue", "The 12 Lanterns",
                                  "Civic Hall", "Mid"])
def test_a_name_with_words_of_its_own_says_something(name):
    assert not aicontents.says_nothing(name)


def test_a_book_numbered_but_for_its_ends_is_numbered():
    assert aicontents.numbered(["Prologue", "02", "03", "04", "05", "06",
                                "07", "Epilogue"])
    assert not aicontents.numbered(["Prologue", "02", "The Gate", "Epilogue"])
    assert not aicontents.numbered(["01"])


def test_titles_for_every_chapter_name_them(tmp_path):
    model = _Model('{"chapters": ["The Gate", "The Orchard", "The Well"], '
                   '"source": "https://example.test/contents"}')
    lines = _ask(tmp_path, model, _rows("01", "02", "03"))
    assert _names(lines) == ["The Gate", "The Orchard", "The Well"]
    assert lines[0] == "CHAPTER01=00:00:00.000"
    assert model.purpose == aicontents.PURPOSE


@pytest.mark.parametrize("answer", [
    '{"chapters": ["The Gate", "The Orchard"]}',
    '{"chapters": ["The Gate", "The Orchard", "The Well", "The Wall"]}',
    '{"chapters": []}',
    '{"chapters": ["The Gate", "", "The Well"]}',
    "I could not find it.",
])
def test_anything_but_a_title_for_every_chapter_keeps_the_numbers(tmp_path, answer):
    rows = _rows("01", "02", "03")
    assert _ask(tmp_path, _Model(answer), rows) == rows


def test_named_chapters_are_not_asked_about(tmp_path):
    model = _Model('{"chapters": ["a", "b", "c"]}')
    rows = _rows("The Gate", "The Orchard", "03")
    assert _ask(tmp_path, model, rows) == rows
    assert model.asked == []


def test_the_setting_off_asks_no_model(tmp_path, monkeypatch):
    monkeypatch.setenv(aicontents.SWITCH_VARIABLE, "off")
    model = _Model('{"chapters": ["a", "b", "c"]}')
    rows = _rows("01", "02", "03")
    assert _ask(tmp_path, model, rows) == rows
    assert model.asked == []


def test_a_model_that_cannot_search_is_not_asked(tmp_path):
    model = _Model('{"chapters": ["a", "b", "c"]}', can_search=False)
    rows = _rows("01", "02", "03")
    assert _ask(tmp_path, model, rows) == rows
    assert model.asked == []


def test_a_failing_model_hands_on_to_the_next(tmp_path):
    folder, tracks = _book(tmp_path)
    failing = _Model(llmclient.LlmError("refused"))
    answering = _Model('{"chapters": ["A", "B", "C"]}')
    subject = onlinecover.subject_for(folder.name, 1800)
    lines = aicontents.book_titles(
        str(folder), "Book", _rows("01", "02", "03"), tracks, "",
        subject=subject, seconds=1800.0, searchers=[failing, answering],
        probe=_tags)
    assert _names(lines) == ["A", "B", "C"]


def test_the_model_is_told_the_book_and_its_chapters(tmp_path):
    model = _Model('{"chapters": []}')
    _ask(tmp_path, model, _rows("01", "02", "03"))
    _system, prompt = model.asked[0]
    assert "The Quiet Orchard" in prompt and "Ann Mirelle" in prompt
    assert "01.mp3" in prompt
    assert "The book has 3 chapters" in prompt
    assert "exactly 3 titles" in prompt
    assert "2. at 10:00 (10:00 long): 02" in prompt
    assert "3. at 20:00 (10:00 long): 03" in prompt


def test_what_the_model_was_asked_is_recorded(tmp_path):
    meter = tmp_path / "contents.meter"
    _ask(tmp_path, _Model('{"chapters": []}'), _rows("01", "02", "03"),
         meter_file=str(meter))
    _spans, tally = modelmeter.gathered(str(meter))
    assert tally.requests == 1
    assert tally.tokens == 40
