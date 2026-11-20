"""Tests for medialib.lib.aichapterdb - asking a language model to pick among
fitting ChapterDB sets for the library's language, or translate one into it.

The model is a stand-in that records what it was asked and returns canned JSON
(or raises LlmError). What is pinned: multi-language choice for the configured
library language; translation preserving exact timestamps and count; strict
guards against invalid indices, wrong counts, empty/multi-line/overlong/generic
titles, and unsure answers; no-op when the deterministic winner already matches
the library's primary language clearly; switch off, no model available,
LlmError fallback, roster task traits, and modelmeter recording.
"""

from __future__ import annotations

import contextlib

import pytest

from medialib.lib import (
    aichapterdb,
    chapterdb,
    languages,
    llmclient,
    llmroster,
    modelmeter,
)

pytestmark = pytest.mark.pure

FILM = 8177.96

EN_CHAPTERS = (
    (0.0, "Opening"),
    (900.25, "The Harbour"),
    (3000.5, "Storm"),
    (6000.75, "Landfall"),
)
DE_CHAPTERS = (
    (0.0, "Eröffnung"),
    (900.25, "Der Hafen"),
    (3000.5, "Sturm"),
    (6000.75, "Landung"),
)
FR_CHAPTERS = (
    (0.0, "Ouverture"),
    (900.25, "Le Port"),
    (3000.5, "Tempête"),
    (6000.75, "Atterrissage"),
)
NUMBERED = (
    (0.0, "Chapter 1"),
    (900.25, "Chapter 2"),
    (3000.5, "Chapter 3"),
    (6000.75, "Chapter 4"),
)


def _set(chapters=EN_CHAPTERS, *, language="eng", confirmations=10,
         set_id="1", title="Nordwind", source="Blu-Ray", seconds=FILM):
    return chapterdb.ChapterSet(set_id, title, source, language,
                                confirmations, seconds, tuple(chapters))


class _Model:
    """Stand-in model answering ``answer`` or raising it."""

    def __init__(self, answer, *, name="stand-in"):
        self.answer = answer
        self.name = name
        self.model = name
        self.asked: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def ask(self, system, prompt, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 42, 320)
        return self.answer


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv(aichapterdb.SWITCH_VARIABLE, "on")
    monkeypatch.setenv("spokenLanguages", "en,de")


class TestTaskAndPrompt:

    def test_roster_task_has_expected_traits(self):
        task = llmroster.CHAPTERDB_CHOOSE
        assert task.name == "chapterdb-choose"
        assert llmroster.TASKS["chapterdb-choose"] is task
        assert task.requires == ()
        assert task.prefers == ("reasoning",)
        assert task.lean == "balanced"

    def test_prompt_lists_film_languages_and_candidates(self):
        eng = _set(EN_CHAPTERS, language="eng", confirmations=5, set_id="11")
        deu = _set(DE_CHAPTERS, language="ger", confirmations=12, set_id="22")
        targets = (languages.language("de"), languages.language("en"))
        text = aichapterdb.prompt("Nordwind", [eng, deu], targets)
        assert "Film: Nordwind" in text
        assert "Primary target language: German (de / ger)" in text
        assert "Other preferred library languages: English (en)" in text
        assert "Candidate 1 (set 11): language=eng" in text
        assert "Candidate 2 (set 22): language=ger" in text
        assert "2. [15:00] Der Hafen" in text


