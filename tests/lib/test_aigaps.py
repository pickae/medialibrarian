"""Tests for model-informed gap analysis in medialib.lib.aigaps.

The model is a stand-in that records what it was asked and returns JSON (or
raises LlmError). What is pinned: prompt construction for episode, dated and
numbered runs; strict validation of findings against input folders, allowed
kinds, bounded strings and safe source schemes; report formatting that leaves
deterministic gaps untouched; switch-off, missing web model, LlmError fallback
and modelmeter recording.
"""

from __future__ import annotations

import json

import pytest

from medialib.lib import aigaps, llmclient, modelmeter

pytestmark = pytest.mark.pure


class _Model:
    """Stand-in searching model returning ``answer`` (or raising it)."""

    def __init__(self, answer, can_search=True, name="stand-in"):
        self.answer = answer
        self.can_search = can_search
        self.name = name
        self.purpose = ""
        self.asked: list[tuple[str, str]] = []

    def searching(self, purpose=""):
        self.purpose = purpose
        return self if self.can_search else None

    def ask(self, system, prompt, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 60, 1200)
        return self.answer


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aigaps.SWITCH_VARIABLE, "on")


def _sample_runs() -> list[aigaps.RunSummary]:
    return [
        aigaps.RunSummary(
            folder="Show/Season 1",
            kind="episodes",
            present="S01E01-E08 (8 episode(s))",
            gaps=(),
            sample_names=("Show S01E01 Pilot.mkv", "Show S01E08 Penultimate.mkv"),
        ),
        aigaps.RunSummary(
            folder="Podcast",
            kind="dates",
            present="16 date(s)",
            gaps=("20231128-20240114 (nothing for 48 days, otherwise weekly)",),
            sample_names=("20231002 Episode 1.mp3", "20240226 Episode 16.mp3"),
            cadence="weekly",
            date_range="2023-10-02..2024-02-26",
        ),
        aigaps.RunSummary(
            folder="Audiobook",
            kind="numbers",
            present="01-02, 04 (3 item(s))",
            gaps=("03",),
            sample_names=("01 Chapter One.mp3", "04 Chapter Four.mp3"),
        ),
    ]


class TestPrompt:
    def test_includes_episodes_dates_and_numbered_runs(self):
        text = aigaps.prompt("MediaLibrary", _sample_runs())
        assert "MediaLibrary" in text
        assert "## Folder: Show/Season 1" in text
        assert "- Kind: episodes" in text
        assert "S01E01-E08 (8 episode(s))" in text
        assert "- Deterministic gaps: none" in text
        assert "Show S01E01 Pilot.mkv" in text

        assert "## Folder: Podcast" in text
        assert "- Kind: dates" in text
        assert "- Date range: 2023-10-02..2024-02-26 (weekly)" in text
        assert "20231128-20240114 (nothing for 48 days, otherwise weekly)" in text

        assert "## Folder: Audiobook" in text
        assert "- Kind: numbers" in text
        assert "- Deterministic gaps: 03" in text

    def test_select_runs_prioritizes_gaps_and_preserves_order(self):
        runs = [
            aigaps.RunSummary("Books/A", "numbers", "01-05 (5 item(s))"),
            aigaps.RunSummary("Show/S1", "episodes", "S01E01-E08 (8 episode(s))"),
            aigaps.RunSummary("Books/B", "numbers", "01, 03 (2 item(s))", gaps=("02",)),
            aigaps.RunSummary("Pod", "dates", "10 date(s)", gaps=("20240101-20240201",)),
        ]
        chosen = aigaps.select_runs(runs, limit=3)
        assert [r.folder for r in chosen] == ["Show/S1", "Books/B", "Pod"]

    def test_system_prompt_names_all_allowed_kinds_and_report_only_guard(self):
        for kind in aigaps.VALID_KINDS:
            assert kind in aigaps.SYSTEM
        assert "report-only" in aigaps.SYSTEM
        assert "Deterministic gaps" in aigaps.SYSTEM


