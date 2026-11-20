"""Tests for classifying an audio recording's content kind with a light
language model - medialib.lib.aicontentkind and its integration into
medialib.lib.onlinecover.

What is pinned:
- prompt construction includes folder path/name, files, sampled tags, duration,
  channel count, and fallback heuristic;
- strict JSON parsing & enum validation over onlinecover.CONTENTS and
  confidence levels;
- integration into onlinecover.book_subject, onlinecover.track_subject, and
  catalogue provider routing order (onlinecover.book_cover / CoverSearch);
- fallback to deterministic onlinecover.probable_content on "other", "unsure",
  low confidence, invalid JSON, LlmError, switch off, or no model available;
- logging of model decisions and modelmeter recording;
- llmroster.AUDIO_CONTENT task traits (no requires/prefers, lean="light").
"""

from __future__ import annotations

import contextlib
import os

import pytest

from medialib.lib import (
    aicontentkind,
    llmclient,
    llmroster,
    modelmeter,
    onlinecover,
)
from medialib.lib.onlinecover import CoverSearch, Row

pytestmark = pytest.mark.pure


class _Model:
    """Stand-in language model recording prompts and returning ``answer``
    (or raising it when it is an exception)."""

    def __init__(self, answer, name="small-local", model="gemma3-4b"):
        self.answer = answer
        self.name = name
        self.model = model
        self.asked: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def ask(self, system, prompt, tally=None):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(0.4, 24, 310)
        return self.answer


def _book(tmp_path):
    folder = tmp_path / "Hoerspiele" / "ARD Radio Tatort" / "Nachtschicht"
    (folder / "CD1").mkdir(parents=True)
    (folder / "CD1" / "01_Szene.mp3").write_bytes(b"x" * 10)
    (folder / "CD1" / "02_Szene.mp3").write_bytes(b"x" * 10)
    (folder / "booklet.txt").write_text("Ein Hoerspiel mit voller Besetzung")
    return folder


def _tracks(folder):
    return sorted(str(p) for p in folder.rglob("*.mp3"))


def _tags(path):
    return {
        "album": "ARD Radio Tatort - Nachtschicht",
        "artist": "WDR",
        "genre": "Hörspiel",
        "title": os.path.basename(path),
    }


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aicontentkind.SWITCH_VARIABLE, "on")
    monkeypatch.setenv(onlinecover.SWITCH_VARIABLE, "on")


class TestTaskAndSwitch:
    def test_audio_content_task_is_light_and_needs_no_special_traits(self):
        task = llmroster.AUDIO_CONTENT
        assert task.name == "audio-content"
        assert task.requires == ()
        assert task.prefers == ()
        assert task.lean == "light"
        assert llmroster.TASKS["audio-content"] is task

    @pytest.mark.parametrize("value, expected", [
        ("on", True),
        ("1", True),
        ("off", False),
        ("OFF", False),
        ("0", False),
        ("no", False),
        ("false", False),
    ])
    def test_switch_controls_enabled(self, monkeypatch, value, expected):
        monkeypatch.setenv(aicontentkind.SWITCH_VARIABLE, value)
        assert aicontentkind.enabled() is expected


class TestPrompt:
    def test_prompt_includes_folder_files_tags_duration_and_channels(self, tmp_path):
        folder = _book(tmp_path)
        text = aicontentkind.prompt(
            folder.name,
            folder_path=str(folder),
            tracks=_tracks(folder),
            seconds=55 * 60,
            stereo=False,
            heuristic=("podcast", "a length of 55:00"),
            probe=_tags,
        )
        assert "Nachtschicht" in text
        assert "Hoerspiele / ARD Radio Tatort" in text
        assert "CD1/01_Szene.mp3" in text
        assert "booklet.txt" in text
        assert "genre: Hörspiel" in text
        assert "artist: WDR" in text
        assert "Running time: 55:00" in text
        assert "mono" in text
        assert "podcast (a length of 55:00)" in text

    def test_prompt_marks_stereo_and_single_track_without_listing_siblings(
            self, tmp_path):
        show = tmp_path / "Podcasts" / "Kite Talk"
        show.mkdir(parents=True)
        track = show / "Episode 12.opus"
        track.write_bytes(b"x")
        (show / "Unrelated File.opus").write_bytes(b"y")
        text = aicontentkind.prompt(
            "Episode 12",
            folder_path=str(show),
            tracks=[str(track)],
            seconds=90 * 60,
            stereo=True,
            list_folder=False,
            probe=lambda _p: {"artist": "Kite Talk", "genre": "Podcast"},
        )
        assert "Kite Talk" in text
        assert "Episode 12.opus" in text
        assert "Unrelated File.opus" not in text
        assert "stereo" in text
        assert "Running time: 1:30:00" in text

    def test_long_folder_listing_is_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(aicontentkind, "MAX_LISTED_FILES", 2)
        folder = _book(tmp_path)
        assert aicontentkind._listing(str(folder))[-1] == "... and 1 more file(s)"


