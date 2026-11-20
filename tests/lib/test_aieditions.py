"""Tests for medialib.lib.aieditions - asking a language model for suggestions
on ambiguous film folders and differing cuts (>60 s apart)."""

import json

import pytest

from medialib.cli import ingest_movies_plex
from medialib.lib import (
    aieditions,
    filmmatch,
    llmclient,
    llmroster,
    modelmeter,
    plexcompare,
    plexserver,
    tmdbworklists,
)

pytestmark = pytest.mark.pure


class _Model:
    """Stand-in searcher that records prompts and returns canned JSON."""

    def __init__(self, answer, can_search=True, name="stand-in"):
        self.answer = answer
        self.can_search = can_search
        self.name = name
        self.asked: list[tuple[str, str]] = []
        self.purpose = ""

    def searching(self, purpose=""):
        self.purpose = purpose
        return self if self.can_search else None

    def ask(self, system, prompt, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 55, 420)
        return self.answer


class TestRosterAndSwitch:
    def test_roster_task_registered_with_web_and_reasoning(self):
        task = llmroster.TASKS["ambiguous-editions"]
        assert task is llmroster.AMBIGUOUS_EDITIONS
        assert task.requires == ("web",)
        assert task.prefers == ("reasoning",)

    @pytest.mark.parametrize("value, expected", [
        ("on", True),
        ("ON", True),
        ("1", True),
        ("off", False),
        ("OFF", False),
        ("0", False),
        ("no", False),
        ("false", False),
    ])
    def test_switch_setting(self, monkeypatch, value, expected):
        monkeypatch.setenv(aieditions.SWITCH_VARIABLE, value)
        assert aieditions.enabled() is expected


class TestSanitizeEdition:
    def test_strips_wrapper_path_separators_braces_and_controls(self):
        assert aieditions.sanitize_edition("{edition-Director's Cut}") == "Director's Cut"
        assert aieditions.sanitize_edition("../Director's/Cut\\2003") == "Director's Cut 2003"
        assert aieditions.sanitize_edition("Extended {Final}\x00\n\tCut") == "Extended Final Cut"
        for ch in ("/", "\\", "{", "}", "\x00", "\n", "\r", "\t"):
            assert ch not in aieditions.sanitize_edition(
                "Bad" + ch + "Edition")

    def test_rejects_unsure_placeholders_and_empty(self):
        for value in ("", "   ", "unsure", "UNSURE", "unknown", "None",
                      "n/a", "{edition-unsure}", "///{}", None, 123):
            assert aieditions.sanitize_edition(value) == ""

    def test_caps_reasonable_length(self):
        long_name = "Extended Collector Anniversary Edition " * 5
        cleaned = aieditions.sanitize_edition(long_name)
        assert 0 < len(cleaned) <= aieditions.MAX_EDITION_CHARS