class TestChoosingAndTranslating:

    def test_picks_matching_language_set_for_library_language(self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "de,en")
        eng = _set(EN_CHAPTERS, language="eng", confirmations=15, set_id="en1")
        deu = _set(DE_CHAPTERS, language="ger", confirmations=8, set_id="de1")
        model = _Model('{"action": "choose", "choice": 2, '
                       '"why": "Candidate 2 is in German, the primary language."}')
        logs: list[str] = []
        picked = aichapterdb.choose([eng, deu], eng, film_title="Nordwind",
                                    models=[model], log=logs.append)
        assert picked == deu
        assert len(model.asked) == 1
        assert any("chose set de1" in line for line in logs)

    def test_translates_well_fitting_set_preserving_exact_timestamps(
            self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "de")
        eng = _set(EN_CHAPTERS, language="eng", confirmations=20, set_id="en1")
        model = _Model(
            '{"action": "translate", "choice": 1, '
            '"titles": ["Eröffnung", "Der Hafen", "Sturm", "Landung"], '
            '"why": "No German set exists; translated the English set."}')
        logs: list[str] = []
        translated = aichapterdb.choose([eng], eng, film_title="Nordwind",
                                        models=[model], log=logs.append)
        assert translated.set_id == "en1"
        assert translated.language == "ger"
        assert translated.seconds == eng.seconds
        assert translated.confirmations == eng.confirmations
        assert translated.source == eng.source
        assert len(translated.chapters) == len(eng.chapters)
        assert [start for start, _ in translated.chapters] == [
            start for start, _ in eng.chapters]
        assert [name for _, name in translated.chapters] == [
            "Eröffnung", "Der Hafen", "Sturm", "Landung"]
        assert translated.named
        assert any("translated set en1 into German" in line for line in logs)

    def test_chapterdb_choose_integrates_model_for_numbered_film(
            self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "en")
        deu = _set(DE_CHAPTERS, language="ger", confirmations=10, set_id="de1")
        existing = chapterdb.Existing(
            FILM, ("Chapter 01", "Chapter 02", "Chapter 03", "Chapter 04"),
            tuple(start for start, _ in DE_CHAPTERS))
        model = _Model(
            '{"action": "translate", "choice": 1, '
            '"titles": ["Opening", "The Harbour", "Storm", "Landfall"], '
            '"why": "Translated German chapters to English."}')
        monkeypatch.setattr(llmroster, "optional",
                            lambda task: ([model], ""))
        logs: list[str] = []
        result = chapterdb.choose([deu], existing, title="Nordwind",
                                  log=logs.append)
        assert result is not None
        assert result.language == "eng"
        assert result.chapters == EN_CHAPTERS
        assert chapterdb.same_marks(result, existing)

    def test_competing_cross_language_sets_ask_model(self):
        eng = _set(EN_CHAPTERS, language="eng", confirmations=2, set_id="en1")
        deu = _set(DE_CHAPTERS, language="ger", confirmations=40, set_id="de1")
        model = _Model(
            '{"action": "translate", "choice": 2, '
            '"titles": ["Opening", "The Harbour", "Storm", "Landfall"], '
            '"why": "German set is far better confirmed."}')
        picked = aichapterdb.choose([eng, deu], eng, film_title="Nordwind",
                                    models=[model], log=[].append)
        assert picked.set_id == "de1"
        assert picked.language == "eng"
        assert [start for start, _ in picked.chapters] == [
            start for start, _ in deu.chapters]


class TestStrictGuards:

    @pytest.mark.parametrize("bad_answer", [
        '{"action": "choose", "choice": 0}',
        '{"action": "choose", "choice": -1}',
        '{"action": "choose", "choice": 3}',
        '{"action": "choose", "choice": true}',
        '{"action": "choose", "choice": false}',
        '{"action": "choose", "choice": 1.5}',
        '{"action": "choose", "choice": "1"}',
        '{"action": "invent", "choice": 1}',
        "not json at all",
    ])
    def test_invalid_index_or_action_falls_back_to_deterministic(
            self, monkeypatch, bad_answer):
        monkeypatch.setenv("spokenLanguages", "de")
        eng = _set(EN_CHAPTERS, language="eng", set_id="en1")
        fra = _set(FR_CHAPTERS, language="fre", set_id="fr1")
        model = _Model(bad_answer)
        logs: list[str] = []
        result = aichapterdb.choose([eng, fra], eng, models=[model],
                                    log=logs.append)
        assert result == eng
        assert any("rejected" in line for line in logs)

    @pytest.mark.parametrize("bad_titles, reason", [
        (["Eröffnung", "Der Hafen", "Sturm"], "got 3 title(s) for 4"),
        (["A", "B", "C", "D", "E"], "got 5 title(s) for 4"),
        (["Eröffnung", "   ", "Sturm", "Landung"], "title 2 is empty"),
        (["Eröffnung", "Der\nHafen", "Sturm", "Landung"], "multiple lines"),
        (["Eröffnung", "Der\rHafen", "Sturm", "Landung"], "multiple lines"),
        (["Eröffnung", "x" * (aichapterdb.MAX_TITLE_CHARS + 1), "Sturm",
          "Landung"], "limit 200"),
        (["Eröffnung", 123, "Sturm", "Landung"], "not text"),
        (["Kapitel 1", "Der Hafen", "Sturm", "Landung"],
         "generic or duplicated"),
        (["Sturm", "Der Hafen", "sturm", "Landung"], "generic or duplicated"),
    ])
    def test_invalid_translated_titles_fall_back(
            self, monkeypatch, bad_titles, reason):
        monkeypatch.setenv("spokenLanguages", "de")
        eng = _set(EN_CHAPTERS, language="eng", set_id="en1")
        import json
        model = _Model(json.dumps({"action": "translate", "choice": 1,
                                   "titles": bad_titles}))
        logs: list[str] = []
        result = aichapterdb.choose([eng], eng, models=[model],
                                    log=logs.append)
        assert result == eng
        assert any(reason in line for line in logs)

    def test_unsure_falls_back_to_deterministic(self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "de")
        eng = _set(EN_CHAPTERS, language="eng", set_id="en1")
        model = _Model('{"action": "unsure", "why": "Titles are puns."}')
        logs: list[str] = []
        result = aichapterdb.choose([eng], eng, models=[model],
                                    log=logs.append)
        assert result == eng
        assert any("was unsure" in line for line in logs)


