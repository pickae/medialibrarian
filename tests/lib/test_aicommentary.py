"""Tests for asking a language model to judge an ambiguous audio track -
medialib.lib.aicommentary - and its wiring into ingest-movies's commentary
detection.

The model is a stand-in that records what it was told and returns JSON (or
raises an error). What is pinned: prompt construction, lenient JSON extraction
with strict kind/confidence validation, the high-confidence guard that acts
only on commentary or description, modelmeter recording, switch-off and
no-model paths, and ingest_movies_run's flagging and recap behaviour.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_run as run_module
from medialib.lib import (
    aicommentary,
    llmclient,
    llmroster,
    modelmeter,
)
from medialib.lib import commentarydetection as cd

pytestmark = pytest.mark.pure

MOVIE = "/library/Film (1970)/Film (1970).mkv"

MAIN_MEASURE = cd.Measure(
    0.81,
    "where were you tonight harbour captain listen carefully nobody leaves "
    "this island before morning tide",
    "en",
)
SUSPECT_MEASURE = cd.Measure(
    0.80,
    "we shot this sequence over three nights because weather kept changing "
    "and the harbour captain was played by an old friend",
    "en",
)
MAIN_SAMPLE = aicommentary.TrackSample("Surround 5.1", "en", MAIN_MEASURE)
SUSPECT_SAMPLE = aicommentary.TrackSample("Stereo", "en", SUSPECT_MEASURE)


class _Model:
    """A stand-in model returning ``answer`` (or raising it when an exception)."""

    def __init__(self, answer, name: str = "stand-in"):
        self.answer = answer
        self.name = name
        self.model = name
        self.asked: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def ask(self, system: str, prompt: str, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 35, 600)
        return self.answer


def _track(id_, type_, codec="", channels="", language="eng", name="",
           commentary="false", visual="false"):
    return rules.Track(id=id_, type=type_, codec=codec, channels=channels,
                       language=language, name=name, commentary=commentary,
                       visual_impaired=visual)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aicommentary.SWITCH_VARIABLE, "on")


class TestPrompt:
    def test_includes_transcripts_names_languages_and_signal(self):
        text = aicommentary.prompt(
            MAIN_SAMPLE,
            aicommentary.TrackSample("null", "", SUSPECT_MEASURE),
            film="Harbour (1970)",
            why="speech 0.80 against the film's 0.81, 88% of its words not the film's",
        )
        assert "Harbour (1970)" in text
        assert "speech 0.80 against the film's 0.81" in text
        assert "Track name: Surround 5.1" in text
        assert "Track name: (none)" in text
        assert "Container language tag: en" in text
        assert "Container language tag: (none)" in text
        assert "Whisper detected language: en" in text
        assert "Speech share across sampled windows: 0.81" in text
        assert "Speech share across sampled windows: 0.80" in text
        assert "harbour captain listen carefully" in text
        assert "we shot this sequence over three nights" in text

    def test_system_prompt_names_every_kind_and_guard(self):
        for kind in aicommentary.KINDS:
            assert kind in aicommentary.SYSTEM
        for level in aicommentary.CONFIDENCES:
            assert level in aicommentary.SYSTEM

    def test_long_transcripts_are_bounded(self, monkeypatch):
        monkeypatch.setattr(aicommentary, "MAX_TRANSCRIPT_CHARS", 40)
        long_sample = aicommentary.TrackSample(
            "English", "en", cd.Measure(0.9, "word " * 50, "en"))
        text = aicommentary.prompt(MAIN_SAMPLE, long_sample)
        assert "..." in text
        assert ("word " * 40) not in text


class TestParse:
    @pytest.mark.parametrize("kind", aicommentary.KINDS)
    @pytest.mark.parametrize("confidence", aicommentary.CONFIDENCES)
    def test_accepts_every_allowed_kind_and_confidence(self, kind, confidence):
        raw = ('Here is the result:\n```json\n'
               '{"kind": "%s", "confidence": "%s", "reason": "  clear  clue  "}\n```'
               % (kind.upper(), confidence.upper()))
        opinion = aicommentary.parse(raw)
        assert opinion == aicommentary.Opinion(kind, confidence, "clear clue")

    @pytest.mark.parametrize("raw", [
        "",
        "plain text with no json",
        '["commentary"]',
        '{"kind": "soundtrack", "confidence": "high", "reason": "main"}',
        '{"kind": "commentary", "confidence": "very_high", "reason": "sure"}',
        '{"kind": "commentary", "confidence": true, "reason": "bool rejected"}',
        '{"kind": "commentary"}',
        '{"confidence": "high"}',
        '{"kind": 123, "confidence": "high"}',
    ])
    def test_rejects_invalid_or_unknown_answers(self, raw):
        assert aicommentary.parse(raw) is None

    def test_acted_kind_guard_only_fires_on_confident_commentary_or_description(self):
        assert aicommentary.Opinion("commentary", "high", "x").acted_kind == "commentary"
        assert aicommentary.Opinion("description", "high", "x").acted_kind == "description"
        for kind in ("commentary", "description"):
            for confidence in ("medium", "low"):
                assert aicommentary.Opinion(kind, confidence, "x").acted_kind == ""
        for kind in ("dub", "score", "music_and_effects", "unsure"):
            for confidence in aicommentary.CONFIDENCES:
                assert aicommentary.Opinion(kind, confidence, "x").acted_kind == ""


class TestJudge:
    def test_returns_opinion_and_records_modelmeter(self, tmp_path, capsys):
        model = _Model(
            '{"kind": "commentary", "confidence": "high", '
            '"reason": "Speakers discuss filming locations over the scene."}'
        )
        usage = aicommentary.Usage()
        meter = tmp_path / "meter"
        opinion = aicommentary.judge(
            MAIN_SAMPLE, SUSPECT_SAMPLE, film="Film (1970)",
            models=[model], usage=usage, meter_file=str(meter),
        )
        assert opinion == aicommentary.Opinion(
            "commentary", "high", "Speakers discuss filming locations over the scene.")
        assert len(model.asked) == 1
        assert usage.requests == 1
        assert "commentary:" in usage.line()
        spans, tally = modelmeter.gathered(str(meter))
        assert tally.requests == 1 and tally.tokens == 35 and tally.read == 600
        assert len(spans.all()) == 1
        assert "commentary:" in capsys.readouterr().err

    def test_switch_off_asks_no_model(self, monkeypatch):
        monkeypatch.setenv(aicommentary.SWITCH_VARIABLE, "off")
        model = _Model('{"kind": "commentary", "confidence": "high", "reason": "x"}')
        assert aicommentary.judge(MAIN_SAMPLE, SUSPECT_SAMPLE, models=[model]) is None
        assert model.asked == []

    def test_no_model_in_roster_returns_none(self, monkeypatch):
        monkeypatch.setattr(llmroster, "optional",
                            lambda _task: ([], "no language model is set up"))
        assert aicommentary.judge(MAIN_SAMPLE, SUSPECT_SAMPLE) is None

    def test_falls_back_to_next_model_on_error(self, capsys):
        broken = _Model(llmclient.LlmError("server down"), name="first")
        backup = _Model(
            '{"kind": "description", "confidence": "high", '
            '"reason": "Narrator describes visual action between lines."}',
            name="second",
        )
        usage = aicommentary.Usage()
        opinion = aicommentary.judge(
            MAIN_SAMPLE, SUSPECT_SAMPLE, models=[broken, backup], usage=usage)
        assert opinion is not None
        assert opinion.acted_kind == "description"
        assert len(broken.asked) == 1 and len(backup.asked) == 1
        assert usage.requests == 1
        assert "No commentary judgement from first: server down" in capsys.readouterr().err

    def test_unparseable_answer_returns_none_and_still_counts_usage(self, capsys):
        model = _Model("I cannot decide.")
        usage = aicommentary.Usage()
        assert aicommentary.judge(
            MAIN_SAMPLE, SUSPECT_SAMPLE, models=[model], usage=usage) is None
        assert usage.requests == 1
        assert "no readable answer" in capsys.readouterr().err


class TestIngestMoviesWiring:
    def _run(self, monkeypatch, tracks, answers, opinion):
        edits: list = []
        judged: list = []
        monkeypatch.setattr(run_module, "main_movies", lambda _root: [MOVIE])
        monkeypatch.setattr(rules, "_identify", lambda _movie: tracks)
        monkeypatch.setattr(
            cd, "probe",
            lambda _movie, main, others, _w: (
                {t.id: answers[t.id] for t in [main] + others}, ""),
        )
        monkeypatch.setattr(rules, "_run_capture",
                            lambda argv: edits.append(argv) or "")

        def fake_judge(main, suspect, *, film="", why="", usage=None, **_kw):
            judged.append((main, suspect, film, why, usage))
            if usage is not None and opinion is not None:
                usage.tally.add(1.0, 20, 400)
                usage.spans.add(0.0, 1.0)
            return opinion

        monkeypatch.setattr(aicommentary, "judge", fake_judge)
        state = SimpleNamespace(
            whisper={"modelMulti": "x"},
            whisper_said=[],
            unsure_commentaries=[],
            commentary_judge_usage=aicommentary.Usage(),
        )
        run_module._detect_commentaries(state, "/library")
        return state, edits, judged

    def test_confident_commentary_is_flagged_and_numbered(self, monkeypatch, capsys):
        tracks = [_track("0", "video"),
                  _track("1", "audio", "A_TRUEHD", "6", "eng", "Surround 5.1"),
                  _track("2", "audio", "A_AC3", "2", "eng", "Stereo")]
        opinion = aicommentary.Opinion(
            "commentary", "high", "Director explains how the scene was lit.")
        state, edits, judged = self._run(
            monkeypatch, tracks, {"1": MAIN_MEASURE, "2": SUSPECT_MEASURE}, opinion)
        assert len(judged) == 1
        assert judged[0][0].language == "en" and judged[0][1].name == "Stereo"
        assert judged[0][2] == "Film (1970)"
        assert edits == [["mkvpropedit", MOVIE, "--edit", "track:3",
                          "--set", "name=Commentary 1",
                          "--set", "flag-commentary=1"]]
        assert state.unsure_commentaries == []
        err = capsys.readouterr().err
        assert "A commentary (per model)" in err
        assert "Director explains how the scene was lit." in err

    def test_confident_description_is_flagged_visual_impaired(self, monkeypatch, capsys):
        tracks = [_track("0", "video"),
                  _track("1", "audio", "A_TRUEHD", "6", "eng", "Surround 5.1"),
                  _track("2", "audio", "A_AC3", "2", "eng", "English")]
        opinion = aicommentary.Opinion(
            "description", "high", "Narrator describes visual movements in pauses.")
        state, edits, _judged = self._run(
            monkeypatch, tracks, {"1": MAIN_MEASURE, "2": SUSPECT_MEASURE}, opinion)
        assert edits == [["mkvpropedit", MOVIE, "--edit", "track:3",
                          "--set", "flag-visual-impaired=1"]]
        assert state.unsure_commentaries == []
        err = capsys.readouterr().err
        assert "An audio description (per model)" in err
        assert "Narrator describes visual movements in pauses." in err

    @pytest.mark.parametrize("kind, confidence, label", [
        ("commentary", "medium", "commentary"),
        ("description", "low", "audio description"),
        ("dub", "high", "dub"),
        ("score", "high", "isolated score"),
        ("music_and_effects", "high", "music and effects"),
        ("unsure", "low", "unsure"),
    ])
    def test_non_confident_or_other_kinds_stay_unsure_with_opinion(
            self, monkeypatch, capsys, kind, confidence, label):
        tracks = [_track("0", "audio", "A_TRUEHD", "6", "eng", "Main"),
                  _track("1", "audio", "A_AC3", "2", "eng", "Stereo")]
        opinion = aicommentary.Opinion(kind, confidence, "short explanation")
        state, edits, _judged = self._run(
            monkeypatch, tracks, {"0": MAIN_MEASURE, "1": SUSPECT_MEASURE}, opinion)
        assert edits == []
        assert len(state.unsure_commentaries) == 1
        entry = state.unsure_commentaries[0]
        assert "model thinks %s (%s confidence): short explanation" % (
            label, confidence) in entry
        assert "Maybe a commentary, left as it is" in capsys.readouterr().err

    def test_no_model_opinion_leaves_original_unsure_line(self, monkeypatch):
        tracks = [_track("0", "audio", "A_TRUEHD", "6", "eng", "Main"),
                  _track("1", "audio", "A_AC3", "2", "eng", "Stereo")]
        state, edits, _judged = self._run(
            monkeypatch, tracks, {"0": MAIN_MEASURE, "1": SUSPECT_MEASURE}, None)
        assert edits == []
        assert len(state.unsure_commentaries) == 1
        assert "model thinks" not in state.unsure_commentaries[0]

    def test_report_commentary_judge_usage_prints_for_multiple_tracks(self, capsys):
        usage = aicommentary.Usage()
        usage.tally.add(1.0, 25, 300)
        usage.spans.add(0.0, 1.0)
        run_module._report_commentary_judge_usage(usage)
        assert capsys.readouterr().err == ""
        usage.tally.add(2.0, 30, 350)
        usage.spans.add(2.0, 4.0)
        run_module._report_commentary_judge_usage(usage)
        err = capsys.readouterr().err
        assert "Model time over the run (commentary judgement):" in err
        assert "commentary:" in err
