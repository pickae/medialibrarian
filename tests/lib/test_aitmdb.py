"""Tests for medialib.lib.aitmdb and its integration with tmdblookup/tmdbworklists."""

from __future__ import annotations

import contextlib
import json

import pytest

from medialib.cli import ingest_movies_run
from medialib.lib import (
    aitmdb,
    durationcheck,
    llmclient,
    llmroster,
    modelmeter,
    tmdbclient,
    tmdbidentify,
    tmdblookup,
    tmdbworklists,
    tooldeps,
)

pytestmark = pytest.mark.pure


def _offer(imdb: str = "", tmdb: str = "", titles: tuple = ("The Film",),
           years: tuple = ("2001",), released: str = "2001",
           runtime: float = 100.0, source: str = "tmdb") -> tmdbidentify.Offer:
    return tmdbidentify.Offer(
        imdb=imdb,
        tmdb=tmdb,
        titles=titles,
        years=years,
        released=released,
        runtime=runtime,
        source=source,
    )


class _StubModel:
    """Minimal stand-in for llmclient.Llm supporting serving or searching."""

    def __init__(self, name: str, reply: str | Exception,
                 *, searchable: bool = False, tokens: int = 42, read: int = 300):
        self.name = name
        self._reply = reply
        self._searchable = searchable
        self._tokens = tokens
        self._read = read
        self.calls: list[tuple[str, str, str]] = []

    def _answer(self, kind: str, system: str, user: str,
                tally: modelmeter.Tally | None) -> str:
        self.calls.append((kind, system, user))
        if isinstance(self._reply, Exception):
            raise self._reply
        if tally is not None:
            tally.add(0.25, self._tokens, self._read)
        return self._reply

    def searching(self, purpose: str):
        if not self._searchable:
            return None
        model = self

        class _Searcher:
            def ask(self, system: str, user: str,
                    tally: modelmeter.Tally | None = None) -> str:
                return model._answer("search:" + purpose, system, user, tally)

        return _Searcher()

    @contextlib.contextmanager
    def serving(self, _log=None):
        model = self

        class _Bound:
            def ask(self, system: str, user: str,
                    tally: modelmeter.Tally | None = None) -> str:
                return model._answer("serve", system, user, tally)

        yield _Bound()


class TestSwitchAndRoster:
    def test_switch_modes(self, monkeypatch):
        monkeypatch.delenv(aitmdb.SWITCH_VARIABLE, raising=False)
        assert aitmdb.mode() == "on"
        assert aitmdb.enabled()
        assert aitmdb.accept_outright()

        monkeypatch.setenv(aitmdb.SWITCH_VARIABLE, "suggest")
        assert aitmdb.mode() == "suggest"
        assert aitmdb.enabled()
        assert not aitmdb.accept_outright()

        for off_value in ("off", "OFF", "0", "no", "false"):
            monkeypatch.setenv(aitmdb.SWITCH_VARIABLE, off_value)
            assert aitmdb.mode() == "off"
            assert not aitmdb.enabled()
            assert not aitmdb.accept_outright()

    def test_roster_task_prefers_web_and_reasoning_without_requiring_web(self):
        assert llmroster.TASKS["film-identify"] is llmroster.FILM_IDENTIFY
        assert llmroster.FILM_IDENTIFY.requires == ()
        assert llmroster.FILM_IDENTIFY.prefers == ("web", "reasoning")
        assert llmroster.FILM_IDENTIFY.lean == "precise"

        local_plain = llmclient.Llm(
            name="local-plain", api="openai", url="http://localhost:8081/v1",
            model="qwen3-8b",
            said={"size": "small", "reasoning": False, "local": True},
        )
        local_reason = llmclient.Llm(
            name="local-reason", api="openai", url="http://localhost:8082/v1",
            model="qwen3-32b",
            said={"size": "medium", "reasoning": True, "local": True},
        )
        cloud_web = llmclient.Llm(
            name="claude", api="anthropic", url="", model="claude-opus-5-5",
            said={"size": "large", "reasoning": True, "cloud": True},
        )
        roster = llmroster.Roster([local_plain, local_reason, cloud_web])
        ordered = [m.name for m in roster.ladder(llmroster.FILM_IDENTIFY)]
        assert ordered == ["claude", "local-reason", "local-plain"]


