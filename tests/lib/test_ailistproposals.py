"""Tests for proposing additions to hand-kept word and extension lists from
logged ingest-movies reasons - medialib.lib.ailistproposals.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib

import pytest

from medialib.cli import ingest_movies
from medialib.lib import (
    ailistproposals,
    commentarynames,
    enums,
    imdbdata,
    llmclient,
    llmroster,
    modelmeter,
    runlog,
)

pytestmark = pytest.mark.pure


class _Model:
    """Stand-in language model recording requests and returning ``answer``
    (or raising it)."""

    def __init__(self, answer: str | Exception, name: str = "local-reasoner"):
        self.answer = answer
        self.name = name
        self.asked: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def ask(self, system: str, prompt: str, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 64, 1200)
        return self.answer


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(ailistproposals.SWITCH_VARIABLE, "on")


def _sample_reasons():
    reasons = ailistproposals.reasons_from_tagging(
        unmatched=["El Secreto (2009)"],
        ambiguous=[("/lib/Die Welle (2008)",
                    "one film in parts that do not stack",
                    ["Die Welle (2008) - Teil1.mkv", "Die Welle (2008) - Teil2.mkv"])],
        near_misses=[("/lib/Det Sjunde Inseglet (1957)",
                      "no confident TMDb match",
                      ['asked TMDb for "Det Sjunde Inseglet" (1957)'])],
        skips=[("/lib/A/sample.mp4", "/lib/A/sample.mp4")],
        root="/lib",
    )
    reasons += ailistproposals.reasons_from_commentary(
        [("./Das Boot (1981)/Das Boot (1981).mkv",
          "the commentaries' opening minutes do not say which track is which")],
        contexts={
            "./Das Boot (1981)/Das Boot (1981).mkv": [
                "listed: Audio commentary by Regisseur Wolfgang Petersen",
                "transcript (2): Hallo, ich bin Wolfgang Petersen.",
            ]
        },
    )
    return reasons


class TestCurrentLists:
    def test_collects_live_lists_dynamically_from_modules(self, monkeypatch):
        live = ailistproposals.current_lists()
        assert set(live) == set(ailistproposals.TARGET_LISTS)
        assert "director" in live[ailistproposals.NOT_NAMES_WORDS]
        assert "my name is" in live[ailistproposals.INTRO_PHRASES]
        assert tuple(ingest_movies.JUNK_EXTENSIONS) == live[ailistproposals.JUNK_EXTENSIONS]
        assert tuple(ingest_movies.SAMPLE_NAMES) == live[ailistproposals.SAMPLE_NAMES]
        assert tuple(imdbdata.ARTICLES) == live[ailistproposals.ARTICLES]
        assert tuple(enums.PART_WORDS) == live[ailistproposals.PART_WORDS]

        # Dynamic reflection: mutating a module list is seen immediately.
        monkeypatch.setattr(enums, "PART_WORDS", enums.PART_WORDS + ("teil",))
        assert "teil" in ailistproposals.current_lists()[ailistproposals.PART_WORDS]


class TestPromptConstruction:
    def test_prompt_includes_all_target_lists_and_logged_reasons(self):
        reasons = _sample_reasons()
        text = ailistproposals.prompt(reasons)

        for key in ailistproposals.TARGET_LISTS:
            assert key in text
        assert "near-miss" in text
        assert "Det Sjunde Inseglet" in text
        assert "ambiguous" in text
        assert "Die Welle (2008) - Teil1.mkv" in text
        assert "unmatched" in text
        assert "El Secreto (2009)" in text
        assert "commentary-refusal" in text
        assert "Regisseur Wolfgang Petersen" in text
        assert "ich bin Wolfgang Petersen" in text
        assert "skip" in text
        assert "sample.mp4" in text


class TestStrictParsingAndFiltering:
    def test_accepts_valid_proposals_across_all_six_lists(self):
        reply = json.dumps({"proposals": [
            {
                "list": ailistproposals.NOT_NAMES_WORDS,
                "value": "Régisseur",
                "evidence": "./Das Boot (1981)/Das Boot (1981).mkv",
                "rationale": "German job title for director.",
            },
            {
                "list": ailistproposals.INTRO_PHRASES,
                "value": "Ich bin",
                "evidence": "transcript (2): Hallo, ich bin Wolfgang Petersen.",
                "rationale": "German opening phrase introducing a speaker.",
            },
            {
                "list": ailistproposals.JUNK_EXTENSIONS,
                "value": ".URL",
                "evidence": "stray release shortcut .url file",
                "rationale": "Release internet shortcut junk file.",
            },
            {
                "list": ailistproposals.SAMPLE_NAMES,
                "value": "sample.mp4",
                "evidence": "./A/sample.mp4",
                "rationale": "MP4 sample clip matching existing sample.mkv names.",
            },
            {
                "list": ailistproposals.ARTICLES,
                "value": "Det",
                "evidence": "./Det Sjunde Inseglet (1957)",
                "rationale": "Swedish/Danish leading article.",
            },
            {
                "list": ailistproposals.PART_WORDS,
                "value": "Teil",
                "evidence": "Die Welle (2008) - Teil1.mkv",
                "rationale": "German word for part in split releases.",
            },
        ]})

        proposals = ailistproposals.parse_proposals(reply)
        assert [(p.target, p.value) for p in proposals] == [
            (ailistproposals.NOT_NAMES_WORDS, "regisseur"),
            (ailistproposals.INTRO_PHRASES, "ich bin"),
            (ailistproposals.JUNK_EXTENSIONS, "url"),
            (ailistproposals.SAMPLE_NAMES, "sample.mp4"),
            (ailistproposals.ARTICLES, "det"),
            (ailistproposals.PART_WORDS, "teil"),
        ]

    def test_rejects_unknown_lists_existing_duplicates_blank_overlong_and_generic(self):
        reply = json.dumps({"proposals": [
            # Unknown list name
            {"list": "unknown.LIST", "value": "foo", "evidence": "e", "rationale": "r"},
            # Already present in each list (including case/dot variants)
            {"list": ailistproposals.NOT_NAMES_WORDS, "value": "Director",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.INTRO_PHRASES, "value": "My Name Is",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.JUNK_EXTENSIONS, "value": ".TXT",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.SAMPLE_NAMES, "value": "sample.mkv",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.ARTICLES, "value": "Der",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.PART_WORDS, "value": "Part",
             "evidence": "e", "rationale": "r"},
            # Blank / overlong / multiline
            {"list": ailistproposals.NOT_NAMES_WORDS, "value": "   ",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.NOT_NAMES_WORDS, "value": "a" * 80,
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.INTRO_PHRASES, "value": "ich\nbin",
             "evidence": "e", "rationale": "r"},
            # Overly generic / protected media extensions / generic filenames
            {"list": ailistproposals.JUNK_EXTENSIONS, "value": "mkv",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.JUNK_EXTENSIONS, "value": ".srt",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.SAMPLE_NAMES, "value": "movie.mkv",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.SAMPLE_NAMES, "value": "../sample.mp4",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.INTRO_PHRASES, "value": "the",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.ARTICLES, "value": "with",
             "evidence": "e", "rationale": "r"},
            {"list": ailistproposals.PART_WORDS, "value": "film",
             "evidence": "e", "rationale": "r"},
            # Missing evidence or rationale
            {"list": ailistproposals.PART_WORDS, "value": "teil",
             "evidence": "", "rationale": "German part word."},
            {"list": ailistproposals.PART_WORDS, "value": "teil",
             "evidence": "Die Welle - Teil1.mkv", "rationale": ""},
            # Valid + duplicate of valid
            {"list": ailistproposals.PART_WORDS, "value": "teil",
             "evidence": "Die Welle - Teil1.mkv", "rationale": "German part word."},
            {"list": ailistproposals.PART_WORDS, "value": "TEIL",
             "evidence": "Die Welle - Teil2.mkv", "rationale": "Duplicate."},
        ]})

        proposals = ailistproposals.parse_proposals(reply)
        assert len(proposals) == 1
        assert proposals[0].target == ailistproposals.PART_WORDS
        assert proposals[0].value == "teil"

    def test_handles_non_json_or_missing_proposals_key(self):
        assert ailistproposals.parse_proposals("not json at all") == []
        assert ailistproposals.parse_proposals('{"other": []}') == []


class TestProposeWorkflow:
    def test_writes_report_under_logs_without_modifying_source_files(self, tmp_path):
        source_files = [
            pathlib.Path(commentarynames.__file__),
            pathlib.Path(ingest_movies.__file__),
            pathlib.Path(imdbdata.__file__),
            pathlib.Path(enums.__file__),
        ]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}

        reply = json.dumps({"proposals": [
            {
                "list": ailistproposals.NOT_NAMES_WORDS,
                "value": "regisseur",
                "evidence": "./Das Boot (1981)/Das Boot (1981).mkv",
                "rationale": "German director credit in commentary description.",
            },
            {
                "list": ailistproposals.INTRO_PHRASES,
                "value": "ich bin",
                "evidence": "transcript (2): Hallo, ich bin Wolfgang Petersen.",
                "rationale": "German commentary opening phrase.",
            },
        ]})
        model = _Model(reply, name="qwen-local")
        meter = tmp_path / "meter.jsonl"
        said: list[str] = []

        report_path = ailistproposals.propose(
            _sample_reasons(),
            str(tmp_path),
            models=[model],
            meter_file=str(meter),
            log=said.append,
        )

        assert report_path
        assert os.path.isfile(report_path)
        assert os.path.dirname(report_path) == os.path.join(str(tmp_path), "logs")
        assert os.path.basename(report_path).startswith("ingest-movies-list-proposals-")
        assert report_path.endswith(".txt")

        text = pathlib.Path(report_path).read_text(encoding="utf-8")
        assert "no source files were modified" in text
        assert "# Model: qwen-local" in text
        assert "[commentarynames._NOT_NAMES_WORDS]" in text
        assert "- regisseur" in text
        assert "[commentarynames._INTRO_PHRASES]" in text
        assert "- ich bin  (tuple: ('ich', 'bin'))" in text
        assert "evidence: ./Das Boot (1981)/Das Boot (1981).mkv" in text
        assert "rationale: German director credit" in text

        # Run output announces the report path and model meter rate line.
        assert any(report_path in line for line in said)
        assert any(ailistproposals.PURPOSE in line for line in said)

        # Model meter recorded the call.
        spans, tally = modelmeter.gathered(str(meter))
        assert tally.requests == 1
        assert tally.tokens == 64
        assert tally.read == 1200
        assert spans.seconds >= 0.0

        # No Python source file was touched.
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
        assert before == after

    def test_no_reasons_skips_model_and_roster_completely(self, tmp_path, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("roster must not be consulted when no reasons exist")

        monkeypatch.setattr(llmroster, "optional", _boom)
        model = _Model('{"proposals": []}')
        assert ailistproposals.propose([], str(tmp_path), models=[model]) == ""
        assert model.asked == []
        assert not (tmp_path / "logs").exists()

    def test_switch_off_skips_model_quietly(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ailistproposals.SWITCH_VARIABLE, "off")
        details: list[str] = []
        monkeypatch.setattr(ailistproposals, "_detail", details.append)
        model = _Model('{"proposals": []}')

        assert ailistproposals.propose(_sample_reasons(), str(tmp_path),
                                       models=[model]) == ""
        assert model.asked == []
        assert any(ailistproposals.SWITCH_VARIABLE in line for line in details)
        assert not (tmp_path / "logs").exists()

    def test_no_model_available_skips_quietly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llmroster, "optional",
                            lambda task: ([], "no models are configured"))
        details: list[str] = []
        monkeypatch.setattr(ailistproposals, "_detail", details.append)
        said: list[str] = []

        assert ailistproposals.propose(_sample_reasons(), str(tmp_path),
                                       log=said.append) == ""
        assert said == []
        assert any("no models are configured" in line for line in details)
        assert not (tmp_path / "logs").exists()

    def test_falls_back_to_next_model_in_ladder_on_llm_error(self, tmp_path):
        broken = _Model(llmclient.LlmError("server busy"), name="first")
        working = _Model(json.dumps({"proposals": [{
            "list": ailistproposals.ARTICLES,
            "value": "ett",
            "evidence": "Ett Glas Till (2020)",
            "rationale": "Swedish neuter indefinite article.",
        }]}), name="second")
        said: list[str] = []

        report_path = ailistproposals.propose(
            _sample_reasons(), str(tmp_path), models=[broken, working],
            log=said.append)

        assert report_path and os.path.isfile(report_path)
        assert len(broken.asked) == 1
        assert len(working.asked) == 1
        assert any("first" in line and "server busy" in line for line in said)
        assert "# Model: second" in pathlib.Path(report_path).read_text(encoding="utf-8")

    def test_empty_accepted_proposals_writes_no_report_file(self, tmp_path, monkeypatch):
        details: list[str] = []
        monkeypatch.setattr(ailistproposals, "_detail", details.append)
        model = _Model('{"proposals": []}')
        meter = tmp_path / "meter.jsonl"

        assert ailistproposals.propose(
            _sample_reasons(), str(tmp_path), models=[model],
            meter_file=str(meter)) == ""
        assert not (tmp_path / "logs").exists()
        _, tally = modelmeter.gathered(str(meter))
        assert tally.requests == 1
        assert any("No curated-list additions" in line for line in details)

    def test_enabled_reads_switch_variable(self, monkeypatch):
        monkeypatch.delenv(ailistproposals.SWITCH_VARIABLE, raising=False)
        assert ailistproposals.enabled() is True
        for off in ("off", "OFF", "0", "no", "false"):
            monkeypatch.setenv(ailistproposals.SWITCH_VARIABLE, off)
            assert ailistproposals.enabled() is False
        monkeypatch.setenv(ailistproposals.SWITCH_VARIABLE, "on")
        assert ailistproposals.enabled() is True
        assert runlog.detail is not None