class TestStrayPromptAndParsing:
    def test_stray_prompt_includes_folder_metadata_files_and_runtimes(self, tmp_path):
        folder = tmp_path / "Films" / "The Quiet Harbor (1998) {tmdb-42}"
        folder.mkdir(parents=True)
        (folder / "The Quiet Harbor (1998).mkv").write_bytes(b"a" * 2048)
        (folder / "The Quiet Harbor II (2001).mkv").write_bytes(b"b" * 1024)
        (folder / "Making Of.mkv").write_bytes(b"c" * 512)

        prompt = aieditions.stray_prompt(
            str(folder),
            "holds a film that is not its own",
            ["The Quiet Harbor II (2001).mkv", "Making Of.mkv"],
            title="The Quiet Harbor",
            year="1998",
            tag="{tmdb-42}",
            runtimes={"The Quiet Harbor II (2001).mkv": 6125.0},
        )
        assert "The Quiet Harbor (1998) {tmdb-42}" in prompt
        assert "Parent folder: Films" in prompt
        assert "Film title: The Quiet Harbor" in prompt
        assert "Year: 1998" in prompt
        assert "ID tag: {tmdb-42}" in prompt
        assert "holds a film that is not its own" in prompt
        assert "The Quiet Harbor II (2001).mkv" in prompt
        assert "102m 05s" in prompt
        assert "Making Of.mkv" in prompt

    def test_parse_strays_validates_files_enums_and_sanitizes_editions(self):
        allowed = [
            "Harbor Extended.mkv",
            "Harbor 2.mkv",
            "Other Film.mkv",
            "Featurette.mkv",
            "Mystery.mkv",
        ]
        answer = "Here is the JSON:\n" + json.dumps({
            "files": [
                # Valid same_film_edition with wrapped & dirty edition
                {"file": "Harbor Extended.mkv", "kind": "same_film_edition",
                 "edition": "{edition-Director's/Cut\n}",
                 "reason": "Matches the 2004 director's cut."},
                # Duplicate entry for same file must be ignored
                {"file": "Harbor Extended.mkv", "kind": "extra",
                 "edition": "", "reason": "duplicate"},
                # Non-same_film_edition must drop any edition string
                {"file": "Harbor 2.mkv", "kind": "sequel",
                 "edition": "Part 2", "reason": "2001 sequel."},
                {"file": "Other Film.mkv", "kind": "another_film",
                 "edition": "", "reason": "Unrelated film."},
                {"file": "Featurette.mkv", "kind": "extra",
                 "edition": "", "reason": "Behind the scenes."},
                {"file": "Mystery.mkv", "kind": "unsure",
                 "edition": "", "reason": "Not enough info."},
                # Invented filename must be strictly rejected
                {"file": "Hallucinated.mkv", "kind": "extra",
                 "edition": "", "reason": "Not in folder."},
                # Path-prefixed filename must be strictly rejected
                {"file": "sub/Harbor 2.mkv", "kind": "sequel",
                 "edition": "", "reason": "Wrong path."},
                # Invalid enum value must be strictly rejected
                {"file": "Mystery.mkv", "kind": "remake",
                 "edition": "", "reason": "Invalid kind."},
            ]
        })

        proposals = aieditions.parse_strays(answer, allowed)
        assert [p.file for p in proposals] == allowed
        assert proposals[0] == aieditions.StrayProposal(
            file="Harbor Extended.mkv",
            kind="same_film_edition",
            edition="Director's Cut",
            reason="Matches the 2004 director's cut.",
        )
        assert proposals[1].kind == "sequel"
        assert proposals[1].edition == ""
        assert proposals[2].kind == "another_film"
        assert proposals[3].kind == "extra"
        assert proposals[4].kind == "unsure"

    def test_parse_strays_rejects_unknown_kind_and_garbage(self):
        assert aieditions.parse_strays("not json at all", ["a.mkv"]) == []
        bad = json.dumps({"files": [
            {"file": "a.mkv", "kind": "bogus_kind", "edition": "", "reason": ""}
        ]})
        assert aieditions.parse_strays(bad, ["a.mkv"]) == []


class TestCutsPromptAndParsing:
    def test_cuts_prompt_includes_identity_runtimes_and_gap(self):
        cuts = [
            aieditions.CutInfo("Blade Runner (1982).mkv", 7050.0, role="new"),
            aieditions.CutInfo("Blade Runner (1982) old.mkv", 7010.0 - 400.0,
                               role="existing"),
        ]
        prompt = aieditions.cuts_prompt(
            "Blade Runner", "1982", cuts, tmdb="78", imdb="tt0083658")
        assert "Film title: Blade Runner" in prompt
        assert "Year: 1982" in prompt
        assert "TMDb id: 78" in prompt
        assert "IMDb id: tt0083658" in prompt
        assert "Blade Runner (1982).mkv" in prompt
        assert "117m 30s" in prompt
        assert "Runtime difference:" in prompt

    def test_parse_cuts_strict_validation_and_unsure_handling(self):
        allowed = ["cut_a.mkv", "cut_b.mkv", "cut_c.mkv"]
        answer = json.dumps({
            "cuts": [
                {"file": "cut_a.mkv", "edition": "{edition-Final/Cut}",
                 "reason": "117m matches 2007 Final Cut."},
                {"file": "cut_b.mkv", "edition": "unsure",
                 "reason": "Runtime does not match a documented cut."},
                {"file": "cut_c.mkv", "edition": None, "unsure": True,
                 "reason": "Unknown fan edit."},
                # Invented cut must be dropped
                {"file": "invented.mkv", "edition": "Theatrical",
                 "reason": "Hallucinated."},
            ]
        })
        proposals = aieditions.parse_cuts(answer, allowed)
        assert proposals == [
            aieditions.CutProposal(
                file="cut_a.mkv", edition="Final Cut", unsure=False,
                reason="117m matches 2007 Final Cut."),
            aieditions.CutProposal(
                file="cut_b.mkv", edition="", unsure=True,
                reason="Runtime does not match a documented cut."),
            aieditions.CutProposal(
                file="cut_c.mkv", edition="", unsure=True,
                reason="Unknown fan edit."),
        ]

    def test_needs_edition_names_predicate(self):
        def prof(seconds, edition=""):
            return plexcompare.Profile(
                path="/lib/film.mkv", seconds=seconds, width=1920,
                height=1080, hdr="", audio=(), commentaries=(), others=(),
                chapters=0, named_chapters=False, edition=edition)

        # > 60 s apart (and not a 4% PAL speed-up), no edition tags -> needs names
        assert plexcompare.needs_edition_names(prof(7200), prof(6600))
        # One tagged, one untagged > 60 s apart -> still needs name for the other
        assert plexcompare.needs_edition_names(
            prof(7200, "Director's Cut"), prof(6600, ""))
        # Both already carry distinct editions -> does not need names
        assert not plexcompare.needs_edition_names(
            prof(7200, "Director's Cut"), prof(6600, "Theatrical"))
        # Under 60 s gap -> same cut
        assert not plexcompare.needs_edition_names(prof(7200), prof(7160))
        # 4% PAL speed-up -> same cut sped up
        assert not plexcompare.needs_edition_names(
            prof(7200), prof(7200 / plexcompare.PAL_SPEEDUP))
        # Soundtracks lined up -> timing settles it
        timing = plexcompare.Timing(0.0, 1.0)
        assert not plexcompare.needs_edition_names(
            prof(7200), prof(6600), timing)


