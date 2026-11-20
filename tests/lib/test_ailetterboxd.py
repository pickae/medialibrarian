"""Tests for report-only Letterboxd match suggestions - medialib.lib.ailetterboxd.

What is pinned:
- prompt construction for ambiguous matches (both directions) and unmatched
  Letterboxd entries (with close Plex candidates and with none);
- strict JSON parsing and guards rejecting invented candidate indices,
  non-integer/non-positive TMDb ids, and conflicting TMDb ids;
- report formatting saying every model decision clearly as report-only;
- full sync-run integration verifying suggestions reach the report while Plex
  and sync state are NEVER mutated;
- switch off, no model available, LlmError handling/fallback, and modelmeter
  recording.
"""

from __future__ import annotations

import contextlib
import zipfile

import pytest

from medialib.cli import plex_letterboxd_sync as sync
from medialib.lib import ailetterboxd, letterboxd, llmclient, modelmeter
from medialib.lib.filmmatch import Film
from medialib.lib.plexserver import Library, Movie

pytestmark = pytest.mark.pure


class _Model:
    """Stand-in model recording prompts and returning canned answers or errors."""

    def __init__(self, answers, *, can_search: bool = True, name: str = "stand-in"):
        self._answers = list(answers) if isinstance(answers, list) else [answers]
        self.can_search = can_search
        self.name = name
        self.asked: list[tuple[str, str]] = []
        self.purpose = ""
        self.used_search = False

    def searching(self, purpose: str = ""):
        self.purpose = purpose
        if self.can_search:
            self.used_search = True
            return self
        return None

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def ask(self, system: str, prompt: str, tally=None):
        self.asked.append((system, prompt))
        reply = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(reply, Exception):
            raise reply
        if tally is not None:
            tally.add(1.5, 35, 400)
        return reply


def _movie(key: str, title: str, year: str, *, played: bool = False,
           tmdb: str = "", imdb: str = "", aliases: tuple = ()) -> Movie:
    return Movie(key, Film(title, year, tmdb=tmdb, imdb=imdb, aliases=aliases),
                 played, "Films")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(ailetterboxd.SWITCH_VARIABLE, "on")


# --- candidate collection & prompt construction -------------------------------

