"""Tests for medialib.lib.aisubtitleads - asking a language model to spot
advert cues when subcleaner has no language profile or is absent.

What is pinned: head/tail sampling without duplicating overlapping cues,
prompt construction, strict JSON parsing and cue-number validation (rejecting
out-of-window numbers, non-int/bool values, and removing too many or all
cues), clean atomic rewriting of SRT and WebVTT files, skipping the model when
subcleaner supports a language, switch-off / no-model / LlmError behavior, and
modelmeter recording.
"""

from __future__ import annotations

import contextlib
import subprocess

import pytest

from medialib.lib import aisubtitleads, llmclient, llmroster, modelmeter, subtitleads

pytestmark = pytest.mark.pure


class _Model:
    """Stand-in language model recording prompts and returning ``answer`` (or
    raising it when it is an Exception)."""

    def __init__(self, answer, name: str = "stand-in", model: str = "stand-in"):
        self.answer = answer
        self.name = name
        self.model = model
        self.asked: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def serving(self, _log):
        yield self

    def ask(self, system: str, prompt: str, tally: modelmeter.Tally | None = None):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 25, 400)
        return self.answer


def _make_srt(texts: list[str], newline: str = "\n") -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        minute = index // 60
        second = index % 60
        timing = "00:%02d:%02d,000 --> 00:%02d:%02d,500" % (
            minute, second, minute, second)
        blocks.append("%d%s%s%s%s" % (index, newline, timing, newline, text))
    return (newline + newline).join(blocks) + newline


@pytest.fixture(autouse=True)
def _enable_switch(monkeypatch):
    monkeypatch.setenv(aisubtitleads.SWITCH_VARIABLE, "on")
    monkeypatch.setattr(subtitleads, "_said_unprofiled", set())


class TestSampling:
    def test_long_file_samples_first_20_and_last_20_cues(self):
        sub = aisubtitleads.parse_subtitle(
            _make_srt(["Line %d" % i for i in range(1, 101)]))
        assert sub is not None
        head, tail = aisubtitleads.sample_cues(sub.cues)
        assert [c.number for c in head] == list(range(1, 21))
        assert [c.number for c in tail] == list(range(81, 101))

    @pytest.mark.parametrize("count", [2, 12, 20, 39, 40])
    def test_short_file_does_not_duplicate_overlapping_cues(self, count):
        sub = aisubtitleads.parse_subtitle(
            _make_srt(["Line %d" % i for i in range(1, count + 1)]))
        assert sub is not None
        head, tail = aisubtitleads.sample_cues(sub.cues)
        assert tail == []
        assert [c.number for c in head] == list(range(1, count + 1))

    def test_41_cues_splits_into_20_head_and_20_tail(self):
        sub = aisubtitleads.parse_subtitle(
            _make_srt(["Line %d" % i for i in range(1, 42)]))
        assert sub is not None
        head, tail = aisubtitleads.sample_cues(sub.cues)
        assert [c.number for c in head] == list(range(1, 21))
        assert [c.number for c in tail] == list(range(22, 42))


class TestPrompt:
    def test_long_file_prompt_includes_language_windows_and_omission(self):
        texts = ["Synced by ExampleSubs"] + [
            "Dialogue %d" % i for i in range(2, 60)
        ] + ["Visit www.subs.example"]
        sub = aisubtitleads.parse_subtitle(_make_srt(texts))
        assert sub is not None
        head, tail = aisubtitleads.sample_cues(sub.cues)
        text = aisubtitleads.prompt(head, tail, "de", len(sub.cues))
        assert "German (de)" in text
        assert "Total cues in file: 60" in text
        assert "## First 20 cues" in text
        assert "## Last 20 cues" in text
        assert "20 middle cues omitted" in text
        assert "[1] 00:00:01,000 --> 00:00:01,500\nSynced by ExampleSubs" in text
        assert "[60] 00:01:00,000 --> 00:01:00,500\nVisit www.subs.example" in text
        assert "Dialogue 30" not in text

    def test_short_file_prompt_lists_all_cues_once(self):
        sub = aisubtitleads.parse_subtitle(_make_srt(["Ad", "Hello", "Bye"]))
        assert sub is not None
        head, tail = aisubtitleads.sample_cues(sub.cues)
        text = aisubtitleads.prompt(head, tail, "ja", len(sub.cues))
        assert "Japanese (ja)" in text
        assert "## All 3 cues" in text
        assert "omitted" not in text
        assert text.count("[2]") == 1

    def test_system_prompt_guards_dialogue_and_requests_json(self):
        for phrase in ("NEVER remove", "Spoken dialogue", "JSON only", '"ads"'):
            assert phrase in aisubtitleads.SYSTEM


