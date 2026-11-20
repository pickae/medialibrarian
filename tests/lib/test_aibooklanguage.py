"""Tests for asking a small language model to settle a book's language when
deterministic detection disagrees or cannot answer - medialib.lib.aibooklanguage.

The model is a stand-in that records every prompt it was sent and answers with
canned JSON (or raises LlmError). What is pinned: the excerpt is capped to
~2 KB; agreeing metadata and stop-word detection (and unambiguous text alone)
never call the model; disagreeing metadata vs text, unknown languages outside
the stop-word table, and borderline bilingual text call the model; the answer
is strictly validated through medialib.lib.languages / pycountry and requires
confidence; unsure, invalid, low-confidence and error responses fall back to
the deterministic result; the switch and missing roster stay silent; and
modelmeter records every call.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from medialib.lib import (
    aibooklanguage,
    booklanguage,
    llmclient,
    llmroster,
    modelmeter,
)

pytestmark = pytest.mark.pure

_SWEDISH_PROSE = (
    "Det var en mörk och stormig kväll i november när den gamla skutan "
    "lämnade hamnen i Göteborg. Ingen ombord visste ännu vad som väntade "
    "bortom horisonten, men kaptenen stod stadigt vid rodret och blickade "
    "ut över vågorna som slog mot fören."
)

_GREEK_PROSE = (
    "Ήταν ένα ήσυχο πρωινό του καλοκαιριού όταν το μικρό πλοίο έφτασε στο "
    "λιμάνι του νησιού. Οι κάτοικοι περίμεναν στην προκυμαία και κοιτούσαν "
    "τη θάλασσα που έλαμπε κάτω από τον ήλιο."
)

_ENGLISH_PROSE = (
    "The quick brown fox is not the animal that was seen from the window, "
    "and it was not what they have said about this. The house is old but "
    "the roof is new, and from the top of that hill you have a view of "
    "the sea."
)

_GERMAN_PROSE = (
    "Der Mann ist nicht das, was die Leute schon immer über ihn gesagt "
    "haben, und auch die Frau wurde nicht gefragt. Das Haus ist alt, aber "
    "das Dach ist neu, und wenn man oben steht, sieht man das Meer."
)


class _Model:
    """A stand-in language model that records every request it is sent."""

    def __init__(self, answer: str | Exception, name: str = "small-local") -> None:
        self.answer = answer
        self.name = name
        self.model = name
        self.asked: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def serving(self, _log: Any):
        yield self

    def ask(self, system: str, prompt: str, tally: modelmeter.Tally | None = None):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(0.4, 24, 520)
        return self.answer


@pytest.fixture(autouse=True)
def _enable_switch(monkeypatch):
    monkeypatch.setenv(aibooklanguage.SWITCH_VARIABLE, "on")


class TestSampleCap:
    def test_ascii_sample_is_capped_to_2kb(self):
        big = ("The story continues across many chapters. " * 200).strip()
        assert len(big) > aibooklanguage.MAX_SAMPLE_CHARS
        capped = aibooklanguage.cap_sample(big)
        assert len(capped) <= aibooklanguage.MAX_SAMPLE_CHARS
        assert len(capped.encode("utf-8")) <= aibooklanguage.MAX_SAMPLE_BYTES
        assert capped == big[:aibooklanguage.MAX_SAMPLE_CHARS].strip()

    def test_multibyte_sample_is_capped_cleanly_to_2kb_bytes(self):
        big = _GREEK_PROSE * 30
        assert len(big.encode("utf-8")) > aibooklanguage.MAX_SAMPLE_BYTES
        capped = aibooklanguage.cap_sample(big)
        assert len(capped) <= aibooklanguage.MAX_SAMPLE_CHARS
        assert len(capped.encode("utf-8")) <= aibooklanguage.MAX_SAMPLE_BYTES
        assert "\ufffd" not in capped

    def test_prompt_carries_metadata_candidate_and_capped_excerpt(self):
        big = "Word " * 1000
        text = aibooklanguage.prompt(
            big, title="Moby.epub", metadata_language="eng", text_language="deu")
        assert "Moby.epub" in text
        assert "Declared metadata language: eng" in text
        assert "Stop-word / script detector candidate: deu" in text
        excerpt = text.split("Excerpt:\n", 1)[1].strip()
        assert len(excerpt) <= aibooklanguage.MAX_SAMPLE_CHARS
        assert len(excerpt.encode("utf-8")) <= aibooklanguage.MAX_SAMPLE_BYTES


class TestLanguageValidation:
    @pytest.mark.parametrize("raw,expected", [
        ("eng", "eng"),
        ("en", "eng"),
        ("en-US", "eng"),
        ("de_DE", "deu"),
        ("ger", "deu"),
        ("German", "deu"),
        ("sv", "swe"),
        ("swe", "swe"),
        ("Swedish", "swe"),
        ("el", "ell"),
        ("gre", "ell"),
        ("ell", "ell"),
        ("Greek", "ell"),
        ("pt-BR", "por"),
        ("zh-Hans-CN", "zho"),
        ("cmn", "zho"),
        ("jp", "jpn"),
        ("esl", "spa"),
        ("haw", "haw"),
    ])
    def test_valid_iso_spellings_normalize_to_iso_639_3(self, raw, expected):
        assert aibooklanguage.normalize_language_code(raw) == expected

    @pytest.mark.parametrize("raw", [
        "",
        "   ",
        "unsure",
        "unknown",
        "und",
        "mis",
        "mul",
        "zxx",
        "qaa",
        "qab",
        "qtz",
        "xx",
        "zzz",
        "elvish",
        "en (English)",
    ])
    def test_invalid_or_special_codes_are_rejected(self, raw):
        assert aibooklanguage.normalize_language_code(raw) == ""


class TestGuardNeverCallsOnAgreement:
    def test_agreeing_metadata_and_stop_words_never_call_model(self):
        model = _Model('{"language": "fra", "confidence": "high", "why": "x"}')
        result = aibooklanguage.detect_language(
            _GERMAN_PROSE, title="Haus.epub",
            metadata_language="deu", text_language="deu", models=[model])
        assert result == "deu"
        assert model.asked == []

    def test_unambiguous_text_without_metadata_never_calls_model(self):
        model = _Model('{"language": "deu", "confidence": "high", "why": "x"}')
        result = aibooklanguage.detect_language(
            _ENGLISH_PROSE, title="Fox.epub",
            metadata_language="", text_language="eng",
            text_ambiguous=False, models=[model])
        assert result == "eng"
        assert model.asked == []

    def test_empty_text_never_calls_model(self):
        model = _Model('{"language": "deu", "confidence": "high", "why": "x"}')
        assert aibooklanguage.detect_language(
            "   ", metadata_language="eng", text_language="", models=[model]
        ) == "eng"
        assert aibooklanguage.detect_language(
            "", metadata_language="", text_language="", models=[model]
        ) == ""
        assert model.asked == []

    def test_book_language_never_calls_model_when_metadata_and_text_agree(
        self, monkeypatch
    ):
        model = _Model('{"language": "fra", "confidence": "high", "why": "x"}')
        monkeypatch.setattr(booklanguage, "book_metadata_language",
                            lambda _src: "deu")
        monkeypatch.setattr(booklanguage, "_book_text_sample",
                            lambda _src, ram_base=None: ("deu", _GERMAN_PROSE, False))
        assert booklanguage.book_language("Haus.epub", models=[model]) == "deu"
        assert model.asked == []


class TestDisagreementAndUnknown:
    def test_disagreeing_metadata_and_text_calls_model_and_validates(self):
        model = _Model('{"language": "de-DE", "confidence": "high", '
                       '"why": "The excerpt is German prose."}')
        result = aibooklanguage.detect_language(
            _GERMAN_PROSE, title="WrongTag.epub",
            metadata_language="eng", text_language="deu", models=[model])
        assert result == "deu"
        assert len(model.asked) == 1
        _system, user_prompt = model.asked[0]
        assert "Declared metadata language: eng" in user_prompt
        assert "Stop-word / script detector candidate: deu" in user_prompt

    def test_metadata_unconfirmed_by_text_calls_model(self):
        model = _Model('{"language": "sv", "confidence": "high", '
                       '"why": "The excerpt is written in Swedish."}')
        result = aibooklanguage.detect_language(
            _SWEDISH_PROSE, title="Skutan.epub",
            metadata_language="eng", text_language="", models=[model])
        assert result == "swe"
        assert len(model.asked) == 1

    @pytest.mark.parametrize("sample,code,reply", [
        (_SWEDISH_PROSE, "swe",
         '{"language": "swe", "confidence": "high", "why": "Swedish prose"}'),
        (_GREEK_PROSE, "ell",
         '{"language": "el", "confidence": "high", "why": "Modern Greek text"}'),
    ])
    def test_language_outside_stop_word_table_calls_model(
        self, sample, code, reply, monkeypatch
    ):
        assert booklanguage.detect_text_language(sample) == ""
        model = _Model(reply)
        monkeypatch.setattr(booklanguage, "book_metadata_language",
                            lambda _src: "")
        monkeypatch.setattr(booklanguage, "_book_text_sample",
                            lambda _src, ram_base=None: ("", sample, False))
        assert booklanguage.book_language("Nordic.epub", models=[model]) == code
        assert len(model.asked) == 1

    def test_bilingual_borderline_text_calls_model(self, monkeypatch):
        bilingual = _ENGLISH_PROSE + "\n" + _GERMAN_PROSE
        det_code, ambiguous = booklanguage._detect_text_language_detail(bilingual)
        assert det_code in ("eng", "deu")
        assert ambiguous is True
        model = _Model('{"language": "deu", "confidence": "high", '
                       '"why": "Bilingual edition with German main narrative"}')
        monkeypatch.setattr(booklanguage, "book_metadata_language",
                            lambda _src: "")
        monkeypatch.setattr(booklanguage, "_book_text_sample",
                            lambda _src, ram_base=None: (det_code, bilingual, ambiguous))
        assert booklanguage.book_language("Bilingual.epub", models=[model]) == "deu"
        assert len(model.asked) == 1

    def test_book_language_disagreement_end_to_end(self, monkeypatch):
        model = _Model('{"language": "deu", "confidence": "high", '
                       '"why": "Metadata defaulted to en; text is German"}')
        monkeypatch.setattr(booklanguage, "book_metadata_language",
                            lambda _src: "eng")
        monkeypatch.setattr(booklanguage, "_book_text_sample",
                            lambda _src, ram_base=None: ("deu", _GERMAN_PROSE, False))
        assert booklanguage.book_language("Roman.epub", models=[model]) == "deu"
        assert len(model.asked) == 1


class TestFallbacks:
    @pytest.mark.parametrize("reply", [
        '{"language": "unsure", "confidence": "low", "why": "too short"}',
        '{"language": "unsure", "confidence": "high", "why": "cannot tell"}',
        '{"language": "zzz", "confidence": "high", "why": "made up code"}',
        '{"language": "und", "confidence": "high", "why": "undetermined"}',
        '{"language": "mul", "confidence": "high", "why": "multiple"}',
        '{"language": "deu", "confidence": "low", "why": "just guessing"}',
        '{"language": "deu", "confidence": "medium", "why": "maybe"}',
        '{"language": "deu", "confidence": 0.4, "why": "low score"}',
        '{"language": "deu", "why": "no confidence field"}',
        "not json at all",
    ])
    def test_unsure_invalid_or_low_confidence_falls_back_to_deterministic(self, reply):
        model = _Model(reply)
        result = aibooklanguage.detect_language(
            _GERMAN_PROSE, title="Book.epub",
            metadata_language="eng", text_language="deu", models=[model])
        assert result == "eng"
        assert len(model.asked) == 1

    def test_unsure_on_unknown_book_returns_empty(self):
        model = _Model('{"language": "unsure", "confidence": "low", "why": "gibberish"}')
        result = aibooklanguage.detect_language(
            "xyzzy plugh frotz", title="Mystery.epub",
            metadata_language="", text_language="", models=[model])
        assert result == ""

    def test_llm_error_falls_back_cleanly(self):
        model = _Model(llmclient.LlmError("connection refused"))
        result = aibooklanguage.detect_language(
            _GERMAN_PROSE, title="Book.epub",
            metadata_language="eng", text_language="deu", models=[model])
        assert result == "eng"
        assert len(model.asked) == 1

    def test_ladder_falls_through_to_second_model_on_error(self):
        broken = _Model(llmclient.LlmError("timeout"), name="first")
        backup = _Model('{"language": "deu", "confidence": "high", "why": "German"}',
                        name="second")
        result = aibooklanguage.detect_language(
            _GERMAN_PROSE, title="Book.epub",
            metadata_language="eng", text_language="deu",
            models=[broken, backup])
        assert result == "deu"
        assert len(broken.asked) == 1
        assert len(backup.asked) == 1


class TestSwitchRosterMeterAndLogging:
    def test_switch_off_never_calls_model(self, monkeypatch):
        monkeypatch.setenv(aibooklanguage.SWITCH_VARIABLE, "off")
        model = _Model('{"language": "deu", "confidence": "high", "why": "German"}')
        result = aibooklanguage.detect_language(
            _GERMAN_PROSE, title="Book.epub",
            metadata_language="eng", text_language="deu", models=[model])
        assert result == "eng"
        assert model.asked == []

    def test_book_language_with_switch_off_skips_text_when_metadata_answers(
        self, monkeypatch
    ):
        monkeypatch.setenv(aibooklanguage.SWITCH_VARIABLE, "off")
        monkeypatch.setattr(booklanguage, "book_metadata_language",
                            lambda _src: "deu")

        def _boom(*_a, **_k):
            raise AssertionError("text must not be read when switch is off")

        monkeypatch.setattr(booklanguage, "_book_text_sample", _boom)
        monkeypatch.setattr(booklanguage, "book_text_language", _boom)
        assert booklanguage.book_language("Book.epub") == "deu"

    def test_no_model_available_returns_deterministic(self, monkeypatch, tmp_path):
        llmroster.forget()
        for name in llmclient.SETTING_FIELDS.values():
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(llmroster.MODELS_VARIABLE,
                           str(tmp_path / "missing-models.toml"))
        result = aibooklanguage.detect_language(
            _SWEDISH_PROSE, title="Nordic.epub",
            metadata_language="", text_language="")
        assert result == ""

    def test_modelmeter_records_request_and_logs_decision(self, monkeypatch, tmp_path):
        logged: list[str] = []
        monkeypatch.setattr(aibooklanguage, "_log", logged.append)
        model = _Model('{"language": "swe", "confidence": "high", '
                       '"why": "Swedish prose"}', name="gemma3-4b")
        meter = tmp_path / "language.meter"
        result = aibooklanguage.detect_language(
            _SWEDISH_PROSE, title="Skutan.epub",
            metadata_language="", text_language="",
            models=[model], meter_file=str(meter))
        assert result == "swe"
        _spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 24, 520)
        assert any("gemma3-4b" in line and "swe" in line for line in logged)
        assert any("language:" in line for line in logged)

    def test_modelmeter_not_written_when_model_skipped(self, tmp_path):
        model = _Model('{"language": "fra", "confidence": "high", "why": "x"}')
        meter = tmp_path / "language.meter"
        result = aibooklanguage.detect_language(
            _GERMAN_PROSE, title="Haus.epub",
            metadata_language="deu", text_language="deu",
            models=[model], meter_file=str(meter))
        assert result == "deu"
        assert not meter.exists()

    def test_book_language_task_traits_and_light_lean(self):
        task = llmroster.BOOK_LANGUAGE
        assert task.name == "book-language"
        assert task.requires == ()
        assert task.prefers == ()
        assert task.lean == "light"
        assert llmroster.TASKS["book-language"] is task
