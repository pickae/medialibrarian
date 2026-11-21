"""Tests for light-model track role classification - medialib.lib.aitrackrole,
and its integration with medialib.lib.subtitletracks and medialib.lib.audiotracks.

The model is a stand-in that records what it was asked and returns a scripted
JSON answer (or raises LlmError). What is pinned: clear deterministic tracks
never call the model; borderline, unmeasurable, or unrecognized descriptive
tracks do; strict enum and high-confidence validation keep uncertain or
malformed answers from changing the deterministic role; switch-off, missing
models, and LlmError fall back safely; every model decision is logged and
recorded in modelmeter.
"""

import contextlib
import json

import pytest

from medialib.cli import ingest_movies
from medialib.lib import (
    aitrackrole,
    audiotracks,
    enums,
    llmclient,
    llmroster,
    modelmeter,
    subtitletracks,
)

pytestmark = pytest.mark.pure


class _FakeModel:
    """A stand-in light model recording prompts and returning scripted answers."""

    def __init__(self, answer, name="small-local", model="gemma3-4b"):
        self.answer = answer
        self.name = name
        self.model = model
        self.asked = []

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def ask(self, system, prompt, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(0.4, 24, 180)
        return self.answer


def _json_reply(role, confidence="high", why="clear cues"):
    return json.dumps({"role": role, "confidence": confidence, "why": why})


@pytest.fixture(autouse=True)
def _enable_switch(monkeypatch):
    monkeypatch.setenv(aitrackrole.SWITCH_VARIABLE, "on")


class TestSwitch:
    def test_on_by_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(aitrackrole.SWITCH_VARIABLE, raising=False)
        assert aitrackrole.enabled() is True

    @pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
    def test_off_values_disable(self, monkeypatch, value):
        monkeypatch.setenv(aitrackrole.SWITCH_VARIABLE, value)
        assert aitrackrole.enabled() is False


class TestBorderlineDetection:
    def test_clear_subtitle_tracks_do_not_trigger(self):
        sample = ["Plain spoken line."] * 20
        # Commentary is never questioned.
        assert aitrackrole.subtitle_borderline_reason(
            "commentary", cues=200, reference_cues=1000, texts=sample) == ""
        # Flagged SDH tracks without count ambiguity are settled.
        assert aitrackrole.subtitle_borderline_reason(
            "sdh", cues=0, reference_cues=0, texts=["[door]"] * 20,
            flagged=True) == ""
        # Clearly forced (10% of reference).
        assert aitrackrole.subtitle_borderline_reason(
            "full", cues=100, reference_cues=1000, texts=sample) == ""
        # Clearly full (80% of reference).
        assert aitrackrole.subtitle_borderline_reason(
            "full", cues=800, reference_cues=1000, texts=sample) == ""
        # Clearly forced track staying forced (10% of reference).
        assert aitrackrole.subtitle_borderline_reason(
            "forced", cues=100, reference_cues=1000, texts=sample) == ""
        # Clearly forced-flagged full track (85% of reference).
        assert aitrackrole.subtitle_borderline_reason(
            "forced", cues=850, reference_cues=1000, texts=sample) == ""
        # Borderline ratio with no texts and no descriptive title has no evidence.
        assert aitrackrole.subtitle_borderline_reason(
            "full", cues=200, reference_cues=1000) == ""
        # Clearly plain dialogue (1/20 = 5% SDH markers).
        plain = ["[knocking]"] + ["Plain spoken line."] * 19
        assert aitrackrole.subtitle_borderline_reason(
            "full", cues=1000, reference_cues=1000, texts=plain) == ""
        # Clearly SDH (25% SDH markers).
        sdh = ["[door slams]"] * 5 + ["Hello there."] * 15
        assert aitrackrole.subtitle_borderline_reason("full", texts=sdh) == ""
        # Plain language / generic names never trigger.
        for title in ("", "English", "Deutsch", "French", "Default", "Full", "Track 1"):
            assert aitrackrole.subtitle_borderline_reason(
                "full", name=title) == ""

    def test_borderline_and_unmeasurable_subtitles_trigger(self):
        sample = ["Plain spoken line."] * 20
        # Near forced threshold (20% and 30% of reference).
        assert aitrackrole.subtitle_borderline_reason(
            "full", cues=200, reference_cues=1000, texts=sample) != ""
        assert aitrackrole.subtitle_borderline_reason(
            "full", cues=300, reference_cues=1000, texts=sample) != ""
        # Forced-flagged track in the grey zone between 25% and 70%.
        assert aitrackrole.subtitle_borderline_reason(
            "forced", cues=450, reference_cues=1000, texts=sample) != ""
        assert aitrackrole.subtitle_borderline_reason(
            "forced", cues=650, reference_cues=1000, texts=sample) != ""
        # Near SDH threshold (8% and 12% SDH markers).
        near_low = ["[thunder]"] * 2 + ["Regular dialogue line."] * 23
        near_high = ["[thunder]"] * 3 + ["Regular dialogue line."] * 22
        assert aitrackrole.subtitle_borderline_reason("full", texts=near_low) != ""
        assert aitrackrole.subtitle_borderline_reason("full", texts=near_high) != ""
        # Unmeasurable reference (> 5000 packets or < 100 cues) with texts.
        assert aitrackrole.subtitle_borderline_reason(
            "full", cues=150, reference_cues=6000, texts=sample) != ""
        assert aitrackrole.subtitle_borderline_reason(
            "full", cues=40, reference_cues=80, texts=sample) != ""
        # Unrecognized descriptive title.
        assert aitrackrole.subtitle_borderline_reason(
            "full", name="Signs & Songs") != ""
        assert aitrackrole.subtitle_borderline_reason(
            "full", name="Untertitel für Hörgeschädigte") != ""

    def test_clear_audio_tracks_do_not_trigger(self):
        # Already classified roles or flagged tracks never ask.
        assert aitrackrole.audio_borderline_reason(
            "commentary", transcript="we shot this in Rome") == ""
        assert aitrackrole.audio_borderline_reason(
            "description", transcript="she opens the door") == ""
        assert aitrackrole.audio_borderline_reason(
            "main", name="Director notes", flagged=True) == ""
        # Plain titles without acoustic/transcript ambiguity never ask.
        for title in ("", "English", "Surround 5.1", "DTS-HD MA 7.1", "Stereo"):
            assert aitrackrole.audio_borderline_reason(
                "main", name=title) == ""
        # Clearly main mix acoustics.
        assert aitrackrole.audio_borderline_reason(
            "main", transcript="hello there",
            speech=0.35, main_speech=0.34, new_words=0.10) == ""
        # Clearly commentary acoustics (both signals far above thresholds).
        assert aitrackrole.audio_borderline_reason(
            "main", transcript="we shot this scene",
            speech=0.88, main_speech=0.35, new_words=0.85) == ""

    def test_borderline_audio_tracks_trigger(self):
        # Unsure commentary detection verdict.
        assert aitrackrole.audio_borderline_reason(
            "main", transcript="talking over the scene", unsure=True) != ""
        # Split signals: high speech, low new words.
        assert aitrackrole.audio_borderline_reason(
            "main", transcript="talking over the scene",
            speech=0.78, main_speech=0.35, new_words=0.20) != ""
        # Near threshold on both speech gain and new words.
        assert aitrackrole.audio_borderline_reason(
            "main", transcript="talking over the scene",
            speech=0.61, main_speech=0.50, new_words=0.52) != ""
        # Transcript available without acoustic comparison.
        assert aitrackrole.audio_borderline_reason(
            "main", transcript="He walks slowly toward the window.") != ""
        # Unrecognized descriptive title.
        assert aitrackrole.audio_borderline_reason(
            "main", name="Hörfilm") != ""


class TestPromptBuilding:
    def test_subtitle_prompt_samples_cues_and_includes_metadata(self):
        cues = [f"Cue number {i}" for i in range(100)]
        sampled = aitrackrole.sample_cues(cues, limit=12)
        assert len(sampled) == 12
        assert sampled[0] == "Cue number 0"
        assert sampled[-1] == "Cue number 99"

        text = aitrackrole.subtitle_prompt(
            role="full", name="Signs & Songs", language="eng",
            cues=100, reference_cues=400, texts=cues,
            reason="cue share near forced threshold",
        )
        assert "Title: Signs & Songs" in text
        assert "Language: eng" in text
        assert "Current deterministic role: full" in text
        assert "Cue count: 100" in text
        assert "Longest other subtitle in film: 400 cues (ratio 0.25)" in text
        assert "Cue number 0" in text and "Cue number 99" in text
        for role in enums.SUBTITLE_ROLES:
            assert role in text

    def test_audio_prompt_samples_transcripts_and_includes_metadata(self):
        long_text = " ".join(f"word{i}" for i in range(600))
        clipped = aitrackrole.sample_transcript(long_text, max_chars=200)
        assert len(clipped) <= 200
        assert "[...]" in clipped

        text = aitrackrole.audio_prompt(
            role="main", name="Bonus Track", language="eng",
            transcript="We filmed this sequence on location in Vienna.",
            main_transcript="Get in the car! Hurry up!",
            speech=0.62, main_speech=0.48, new_words=0.45,
            reason="acoustic commentary check was unsure",
        )
        assert "Title: Bonus Track" in text
        assert "Language: eng" in text
        assert "Track speech share: 0.62" in text
        assert "Film main track speech share: 0.48" in text
        assert "Words absent from film main track: 45%" in text
        assert "We filmed this sequence on location in Vienna." in text
        assert "Get in the car! Hurry up!" in text
        for role in enums.AUDIO_ROLES:
            assert role in text


class TestStrictParsingAndValidation:
    def test_parses_clean_and_fenced_json(self):
        v1 = aitrackrole.parse_verdict(
            _json_reply("sdh", "high", "bracketed sounds"),
            enums.SUBTITLE_ROLES,
        )
        assert v1 == aitrackrole.Verdict("sdh", "high", "bracketed sounds")
        assert aitrackrole.is_actionable(v1, enums.SUBTITLE_ROLES) is True

        fenced = (
            'Sure:\n```json\n{"role": "description", "confidence": "high", '
            '"why": "narrates visual actions"}\n```'
        )
        v2 = aitrackrole.parse_verdict(fenced, enums.AUDIO_ROLES)
        assert v2 == aitrackrole.Verdict(
            "description", "high", "narrates visual actions")
        assert aitrackrole.is_actionable(v2, enums.AUDIO_ROLES) is True

    @pytest.mark.parametrize("raw", [
        "",
        "sdh",
        "[]",
        '{"role": "description", "confidence": "high"}',  # audio role in subtitle enum
        '{"role": "dub", "confidence": "high"}',          # invalid role
        '{"role": "sdh", "confidence": "certain"}',       # invalid confidence
        '{"role": 123, "confidence": "high"}',
        '{"confidence": "high"}',
    ])
    def test_rejects_malformed_or_out_of_enum_subtitle_answers(self, raw):
        assert aitrackrole.parse_verdict(raw, enums.SUBTITLE_ROLES) is None

    def test_rejects_subtitle_role_for_audio(self):
        raw = _json_reply("sdh", "high")
        assert aitrackrole.parse_verdict(raw, enums.AUDIO_ROLES) is None

    @pytest.mark.parametrize("role,confidence", [
        ("unsure", "high"),
        ("sdh", "medium"),
        ("sdh", "low"),
        ("forced", "low"),
    ])
    def test_non_actionable_verdicts_do_not_override(self, role, confidence):
        verdict = aitrackrole.parse_verdict(
            _json_reply(role, confidence), enums.SUBTITLE_ROLES)
        assert verdict is not None
        assert aitrackrole.is_actionable(verdict, enums.SUBTITLE_ROLES) is False


class TestResolutionAndFallbacks:
    def test_records_modelmeter_and_logs_decision(self, tmp_path, monkeypatch):
        meter = tmp_path / "meter"
        model = _FakeModel(_json_reply("sdh", "high", "sound cues throughout"))
        logged = []
        details = []
        monkeypatch.setattr(aitrackrole, "_log", logged.append)
        monkeypatch.setattr(aitrackrole, "_detail", details.append)

        role = aitrackrole.resolve_subtitle_role(
            "full", said="full",
            texts=["[music]"] * 2 + ["Dialogue line."] * 23,
            name="English", language="eng",
            models=[model], meter_file=str(meter),
        )

        assert role == "sdh"
        assert len(model.asked) == 1
        spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 24, 180)
        assert any(aitrackrole.PURPOSE in line for line in details)
        assert any("sdh" in line and "high" in line for line in logged)

    def test_keeps_deterministic_on_unsure_low_confidence_or_malformed(self, monkeypatch):
        near_sdh = ["[music]"] * 2 + ["Dialogue line."] * 23
        for reply in (
            _json_reply("unsure", "high", "too ambiguous"),
            _json_reply("sdh", "medium", "maybe"),
            _json_reply("sdh", "low", "guess"),
            "not json at all",
            _json_reply("bogus", "high"),
        ):
            model = _FakeModel(reply)
            logged = []
            monkeypatch.setattr(aitrackrole, "_log", logged.append)
            role = aitrackrole.resolve_subtitle_role(
                "full", said="full", texts=near_sdh, models=[model])
            assert role == "full"
            assert len(model.asked) == 1
            assert logged

    def test_llm_error_falls_back_to_next_model_or_deterministic(self, monkeypatch):
        near_sdh = ["[music]"] * 2 + ["Dialogue line."] * 23
        broken = _FakeModel(llmclient.LlmError("timeout"), name="first")
        backup = _FakeModel(_json_reply("sdh", "high", "cues"), name="second")
        logged = []
        monkeypatch.setattr(aitrackrole, "_log", logged.append)

        role = aitrackrole.resolve_subtitle_role(
            "full", said="full", texts=near_sdh, models=[broken, backup])
        assert role == "sdh"
        assert len(broken.asked) == 1 and len(backup.asked) == 1
        assert any("first" in line and "timeout" in line for line in logged)

        only_broken = _FakeModel(llmclient.LlmError("offline"), name="first")
        role2 = aitrackrole.resolve_subtitle_role(
            "full", said="full", texts=near_sdh, models=[only_broken])
        assert role2 == "full"

    def test_switch_off_never_calls_model(self, monkeypatch):
        monkeypatch.setenv(aitrackrole.SWITCH_VARIABLE, "off")
        model = _FakeModel(_json_reply("sdh", "high"))
        near_sdh = ["[music]"] * 2 + ["Dialogue line."] * 23
        role = aitrackrole.resolve_subtitle_role(
            "full", said="full", texts=near_sdh, models=[model])
        assert role == "full"
        assert model.asked == []

    def test_no_model_available_returns_deterministic(self, monkeypatch):
        monkeypatch.setattr(
            llmroster, "optional", lambda task: ([], "no model is configured"))
        near_sdh = ["[music]"] * 2 + ["Dialogue line."] * 23
        role = aitrackrole.resolve_subtitle_role(
            "full", said="full", texts=near_sdh)
        assert role == "full"


