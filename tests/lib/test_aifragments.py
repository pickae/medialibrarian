"""Tests for classifying fragment candidates with a language model -
medialib.lib.aifragments.

The model is a stand-in that records what it was told and returns JSON
decisions. What is pinned: prompt construction includes candidate tokens,
sample names and existing sections of data/fragments.txt; strict JSON
validation rejects invented tokens and already-present tokens and never
proposes title_word or unsure tokens; unified diffs land under logs/ without
ever touching data/fragments.txt; the switch, missing-model, fallback and
modelmeter paths behave as expected.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from medialib.cli import find_fragment_candidates as ffc
from medialib.lib import aifragments, llmclient, llmroster, modelmeter

pytestmark = pytest.mark.pure


_EXISTING_FRAGMENTS = """# Curated fragments for this library
# Edit carefully.

# Release groups
yify
rarbg

# Quality and source
1080p
webrip
"""


class _Model:
    """Stand-in language model matching llmclient.Llm's searching/serving/ask."""

    def __init__(self, answer, *, name="stand-in", model="stand-in-7b",
                 can_search=True):
        self.answer = answer
        self.name = name
        self.model = model
        self.can_search = can_search
        self.purpose = ""
        self.asked: list[tuple[str, str]] = []

    def searching(self, purpose=""):
        self.purpose = purpose
        return self if self.can_search else None

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def ask(self, system, prompt, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 60, 420)
        return self.answer


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aifragments.SWITCH_VARIABLE, "on")


class TestParsingAndPrompt:
    def test_parses_sections_and_skips_multiline_preamble(self):
        doc = aifragments.parse_fragments(_EXISTING_FRAGMENTS)
        assert [sec.name for sec in doc.sections] == [
            "Release groups", "Quality and source"]
        assert doc.sections[0].entries == ("yify", "rarbg")
        assert doc.sections[1].entries == ("1080p", "webrip")
        assert doc.existing == frozenset({"yify", "rarbg", "1080p", "webrip"})

    def test_prompt_includes_candidates_samples_and_existing_sections(self):
        doc = aifragments.parse_fragments(_EXISTING_FRAGMENTS)
        names = [
            "Some.Show.S01E01.x264-FGT.mkv",
            "Some.Show.S01E02.x264-FGT.mkv",
            "Another.Show.bluray.mkv",
        ]
        rows = [(3, "show"), (2, "fgt"), (2, "x264")]
        samples = aifragments.sample_names(names, [t for _, t in rows])
        text = aifragments.prompt(rows, samples, doc)

        assert "# Release groups" in text
        assert "yify, rarbg" in text
        assert "# Quality and source" in text
        assert "`fgt` (2 name(s))" in text
        assert "Some.Show.S01E01.x264-FGT.mkv" in text
        assert "`show` (3 name(s))" in text

    def test_prompt_handles_missing_fragments_file(self):
        doc = aifragments.parse_fragments("", exists=False)
        text = aifragments.prompt([(3, "hevc")], {"hevc": ["Film.hevc.mkv"]}, doc)
        assert "Release groups" in text
        assert "Quality and source" in text
        assert "Publishers" in text
        assert "`hevc` (3 name(s))" in text


class TestStrictValidation:
    def test_rejects_invented_and_existing_and_excludes_title_and_unsure(self):
        answer = "Here is the classification:\n```json\n" + json.dumps({
            "decisions": [
                # Valid addable tokens
                {"token": "fgt", "category": "release_group",
                 "section": "# Release groups", "reason": "P2P release group"},
                {"token": "hevc", "category": "quality_or_source",
                 "section": "", "reason": "video codec tag"},
                {"token": "audible", "category": "publisher",
                 "section": "Publishers", "reason": "audiobook platform tag"},
                # Excluded categories - NEVER proposed
                {"token": "show", "category": "title_word",
                 "section": "Release groups", "reason": "ordinary title word"},
                {"token": "phoenix", "category": "unsure",
                 "section": "Release groups", "reason": "could be title or group"},
                # Invented token not in candidates - MUST be rejected
                {"token": "sparky", "category": "release_group",
                 "section": "Release groups", "reason": "invented by model"},
                # Already present in fragments.txt (case-insensitive) - MUST be rejected
                {"token": "1080p", "category": "quality_or_source",
                 "section": "Quality and source", "reason": "already curated"},
                # Duplicate of fgt - ignored
                {"token": "fgt", "category": "publisher",
                 "section": "Publishers", "reason": "duplicate"},
                # Unknown category - rejected
                {"token": "x264", "category": "codec",
                 "section": "Quality and source", "reason": "bad category"},
            ]
        }) + "\n```"

        result = aifragments.parse_decisions(
            answer,
            candidates=["fgt", "hevc", "audible", "show", "phoenix", "1080p", "x264"],
            existing=frozenset({"1080p", "yify"}),
        )

        assert [(d.token, d.category, d.section) for d in result.proposed] == [
            ("fgt", "release_group", "Release groups"),
            ("hevc", "quality_or_source", "Quality and source"),
            ("audible", "publisher", "Publishers"),
        ]
        assert [(d.token, d.category, d.section) for d in result.excluded] == [
            ("show", "title_word", ""),
            ("phoenix", "unsure", ""),
        ]
        assert any("sparky" in note for note in result.rejected)
        assert any("1080p" in note for note in result.rejected)
        assert any("x264" in note for note in result.rejected)

    @pytest.mark.parametrize("bad_answer", [
        "no json at all",
        "[1, 2, 3]",
        '{"not_decisions": []}',
        '{"decisions": "wrong-type"}',
    ])
    def test_malformed_answers_propose_nothing(self, bad_answer):
        result = aifragments.parse_decisions(bad_answer, candidates=["hevc"])
        assert result.proposed == ()
        assert result.excluded == ()
        assert result.rejected


