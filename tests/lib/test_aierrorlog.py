"""Unit tests for advisory error-log triage (`medialib/lib/aierrorlog.py`)."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from medialib.lib import (
    aierrorlog,
    errorlog,
    llmclient,
    llmroster,
    llmtraits,
    modelmeter,
    runcontext,
)

pytestmark = pytest.mark.pure


def _fake_llm(name: str, reply: str | Exception, *,
              model: str = "qwen3-local",
              on_ask=None,
              prompt_tokens: int = 120,
              completion_tokens: int = 45):
    calls: list[tuple[str, str]] = []

    class FakeBound:
        def __init__(self) -> None:
            self.name = name
            self.model = model

        def ask(self, system: str, prompt: str, tally=None) -> str:
            calls.append((system, prompt))
            if on_ask is not None:
                on_ask(system, prompt)
            if isinstance(reply, Exception):
                raise reply
            if tally is not None:
                tally.add(0.25, completion_tokens, prompt_tokens)
            return reply

    class FakeLlm:
        def __init__(self) -> None:
            self.name = name
            self.model = model
            self.calls = calls

        @contextmanager
        def serving(self, _detail=None):
            yield FakeBound()

    return FakeLlm()


def _write_sample_log(tmp_path) -> str:
    """Create a real error log with both matched and unmatched records."""
    path = errorlog.record(
        "tool-failed",
        "ffmpeg failed on track1.flac",
        tool_argv=["ffmpeg", "-i", "/lib/album/track1.flac", "out.opus"],
        tool_status="exit 1",
        tool_output=[
            "Input #0, flac, from '/lib/album/track1.flac'",
            "No space left on device",
        ],
        causes=[errorlog.CAUSES["disk-full"].says],
    )
    errorlog.record(
        "tool-failed",
        "ffmpeg failed on track2.flac",
        tool_argv=["ffmpeg", "-i", "/lib/album/track2.flac", "out.opus"],
        tool_status="exit 1",
        tool_output=[
            "[Parsed_loudnorm_0 @ 0x55b2a10] Invalid filter option 'bogus_opt'",
            "Error initializing complex filters: Invalid argument",
        ],
    )
    try:
        raise ValueError("unexpected tag layout in track3")
    except ValueError as exc:
        errorlog.record("crash", "unexpected ValueError", exc=exc)
    return path


class TestUnmatchedRecords:
    def test_skips_notable_and_causes_and_keeps_unmatched(self, tmp_path):
        path = _write_sample_log(tmp_path)
        text = Path(path).read_text(encoding="utf-8")
        records = aierrorlog.parse_records(text)
        assert len(records) == 3

        unmatched = aierrorlog.unmatched_records(records)
        assert len(unmatched) == 2
        assert [r.kind for r in unmatched] == ["tool-failed", "crash"]
        assert "Invalid filter option" in "\n".join(unmatched[0].output_lines)
        assert any("ValueError: unexpected tag layout" in line
                   for line in unmatched[1].traceback_lines)

    def test_skips_notable_even_when_cause_line_absent(self):
        # Even if a record had no recorded `cause:` line, matching a NOTABLE
        # regex in output or `what` marks it as deterministically explained.
        notable_output = aierrorlog.ErrorRecord(
            index=1, severity="ERROR", kind="tool-failed",
            what="ffmpeg failed",
            output_lines=("Invalid data found when processing input",),
        )
        notable_what = aierrorlog.ErrorRecord(
            index=2, severity="ERROR", kind="error",
            what="Permission denied writing /lib/a.mkv",
        )
        self_explaining = aierrorlog.ErrorRecord(
            index=3, severity="ERROR", kind="input-missing",
            what="/lib/missing does not exist",
        )
        genuinely_unmatched = aierrorlog.ErrorRecord(
            index=4, severity="ERROR", kind="tool-failed",
            what="mkvmerge failed",
            output_lines=("Error: Track 3 uses unsupported codec V_CUSTOM",),
        )

        assert aierrorlog.is_matched(notable_output)
        assert aierrorlog.is_matched(notable_what)
        assert aierrorlog.is_matched(self_explaining)
        assert not aierrorlog.is_matched(genuinely_unmatched)
        assert aierrorlog.unmatched_records([
            notable_output, notable_what, self_explaining, genuinely_unmatched,
        ]) == [genuinely_unmatched]

    def test_parse_records_ignores_advisory_section(self, tmp_path):
        path = _write_sample_log(tmp_path)
        text = Path(path).read_text(encoding="utf-8")
        records = aierrorlog.unmatched_records(aierrorlog.parse_records(text))
        block = aierrorlog.format_log(
            records,
            [aierrorlog.Advice(index=records[0].index, cause="bad flag", next_step="check args")],
            model_name="qwen3-local",
        )
        combined = text + block
        assert len(aierrorlog.parse_records(combined)) == 3


class TestDeduplicateAndCap:
    def test_deduplicates_same_failure_across_files_and_hex_addresses(self):
        records = [
            aierrorlog.ErrorRecord(
                index=1, severity="ERROR", kind="tool-failed",
                what="ffmpeg failed on /lib/a.flac",
                item="/lib/a.flac", input_path="/lib/a.flac",
                tool="ffmpeg -i /lib/a.flac out.opus", status="exit 1",
                output_lines=("[filter @ 0x1111abcd] Unknown option 'foo'",),
            ),
            aierrorlog.ErrorRecord(
                index=2, severity="ERROR", kind="tool-failed",
                what="ffmpeg failed on /lib/b.flac",
                item="/lib/b.flac", input_path="/lib/b.flac",
                tool="ffmpeg -i /lib/b.flac out.opus", status="exit 1",
                output_lines=("[filter @ 0x9999ffff] Unknown option 'foo'",),
            ),
            aierrorlog.ErrorRecord(
                index=3, severity="ERROR", kind="output-check",
                what="output shorter than input",
                item="/lib/c.flac",
            ),
        ]

        unique, omitted = aierrorlog.deduplicate(records)
        assert omitted == 0
        assert len(unique) == 2
        assert unique[0].index == 1 and unique[0].count == 2
        assert unique[1].index == 2 and unique[1].count == 1

    def test_caps_unique_records_and_huge_output(self):
        many = [
            aierrorlog.ErrorRecord(
                index=i, severity="ERROR", kind="tool-failed",
                what="unique failure #%d" % i,
                output_lines=tuple("error #%d line %d: %s" % (i, n, "x" * 300)
                                   for n in range(100)),
            )
            for i in range(1, aierrorlog.MAX_RECORDS + 7)
        ]

        unique, omitted = aierrorlog.deduplicate(many)
        assert len(unique) == aierrorlog.MAX_RECORDS
        assert omitted == 6
        assert [r.index for r in unique] == list(range(1, aierrorlog.MAX_RECORDS + 1))

        built = aierrorlog.prompt(unique, omitted=omitted)
        assert "6 additional unique error pattern(s) were omitted" in built
        assert "line(s) omitted" in built or "truncated" in built
        # Every individual formatted record stays bounded by MAX_RECORD_CHARS.
        for rec in unique:
            assert len(aierrorlog._format_record(rec)) <= aierrorlog.MAX_RECORD_CHARS


class TestPromptAndParsing:
    def test_prompt_redacts_secrets_and_lists_indices(self, monkeypatch):
        monkeypatch.setenv("MY_API_TOKEN", "supersecretvalue123")
        rec = aierrorlog.ErrorRecord(
            index=1, severity="ERROR", kind="tool-failed",
            what="curl failed with api_key=mykey999 and supersecretvalue123",
            count=3,
        )
        built = aierrorlog.prompt([rec])
        assert "Record 1 (occurred 3 times)" in built
        assert "mykey999" not in built
        assert "supersecretvalue123" not in built
        assert "api_key=***" in built
        assert '"entries"' in built

    def test_strict_json_parsing_filters_invalid_entries(self):
        reply = "Here is the triage:\n```json\n" + json.dumps({
            "entries": [
                {"index": 2, "cause": "Codec\nnot\tsupported", "next_step": "Check ffmpeg -codecs"},
                {"index": 1, "cause": "x" * 400, "next_step": "Re-run with -v"},
                {"index": 1, "cause": "duplicate", "next_step": "ignored"},
                {"index": 99, "cause": "out of range", "next_step": "ignored"},
                {"index": True, "cause": "bool index", "next_step": "ignored"},
                {"index": "2", "cause": "string index", "next_step": "ignored"},
                {"index": 3, "cause": "", "next_step": "missing cause"},
                {"index": 3, "cause": "missing next", "next_step": "   "},
                "not a dict",
            ]
        }) + "\n```"

        advice = aierrorlog.parse_advice(reply, [1, 2, 3])
        assert [a.index for a in advice] == [1, 2]
        assert "\n" not in advice[1].cause
        assert advice[1].cause == "Codec not supported"
        assert advice[1].next_step == "Check ffmpeg -codecs"
        assert len(advice[0].cause) <= aierrorlog.MAX_FIELD_CHARS
        assert advice[0].cause.endswith("...")

    @pytest.mark.parametrize("bad", [
        "",
        "not json at all",
        "[1, 2, 3]",
        '{"entries": "not a list"}',
        '{"wrong_key": []}',
    ])
    def test_malformed_json_returns_empty(self, bad):
        assert aierrorlog.parse_advice(bad, [1, 2]) == []


class TestRosterPreference:
    def test_error_triage_prefers_local_models(self, monkeypatch, tmp_path):
        task = llmroster.ERROR_TRIAGE
        assert task.name == "error-triage"
        assert task.requires == ()
        assert "local" in task.prefers
        assert "reasoning" in task.prefers
        assert task.lean == "balanced"
        assert llmroster.TASKS["error-triage"] is task

        llmroster.forget()
        llmtraits.forget()
        monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)
        for name in llmclient.SETTING_FIELDS.values():
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(llmroster.LEAN_VARIABLE, raising=False)

        # Put cloud models BEFORE local models in the roster to prove preference
        # overrides declaration order.
        roster_file = tmp_path / "models.toml"
        roster_file.write_text(
            """