class TestSubtitleAndAudioIntegration:
    def test_subtitle_refine_role_asks_only_when_borderline(self, monkeypatch):
        model = _FakeModel(_json_reply("forced", "high", "only on-screen signs"))
        monkeypatch.setattr(llmroster, "optional", lambda task: ([model], ""))

        # Clear full track (800 / 1000) does NOT call the model.
        assert subtitletracks.refine_role("full", 800, 1000) == "full"
        assert model.asked == []

        # Borderline ratio (280 / 1000) DOES call the model and adopts "forced".
        assert subtitletracks.refine_role(
            "full", 280, 1000, texts=["SIGN: TOKYO", "HOTEL"]) == "forced"
        assert len(model.asked) == 1

    def test_subtitle_role_checks_unrecognized_descriptive_title_when_allowed(self, monkeypatch):
        model = _FakeModel(_json_reply("forced", "high", "signs and songs"))
        monkeypatch.setattr(llmroster, "optional", lambda task: ([model], ""))

        # Default allow_llm=False stays purely deterministic.
        assert subtitletracks.subtitle_role("Signs & Songs") == "full"
        assert model.asked == []

        # With allow_llm=True, plain titles still skip the model.
        assert subtitletracks.subtitle_role("English", allow_llm=True) == "full"
        assert model.asked == []

        # Flagged/recognized titles skip the model even with allow_llm=True.
        assert subtitletracks.subtitle_role("SDH", allow_llm=True) == "sdh"
        assert model.asked == []

        # Unrecognized descriptive title asks the model.
        assert subtitletracks.subtitle_role(
            "Signs & Songs", language="eng", allow_llm=True) == "forced"
        assert len(model.asked) == 1

    def test_audio_refine_role_resolves_unsure_commentary_and_description(self, monkeypatch):
        commentary_model = _FakeModel(
            _json_reply("commentary", "high", "director discusses filming"))
        monkeypatch.setattr(
            llmroster, "optional", lambda task: ([commentary_model], ""))

        role = audiotracks.refine_audio_role(
            "main", name="English", language="eng",
            transcript="We shot this alley scene in three takes on a cold night.",
            main_transcript="Stop right there! Drop the bag!",
            speech=0.72, main_speech=0.40, unsure=True,
        )
        assert role == "commentary"
        assert len(commentary_model.asked) == 1

        desc_model = _FakeModel(
            _json_reply("description", "high", "narrates visual actions"))
        monkeypatch.setattr(
            llmroster, "optional", lambda task: ([desc_model], ""))

        role2 = audiotracks.refine_audio_role(
            "main", name="English", language="eng",
            transcript="She steps quietly through the doorway and turns off the lamp.",
            main_transcript="Goodnight.",
            speech=0.65, main_speech=0.45, unsure=True,
        )
        assert role2 == "description"
        assert len(desc_model.asked) == 1

    def test_audio_role_and_is_main_check_descriptive_title_only_when_allowed(self, monkeypatch):
        model = _FakeModel(_json_reply("description", "high", "German audio description"))
        monkeypatch.setattr(llmroster, "optional", lambda task: ([model], ""))

        assert audiotracks.audio_role("Hörfilm") == "main"
        assert model.asked == []

        # Plain title skips model even when allow_llm=True.
        assert audiotracks.is_main("Deutsch", language="deu", allow_llm=True) is True
        assert model.asked == []

        # Unrecognized descriptive title asks model.
        assert audiotracks.is_main("Hörfilm", language="deu", allow_llm=True) is False
        assert len(model.asked) == 1

    def test_ingest_movies_subtitle_role_edits_never_changes_into_commentary(self, monkeypatch):
        model = _FakeModel(_json_reply("commentary", "high", "speaker talks about film"))
        monkeypatch.setattr(llmroster, "optional", lambda task: ([model], ""))

        full = ingest_movies.Track(
            id="1", type="subtitles", codec="S_TEXT/UTF8", language="eng",
            name="English", forced="false", hearing_impaired="false",
            commentary="false", frames="1000",
        )
        borderline = ingest_movies.Track(
            id="2", type="subtitles", codec="S_TEXT/UTF8", language="eng",
            name="Bonus Text", forced="false", hearing_impaired="false",
            commentary="false", frames="260",
        )
        # Even if the model returns "commentary", subtitle_role_edits refuses
        # to turn a subtitle into or out of commentary.
        edits = ingest_movies.subtitle_role_edits(
            [full, borderline], texts={"2": ["We filmed this in Paris."] * 20})
        assert "2" not in edits
        assert len(model.asked) == 1