class TestStrictParsing:
    def test_valid_in_window_cues_are_accepted_sorted_and_deduplicated(self):
        allowed = set(range(1, 21)) | set(range(81, 101))
        answer = 'Sure:\n```json\n{"ads": [100, 1, 1, 2], "why": "site plugs"}\n```'
        cues, why, refusal = aisubtitleads.parse_ad_cues(answer, allowed, 100)
        assert cues == [1, 2, 100]
        assert why == "site plugs"
        assert refusal == ""

    def test_rejects_cues_outside_head_tail_window(self):
        allowed = set(range(1, 21)) | set(range(81, 101))
        cues, _, refusal = aisubtitleads.parse_ad_cues(
            '{"ads": [50, 0, -1, 101], "why": "middle"}', allowed, 100)
        assert cues == []
        assert refusal == ""

        mixed, _, refusal = aisubtitleads.parse_ad_cues(
            '{"ads": [1, 50, 99], "why": "ends"}', allowed, 100)
        assert mixed == [1, 99]
        assert refusal == ""

    def test_rejects_non_int_and_bool_values(self):
        allowed = {1, 2, 3, 4, 5}
        answer = '{"ads": [true, false, "1", 1.5, null, [2], {"id": 3}], "why": "bad"}'
        cues, _, refusal = aisubtitleads.parse_ad_cues(answer, allowed, 20)
        assert cues == []
        assert refusal == ""

    def test_rejects_removing_all_cues(self):
        allowed = {1, 2, 3}
        cues, _, refusal = aisubtitleads.parse_ad_cues(
            '{"ads": [1, 2, 3], "why": "all"}', allowed, 3)
        assert cues == []
        assert "all" in refusal

    def test_rejects_removing_more_than_max_count(self):
        allowed = set(range(1, 21)) | set(range(181, 201))
        too_many = list(range(1, aisubtitleads.MAX_REMOVALS + 2))
        cues, _, refusal = aisubtitleads.parse_ad_cues(
            '{"ads": %s, "why": "spam"}' % too_many, allowed, 200)
        assert cues == []
        assert "cap" in refusal

    def test_rejects_removing_more_than_max_fraction(self):
        # For 12 cues, 25% cap is 3; asking to remove 4 must be refused.
        allowed = set(range(1, 13))
        cues, _, refusal = aisubtitleads.parse_ad_cues(
            '{"ads": [1, 2, 11, 12], "why": "too many"}', allowed, 12)
        assert cues == []
        assert "cap" in refusal

    def test_rejects_malformed_or_non_json_answer(self):
        cues, _, refusal = aisubtitleads.parse_ad_cues("no json here", {1, 2}, 10)
        assert cues == []
        assert refusal
        cues, _, refusal = aisubtitleads.parse_ad_cues('{"other": [1]}', {1, 2}, 10)
        assert cues == []
        assert refusal