class TestParseFindings:
    def test_accepts_valid_findings_in_fenced_json_and_normalizes_folder(self):
        payload = {
            "findings": [
                {
                    "folder": "./Show/Season 1/",
                    "kind": "MISSING_FINAL",
                    "target": "S01E09-E10",
                    "note": "Season 1 has 10 episodes in total.",
                    "source": "https://example.test/show",
                },
                {
                    "folder": "Podcast",
                    "kind": "false_gap_break",
                    "target": "20231128-20240114",
                    "note": "Scheduled winter hiatus between seasons.",
                    "source": "Official RSS feed: seasonal break notice",
                },
            ]
        }
        answer = "Here are the verified notes:\n```json\n%s\n```" % json.dumps(payload)
        parsed = aigaps.parse_findings(answer, {"Show/Season 1", "Podcast"})
        assert parsed == [
            aigaps.Finding(
                folder="Show/Season 1",
                kind="missing_final",
                target="S01E09-E10",
                note="Season 1 has 10 episodes in total.",
                source="https://example.test/show",
            ),
            aigaps.Finding(
                folder="Podcast",
                kind="false_gap_break",
                target="20231128-20240114",
                note="Scheduled winter hiatus between seasons.",
                source="Official RSS feed: seasonal break notice",
            ),
        ]

    def test_rejects_unknown_folders_invalid_kinds_empty_notes_and_bad_types(self):
        payload = {
            "findings": [
                "not a dict",
                {"folder": "Invented/Folder", "kind": "missing_final", "note": "Made up"},
                {"folder": "Show/Season 1", "kind": "delete_files", "note": "Bad kind"},
                {"folder": "Show/Season 1", "kind": "missing_final", "note": "   "},
                {"folder": "Show/Season 1", "kind": "missing_final", "note": 123},
                {"folder": "Show/Season 1", "kind": "missing_final",
                 "target": ["E09"], "note": "Bad target"},
                {"folder": "Show/Season 1", "kind": "missing_final", "note": "Bad url",
                 "source": "file:///etc/passwd"},
                {"folder": "Show/Season 1", "kind": "missing_final", "note": "Bad js",
                 "source": "javascript:alert(1)"},
                {"folder": "Show/Season 1", "kind": "missing_final", "note": "Bad ftp",
                 "source": "ftp://example.test/list"},
                {"folder": "Show/Season 1", "kind": "missing_special",
                 "target": "S00E01", "note": "Christmas special", "source": None},
            ]
        }
        parsed = aigaps.parse_findings(json.dumps(payload), {"Show/Season 1"})
        assert parsed == [
            aigaps.Finding(
                folder="Show/Season 1",
                kind="missing_special",
                target="S00E01",
                note="Christmas special",
                source="",
            )
        ]

    def test_bounds_long_strings_and_deduplicates(self):
        long_note = "word " * 120
        long_target = "E" * 200
        long_source = "https://example.test/" + "a" * 300
        entry = {
            "folder": "Show/Season 1",
            "kind": "short_season_note",
            "target": long_target,
            "note": long_note,
            "source": long_source,
        }
        parsed = aigaps.parse_findings(
            json.dumps({"findings": [entry, entry]}), {"Show/Season 1"})
        assert len(parsed) == 1
        assert len(parsed[0].note) == aigaps.MAX_NOTE_CHARS
        assert len(parsed[0].target) == aigaps.MAX_TARGET_CHARS
        assert len(parsed[0].source) == aigaps.MAX_SOURCE_CHARS

    def test_handles_non_json_and_empty_inputs(self):
        assert aigaps.parse_findings("nothing to report", {"Show"}) == []
        assert aigaps.parse_findings('{"findings": []}', {"Show"}) == []
        assert aigaps.parse_findings('[{"folder": "Show"}]', {"Show"}) == []
        one = '{"findings": [{"folder": "Show", "kind": "missing_final", "note": "x"}]}'
        assert aigaps.parse_findings(one, set()) == []