[models.cloud-plain]
url = "https://api.example.com/v1"
model = "cloud-plain"
traits = { local = false, reasoning = false }

[models.cloud-reason]
url = "https://api.example.com/v1"
model = "cloud-reason"
traits = { local = false, reasoning = true }

[models.local-plain]
url = "http://localhost:8081/v1"
model = "local-plain"
traits = { local = true, reasoning = false }

[models.local-reason]
url = "http://localhost:8082/v1"
model = "local-reason"
traits = { local = true, reasoning = true }
""",
            encoding="utf-8",
        )
        monkeypatch.setenv(llmroster.MODELS_VARIABLE, str(roster_file))
        roster = llmroster.load()
        ladder = [m.name for m in roster.ladder(llmroster.ERROR_TRIAGE)]
        assert ladder[0] == "local-reason"
        assert set(ladder[1:3]) == {"cloud-reason", "local-plain"}
        assert ladder[3] == "cloud-plain"


class TestTriageFile:
    def test_skips_model_when_all_errors_matched_notable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("errorTriageLlm", "on")
        path = errorlog.record(
            "tool-failed",
            "ffmpeg ran out of space",
            tool_argv=["ffmpeg", "-i", "a.flac", "b.opus"],
            tool_status="exit 1",
            tool_output=["No space left on device"],
            causes=[errorlog.CAUSES["disk-full"].says],
        )
        called = False

        def fail_if_asked(_task):
            nonlocal called
            called = True
            return [], ""

        monkeypatch.setattr(llmroster, "optional", fail_if_asked)
        assert aierrorlog.triage_file(path) == []
        assert not called

    def test_switch_off_skips_model(self, monkeypatch, tmp_path):
        monkeypatch.setenv("errorTriageLlm", "off")
        path = _write_sample_log(tmp_path)
        fake = _fake_llm("local", '{"entries": [{"index": 1, "cause": "c", "next_step": "n"}]}')
        assert aierrorlog.triage_file(path, models=[fake]) == []
        assert fake.calls == []

    def test_no_model_available_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("errorTriageLlm", "on")
        path = _write_sample_log(tmp_path)
        monkeypatch.setattr(llmroster, "optional",
                            lambda _task: ([], "no model configured"))
        assert aierrorlog.triage_file(path) == []

    def test_llm_error_falls_back_and_never_breaks_log(self, monkeypatch, tmp_path):
        monkeypatch.setenv("errorTriageLlm", "on")
        path = _write_sample_log(tmp_path)
        before = Path(path).read_text(encoding="utf-8")

        broken = _fake_llm("broken", llmclient.LlmError("connection refused"))
        malformed = _fake_llm("garbled", "not json at all")
        assert aierrorlog.triage_file(path, models=[broken, malformed]) == []
        assert Path(path).read_text(encoding="utf-8") == before

        good_reply = json.dumps({
            "entries": [
                {"index": 1, "cause": "Unknown ffmpeg filter option 'bogus_opt'.",
                 "next_step": "Remove or fix the loudnorm option and retry."},
            ]
        })
        good = _fake_llm("qwen-local", good_reply, model="qwen3-local")
        advice = aierrorlog.triage_file(path, models=[broken, good], announce=False)
        assert len(advice) == 1
        assert advice[0].cause == "Unknown ffmpeg filter option 'bogus_opt'."
        after = Path(path).read_text(encoding="utf-8")
        assert aierrorlog.ADVISORY_MARKER in after
        assert "likely cause: Unknown ffmpeg filter option 'bogus_opt'." in after

        # Re-running triage on an already-triaged file does not call the model again.
        again = _fake_llm("qwen-local", good_reply)
        assert aierrorlog.triage_file(path, models=[again], announce=False) == []
        assert again.calls == []

    def test_records_modelmeter_and_announces_on_stderr(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("errorTriageLlm", "on")
        path = _write_sample_log(tmp_path)
        meter = str(tmp_path / "meter.txt")
        reply = json.dumps({
            "entries": [
                {"index": 1, "cause": "Invalid loudnorm filter option.",
                 "next_step": "Check ffmpeg -h filter=loudnorm."},
                {"index": 2, "cause": "Unexpected tag layout triggered ValueError.",
                 "next_step": "Inspect the tags on track3."},
            ]
        })
        fake = _fake_llm("qwen-local", reply, model="qwen3-local",
                         prompt_tokens=200, completion_tokens=60)

        advice = aierrorlog.triage_file(path, models=[fake], meter_file=meter)
        assert len(advice) == 2

        err = capsys.readouterr().err
        assert "Advisory triage for 2 unexplained error(s)" in err
        assert "likely cause: Invalid loudnorm filter option." in err
        assert "next step:    Check ffmpeg -h filter=loudnorm." in err
        assert "triage" in err

        spans, tally = modelmeter.gathered(meter)
        assert tally.requests == 1
        assert tally.read == 200
        assert tally.tokens == 60
        assert spans.seconds >= 0.0

    def test_reentrancy_guard_prevents_recursion(self, monkeypatch, tmp_path):
        monkeypatch.setenv("errorTriageLlm", "on")
        path = _write_sample_log(tmp_path)
        nested_results: list[list[aierrorlog.Advice]] = []

        def trigger_nested(_system, _prompt):
            nested_results.append(aierrorlog.triage_file(path, models=[fake]))

        reply = json.dumps({
            "entries": [{"index": 1, "cause": "Bad filter.", "next_step": "Fix filter."}]
        })
        fake = _fake_llm("qwen-local", reply, on_ask=trigger_nested)
        advice = aierrorlog.triage_file(path, models=[fake], announce=False)
        assert len(advice) == 1
        assert nested_results == [[]]
        assert len(fake.calls) == 1


class TestExitHook:
    def test_triage_at_exit_only_runs_for_owner_and_never_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("errorTriageLlm", "on")
        errorlog.settle_run(["convert-audio", "/lib"])
        path = _write_sample_log(tmp_path)

        seen: list[str] = []
        monkeypatch.setattr(aierrorlog, "triage_file", lambda p: seen.append(p) or [])

        # Child process (different owner PID) must skip.
        runcontext.settle(errorlog.OWNER_VARIABLE, str(os.getpid() + 99999))
        errorlog._triage_at_exit()
        assert seen == []

        # Aborted run must skip.
        runcontext.settle(errorlog.OWNER_VARIABLE, str(os.getpid()))
        monkeypatch.setattr(runcontext, "abort_requested", lambda: True)
        errorlog._triage_at_exit()
        assert seen == []

        # Owning, non-aborted run calls triage_file once with the error log path.
        monkeypatch.setattr(runcontext, "abort_requested", lambda: False)
        errorlog._triage_at_exit()
        assert seen == [path]

        # Even if triage raises unexpectedly, exit cleanup swallows it cleanly.
        monkeypatch.setattr(
            aierrorlog, "triage_file",
            lambda _p: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        errorlog._triage_at_exit()

    def test_triage_at_exit_stops_lazily_loaded_modelserver(self, monkeypatch, tmp_path):
        monkeypatch.setenv("errorTriageLlm", "on")
        errorlog.settle_run(["convert-audio", "/lib"])
        _write_sample_log(tmp_path)

        stopped: list[bool] = []
        fake_server = SimpleNamespace(stop_all=lambda: stopped.append(True))

        def load_server(_path):
            monkeypatch.setitem(sys.modules, "medialib.lib.modelserver", fake_server)
            return []

        monkeypatch.delitem(sys.modules, "medialib.lib.modelserver", raising=False)
        monkeypatch.setattr(aierrorlog, "triage_file", load_server)
        errorlog._triage_at_exit()
        assert stopped == [True]
