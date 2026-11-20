"""Tests for undated film folder release-year lookup via a web-searching
language model - medialib.lib.aifilmyear and its integration with
medialib.lib.tmdblookup / medialib.lib.tmdbidentify.
"""

from __future__ import annotations

import json
import re

import pytest

from medialib.lib import (
    aifilmyear,
    llmclient,
    llmroster,
    modelmeter,
    tmdbclient,
    tmdbidentify,
    tmdblookup,
)
from medialib.lib.safety import SkipLog

pytestmark = pytest.mark.pure


class _Model:
    """Stand-in web-searching model that records prompts and answers or raises."""

    def __init__(self, answer, can_search: bool = True,
                 name: str = "stand-in") -> None:
        self.answer = answer
        self.can_search = can_search
        self.name = name
        self.asked: list[tuple[str, str]] = []
        self.purpose = ""

    def searching(self, purpose: str = ""):
        self.purpose = purpose
        return self if self.can_search else None

    def ask(self, system: str, prompt: str, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 35, 600)
        return self.answer


def _film(film_id: int, title: str, released: str, imdb: str,
          runtime: int | None = None) -> dict:
    return {
        "id": film_id,
        "title": title,
        "original_title": title,
        "release_date": released,
        "runtime": runtime,
        "external_ids": {"imdb_id": imdb},
        "alternative_titles": {"titles": []},
        "release_dates": {"results": []},
    }


def _stub_tmdb(monkeypatch, catalogue: list[dict]):
    """Stub TMDb search and movie-detail requests against ``catalogue``."""
    monkeypatch.setenv("tmdbApiKey", "test-key")
    asked: list[dict[str, str]] = []
    by_id = {str(film["id"]): film for film in catalogue}

    def fake_curl(url, params):
        query = dict(params)
        if url.endswith("/search/movie"):
            asked.append(query)
            wanted_year = query.get("year", "")
            rows = [
                {
                    "id": film["id"],
                    "title": film["title"],
                    "original_title": film["original_title"],
                    "release_date": film["release_date"],
                }
                for film in catalogue
                if not wanted_year or film["release_date"].startswith(wanted_year)
            ]
            return json.dumps({"results": rows}).encode()
        match = re.search(r"/movie/(\d+)$", url)
        if match and match.group(1) in by_id:
            return json.dumps(by_id[match.group(1)]).encode()
        return json.dumps({}).encode()

    monkeypatch.setattr(tmdbclient, "_curl", fake_curl)
    return asked


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aifilmyear.SWITCH_VARIABLE, "on")
    aifilmyear.remember_answers(False)
    yield
    aifilmyear.remember_answers(False)


# --- roster task definition --------------------------------------------------


def test_roster_task_traits():
    task = llmroster.FILM_YEAR
    assert task.name == "film-year"
    assert task.requires == ("web",)
    assert task.prefers == ("reasoning",)
    assert task.lean == "balanced"
    assert llmroster.TASKS["film-year"] is task


# --- prompt construction -----------------------------------------------------


def test_prompt_includes_folder_files_runtime_and_written_year():
    text = aifilmyear.prompt(
        "Unplugged in Oslo",
        "Unplugged in Oslo",
        ["Unplugged in Oslo.mkv", "Unplugged in Oslo.en.srt"],
        runtime_seconds=5100.0,
        written_year="2021",
        parent_name="Concerts",
    )
    assert "Folder name: Unplugged in Oslo" in text
    assert "Parent folder: Concerts" in text
    assert "- Unplugged in Oslo.en.srt" in text
    assert "- Unplugged in Oslo.mkv" in text
    assert "1:25:00 (85 min)" in text
    assert "2021" in text


def test_prompt_omits_optional_sections_when_absent_and_caps_file_list():
    many = [f"file-{i:03d}.mkv" for i in range(aifilmyear.MAX_LISTED_FILES + 5)]
    text = aifilmyear.prompt("Raw Concert", "Concert", many)
    assert "Parsed title: Concert" in text
    assert "## Media details" not in text
    assert "... and 5 more file(s)" in text


# --- strict JSON parsing & validation guards ---------------------------------