class TestPromptAndParse:
    def test_prompt_includes_folder_files_offers_and_notes(self, tmp_path):
        folder = tmp_path / "Cinema" / "Nordwind (2001)"
        folder.mkdir(parents=True)
        movie = folder / "Nordwind.2001.German.1080p.mkv"
        movie.write_bytes(b"x" * 2048)

        offers = [
            _offer(imdb="tt0100001", tmdb="101",
                   titles=("Nordwind", "North Wind"), years=("2001",),
                   runtime=98.0, source="tmdb"),
            _offer(tmdb="202", titles=("Nordwind Live",), years=("2001",),
                   runtime=0.0, source="tmdb-search"),
        ]
        text = aitmdb.prompt(
            str(folder), "Nordwind (2001)", [movie.name], offers,
            ['asked "Nordwind": 2 result(s)', "    two films fit the year"],
            seconds=5880.0, written_year="2019", year="2001",
        )
        assert "Folder: Nordwind (2001)" in text
        assert "Cinema" in text
        assert "Year from folder: 2001" in text
        assert "1:38:00 (98 min)" in text
        assert "Earliest mux/container year: 2019" in text
        assert "Nordwind.2001.German.1080p.mkv (2 KiB)" in text
        assert "tt0100001 / tmdb-101" in text
        assert "tmdb-202" in text
        assert "TMDb search only" in text
        assert 'asked "Nordwind": 2 result(s)' in text

    @pytest.mark.parametrize("raw_id,expected_imdb,expected_tmdb", [
        ("tt0100001", "tt0100001", "101"),
        ("imdb-tt0100001", "tt0100001", "101"),
        ("{imdb-tt0100001}", "tt0100001", "101"),
        ("tmdb-101", "tt0100001", "101"),
        ("101", "tt0100001", "101"),
        (101, "tt0100001", "101"),
        ("tmdb-202", "", "202"),
        ("{tmdb-202}", "", "202"),
    ])
    def test_parse_accepts_any_spelling_of_an_offered_id(
        self, raw_id, expected_imdb, expected_tmdb
    ):
        offers = [
            _offer(imdb="tt0100001", tmdb="101"),
            _offer(imdb="", tmdb="202"),
        ]
        reply = '```json\n%s\n```' % json.dumps({
            "id": raw_id,
            "confidence": "high",
            "reason": "German title and runtime match.",
        })
        verdict = aitmdb.parse(reply, offers)
        assert verdict.asked
        assert verdict.offer is not None
        assert verdict.offer.imdb == expected_imdb
        assert verdict.offer.tmdb == expected_tmdb
        assert verdict.confidence == "high"
        assert verdict.rejected_id == ""

    @pytest.mark.parametrize("invented", ["tt9999999", "tmdb-99999", "99999", "not-an-id"])
    def test_parse_rejects_invented_ids_not_in_offered(self, invented):
        offers = [_offer(imdb="tt0100001", tmdb="101")]
        reply = json.dumps({
            "id": invented,
            "confidence": "high",
            "reason": "Sure it is this other film.",
        })
        verdict = aitmdb.parse(reply, offers)
        assert verdict.asked
        assert verdict.offer is None
        assert verdict.rejected_id == invented

    @pytest.mark.parametrize("empty_id", [None, "", "null", "none", "NONE"])
    def test_parse_handles_none_decision(self, empty_id):
        offers = [_offer(imdb="tt0100001", tmdb="101")]
        reply = json.dumps({
            "id": empty_id,
            "confidence": "medium",
            "reason": "None of the offered candidates matches the runtime.",
        })
        verdict = aitmdb.parse(reply, offers)
        assert verdict.asked
        assert verdict.offer is None
        assert verdict.rejected_id == ""
        assert "runtime" in verdict.reason

    def test_parse_handles_unparseable_reply(self):
        offers = [_offer(imdb="tt0100001", tmdb="101")]
        verdict = aitmdb.parse("Sorry, I cannot help with that.", offers)
        assert verdict.asked
        assert verdict.offer is None
        assert verdict.rejected_id == ""