class TestPromptAndCandidates:
    def test_prompt_for_ambiguous_plex_candidates(self):
        entry = ailetterboxd.Entry(
            status="ambiguous",
            subject=Film("Crash", ""),
            candidates=(
                Film("Crash", "1996", tmdb="883"),
                Film("Crash", "2004", tmdb="1640", aliases=("L.A. Crash",)),
            ),
            direction="plex",
            how="title, no year to compare",
        )
        text = ailetterboxd.prompt(entry)
        assert "Letterboxd film matched more than one Plex film" in text
        assert "title, no year to compare" in text
        assert "Crash (year unknown)" in text
        assert "1. Crash (1996) [tmdb:883]" in text
        assert "2. Crash (2004) aka: L.A. Crash [tmdb:1640]" in text
        assert "<1..2 or null>" in text

    def test_prompt_for_ambiguous_letterboxd_candidates(self):
        entry = ailetterboxd.Entry(
            status="ambiguous",
            subject=Film("Harbour Tales", "1977"),
            candidates=(
                Film("The Harbour Tales", "1977"),
                Film("Harbour Tales, The", "1977"),
            ),
            direction="letterboxd",
            how="title",
        )
        text = ailetterboxd.prompt(entry)
        assert "Plex film matched more than one Letterboxd film" in text
        assert "## Plex entry" in text
        assert "## Candidate Letterboxd films" in text
        assert "1. The Harbour Tales (1977)" in text
        assert "2. Harbour Tales, The (1977)" in text

    def test_prompt_for_unmatched_entry_with_and_without_candidates(self):
        with_cands = ailetterboxd.Entry(
            status="unmatched",
            subject=Film("Spirited Away", "2001"),
            candidates=(Film("Sen to Chihiro no Kamikakushi", "2001", tmdb="129"),),
        )
        text = ailetterboxd.prompt(with_cands)
        assert "had no automatic match in Plex" in text
        assert "1. Sen to Chihiro no Kamikakushi (2001) [tmdb:129]" in text
        assert "<1..1 or null>" in text

        no_cands = ailetterboxd.Entry(
            status="unmatched",
            subject=Film("Obscure Film", "1961"),
            candidates=(),
        )
        empty_text = ailetterboxd.prompt(no_cands)
        assert "no close candidate films were found" in empty_text
        assert "(none)" in empty_text
        assert '"choice": null' in empty_text

    def test_close_candidates_finds_translated_same_year_and_subtitle_matches(self):
        pool = [
            Film("Sen to Chihiro no Kamikakushi", "2001", tmdb="129"),
            Film("Birdman or (The Unexpected Virtue of Ignorance)", "2014", tmdb="194662"),
            Film("Totally Unrelated", "1985"),
            Film("Wrong Id Same Year", "2001", tmdb="999"),
        ]
        # Same year surfaces translated title; conflicting TMDb id is vetoed.
        found_2001 = ailetterboxd.close_candidates(
            Film("Spirited Away", "2001", tmdb="129"), pool)
        assert [f.title for f in found_2001] == ["Sen to Chihiro no Kamikakushi"]

        # Subtitle containment surfaces Birdman (2014) at #1.
        found_birdman = ailetterboxd.close_candidates(Film("Birdman", "2014"), pool)
        assert [f.title for f in found_birdman] == [
            "Birdman or (The Unexpected Virtue of Ignorance)"]

        # Distant film not in Plex yields no candidates.
        assert ailetterboxd.close_candidates(Film("Silent Era Classic", "1927"), pool) == ()

    def test_collect_entries_gathers_ambiguous_before_unmatched(self):
        movies = [
            _movie("1", "Harbour Tales", "1977"),
            _movie("2", "Sen to Chihiro no Kamikakushi", "2001", tmdb="129"),
            _movie("3", "Matched Already", "2020"),
        ]
        films = [
            Film("The Harbour Tales", "1977"),
            Film("Harbour Tales, The", "1977"),
            Film("Spirited Away", "2001"),
            Film("Matched Already", "2020"),
            Film("Not In Library", "1952"),
        ]
        plan = sync.plan(movies, films, [])
        entries = ailetterboxd.collect_entries(movies, films, plan.ambiguous)
        assert len(entries) == 2
        assert entries[0].status == "ambiguous"
        assert entries[0].direction == "letterboxd"
        assert entries[0].subject.title == "Harbour Tales"
        assert entries[1].status == "unmatched"
        assert entries[1].subject.title == "Spirited Away"
        assert [c.title for c in entries[1].candidates] == [
            "Sen to Chihiro no Kamikakushi"]


# --- strict JSON parsing & guards --------------------------------------------