@pytest.mark.parametrize("confidence", ["high", "HIGH", "Confident", " confident "])
def test_parse_answer_accepts_valid_confident_integer_year(confidence):
    reply = json.dumps({
        "year": 2019,
        "confidence": confidence,
        "source": "https://www.themoviedb.org/movie/123",
        "reason": "Concert film released in 2019, 85 min runtime matches.",
    })
    verdict = aifilmyear.parse_answer(reply, written_year="2021", max_year=2027)
    assert verdict.year == "2019"
    assert verdict.confidence in ("high", "confident")
    assert "themoviedb.org" in verdict.source
    assert "85 min" in verdict.reason
    assert verdict.rejected == ""


def test_parse_answer_accepts_min_and_max_boundaries_and_fenced_json():
    fenced = 'Sure:\n```json\n{"year": 1888, "confidence": "high"}\n```'
    assert aifilmyear.parse_answer(fenced, max_year=2027).year == "1888"
    at_max = json.dumps({"year": 2027, "confidence": "high"})
    assert aifilmyear.parse_answer(at_max, max_year=2027).year == "2027"
    at_written = json.dumps({"year": 2020, "confidence": "high"})
    assert aifilmyear.parse_answer(at_written, written_year="2020",
                                   max_year=2027).year == "2020"


@pytest.mark.parametrize("reply", [
    "no json here at all",
    "[2019]",
    json.dumps({"year": None, "confidence": "unsure", "reason": "ambiguous"}),
    json.dumps({"year": None, "confidence": "high"}),
    json.dumps({"year": 2019, "confidence": "medium"}),
    json.dumps({"year": 2019, "confidence": "low"}),
    json.dumps({"year": 2019, "confidence": "unsure"}),
    json.dumps({"year": 2019}),
    json.dumps({"year": 2019, "confidence": True}),
    json.dumps({"year": True, "confidence": "high"}),
    json.dumps({"year": False, "confidence": "high"}),
    json.dumps({"year": "2019", "confidence": "high"}),
    json.dumps({"year": 2019.0, "confidence": "high"}),
    json.dumps({"year": 1887, "confidence": "high"}),
    json.dumps({"year": 2028, "confidence": "high"}),
    json.dumps({"year": -1999, "confidence": "high"}),
])
def test_parse_answer_rejects_invalid_or_unconfident_replies(reply):
    verdict = aifilmyear.parse_answer(reply, max_year=2027)
    assert verdict.year == ""
    assert verdict.rejected


def test_parse_answer_rejects_year_after_written_mux_year():
    reply = json.dumps({
        "year": 2023,
        "confidence": "high",
        "source": "IMDb",
        "reason": "Remake released in 2023.",
    })
    verdict = aifilmyear.parse_answer(reply, written_year="2020", max_year=2027)
    assert verdict.year == ""
    assert "after file written year 2020" in verdict.rejected


# --- lookup_year control flow, errors, logging & modelmeter ------------------


def test_switch_off_skips_model_and_probes(monkeypatch):
    monkeypatch.setenv(aifilmyear.SWITCH_VARIABLE, "off")
    model = _Model(json.dumps({"year": 2019, "confidence": "high"}))
    probed = []

    verdict = aifilmyear.lookup_year(
        "Live in Oslo", "Live in Oslo", ["Live in Oslo.mkv"],
        runtime_seconds=lambda: (probed.append("runtime"), 3600.0)[1],
        written_year=lambda: (probed.append("written"), "2020")[1],
        searchers=[model],
    )
    assert verdict.year == ""
    assert model.asked == []
    assert probed == []


def test_no_web_model_available_returns_empty_without_probing(monkeypatch):
    monkeypatch.setattr(llmroster, "optional",
                        lambda task: ([], "no model has web"))
    probed = []

    verdict = aifilmyear.lookup_year(
        "Live in Oslo", "Live in Oslo", ["Live in Oslo.mkv"],
        runtime_seconds=lambda: (probed.append("runtime"), 3600.0)[1],
    )
    assert verdict.year == ""
    assert probed == []