class TestEvaluateGuard:
    def test_accepts_only_high_confidence_with_fitting_runtime(self, monkeypatch):
        monkeypatch.setenv(aitmdb.SWITCH_VARIABLE, "on")
        offer = _offer(imdb="tt0100001", tmdb="101", years=("2001",),
                       released="2001", runtime=100.0)
        base = aitmdb.Verdict(offer=offer, confidence="high",
                              reason="Exact match.", asked=True)

        accepted = aitmdb.evaluate(base, seconds=6060.0, written_year="2015",
                                   folder_year="2001")
        assert accepted.accepted
        assert accepted.hold_reason == ""

        # Medium confidence -> held for review
        med = aitmdb.evaluate(base._replace(confidence="medium"),
                              seconds=6060.0, written_year="2015",
                              folder_year="2001")
        assert not med.accepted
        assert "medium" in med.hold_reason

        # Runtime mismatch -> held for review
        wrong_len = aitmdb.evaluate(base, seconds=8400.0, written_year="2015",
                                    folder_year="2001")
        assert not wrong_len.accepted
        assert "runtime" in wrong_len.hold_reason

        # Disk runtime unknown -> held for review
        no_disk = aitmdb.evaluate(base, seconds=0.0, written_year="2015",
                                  folder_year="2001")
        assert not no_disk.accepted
        assert "disk runtime unknown" in no_disk.hold_reason

        # Catalogue runtime unknown -> held for review
        no_cat = aitmdb.evaluate(
            base._replace(offer=offer._replace(runtime=0.0)),
            seconds=6060.0, written_year="2015", folder_year="2001",
        )
        assert not no_cat.accepted
        assert "catalogue runtime unknown" in no_cat.hold_reason

        # Released after mux year -> held for review
        future = aitmdb.evaluate(base, seconds=6060.0, written_year="1999",
                                 folder_year="2001")
        assert not future.accepted
        assert "after the file was written" in future.hold_reason

        # Undated folder + undated offer -> held for review
        undated = aitmdb.evaluate(
            base._replace(offer=offer._replace(years=(), released="")),
            seconds=6060.0, written_year="2015", folder_year="",
        )
        assert not undated.accepted
        assert "no release year" in undated.hold_reason

        # Undated folder + dated offer -> accepted when runtime fits
        dated_offer = aitmdb.evaluate(base, seconds=6060.0, written_year="2015",
                                      folder_year="")
        assert dated_offer.accepted

    def test_suggest_mode_never_accepts_outright(self, monkeypatch):
        monkeypatch.setenv(aitmdb.SWITCH_VARIABLE, "suggest")
        offer = _offer(imdb="tt0100001", runtime=100.0)
        base = aitmdb.Verdict(offer=offer, confidence="high", asked=True)
        checked = aitmdb.evaluate(base, seconds=6000.0, folder_year="2001")
        assert not checked.accepted
        assert "suggest" in checked.hold_reason