class TestFormatFindings:
    def test_formats_all_finding_kinds_grouped_by_folder(self):
        findings = [
            aigaps.Finding("Podcast", "false_gap_break",
                           "Scheduled winter hiatus",
                           target="20231128-20240114",
                           source="https://podcast.test/news"),
            aigaps.Finding("Show/Season 1", "missing_final",
                           "Season 1 aired 10 episodes",
                           target="S01E09-E10",
                           source="https://tvdb.test/show"),
            aigaps.Finding("Show/Season 1", "missing_special",
                           "Holiday special aired after E10",
                           target="S00E01"),
            aigaps.Finding("Show/Season 2", "short_season_note",
                           "Miniseries season of 6 episodes"),
            aigaps.Finding(".", "confirmed_missing",
                           "Episode 3 aired on 2024-01-15",
                           target="03"),
        ]
        lines = aigaps.format_findings(findings)
        assert lines == [
            "Model notes (web-checked; deterministic gaps above are unchanged):",
            "Podcast/",
            "  [likely hiatus/break] 20231128-20240114: Scheduled winter hiatus (source: https://podcast.test/news)",
            "Show/Season 1/",
            "  [missing final] S01E09-E10: Season 1 aired 10 episodes (source: https://tvdb.test/show)",
            "  [missing special] S00E01: Holiday special aired after E10",
            "Show/Season 2/",
            "  [short season] Miniseries season of 6 episodes",
            ".",
            "  [confirmed missing] 03: Episode 3 aired on 2024-01-15",
            "",
            "5 model note(s) across 4 folder(s)",
        ]

    def test_empty_findings_returns_no_lines(self):
        assert aigaps.format_findings([]) == []


class TestCheckGaps:
    def test_returns_validated_findings_and_records_modelmeter(self, tmp_path):
        answer = json.dumps({
            "findings": [
                {
                    "folder": "Show/Season 1",
                    "kind": "missing_final",
                    "target": "S01E09-E10",
                    "note": "Season 1 has 10 episodes.",
                    "source": "https://tvdb.test/s1",
                },
                {
                    "folder": "Ghost/Folder",
                    "kind": "missing_final",
                    "note": "Should be dropped",
                },
            ]
        })
        model = _Model(answer)
        meter = tmp_path / "meter"
        findings = aigaps.check_gaps("Library", _sample_runs(),
                                     searchers=[model], meter_file=str(meter))
        assert len(findings) == 1
        assert findings[0].folder == "Show/Season 1"
        assert findings[0].kind == "missing_final"
        assert model.purpose == aigaps.PURPOSE
        assert len(model.asked) == 1
        _spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 60, 1200)

    def test_switch_off_skips_model_completely(self, monkeypatch):
        monkeypatch.setenv(aigaps.SWITCH_VARIABLE, "off")
        model = _Model('{"findings": []}')
        assert aigaps.check_gaps("Library", _sample_runs(), searchers=[model]) == []
        assert model.asked == []

    def test_no_web_model_available_returns_empty(self, monkeypatch):
        for name in (llmclient.API_VARIABLE, llmclient.URL_VARIABLE,
                     llmclient.HARNESS_VARIABLE):
            monkeypatch.delenv(name, raising=False)
        assert aigaps.check_gaps("Library", _sample_runs()) == []

    def test_model_without_search_capability_is_not_asked(self):
        model = _Model('{"findings": []}', can_search=False)
        assert aigaps.check_gaps("Library", _sample_runs(), searchers=[model]) == []
        assert model.asked == []

    def test_llm_error_falls_back_to_next_searcher_and_records_meter(self, tmp_path):
        failing = _Model(llmclient.LlmError("timeout"), name="first")
        backup = _Model(json.dumps({
            "findings": [{
                "folder": "Podcast",
                "kind": "false_gap_break",
                "target": "20231128-20240114",
                "note": "Annual winter break",
            }]
        }), name="second")
        meter = tmp_path / "meter"
        findings = aigaps.check_gaps("Library", _sample_runs(),
                                     searchers=[failing, backup],
                                     meter_file=str(meter))
        assert len(findings) == 1
        assert findings[0].folder == "Podcast"
        assert len(failing.asked) == 1
        assert len(backup.asked) == 1
        assert modelmeter.gathered(str(meter))[1].requests == 1

    def test_all_searchers_failing_returns_empty(self, tmp_path):
        failing = _Model(llmclient.LlmError("offline"))
        meter = tmp_path / "meter"
        assert aigaps.check_gaps("Library", _sample_runs(),
                                 searchers=[failing], meter_file=str(meter)) == []
        assert modelmeter.gathered(str(meter))[1].requests == 0

    def test_empty_runs_does_not_ask_model(self):
        model = _Model('{"findings": []}')
        assert aigaps.check_gaps("Library", [], searchers=[model]) == []
        assert model.asked == []