class TestStrictParsing:
    @pytest.mark.parametrize("content", ("podcast", "drama", "audiobook", "other"))
    @pytest.mark.parametrize("confidence", ("high", "medium", "low"))
    def test_valid_enum_values_parse(self, content, confidence):
        raw = '{"content": "%s", "confidence": "%s", "why": "clear tags"}' % (
            content.upper(), confidence.upper())
        verdict = aicontentkind.parse(raw)
        assert verdict == aicontentkind.Verdict(content, confidence, "clear tags")

    def test_parses_json_inside_markdown_fence(self):
        raw = ('Sure!\n```json\n{"content": "drama", "confidence": "high", '
               '"why": "Hoerspiel tag"}\n```')
        assert aicontentkind.parse(raw) == aicontentkind.Verdict(
            "drama", "high", "Hoerspiel tag")

    @pytest.mark.parametrize("raw", [
        "not json at all",
        '["podcast"]',
        '{"content": "unsure", "confidence": "high", "why": "cannot tell"}',
        '{"content": "music", "confidence": "high", "why": "songs"}',
        '{"content": "hoerspiel", "confidence": "high"}',
        '{"content": "audiobook", "confidence": "certain"}',
        '{"content": "audiobook", "confidence": 0.9}',
        '{"content": "audiobook"}',
        '{"confidence": "high"}',
        '{"content": null, "confidence": "high"}',
        '{"content": 1, "confidence": "high"}',
    ])
    def test_invalid_or_unknown_values_return_none(self, raw):
        assert aicontentkind.parse(raw) is None


class TestClassifyAndFallbacks:
    @pytest.mark.parametrize("content, confidence", [
        ("podcast", "high"),
        ("drama", "medium"),
        ("audiobook", "high"),
    ])
    def test_confident_classification_returns_content_and_records_meter(
            self, tmp_path, monkeypatch, content, confidence):
        said: list[str] = []
        monkeypatch.setattr(aicontentkind, "_log", said.append)
        folder = _book(tmp_path)
        model = _Model(
            '{"content": "%s", "confidence": "%s", "why": "matched folder clues"}'
            % (content, confidence))
        meter = tmp_path / "meter"
        result = aicontentkind.classify(
            folder.name, folder_path=str(folder), tracks=_tracks(folder),
            seconds=55 * 60, stereo=False, classifiers=[model],
            meter_file=str(meter), probe=_tags)
        assert result == (content, "the model: matched folder clues")
        assert len(model.asked) == 1
        assert any(
            "Content kind from gemma3-4b: %s (%s confidence" % (content, confidence)
            in line for line in said), said
        spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 24, 310)
        assert spans.seconds >= 0.0

    @pytest.mark.parametrize("answer", [
        '{"content": "other", "confidence": "high", "why": "a standalone lecture"}',
        '{"content": "drama", "confidence": "low", "why": "not enough clues"}',
        '{"content": "unsure", "confidence": "high", "why": "ambiguous"}',
        "I am not sure what this is.",
        '{"content": "music", "confidence": "high"}',
    ])
    def test_other_low_confidence_unsure_or_invalid_falls_back(
            self, tmp_path, monkeypatch, answer):
        said: list[str] = []
        monkeypatch.setattr(aicontentkind, "_log", said.append)
        folder = _book(tmp_path)
        model = _Model(answer)
        result = aicontentkind.classify(
            folder.name, folder_path=str(folder), tracks=_tracks(folder),
            seconds=55 * 60, stereo=False, classifiers=[model], probe=_tags)
        assert result is None
        assert any("falling back to the length heuristic" in line
                   for line in said), said

    def test_llm_error_falls_back_to_next_model_then_none(
            self, tmp_path, monkeypatch):
        said: list[str] = []
        monkeypatch.setattr(aicontentkind, "_log", said.append)
        folder = _book(tmp_path)
        broken = _Model(llmclient.LlmError("server down"), name="first")
        working = _Model('{"content": "drama", "confidence": "high", "why": "tag"}',
                         name="second", model="backup-4b")
        meter = tmp_path / "meter"
        assert aicontentkind.classify(
            folder.name, folder_path=str(folder), tracks=_tracks(folder),
            seconds=55 * 60, classifiers=[broken, working],
            meter_file=str(meter), probe=_tags) == ("drama", "the model: tag")
        assert any("No content kind from first: server down" in line
                   for line in said), said
        assert modelmeter.gathered(str(meter))[1].requests == 1

        # When every model fails, returns None and records 0 requests.
        meter2 = tmp_path / "meter2"
        assert aicontentkind.classify(
            folder.name, folder_path=str(folder), tracks=_tracks(folder),
            seconds=55 * 60, classifiers=[broken],
            meter_file=str(meter2), probe=_tags) is None
        assert modelmeter.gathered(str(meter2))[1].requests == 0

    def test_switch_off_skips_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv(aicontentkind.SWITCH_VARIABLE, "off")
        folder = _book(tmp_path)
        model = _Model('{"content": "drama", "confidence": "high"}')
        assert aicontentkind.classify(
            folder.name, folder_path=str(folder), tracks=_tracks(folder),
            seconds=55 * 60, classifiers=[model], probe=_tags) is None
        assert model.asked == []

    def test_no_model_available_returns_none(self, tmp_path, monkeypatch):
        llmroster.forget()
        for name in llmclient.SETTING_FIELDS.values():
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(llmroster.MODELS_VARIABLE,
                           str(tmp_path / "no-models.toml"))
        folder = _book(tmp_path)
        assert aicontentkind.classify(
            folder.name, folder_path=str(folder), tracks=_tracks(folder),
            seconds=55 * 60, probe=_tags) is None