class TestDiffAndProposalFile:
    def test_writes_unified_diff_under_logs_without_touching_fragments_txt(
            self, tmp_path, capsys):
        fragments = tmp_path / "data" / "fragments.txt"
        fragments.parent.mkdir(parents=True)
        fragments.write_text(_EXISTING_FRAGMENTS, encoding="utf-8")
        before_text = fragments.read_text(encoding="utf-8")
        before_mtime = fragments.stat().st_mtime_ns

        answer = json.dumps({
            "decisions": [
                {"token": "fgt", "category": "release_group",
                 "section": "Release groups", "reason": "scene/P2P group"},
                {"token": "hevc", "category": "quality_or_source",
                 "section": "Quality and source", "reason": "video codec"},
                {"token": "audible", "category": "publisher",
                 "section": "Publishers", "reason": "audiobook distributor"},
                {"token": "show", "category": "title_word",
                 "reason": "ordinary word in series titles"},
                {"token": "ambig", "category": "unsure",
                 "reason": "unclear context"},
            ]
        })
        model = _Model(answer)
        meter = tmp_path / "meter.jsonl"

        result = aifragments.propose(
            rows=[(5, "show"), (4, "fgt"), (3, "hevc"), (2, "audible"),
                  (2, "ambig"), (2, "1080p")],
            names=["Some.Show.1080p.hevc-FGT.mkv", "Audible.Ambig.Show.m4b"],
            tree_file=str(tmp_path / "lib.tree"),
            fragments_path=str(fragments),
            script_dir=str(tmp_path),
            models=[model],
            meter_file=str(meter),
            stamp="20261120-153000",
        )

        assert result is not None
        assert model.purpose == aifragments.PURPOSE

        # CRITICAL GUARD: curated data/fragments.txt is NEVER modified.
        assert fragments.read_text(encoding="utf-8") == before_text
        assert fragments.stat().st_mtime_ns == before_mtime

        # Proposal file lives under logs/ with unified diff + summary.
        expected_path = (
            tmp_path / "logs"
            / "find-fragment-candidates-proposals-20261120-153000.diff"
        )
        assert result.proposal_path == str(expected_path)
        assert expected_path.is_file()

        content = expected_path.read_text(encoding="utf-8")
        assert "--- data/fragments.txt" in content
        assert "+++ data/fragments.txt" in content
        assert "+fgt" in content
        assert "+hevc" in content
        assert "+# Publishers" in content
        assert "+audible" in content
        # Excluded tokens are explained in header but NEVER added in the diff.
        assert "#   - show [title_word]: ordinary word in series titles" in content
        assert "#   - ambig [unsure]: unclear context" in content
        diff_body = content.split("--- data/fragments.txt", 1)[1]
        assert "+show" not in diff_body
        assert "+ambig" not in diff_body
        assert "+1080p" not in diff_body

        # Every decision, proposal path and modelmeter rate line were logged.
        stderr = capsys.readouterr().err
        assert "+ fgt [release_group -> Release groups]: scene/P2P group" in stderr
        assert "+ hevc [quality_or_source -> Quality and source]: video codec" in stderr
        assert "+ audible [publisher -> Publishers]: audiobook distributor" in stderr
        assert "- show [title_word, not added]: ordinary word in series titles" in stderr
        assert "- ambig [unsure, not added]: unclear context" in stderr
        assert str(expected_path) in stderr
        assert "fragments:" in stderr

        # modelmeter recorded the call.
        _spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 60, 420)

    def test_works_when_fragments_file_does_not_exist_yet_and_never_creates_it(
            self, tmp_path):
        missing = tmp_path / "data" / "fragments.txt"
        model = _Model(json.dumps({
            "decisions": [
                {"token": "x265", "category": "quality_or_source",
                 "section": "Quality and source", "reason": "codec tag"},
            ]
        }))
        result = aifragments.propose(
            rows=[(3, "x265")],
            names=["Film.x265.mkv"],
            fragments_path=str(missing),
            script_dir=str(tmp_path),
            models=[model],
            stamp="20261120-153100",
        )
        assert result is not None
        assert not missing.exists()
        content = (tmp_path / "logs"
                   / "find-fragment-candidates-proposals-20261120-153100.diff"
                   ).read_text(encoding="utf-8")
        assert "--- /dev/null" in content
        assert "+++ data/fragments.txt" in content
        assert "+# Quality and source" in content
        assert "+x265" in content

    def test_all_excluded_writes_no_diff_file_but_logs_decisions(
            self, tmp_path, capsys):
        model = _Model(json.dumps({
            "decisions": [
                {"token": "season", "category": "title_word",
                 "reason": "series word"},
                {"token": "nova", "category": "unsure",
                 "reason": "could be title or group"},
            ]
        }))
        result = aifragments.propose(
            rows=[(4, "season"), (2, "nova")],
            names=["Nova.Season.01.mkv"],
            fragments_path=str(tmp_path / "data" / "fragments.txt"),
            script_dir=str(tmp_path),
            models=[model],
            stamp="20261120-153200",
        )
        assert result is not None
        assert result.proposal_path == ""
        assert not (tmp_path / "logs").exists()
        stderr = capsys.readouterr().err
        assert "- season [title_word, not added]: series word" in stderr
        assert "- nova [unsure, not added]: could be title or group" in stderr
        assert "No fragment additions proposed" in stderr