class TestNoOpAndFallbacks:

    def test_clear_primary_language_match_does_not_call_model(self):
        eng1 = _set(EN_CHAPTERS, language="eng", confirmations=20, set_id="e1")
        eng2 = _set(EN_CHAPTERS, language="eng", confirmations=5, set_id="e2")
        model = _Model('{"action": "choose", "choice": 2}')
        result = aichapterdb.choose([eng1, eng2], eng1, models=[model],
                                    log=[].append)
        assert result == eng1
        assert model.asked == []

    def test_primary_language_outranking_other_languages_does_not_call_model(
            self):
        eng = _set(EN_CHAPTERS, language="eng", confirmations=20, set_id="e1")
        deu = _set(DE_CHAPTERS, language="ger", confirmations=5, set_id="d1")
        model = _Model('{"action": "choose", "choice": 2}')
        result = aichapterdb.choose([eng, deu], eng, models=[model],
                                    log=[].append)
        assert result == eng
        assert model.asked == []

    def test_numbered_only_sets_never_call_model(self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "de")
        numbered = _set(NUMBERED, language="eng", set_id="n1")
        called = []
        monkeypatch.setattr(llmroster, "optional",
                            lambda task: called.append(task) or ([], ""))
        result = chapterdb.choose([numbered], chapterdb.Existing(FILM, ()))
        assert result == numbered
        assert called == []

    def test_use_llm_false_skips_model(self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "de")
        eng = _set(EN_CHAPTERS, language="eng", set_id="e1")
        called = []
        monkeypatch.setattr(llmroster, "optional",
                            lambda task: called.append(task) or ([], ""))
        result = chapterdb.choose([eng], chapterdb.Existing(FILM, ()),
                                  use_llm=False)
        assert result == eng
        assert called == []

    def test_switch_off_keeps_model_unasked(self, monkeypatch):
        monkeypatch.setenv(aichapterdb.SWITCH_VARIABLE, "off")
        monkeypatch.setenv("spokenLanguages", "de")
        eng = _set(EN_CHAPTERS, language="eng", set_id="e1")
        model = _Model('{"action": "choose", "choice": 1}')
        result = aichapterdb.choose([eng], eng, models=[model], log=[].append)
        assert result == eng
        assert model.asked == []

    def test_no_model_available_falls_back_cleanly(self, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "de")
        for name in (llmclient.API_VARIABLE, llmclient.URL_VARIABLE,
                     llmclient.HARNESS_VARIABLE):
            monkeypatch.delenv(name, raising=False)
        llmroster.forget()
        eng = _set(EN_CHAPTERS, language="eng", set_id="e1")
        result = aichapterdb.choose([eng], eng, log=[].append)
        assert result == eng

    def test_llm_error_falls_through_ladder_and_falls_back(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("spokenLanguages", "de")
        eng = _set(EN_CHAPTERS, language="eng", set_id="e1")
        broken = _Model(llmclient.LlmError("timeout"), name="first")
        backup = _Model(
            '{"action": "translate", "choice": 1, '
            '"titles": ["Eröffnung", "Der Hafen", "Sturm", "Landung"]}',
            name="second")
        logs: list[str] = []
        meter = tmp_path / "meter.jsonl"
        result = aichapterdb.choose([eng], eng, models=[broken, backup],
                                    meter_file=str(meter), log=logs.append)
        assert result.language == "ger"
        assert any("first: timeout" in line for line in logs)
        assert any("chapters:" in line for line in logs)
        spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 42, 320)
        assert spans.seconds >= 0.0

        # When every model raises LlmError, deterministic winner stands.
        logs2: list[str] = []
        meter2 = tmp_path / "meter2.jsonl"
        result2 = aichapterdb.choose(
            [eng], eng, models=[_Model(llmclient.LlmError("down"))],
            meter_file=str(meter2), log=logs2.append)
        assert result2 == eng
        assert modelmeter.gathered(str(meter2))[1].requests == 0
        assert any("keeping set e1" in line for line in logs2)