class TestStrictParsingAndGuards:
    def _entry(self, count: int = 2) -> ailetterboxd.Entry:
        cands = (
            Film("Crash", "1996", tmdb="883"),
            Film("Crash", "2004", tmdb="1640"),
        )[:count]
        return ailetterboxd.Entry(
            status="ambiguous" if count > 1 else "unmatched",
            subject=Film("Crash", ""),
            candidates=cands,
        )

    def test_valid_candidate_choice_wrapped_in_fence(self):
        raw = (
            'Sure:\n```json\n{"choice": 2, "tmdb": 1640, "confidence": "HIGH", '
            '"reason": "  Matches the 2004 ensemble film.  "}\n```'
        )
        suggestion = ailetterboxd.parse_answer(raw, self._entry(2), model="m")
        assert suggestion.rejected == ""
        assert suggestion.choice_index == 2
        assert suggestion.choice == Film("Crash", "2004", tmdb="1640")
        assert suggestion.tmdb == "1640"
        assert suggestion.confidence == "high"
        assert suggestion.reason == "Matches the 2004 ensemble film."

    def test_valid_null_choice_with_positive_integer_tmdb(self):
        entry = ailetterboxd.Entry(status="unmatched", subject=Film("Spirited Away", "2001"))
        s_int = ailetterboxd.parse_answer(
            '{"choice": null, "tmdb": 129, "confidence": "high", "reason": "TMDb 129."}',
            entry)
        assert s_int.rejected == ""
        assert s_int.choice is None
        assert s_int.tmdb == "129"

        s_str = ailetterboxd.parse_answer(
            '{"choice": "none", "tmdb": "00129", "confidence": "medium", "reason": "ok"}',
            entry)
        assert s_str.rejected == ""
        assert s_str.choice is None
        assert s_str.tmdb == "129"

    def test_valid_explicit_no_match(self):
        suggestion = ailetterboxd.parse_answer(
            '{"choice": null, "tmdb": null, "confidence": "high", '
            '"reason": "Neither candidate is this film."}',
            self._entry(2))
        assert suggestion.rejected == ""
        assert suggestion.choice is None
        assert suggestion.tmdb == ""
        assert suggestion.reason == "Neither candidate is this film."

    @pytest.mark.parametrize("bad_choice", [0, -1, 3, 99, True, False, 1.5, "2", "Crash", [1]])
    def test_rejects_invented_or_invalid_candidate_index(self, bad_choice):
        import json
        raw = json.dumps({"choice": bad_choice, "tmdb": 1640,
                          "confidence": "high", "reason": "guessed"})
        suggestion = ailetterboxd.parse_answer(raw, self._entry(2))
        assert suggestion.rejected
        assert suggestion.choice is None
        assert suggestion.choice_index is None
        assert suggestion.tmdb == ""

    def test_rejects_choice_when_no_candidates_offered(self):
        entry = ailetterboxd.Entry(status="unmatched", subject=Film("Solo", "2018"))
        suggestion = ailetterboxd.parse_answer(
            '{"choice": 1, "tmdb": 348350, "confidence": "high", "reason": "picked"}',
            entry)
        assert "no candidates offered" in suggestion.rejected
        assert suggestion.choice is None
        assert suggestion.tmdb == ""

    @pytest.mark.parametrize(
        "bad_tmdb", [0, -12, True, False, 12.5, "tt0245429", "tmdb-129", "abc", "0"])
    def test_rejects_non_integer_or_non_positive_tmdb_id(self, bad_tmdb):
        import json
        raw = json.dumps({"choice": None, "tmdb": bad_tmdb,
                          "confidence": "medium", "reason": "bad id"})
        suggestion = ailetterboxd.parse_answer(raw, self._entry(2))
        assert "TMDb id" in suggestion.rejected
        assert suggestion.choice is None
        assert suggestion.tmdb == ""

    def test_rejects_tmdb_id_conflicting_with_candidate_or_subject(self):
        # Candidate 1 has tmdb="883", model claims 9999.
        conflict_cand = ailetterboxd.parse_answer(
            '{"choice": 1, "tmdb": 9999, "confidence": "high", "reason": "x"}',
            self._entry(2))
        assert "conflicts with candidate" in conflict_cand.rejected
        assert conflict_cand.choice is None
        assert conflict_cand.tmdb == ""

        # Subject already has tmdb="129", model claims 555.
        entry = ailetterboxd.Entry(
            status="unmatched", subject=Film("Spirited Away", "2001", tmdb="129"))
        conflict_subj = ailetterboxd.parse_answer(
            '{"choice": null, "tmdb": 555, "confidence": "high", "reason": "x"}',
            entry)
        assert "conflicts with entry" in conflict_subj.rejected
        assert conflict_subj.tmdb == ""

    @pytest.mark.parametrize("raw", ["not json at all", "[1, 2, 3]", "{}"])
    def test_rejects_non_json_or_empty_object(self, raw):
        suggestion = ailetterboxd.parse_answer(raw, self._entry(2))
        assert suggestion.rejected
        assert suggestion.choice is None
        assert suggestion.tmdb == ""


# --- execution, switch, errors, modelmeter -----------------------------------