def test_llm_error_falls_back_to_next_searcher_and_handles_all_failing(tmp_path):
    meter = str(tmp_path / "meter")
    lines: list[str] = []
    failing = _Model(llmclient.LlmError("rate limited"), name="first")
    non_web = _Model(json.dumps({"year": 2000, "confidence": "high"}),
                     can_search=False, name="local-only")
    backup = _Model(json.dumps({
        "year": 2016,
        "confidence": "high",
        "source": "TMDb",
        "reason": "2016 live special.",
    }), name="backup")

    verdict = aifilmyear.lookup_year(
        "Acoustic Night", "Acoustic Night", ["Acoustic Night.mkv"],
        searchers=[non_web, failing, backup],
        log=lines.append,
        meter_file=meter,
        max_year=2027,
    )
    assert verdict.year == "2016"
    assert non_web.asked == []
    assert len(failing.asked) == 1
    assert len(backup.asked) == 1
    assert backup.purpose == aifilmyear.PURPOSE
    assert any("rate limited" in line for line in lines)
    assert any("2016" in line and "TMDb" in line and "2016 live special" in line
               for line in lines)

    spans, tally = modelmeter.gathered(meter)
    assert tally.requests == 1
    assert tally.tokens == 35
    assert tally.read == 600
    assert spans.seconds >= 0.0

    # All searchers failing returns empty cleanly without raising.
    all_fail_lines: list[str] = []
    empty = aifilmyear.lookup_year(
        "Acoustic Night", "Acoustic Night", ["Acoustic Night.mkv"],
        searchers=[_Model(llmclient.LlmError("offline"), name="down")],
        log=all_fail_lines.append,
    )
    assert empty.year == ""
    assert any("offline" in line for line in all_fail_lines)


def test_remember_answers_caches_roster_lookup(monkeypatch):
    model = _Model(json.dumps({
        "year": 2018, "confidence": "high", "source": "IMDb", "reason": "matched",
    }))
    monkeypatch.setattr(llmroster, "optional", lambda task: ([model], ""))
    aifilmyear.remember_answers(True)

    first = aifilmyear.lookup_year("Live Set", "Live Set", ["Live Set.mkv"],
                                   log=lambda _m: None, max_year=2027)
    second = aifilmyear.lookup_year("Live Set", "Live Set", ["Live Set.mkv"],
                                    log=lambda _m: None, max_year=2027)
    assert first.year == "2018"
    assert second == first
    assert len(model.asked) == 1


# --- integration with tmdblookup.tag_plex_ids --------------------------------


def test_dated_and_file_dated_and_glued_folders_never_call_model(
        tmp_path, monkeypatch):
    for rel, filename in [
        ("Moss Harbour (1953)", "Moss Harbour (1953).mkv"),
        ("File Dated", "File Dated (1978).mkv"),
        ("Glued Film(1999)", "Glued Film(1999).mkv"),
    ]:
        folder = tmp_path / rel
        folder.mkdir()
        (folder / filename).write_bytes(b"video")

    _stub_tmdb(monkeypatch, [
        _film(1, "Moss Harbour", "1953-04-01", "tt0000101", runtime=80),
        _film(2, "File Dated", "1978-05-01", "tt0000102", runtime=90),
        _film(3, "Glued Film", "1999-06-01", "tt0000103", runtime=100),
    ])
    called = []
    monkeypatch.setattr(
        aifilmyear, "lookup_year",
        lambda *args, **kwargs: (called.append(args), aifilmyear.YearLookup())[1],
    )
    monkeypatch.setattr(tmdblookup, "_folder_runtime", lambda *_a: 0.0)
    monkeypatch.setattr(tmdbidentify, "_written_year", lambda _paths: "")

    tmdblookup.tag_plex_ids(str(tmp_path), lambda _m: None, SkipLog())
    assert called == []
    assert (tmp_path / "Moss Harbour (1953) {imdb-tt0000101}").is_dir()
    assert (tmp_path / "File Dated (1978) {imdb-tt0000102}").is_dir()
    assert (tmp_path / "Glued Film (1999) {imdb-tt0000103}").is_dir()