class TestCleanRemoval:
    def test_removes_only_ad_cues_and_renumbers_srt_cleanly(self, tmp_path):
        texts = (
            ["Synced by ExampleSubs", "www.subs.example"]
            + ["Dialogue line %d" % i for i in range(1, 10)]
            + ["Rate this subtitle at example.org"]
        )
        srt = tmp_path / "Movie.de.srt"
        srt.write_text(_make_srt(texts), encoding="utf-8")
        model = _Model('{"ads": [1, 2, 12], "why": "syncer credit and website links"}')
        logs: list[str] = []
        meter = tmp_path / "meter"

        removed = aisubtitleads.clean(
            str(srt), "de", logs.append, models=[model], meter_file=str(meter))
        assert removed == 3

        parsed = aisubtitleads.read_subtitle(str(srt))
        assert parsed is not None
        assert [c.number for c in parsed.cues] == list(range(1, 10))
        assert [c.text for c in parsed.cues] == [
            "Dialogue line %d" % i for i in range(1, 10)]
        assert any("removed 3 advert cues" in line and "syncer credit" in line
                   for line in logs)
        _spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 25, 400)

    def test_preserves_utf8_bom_and_crlf_and_non_ascii(self, tmp_path):
        texts = ["Übersetzt von Beispiel.de"] + [
            "Schöne Grüße %d" % i for i in range(1, 8)]
        srt = tmp_path / "Film.de.srt"
        srt.write_bytes(b"\xef\xbb\xbf" + _make_srt(texts, newline="\r\n").encode("utf-8"))
        model = _Model('{"ads": [1], "why": "translator plug"}')

        assert aisubtitleads.clean(str(srt), "de", [].append, models=[model]) == 1
        raw = srt.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" in raw
        decoded = raw.decode("utf-8-sig")
        assert "Übersetzt von Beispiel.de" not in decoded
        assert "Schöne Grüße 1" in decoded

    def test_cleans_webvtt_preserving_header_notes_and_identifiers(self, tmp_path):
        vtt = tmp_path / "Movie.fr.vtt"
        vtt.write_text(
            "WEBVTT\n\n"
            "NOTE opening note\n\n"
            "ad-1\n00:00:01.000 --> 00:00:03.000\nSubtitles by SubSite.org\n\n"
            "dlg-1\n00:00:10.000 --> 00:00:12.000\nBonjour.\n\n"
            "dlg-2\n00:00:15.000 --> 00:00:17.000\nComment allez-vous?\n\n"
            "dlg-3\n00:00:20.000 --> 00:00:22.000\nTrès bien.\n",
            encoding="utf-8",
        )
        model = _Model('{"ads": [1], "why": "subtitle site plug"}')
        assert aisubtitleads.clean(str(vtt), "fr", [].append, models=[model]) == 1
        out = vtt.read_text(encoding="utf-8")
        assert out.startswith("WEBVTT\n\nNOTE opening note\n\n")
        assert "SubSite.org" not in out
        assert "dlg-1\n00:00:10.000 --> 00:00:12.000\nBonjour." in out
        assert "dlg-3\n00:00:20.000 --> 00:00:22.000\nTrès bien." in out

    def test_unparseable_file_is_never_clobbered_and_model_is_not_asked(self, tmp_path):
        bad = tmp_path / "Broken.de.srt"
        original = "[Script Info]\nTitle: Not SRT\n"
        bad.write_text(original, encoding="utf-8")
        model = _Model('{"ads": [1], "why": "should not run"}')
        assert aisubtitleads.clean(str(bad), "de", [].append, models=[model]) == 0
        assert bad.read_text(encoding="utf-8") == original
        assert model.asked == []

    def test_refused_over_removal_leaves_file_untouched_and_logs_warning(self, tmp_path):
        srt = tmp_path / "Short.de.srt"
        original = _make_srt(["Ad", "Hello", "World"])
        srt.write_text(original, encoding="utf-8")
        model = _Model('{"ads": [1, 2, 3], "why": "everything"}')
        logs: list[str] = []
        assert aisubtitleads.clean(str(srt), "de", logs.append, models=[model]) == 0
        assert srt.read_text(encoding="utf-8") == original
        assert any("WARNING:" in line and "left untouched" in line for line in logs)


class TestGuardsAndMetering:
    def test_switch_off_skips_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv(aisubtitleads.SWITCH_VARIABLE, "off")
        srt = tmp_path / "Movie.de.srt"
        srt.write_text(_make_srt(["Ad", "Hello", "World"]), encoding="utf-8")
        model = _Model('{"ads": [1], "why": "ad"}')
        assert aisubtitleads.clean(str(srt), "de", [].append, models=[model]) == 0
        assert model.asked == []

    def test_no_model_available_returns_zero(self, tmp_path, monkeypatch):
        for name in (llmclient.API_VARIABLE, llmclient.URL_VARIABLE,
                     llmclient.HARNESS_VARIABLE):
            monkeypatch.delenv(name, raising=False)
        llmroster.forget()
        srt = tmp_path / "Movie.de.srt"
        srt.write_text(_make_srt(["Ad", "Hello", "World"]), encoding="utf-8")
        assert aisubtitleads.clean(str(srt), "de", [].append) == 0

    def test_llm_error_logs_and_falls_back_to_next_model(self, tmp_path):
        srt = tmp_path / "Movie.de.srt"
        srt.write_text(_make_srt(["Synced by X"] + ["Line %d" % i for i in range(6)]),
                       encoding="utf-8")
        broken = _Model(llmclient.LlmError("timeout"), name="first")
        working = _Model('{"ads": [1], "why": "syncer"}', name="second", model="small")
        logs: list[str] = []
        meter = tmp_path / "meter"
        removed = aisubtitleads.clean(
            str(srt), "de", logs.append, models=[broken, working],
            meter_file=str(meter))
        assert removed == 1
        assert any("first" in line and "timeout" in line for line in logs)
        assert any("small" in line and "removed 1 advert cue" in line for line in logs)
        assert modelmeter.gathered(str(meter))[1].requests == 1

    def test_all_models_failing_returns_zero_and_records_zero_requests(self, tmp_path):
        srt = tmp_path / "Movie.de.srt"
        srt.write_text(_make_srt(["Ad", "Hello", "World"]), encoding="utf-8")
        broken = _Model(llmclient.LlmError("server down"), name="local")
        logs: list[str] = []
        meter = tmp_path / "meter"
        result = aisubtitleads.clean_file(
            str(srt), "de", logs.append, models=[broken], meter_file=str(meter))
        assert result == aisubtitleads.Result(0, False)
        assert any("server down" in line for line in logs)
        assert modelmeter.gathered(str(meter))[1].requests == 0

    def test_metering_context_manager_records_usage(self, tmp_path):
        srt = tmp_path / "Movie.de.srt"
        srt.write_text(_make_srt(["Ad", "Line 1", "Line 2", "Line 3", "Line 4"]),
                       encoding="utf-8")
        model = _Model('{"ads": [], "why": "all dialogue"}')
        meter = tmp_path / "run.meter"
        logs: list[str] = []
        with aisubtitleads.metering(str(meter)):
            assert aisubtitleads.clean(str(srt), "de", logs.append, models=[model]) == 0
        assert any("found no advert cues" in line for line in logs)
        assert modelmeter.gathered(str(meter))[1].requests == 1

    def test_roster_task_definition(self):
        task = llmroster.SUBTITLE_ADS
        assert task.name == "subtitle-ads"
        assert task.requires == ()
        assert task.prefers == ()
        assert task.lean == "light"
        assert llmroster.TASKS["subtitle-ads"] is task