class TestOnlineCoverIntegration:
    def test_book_subject_uses_model_classification_over_length_heuristic(
            self, tmp_path):
        # 55 min mono would deterministically be "podcast", but folder & tags
        # identify a German radio drama ("drama").
        folder = _book(tmp_path)
        assert onlinecover.probable_content(folder.name, 55 * 60, stereo=False) == (
            "podcast", "a length of 55:00")
        model = _Model('{"content": "drama", "confidence": "high", '
                       '"why": "ARD Radio Tatort Hoerspiel"}')
        meter = tmp_path / "meter"
        subject = onlinecover.book_subject(
            str(folder), _tracks(folder), 55 * 60, stereo=False,
            probe=_tags, meter_file=str(meter), classifiers=[model])
        assert subject.content == "drama"
        assert subject.why == "the model: ARD Radio Tatort Hoerspiel"
        assert modelmeter.gathered(str(meter))[1].requests == 1

    def test_book_cover_routes_to_model_classified_provider_rows(self, tmp_path):
        # Short 20-minute book would deterministically be "other", but the
        # model classifies it as "audiobook" so audiobook rows are queried.
        folder = tmp_path / "Ann Mirelle" / "The Short Novella"
        folder.mkdir(parents=True)
        model = _Model('{"content": "audiobook", "confidence": "high", '
                       '"why": "narrated novella"}')
        asked: list[str] = []
        rows = [
            Row("podcast", "youtube", "strict", 0, "podcast:{title}"),
            Row("drama", "audible", "strict", 0, "drama:{title}"),
            Row("audiobook", "audible", "strict", 0, "audiobook:{title}"),
            Row("other", "audible", "strict", 0, "other:{title}"),
        ]
        search = CoverSearch(rows, fetch=lambda url: (asked.append(url), None)[1],
                             download=lambda _u, _d: False, pace=False)
        onlinecover.book_cover(
            str(folder), [], 20 * 60, str(tmp_path / "out"),
            search=search, stereo=False, classifiers=[model])
        assert asked == ["audiobook:The+Short+Novella"]

    def test_book_subject_falls_back_to_probable_content_on_error_or_other(
            self, tmp_path):
        folder = _book(tmp_path)
        for bad in (
            _Model(llmclient.LlmError("timeout")),
            _Model('{"content": "other", "confidence": "high", "why": "misc"}'),
            _Model('{"content": "drama", "confidence": "low", "why": "guess"}'),
            _Model('{"content": "unsure", "confidence": "high"}'),
        ):
            subject = onlinecover.book_subject(
                str(folder), _tracks(folder), 55 * 60, stereo=False,
                probe=_tags, classifiers=[bad])
            assert (subject.content, subject.why) == ("podcast", "a length of 55:00")

    def test_youtube_id_skips_model_and_stays_podcast(self, tmp_path):
        folder = tmp_path / "Shows" / "Kite Talk [aB3_x-9QzK1]"
        folder.mkdir(parents=True)
        model = _Model('{"content": "audiobook", "confidence": "high"}')
        subject = onlinecover.book_subject(
            str(folder), [], 5 * 3600, stereo=False, probe=lambda _p: {},
            classifiers=[model])
        assert subject.content == "podcast"
        assert subject.youtube_id == "aB3_x-9QzK1"
        assert model.asked == []

    def test_track_subject_uses_model_classification_and_falls_back(self, tmp_path):
        show = tmp_path / "Podcasts" / "Kite Talk"
        show.mkdir(parents=True)
        track = show / "Episode 42.opus"
        track.write_bytes(b"x")
        # 90 min stereo would deterministically be "drama", but model sees
        # Podcast folder/tags and classifies as "podcast".
        model = _Model('{"content": "podcast", "confidence": "high", '
                       '"why": "Podcasts folder and genre"}')
        subject = onlinecover.track_subject(
            str(track), 90 * 60, stereo=True,
            probe=lambda _p: {"title": "Episode 42", "artist": "Kite Talk",
                              "genre": "Podcast"},
            classifiers=[model])
        assert subject.content == "podcast"
        assert subject.why == "the model: Podcasts folder and genre"

        # On failure, deterministic stereo heuristic ("drama") stands.
        fallback = onlinecover.track_subject(
            str(track), 90 * 60, stereo=True,
            probe=lambda _p: {"title": "Episode 42"},
            classifiers=[_Model(llmclient.LlmError("down"))])
        assert fallback.content == "drama"
        assert fallback.why == "a stereo recording of 1:30:00"