class TestExecutionAndMetering:
    def test_switch_off_asks_no_model(self, monkeypatch):
        monkeypatch.setenv(ailetterboxd.SWITCH_VARIABLE, "off")
        entry = ailetterboxd.Entry(
            status="unmatched",
            subject=Film("Spirited Away", "2001"),
            candidates=(Film("Sen to Chihiro no Kamikakushi", "2001", tmdb="129"),),
        )
        model = _Model('{"choice": 1, "tmdb": 129, "confidence": "high", "reason": "ok"}')
        assert ailetterboxd.suggest_matches([entry], models=[model]) == []
        assert model.asked == []

    def test_no_model_available_returns_empty(self, monkeypatch):
        for name in (llmclient.API_VARIABLE, llmclient.URL_VARIABLE,
                     llmclient.HARNESS_VARIABLE):
            monkeypatch.delenv(name, raising=False)
        entry = ailetterboxd.Entry(
            status="unmatched",
            subject=Film("Spirited Away", "2001"),
            candidates=(Film("Sen to Chihiro no Kamikakushi", "2001", tmdb="129"),),
        )
        assert ailetterboxd.suggest_matches([entry]) == []

    def test_records_modelmeter_and_uses_search_when_available(self, tmp_path):
        entry = ailetterboxd.Entry(
            status="unmatched",
            subject=Film("Spirited Away", "2001"),
            candidates=(Film("Sen to Chihiro no Kamikakushi", "2001", tmdb="129"),),
        )
        model = _Model('{"choice": 1, "tmdb": 129, "confidence": "high", '
                       '"reason": "Original Japanese title."}', can_search=True)
        meter = tmp_path / "meter"
        suggestions = ailetterboxd.suggest_matches(
            [entry], models=[model], meter_file=str(meter))
        assert len(suggestions) == 1
        assert suggestions[0].choice == entry.candidates[0]
        assert model.used_search is True
        assert model.purpose == ailetterboxd.PURPOSE
        _spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 35, 400)

    def test_model_without_web_search_still_reasons_directly(self):
        entry = ailetterboxd.Entry(
            status="unmatched",
            subject=Film("Birdman", "2014"),
            candidates=(Film("Birdman or (The Unexpected Virtue of Ignorance)", "2014"),),
        )
        model = _Model('{"choice": 1, "tmdb": null, "confidence": "high", '
                       '"reason": "Same film with full subtitle."}', can_search=False)
        suggestions = ailetterboxd.suggest_matches([entry], models=[model])
        assert len(suggestions) == 1
        assert suggestions[0].choice == entry.candidates[0]
        assert model.used_search is False
        assert len(model.asked) == 1

    def test_llm_error_falls_back_to_next_model_or_reports_error(self, tmp_path):
        entry = ailetterboxd.Entry(
            status="unmatched",
            subject=Film("Spirited Away", "2001"),
            candidates=(Film("Sen to Chihiro no Kamikakushi", "2001", tmdb="129"),),
        )
        broken = _Model(llmclient.LlmError("server down"), name="first")
        backup = _Model('{"choice": 1, "tmdb": 129, "confidence": "high", '
                        '"reason": "Original title."}', name="second")
        suggestions = ailetterboxd.suggest_matches([entry], models=[broken, backup])
        assert suggestions[0].choice == entry.candidates[0]
        assert suggestions[0].model == "second"

        meter = tmp_path / "meter"
        failed = ailetterboxd.suggest_matches(
            [entry], models=[_Model(llmclient.LlmError("timeout"), name="only")],
            meter_file=str(meter))
        assert len(failed) == 1
        assert "timeout" in failed[0].rejected
        assert failed[0].choice is None
        assert modelmeter.gathered(str(meter))[1].requests == 0


# --- report formatting & report-only guarantee in full sync ------------------

class _Server:
    def __init__(self, *movies: Movie):
        self._movies = list(movies)
        self.marked: list[str] = []
        self.rated: list[tuple[str, int]] = []
        self.watchlist_added: list[str] = []

    def library(self, cache, whole=False):
        return Library(list(self._movies), {"movies": []}, cache is None or whole)

    def mark_played(self, movie: Movie):
        self.marked.append(movie.key)

    def rate(self, movie: Movie, rating: int):
        self.rated.append((movie.key, rating))

    def catalogue_key(self, film: Film):
        return ""

    def add_to_watchlist(self, key: str):
        self.watchlist_added.append(key)