class TestExecutionLoggingMeteringAndReport:
    def test_switch_off_asks_nothing(self, monkeypatch):
        monkeypatch.setenv(aieditions.SWITCH_VARIABLE, "off")
        model = _Model(json.dumps({"files": [
            {"file": "a.mkv", "kind": "extra", "edition": "", "reason": "x"}
        ]}))
        assert aieditions.classify_strays(
            "/films/Foo (2020)", "reason", ["a.mkv"], searchers=[model]) == []
        assert aieditions.identify_cuts(
            "Foo", "2020",
            [aieditions.CutInfo("a.mkv", 7200),
             aieditions.CutInfo("b.mkv", 6800)],
            searchers=[model]) == []
        assert model.asked == []

    def test_no_model_available_returns_empty(self, monkeypatch):
        monkeypatch.setenv(aieditions.SWITCH_VARIABLE, "on")
        non_searcher = _Model("{}", can_search=False)
        assert aieditions.classify_strays(
            "/films/Foo (2020)", "reason", ["a.mkv"],
            searchers=[non_searcher]) == []
        # Empty roster from llmroster.optional (conftest sets llmModels to missing file)
        assert aieditions.classify_strays(
            "/films/Foo (2020)", "reason", ["a.mkv"]) == []

    def test_fallback_on_error_and_records_modelmeter(self, monkeypatch, tmp_path):
        monkeypatch.setenv(aieditions.SWITCH_VARIABLE, "on")
        broken = _Model(llmclient.LlmError("timeout"), name="first")
        good = _Model(json.dumps({
            "cuts": [
                {"file": "cut1.mkv", "edition": "Theatrical",
                 "reason": "117m theatrical cut."},
                {"file": "cut2.mkv", "edition": "Director's Cut",
                 "reason": "125m director's cut."},
            ]
        }), name="second")
        meter_file = str(tmp_path / "editions.meter")
        logged: list[str] = []
        tally = modelmeter.Tally()
        spans = modelmeter.Spans()

        proposals = aieditions.identify_cuts(
            "The Film", "1999",
            [aieditions.CutInfo("cut1.mkv", 7020.0),
             aieditions.CutInfo("cut2.mkv", 7500.0)],
            tmdb="999",
            searchers=[broken, good],
            meter_file=meter_file,
            log=logged.append,
            tally=tally,
            spans=spans,
        )

        assert good.purpose == aieditions.PURPOSE
        assert len(proposals) == 2
        assert proposals[0].edition == "Theatrical"
        assert proposals[1].edition == "Director's Cut"
        assert any("No edition suggestion from first: timeout" in line
                   for line in logged)
        assert any("{edition-Theatrical}" in line for line in logged)
        assert any("{edition-Director's Cut}" in line for line in logged)
        assert tally.requests == 1
        assert tally.tokens == 55
        recorded_spans, recorded_tally = modelmeter.gathered(meter_file)
        assert recorded_tally.requests == 1
        assert recorded_tally.tokens == 55
        assert recorded_spans.all()

    def test_ambiguous_report_writes_suggestions_without_renaming_files(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(aieditions.SWITCH_VARIABLE, "on")
        folder = tmp_path / "The Harbor (1998) {tmdb-42}"
        folder.mkdir()
        original_files = sorted([
            "The Harbor (1998).mkv",
            "The Harbor Uncut.mkv",
            "The Harbor Returns (2001).mkv",
        ])
        for name in original_files:
            (folder / name).write_bytes(b"video")

        model = _Model(json.dumps({
            "files": [
                {"file": "The Harbor Uncut.mkv", "kind": "same_film_edition",
                 "edition": "Unrated", "reason": "Uncut home release."},
                {"file": "The Harbor Returns (2001).mkv", "kind": "sequel",
                 "edition": "", "reason": "2001 sequel."},
            ]
        }))
        logged: list[str] = []
        ambiguous = [(
            str(folder),
            "holds a film that is not its own",
            ["The Harbor Uncut.mkv", "The Harbor Returns (2001).mkv"],
        )]

        suggestions = aieditions.suggest_for_ambiguous(
            ambiguous, searchers=[model], log=logged.append,
            probe_runtimes=False)
        report = tmp_path / "ingest-movies-ambiguous-Films.txt"
        assert tmdbworklists.write_ambiguous_list(
            str(report), ambiguous, str(tmp_path), log=logged.append,
            suggestions=suggestions)

        text = report.read_text(encoding="utf-8")
        assert (
            'model suggestion for "The Harbor Uncut.mkv": '
            'same_film_edition ({edition-Unrated})'
        ) in text
        assert 'model suggestion for "The Harbor Returns (2001).mkv": sequel' in text
        assert "nothing was" in text and "renamed or moved" in text
        # Every decision was logged in run output too
        assert any("{edition-Unrated}" in line for line in logged)
        assert any("sequel" in line for line in logged)
        # Guard: no file on disk was renamed or moved
        assert sorted(p.name for p in folder.iterdir()) == original_files

    def test_plex_different_cuts_logs_suggestions_and_handles_same_basename(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(aieditions.SWITCH_VARIABLE, "on")
        new_dir = tmp_path / "incoming" / "Alien (1979) {tmdb-348}"
        old_dir = tmp_path / "library" / "Alien (1979) {tmdb-348}"
        new_dir.mkdir(parents=True)
        old_dir.mkdir(parents=True)
        new_file = new_dir / "Alien (1979) {tmdb-348}.mkv"
        old_file = old_dir / "Alien (1979) {tmdb-348}.mkv"
        new_file.write_bytes(b"new")
        old_file.write_bytes(b"old")

        model = _Model(json.dumps({
            "cuts": [
                {"file": "new: Alien (1979) {tmdb-348}.mkv",
                 "edition": "Director's Cut",
                 "reason": "116m matches the 2003 Director's Cut."},
                {"file": "existing: Alien (1979) {tmdb-348}.mkv",
                 "edition": "Theatrical",
                 "reason": "117m+ matches the Theatrical Cut."},
            ]
        }))
        new_prof = plexcompare.Profile(
            path=str(new_file), seconds=6960.0, width=3840, height=1600,
            hdr="HDR10", audio=(), commentaries=(), others=(),
            chapters=0, named_chapters=False, edition="")
        old_prof = plexcompare.Profile(
            path=str(old_file), seconds=7080.0, width=1920, height=800,
            hdr="", audio=(), commentaries=(), others=(),
            chapters=0, named_chapters=False, edition="")
        copy = plexserver.Copy(
            key="1",
            film=filmmatch.Film(title="Alien", year="1979", tmdb="348",
                                imdb="tt0078748"),
            section="Movies", edition="", files=(str(old_file),),
        )
        cut_entries: list = []
        cut_suggestions: dict[str, list[str]] = {}
        ctx = ingest_movies_plex._EditionContext(
            searchers=[model], resolved=True,
            cut_entries=cut_entries, cut_suggestions=cut_suggestions)
        logged: list[str] = []

        ingest_movies_plex._suggest_cut_editions(
            str(new_file), new_prof, old_prof, copy, logged.append, ctx)

        assert any("{edition-Director's Cut}" in line for line in logged)
        assert any("{edition-Theatrical}" in line for line in logged)
        assert ctx.tally.requests == 1
        assert len(cut_entries) == 1
        assert str(new_dir) in cut_suggestions
        # Guard: neither file was renamed
        assert new_file.is_file()
        assert old_file.is_file()