class TestSubcleanerIntegration:
    def test_subcleaner_supported_language_skips_model(self, tmp_path, monkeypatch):
        srt = tmp_path / "Movie.en.srt"
        srt.write_text(_make_srt(["Line 1", "Line 2", "Line 3"]), encoding="utf-8")
        monkeypatch.setattr(subtitleads, "available", lambda: True)
        monkeypatch.setattr(
            subtitleads.toolcapture, "watched",
            lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=""))
        called = []
        monkeypatch.setattr(
            aisubtitleads, "clean_file",
            lambda *a, **k: called.append(a) or aisubtitleads.Result(0, True))
        assert subtitleads.clean(str(srt), "en", [].append) == 0
        assert called == []

    def test_unprofiled_language_falls_back_to_model_without_uncleaned_warning(
            self, tmp_path, monkeypatch):
        srt = tmp_path / "Movie.de.srt"
        srt.write_text(
            _make_srt(["Subtitles by Foo.org"] + ["Hallo %d" % i for i in range(6)]),
            encoding="utf-8",
        )
        model = _Model('{"ads": [1], "why": "subtitle site plug"}')
        monkeypatch.setattr(subtitleads, "available", lambda: True)
        monkeypatch.setattr(
            subtitleads.toolcapture, "watched",
            lambda *_a, **_k: subprocess.CompletedProcess(
                [], 0, stdout="WARNING: language 'de' have no regex profile"))
        monkeypatch.setattr(
            aisubtitleads.llmroster, "optional", lambda _task: ([model], ""))
        logs: list[str] = []

        assert subtitleads.clean(str(srt), "de", logs.append) == 1
        assert not any("not cleaned of adverts" in line for line in logs)
        assert any("removed 1 advert cue" in line for line in logs)
        assert "Foo.org" not in srt.read_text(encoding="utf-8")

    def test_missing_subcleaner_falls_back_to_model_when_enabled(
            self, tmp_path, monkeypatch):
        srt = tmp_path / "Movie.fr.srt"
        srt.write_text(
            _make_srt(["Synced by Ripper"] + ["Bonjour %d" % i for i in range(6)]),
            encoding="utf-8",
        )
        model = _Model('{"ads": [1], "why": "ripper plug"}')
        monkeypatch.setattr(subtitleads, "available", lambda: False)
        monkeypatch.setattr(
            aisubtitleads.llmroster, "optional", lambda _task: ([model], ""))
        logs: list[str] = []

        assert subtitleads.clean(str(srt), "fr", logs.append) == 1
        assert "Synced by Ripper" not in srt.read_text(encoding="utf-8")
        assert any("removed 1 advert cue" in line for line in logs)

    def test_unprofiled_language_warns_once_when_llm_switch_is_off(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv(aisubtitleads.SWITCH_VARIABLE, "off")
        srt = tmp_path / "Movie.de.srt"
        srt.write_text(_make_srt(["Ad", "Hallo 1", "Hallo 2"]), encoding="utf-8")
        monkeypatch.setattr(subtitleads, "available", lambda: True)
        monkeypatch.setattr(
            subtitleads.toolcapture, "watched",
            lambda *_a, **_k: subprocess.CompletedProcess(
                [], 0, stdout="WARNING: language 'de' have no regex profile"))
        logs: list[str] = []

        for _ in range(2):
            assert subtitleads.clean(str(srt), "de", logs.append) == 0
        assert len(logs) == 1
        assert "not cleaned of adverts" in logs[0]