class TestIdentifyFilmExecution:
    def test_skips_quietly_when_off_without_probing_disk(self, monkeypatch):
        monkeypatch.setenv(aitmdb.SWITCH_VARIABLE, "off")
        probed = []
        model = _StubModel("m1", '{"id": "tt0100001", "confidence": "high", "reason": "ok"}')
        verdict = aitmdb.identify_film(
            "/films/Test (2001)", "Test (2001)", ["Test (2001).mkv"],
            [_offer(imdb="tt0100001")], [],
            seconds=lambda: (probed.append("runtime"), 6000.0)[1],
            written_year=lambda: (probed.append("written"), "2020")[1],
            year="2001", models=[model],
        )
        assert not verdict.asked
        assert probed == []
        assert model.calls == []

    def test_uses_searching_when_available_and_falls_back_on_error(self, monkeypatch):
        monkeypatch.setenv(aitmdb.SWITCH_VARIABLE, "on")
        failing = _StubModel("first", llmclient.LlmError("timeout"), searchable=True)
        backup = _StubModel(
            "second",
            json.dumps({"id": "tt0100001", "confidence": "high",
                        "reason": "Translated title matches."}),
            searchable=False,
        )
        lines: list[str] = []
        meter = (modelmeter.Spans(), modelmeter.Tally())
        verdict = aitmdb.identify_film(
            "/films/Test (2001)", "Test (2001)", ["Test (2001).mkv"],
            [_offer(imdb="tt0100001", runtime=100.0)], ["note 1"],
            seconds=6000.0, written_year="2020", year="2001",
            models=[failing, backup], log=lines.append, meter=meter,
        )
        assert verdict.accepted
        assert verdict.offer is not None and verdict.offer.imdb == "tt0100001"
        assert failing.calls[0][0] == "search:identify"
        assert backup.calls[0][0] == "serve"
        assert any("No film identification from first: timeout" in line for line in lines)
        assert any("identify" in line for line in lines)
        assert meter[1].requests == 1
        assert meter[1].tokens == 42


class TestWorklistSuggestionsRoundTrip:
    def test_write_and_read_prefilled_suggestions_and_cleared_rows(self, tmp_path):
        tsv = tmp_path / "unmatched.tsv"
        suggestions = {
            "Nordwind (2001)": ("tt0100001", "medium", "German title matches"),
            "Concert Live (2015)": ("tmdb-9090", "low", "Concert recording"),
        }
        assert tmdbworklists.write_id_list(
            str(tsv),
            ["Nordwind (2001)", "Concert Live (2015)", "Mystery Film (1990)"],
            ids={"Already Known (1985)": "{imdb-tt0000099}"},
            suggestions=suggestions,
        )
        text = tsv.read_text(encoding="utf-8")
        assert "# Rows marked '# model (...)'" in text
        assert "Nordwind (2001)\ttt0100001\t# model (medium): German title matches" in text
        assert "Concert Live (2015)\ttmdb-9090\t# model (low): Concert recording" in text
        assert "Mystery Film (1990)\t" in text

        blank: list[str] = []
        read = tmdbworklists.read_id_list(str(tsv), blank)
        assert read == {
            "Already Known (1985)": "{imdb-tt0000099}",
            "Nordwind (2001)": "{imdb-tt0100001}",
            "Concert Live (2015)": "{tmdb-9090}",
        }
        assert blank == ["Mystery Film (1990)"]

        # Clearing a suggested id while keeping its trailing comment makes it blank
        # and a '#' inside a folder name is preserved.
        edited = tmp_path / "edited.tsv"
        edited.write_text(
            "Nordwind (2001)\t\t# model (medium): German title matches\n"
            "Film #2 (2005)\ttt0200002\t# model (high): exact\n",
            encoding="utf-8",
        )
        blank2: list[str] = []
        read2 = tmdbworklists.read_id_list(str(edited), blank2)
        assert read2 == {"Film #2 (2005)": "{imdb-tt0200002}"}
        assert blank2 == ["Nordwind (2001)"]