def test_format_suggestions_covers_every_decision_kind():
    s_pick = ailetterboxd.Suggestion(
        entry=ailetterboxd.Entry(
            status="unmatched",
            subject=Film("Spirited Away", "2001"),
            candidates=(Film("Sen to Chihiro no Kamikakushi", "2001", tmdb="129"),),
        ),
        choice=Film("Sen to Chihiro no Kamikakushi", "2001", tmdb="129"),
        choice_index=1,
        tmdb="129",
        confidence="high",
        reason="Japanese original title.",
    )
    s_tmdb = ailetterboxd.Suggestion(
        entry=ailetterboxd.Entry(status="unmatched", subject=Film("Obscure", "1999")),
        tmdb="4242",
        confidence="medium",
        reason="Matched by year and title.",
    )
    s_none = ailetterboxd.Suggestion(
        entry=ailetterboxd.Entry(
            status="ambiguous",
            subject=Film("Harbour Tales", "1977"),
            candidates=(Film("The Harbour Tales", "1977"), Film("Harbour Tales, The", "1977")),
            direction="letterboxd",
        ),
        confidence="low",
        reason="Not enough detail to distinguish.",
    )
    s_bad = ailetterboxd.Suggestion(
        entry=ailetterboxd.Entry(status="unmatched", subject=Film("Broken", "2005")),
        rejected="invented candidate index 9 (no candidates offered)",
    )
    lines = ailetterboxd.format_suggestions([s_pick, s_tmdb, s_none, s_bad])
    assert "report only - nothing changed in Plex" in lines[0]
    assert "4 entry(s) reviewed" in lines[0]
    assert "Plex: Sen to Chihiro no Kamikakushi (2001) [tmdb:129]" in lines[1]
    assert "(high confidence)" in lines[1]
    assert "suggested tmdb:4242" in lines[2]
    assert "-> no match" in lines[3]
    assert "rejected: invented candidate index 9" in lines[4]


def test_sync_reports_model_suggestions_without_ever_mutating_plex_or_state(
        monkeypatch, tmp_path, capsys):
    # Export has Spirited Away (2001) rated 4.5 stars, which does NOT match
    # Plex's Japanese-titled movie deterministically.
    export = tmp_path / "export.zip"
    with zipfile.ZipFile(export, "w") as archive:
        archive.writestr(
            "watched.csv",
            "Date,Name,Year,Letterboxd URI\n"
            "2024-01-02,Spirited Away,2001,https://boxd.it/sa\n",
        )
        archive.writestr(
            "ratings.csv",
            "Date,Name,Year,Letterboxd URI,Rating\n"
            "2024-01-02,Spirited Away,2001,https://boxd.it/sa,4.5\n",
        )

    server = _Server(
        _movie("42", "Sen to Chihiro no Kamikakushi", "2001", played=False, tmdb="129"),
    )
    model = _Model(
        '{"choice": 1, "tmdb": 129, "confidence": "high", '
        '"reason": "Sen to Chihiro no Kamikakushi is Spirited Away."}',
        name="stub-reasoner",
    )
    monkeypatch.setattr(sync.plexserver, "from_environment", lambda _account: server)
    monkeypatch.setattr(sync.tooldeps, "require_tools", lambda *a, **k: 0)
    monkeypatch.delenv("tmdbApiKey", raising=False)
    monkeypatch.setattr(
        ailetterboxd.llmroster, "optional", lambda task: ([model], ""))

    state_path = tmp_path / "state.json"
    out_path = tmp_path / "import.csv"
    code = sync.main(
        [str(export), "-S", str(state_path), "-o", str(out_path)],
        script_dir=str(tmp_path),
    )
    assert code == 0

    # CRITICAL GUARD: Plex was NEVER marked, rated, or added to watchlist.
    assert server.marked == []
    assert server.rated == []
    assert server.watchlist_added == []

    # State keeps Spirited Away with NO model-injected TMDb id.
    saved = letterboxd.read_state(str(state_path))
    assert len(saved.films) == 1
    assert saved.films[0].title == "Spirited Away"
    assert saved.films[0].tmdb == ""

    # Suggestion and modelmeter rate line appear in the run output.
    output = capsys.readouterr().err
    assert "Model match suggestions (report only - nothing changed in Plex)" in output
    assert (
        "Spirited Away (2001) [unmatched] -> "
        "Plex: Sen to Chihiro no Kamikakushi (2001) [tmdb:129]"
    ) in output
    assert "(high confidence)" in output
    assert "letterboxd:" in output