class TestSwitchesFallbacksAndEdgeCases:
    def test_switch_off_never_asks_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv(aifragments.SWITCH_VARIABLE, "off")
        model = _Model('{"decisions": []}')
        result = aifragments.propose(
            rows=[(3, "x264")],
            names=["Film.x264.mkv"],
            fragments_path=str(tmp_path / "data" / "fragments.txt"),
            script_dir=str(tmp_path),
            models=[model],
        )
        assert result is None
        assert model.asked == []

    def test_no_model_configured_returns_none(self, tmp_path, monkeypatch):
        for name in (llmclient.API_VARIABLE, llmclient.URL_VARIABLE,
                     llmclient.HARNESS_VARIABLE):
            monkeypatch.delenv(name, raising=False)
        result = aifragments.propose(
            rows=[(3, "x264")],
            names=["Film.x264.mkv"],
            fragments_path=str(tmp_path / "data" / "fragments.txt"),
            script_dir=str(tmp_path),
        )
        assert result is None

    def test_all_candidates_already_in_fragments_skips_model(self, tmp_path):
        fragments = tmp_path / "data" / "fragments.txt"
        fragments.parent.mkdir(parents=True)
        fragments.write_text("1080p\nx264\n", encoding="utf-8")
        model = _Model('{"decisions": []}')
        result = aifragments.propose(
            rows=[(3, "1080p"), (2, "x264")],
            names=["Film.1080p.x264.mkv"],
            fragments_path=str(fragments),
            script_dir=str(tmp_path),
            models=[model],
        )
        assert result is None
        assert model.asked == []

    def test_non_searching_model_is_still_asked_and_fallback_works(self, tmp_path):
        failing = _Model(llmclient.LlmError("server busy"), name="first")
        local = _Model(
            json.dumps({
                "decisions": [
                    {"token": "x264", "category": "quality_or_source",
                     "section": "Quality and source", "reason": "codec"},
                ]
            }),
            name="local-no-web",
            model="qwen-local",
            can_search=False,
        )
        result = aifragments.propose(
            rows=[(3, "x264")],
            names=["Film.x264.mkv"],
            fragments_path=str(tmp_path / "data" / "fragments.txt"),
            script_dir=str(tmp_path),
            models=[failing, local],
            stamp="20261120-153300",
        )
        assert result is not None
        assert result.model_name == "qwen-local"
        assert [d.token for d in result.classification.proposed] == ["x264"]
        assert len(local.asked) == 1

    def test_find_fragment_candidates_cli_writes_proposal_under_logs(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
        fragments = tmp_path / "data" / "fragments.txt"
        fragments.parent.mkdir(parents=True)
        fragments.write_text("# Quality and source\n1080p\n", encoding="utf-8")

        tree = tmp_path / "lib.tree"
        tree.write_text(
            "lib\n"
            "|-- Show.S01E01.1080p.x264-FGT.mkv\n"
            "`-- Show.S01E02.1080p.x264-FGT.mkv\n",
            encoding="utf-8",
        )
        model = _Model(json.dumps({
            "decisions": [
                {"token": "x264", "category": "quality_or_source",
                 "section": "Quality and source", "reason": "video codec"},
                {"token": "fgt", "category": "release_group",
                 "section": "Release groups", "reason": "release group"},
                {"token": "show", "category": "title_word",
                 "reason": "title word"},
            ]
        }))
        monkeypatch.setattr(
            llmroster, "optional",
            lambda task: ([model], "") if task is llmroster.FRAGMENT_CLASSIFY else ([], "no"),
        )

        rc = ffc.main([str(tree)])
        assert rc == 0
        # Curated fragments.txt stays untouched.
        assert fragments.read_text(encoding="utf-8") == "# Quality and source\n1080p\n"
        diffs = list((tmp_path / "logs").glob("find-fragment-candidates-proposals-*.diff"))
        assert len(diffs) == 1
        diff_text = diffs[0].read_text(encoding="utf-8")
        assert "+x264" in diff_text
        assert "+fgt" in diff_text
        assert "+show" not in diff_text.split("--- data/fragments.txt", 1)[1]
        captured = capsys.readouterr()
        assert str(diffs[0]) in captured.err
        assert "fragments:" in captured.err