class TestTmdbLookupIntegration:
    """Exercise the full give-up branch in tmdblookup.tag_plex_ids."""

    def _stub_two_ambiguous_candidates(self, monkeypatch):
        """Make TMDb return two films sharing title & year so certainty rules
        cannot settle, handing both to aitmdb."""
        monkeypatch.setenv("tmdbApiKey", "test-key")
        monkeypatch.setenv(aitmdb.SWITCH_VARIABLE, "on")

        def fake_curl(url: str, params):
            if url.endswith("/search/movie"):
                return json.dumps({"results": [
                    {"id": 101, "title": "Mirage", "original_title": "Mirage",
                     "release_date": "2001-05-10"},
                    {"id": 102, "title": "Mirage", "original_title": "Mirage",
                     "release_date": "2001-09-20"},
                ]})
            if url.endswith("/movie/101"):
                return json.dumps({
                    "id": 101, "title": "Mirage", "original_title": "Mirage",
                    "release_date": "2001-05-10", "runtime": 100,
                    "external_ids": {"imdb_id": "tt0101001"},
                    "alternative_titles": {"titles": []},
                    "translations": {"translations": []},
                    "release_dates": {"results": []},
                })
            if url.endswith("/movie/102"):
                return json.dumps({
                    "id": 102, "title": "Mirage", "original_title": "Mirage",
                    "release_date": "2001-09-20", "runtime": 102,
                    "external_ids": {"imdb_id": "tt0102002"},
                    "alternative_titles": {"titles": []},
                    "translations": {"translations": []},
                    "release_dates": {"results": []},
                })
            return "{}"

        monkeypatch.setattr(tmdbclient, "_curl", fake_curl)
        monkeypatch.setattr(tmdbidentify, "_written_year", lambda _files: "2020")

    def test_outright_accept_renames_in_dry_run_and_write(self, tmp_path, monkeypatch):
        self._stub_two_ambiguous_candidates(monkeypatch)
        folder = tmp_path / "Mirage (2001)"
        folder.mkdir()
        (folder / "Mirage (2001).mkv").write_bytes(b"movie")
        # Both 100 min and 102 min fit 101 min, so certainty rules cannot settle;
        # the model chooses tt0101001 at high confidence and runtime fits it.
        monkeypatch.setattr(durationcheck, "total_duration", lambda _files: 6060.0)
        stub = _StubModel(
            "test-llm",
            json.dumps({"id": "tt0101001", "confidence": "high",
                        "reason": "May festival release matches file clues."}),
        )
        monkeypatch.setattr(llmroster, "optional", lambda _task: ([stub], ""))

        lines: list[str] = []
        unmatched: list[str] = []
        planned: list[tuple[str, str]] = []
        near_misses: list = []
        suggestions: dict = {}
        tmdblookup.tag_plex_ids(
            str(tmp_path), lines.append, dry_run=True, unmatched=unmatched,
            planned=planned, near_misses=near_misses, suggestions=suggestions,
        )
        assert unmatched == []
        assert near_misses == []
        assert suggestions == {}
        assert len(planned) == 2
        assert any("model matched (high, runtime fits)" in line for line in lines)
        assert folder.exists()

        # Real write renames the folder and file
        tmdblookup.tag_plex_ids(str(tmp_path), lambda _m: None, dry_run=False)
        renamed = tmp_path / "Mirage (2001) {imdb-tt0101001}"
        assert renamed.is_dir()
        assert (renamed / "Mirage (2001) {imdb-tt0101001}.mkv").is_file()

    def test_medium_confidence_writes_suggestion_and_near_miss_note(
        self, tmp_path, monkeypatch
    ):
        self._stub_two_ambiguous_candidates(monkeypatch)
        folder = tmp_path / "Mirage (2001)"
        folder.mkdir()
        (folder / "Mirage (2001).mkv").write_bytes(b"movie")
        monkeypatch.setattr(durationcheck, "total_duration", lambda _files: 6060.0)
        stub = _StubModel(
            "test-llm",
            json.dumps({"id": "tt0102002", "confidence": "medium",
                        "reason": "Likely the September release."}),
        )
        monkeypatch.setattr(llmroster, "optional", lambda _task: ([stub], ""))

        lines: list[str] = []
        unmatched: list[str] = []
        near_misses: list = []
        suggestions: dict = {}
        tmdblookup.tag_plex_ids(
            str(tmp_path), lines.append, dry_run=True, unmatched=unmatched,
            near_misses=near_misses, suggestions=suggestions,
        )
        assert unmatched == ["Mirage (2001)"]
        assert suggestions == {
            "Mirage (2001)": ("tt0102002", "medium", "Likely the September release.")
        }
        assert any("model suggested tt0102002" in line for line in lines)
        assert len(near_misses) == 1
        assert any("model suggested tt0102002 (medium)" in note
                   for note in near_misses[0][2])

    def test_invented_id_is_rejected_and_logged(self, tmp_path, monkeypatch):
        self._stub_two_ambiguous_candidates(monkeypatch)
        folder = tmp_path / "Mirage (2001)"
        folder.mkdir()
        (folder / "Mirage (2001).mkv").write_bytes(b"movie")
        monkeypatch.setattr(durationcheck, "total_duration", lambda _files: 6060.0)
        stub = _StubModel(
            "test-llm",
            json.dumps({"id": "tt9999999", "confidence": "high",
                        "reason": "Hallucinated id."}),
        )
        monkeypatch.setattr(llmroster, "optional", lambda _task: ([stub], ""))

        lines: list[str] = []
        unmatched: list[str] = []
        near_misses: list = []
        suggestions: dict = {}
        tmdblookup.tag_plex_ids(
            str(tmp_path), lines.append, dry_run=True, unmatched=unmatched,
            near_misses=near_misses, suggestions=suggestions,
        )
        assert unmatched == ["Mirage (2001)"]
        assert suggestions == {}
        assert any('not offered - ignored' in line for line in lines)
        assert any('tt9999999' in note and 'ignored' in note
                   for note in near_misses[0][2])

    def test_identify_collects_and_deduplicates_offers(self, monkeypatch):
        self._stub_two_ambiguous_candidates(monkeypatch)
        offers: list[tmdbidentify.Offer] = []
        notes: list[str] = []
        match = tmdbidentify.identify(
            "Mirage", "2001", runtime=lambda: 6060.0, notes=notes, offered=offers,
        )
        assert not match.tag
        assert [(o.imdb, o.tmdb, o.runtime) for o in offers] == [
            ("tt0101001", "101", 100.0),
            ("tt0102002", "102", 102.0),
        ]

    def test_ids_only_dry_run_writes_prefilled_unmatched_tsv_and_reads_it_back(
        self, tmp_path, monkeypatch
    ):
        self._stub_two_ambiguous_candidates(monkeypatch)
        monkeypatch.setenv(aitmdb.SWITCH_VARIABLE, "suggest")
        library = tmp_path / "Movies"
        folder = library / "Mirage (2001)"
        folder.mkdir(parents=True)
        (folder / "Mirage (2001).mkv").write_bytes(b"movie")
        script_dir = tmp_path / "script"
        script_dir.mkdir()
        monkeypatch.setattr(durationcheck, "total_duration", lambda _files: 6060.0)
        monkeypatch.setattr(tooldeps, "require_tools", lambda _p, _t: 0)
        stub = _StubModel(
            "test-llm",
            json.dumps({"id": "tt0101001", "confidence": "high",
                        "reason": "May festival release matches."}),
        )
        monkeypatch.setattr(llmroster, "optional", lambda _task: ([stub], ""))

        rc = ingest_movies_run._tags_only(
            "ingest-movies", str(script_dir), [str(library)], ["Movies"],
            write=False, id_list="",
        )
        assert rc == 0
        unmatched_tsv = (script_dir / "logs" / "ingest-movies"
                         / "ingest-movies-unmatched-Movies.tsv")
        assert unmatched_tsv.is_file()
        content = unmatched_tsv.read_text(encoding="utf-8")
        assert "Mirage (2001)\ttt0101001\t# model (high): May festival release matches." in content

        # Feeding that TSV straight back with -i applies the pre-filled id
        # without needing the model again.
        monkeypatch.setenv(aitmdb.SWITCH_VARIABLE, "off")
        rc2 = ingest_movies_run._tags_only(
            "ingest-movies", str(script_dir), [str(library)], ["Movies"],
            write=True, id_list=str(unmatched_tsv),
        )
        assert rc2 == 0
        renamed = library / "Mirage (2001) {imdb-tt0101001}"
        assert renamed.is_dir()
        assert (renamed / "Mirage (2001) {imdb-tt0101001}.mkv").is_file()