def test_undated_folder_feeds_model_year_into_tmdb_identification(
        tmp_path, monkeypatch):
    # Two films share the exact title AND nearly identical runtimes, so undated
    # deterministic lookup cannot pick between them; the model supplies 1978
    # and TMDb verifies title + year + runtime to rename cleanly.
    folder = tmp_path / "Moss Harbour"
    folder.mkdir()
    (folder / "Moss Harbour.mkv").write_bytes(b"video")

    asked_tmdb = _stub_tmdb(monkeypatch, [
        _film(1, "Moss Harbour", "1953-04-01", "tt0000101", runtime=80),
        _film(2, "Moss Harbour", "1978-05-01", "tt0000102", runtime=79),
    ])
    model = _Model(json.dumps({
        "year": 1978,
        "confidence": "high",
        "source": "https://www.themoviedb.org/movie/2",
        "reason": "1978 concert documentary runs 79 minutes.",
    }), name="web-llm")
    monkeypatch.setattr(llmroster, "optional", lambda task: ([model], ""))
    monkeypatch.setattr(tmdblookup, "_folder_runtime", lambda *_a: 79 * 60.0)
    monkeypatch.setattr(tmdbidentify, "_written_year", lambda _paths: "2020")

    lines: list[str] = []
    meter = str(tmp_path / "yearMeter")
    tmdblookup.tag_plex_ids(str(tmp_path), lines.append, SkipLog(),
                            meter_file=meter)

    assert len(model.asked) == 1
    assert "Moss Harbour" in model.asked[0][1]
    assert "79 min" in model.asked[0][1]
    assert any(q.get("year") == "1978" for q in asked_tmdb)
    assert (tmp_path / "Moss Harbour (1978) {imdb-tt0000102}").is_dir()
    assert (tmp_path / "Moss Harbour (1978) {imdb-tt0000102}"
            / "Moss Harbour (1978) {imdb-tt0000102}.mkv").is_file()
    assert any("1978" in line and "themoviedb.org" in line for line in lines)
    _spans, tally = modelmeter.gathered(meter)
    assert tally.requests == 1


def test_model_year_never_renames_alone_when_tmdb_rejects_or_runtime_conflicts(
        tmp_path, monkeypatch):
    # Case A: Model suggests 1953, but TMDb's 1953 film is 45 min while disk is
    # 120 min (and there is no 120 min film on TMDb). Runtime guard refuses it
    # and the folder stays completely untouched and listed as unmatched.
    folder = tmp_path / "Moss Harbour"
    folder.mkdir()
    (folder / "Moss Harbour.mkv").write_bytes(b"video")

    _stub_tmdb(monkeypatch, [
        _film(1, "Moss Harbour", "1953-04-01", "tt0000101", runtime=45),
    ])
    model = _Model(json.dumps({
        "year": 1953,
        "confidence": "high",
        "source": "guess",
        "reason": "wrong film",
    }))
    monkeypatch.setattr(llmroster, "optional", lambda task: ([model], ""))
    monkeypatch.setattr(tmdblookup, "_folder_runtime", lambda *_a: 120 * 60.0)
    monkeypatch.setattr(tmdbidentify, "_written_year", lambda _paths: "2020")

    unmatched: list[str] = []
    tmdblookup.tag_plex_ids(str(tmp_path), lambda _m: None, SkipLog(),
                            unmatched=unmatched)

    assert (tmp_path / "Moss Harbour" / "Moss Harbour.mkv").is_file()
    assert unmatched == ["Moss Harbour"]


def test_unsure_model_falls_back_to_deterministic_undated_lookup(
        tmp_path, monkeypatch):
    folder = tmp_path / "Moss Harbour"
    folder.mkdir()
    (folder / "Moss Harbour.mkv").write_bytes(b"video")

    _stub_tmdb(monkeypatch, [
        _film(1, "Moss Harbour", "1953-04-01", "tt0000101", runtime=80),
        _film(2, "Moss Harbour", "2008-05-01", "tt0000102", runtime=58),
    ])
    model = _Model(json.dumps({
        "year": None,
        "confidence": "unsure",
        "reason": "two films share this title",
    }))
    monkeypatch.setattr(llmroster, "optional", lambda task: ([model], ""))
    monkeypatch.setattr(tmdblookup, "_folder_runtime", lambda *_a: 79 * 60.0)
    monkeypatch.setattr(tmdbidentify, "_written_year", lambda _paths: "")

    tmdblookup.tag_plex_ids(str(tmp_path), lambda _m: None, SkipLog())

    assert len(model.asked) == 1
    assert (tmp_path / "Moss Harbour (1953) {imdb-tt0000101}").is_dir()
