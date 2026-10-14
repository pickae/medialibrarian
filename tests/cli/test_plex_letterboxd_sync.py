"""Tests for plex-letterboxd-sync: what a run plans in each direction, what it
does about rows it sent before, and a whole run against a stand-in server."""

import csv

import pytest

from medialib.cli import plex_letterboxd_sync as sync
from medialib.lib import letterboxd
from medialib.lib.filmmatch import Film
from medialib.lib.letterboxd import Row
from medialib.lib.plexserver import Library, Movie, Wanted

pytestmark = pytest.mark.pure


def _movie(key, title, year, played=False, rating=0, dates=(), section="Films"):
    return Movie(key, Film(title, year, rating=rating, dates=tuple(dates)),
                 played, section)


# --- Letterboxd -> Plex -------------------------------------------------------

def test_a_film_written_differently_is_marked_played():
    plan = sync.plan([_movie("1", "12 Lanterns", "1995"),
                      _movie("2", "Copper Lamp", "1992")],
                     [Film("Twelve Lanterns", "1995")], [])
    assert [movie.key for movie, _found in plan.mark] == ["1"]
    assert plan.rows == []


def test_a_film_a_year_apart_is_not_the_film_when_plex_holds_the_film_itself():
    plan = sync.plan([_movie("1", "Hollow", "2020"),
                      _movie("2", "HOLLOW", "2021", played=True)],
                     [Film("HOLLOW", "2021")], [])
    assert plan.mark == []


def test_every_copy_of_a_film_is_marked():
    plan = sync.plan([_movie("1", "Lantern", "2007", section="Films"),
                      _movie("2", "Lantern", "2007", section="Kids")],
                     [Film("Lantern", "2007")], [])
    assert sorted(movie.key for movie, _found in plan.mark) == ["1", "2"]


def test_a_letterboxd_rating_fills_an_unrated_plex_film():
    plan = sync.plan([_movie("1", "Lantern", "2007", played=True)],
                     [Film("Lantern", "2007", rating=7)], [])
    assert [(movie.key, rating) for movie, rating in plan.rate] == [("1", 7)]


def test_an_ambiguous_film_is_left_unmarked():
    plan = sync.plan([_movie("1", "Harbour Tales", "1977")],
                     [Film("The Harbour Tales", "1977"),
                      Film("Harbour Tales, The", "1977")], [])
    assert plan.mark == [] and len(plan.ambiguous) == 1


# --- Plex -> Letterboxd -------------------------------------------------------

def test_every_play_letterboxd_lacks_is_a_diary_row():
    plan = sync.plan([_movie("1", "Lantern", "2007", played=True,
                             dates=["2023-01-01", "2024-02-02"])],
                     [Film("Lantern", "2007", dates=("2023-01-01",),
                           logged="2023-01-01")], [])
    assert plan.rows == [Row(Film("Lantern", "2007"), "2024-02-02", rewatch=True)]


def test_the_first_play_of_a_film_new_to_letterboxd_is_no_rewatch():
    plan = sync.plan([_movie("1", "Lantern", "2007", played=True,
                             dates=["2023-01-01", "2024-02-02"])], [], [])
    assert [(row.date, row.rewatch) for row in plan.rows] == [
        ("2023-01-01", False), ("2024-02-02", True)]


def test_a_film_logged_before_the_play_makes_it_a_rewatch():
    plan = sync.plan([_movie("1", "Lantern", "2007", played=True,
                             dates=["2023-01-01"])],
                     [Film("Lantern", "2007", logged="2020-07-08")], [])
    assert [(row.date, row.rewatch) for row in plan.rows] == [("2023-01-01", True)]


def test_a_film_played_with_no_play_on_record_goes_undated():
    plan = sync.plan([_movie("1", "Lantern", "2007", played=True)], [], [])
    assert plan.rows == [Row(Film("Lantern", "2007"))]


def test_a_plex_rating_rides_on_the_last_new_row_or_alone():
    dated = sync.plan([_movie("1", "Lantern", "2007", played=True, rating=8,
                              dates=["2023-01-01", "2024-02-02"])], [], [])
    assert [row.rating for row in dated.rows] == [0, 8]
    alone = sync.plan([_movie("1", "Lantern", "2007", played=True, rating=8)],
                      [Film("Lantern", "2007")], [])
    assert alone.rows == [Row(Film("Lantern", "2007"), rating=8)]


def test_two_ratings_that_disagree_change_neither():
    plan = sync.plan([_movie("1", "Lantern", "2007", played=True, rating=8)],
                     [Film("Lantern", "2007", rating=4)], [])
    assert plan.rows == [] and plan.rate == [] and len(plan.disagree) == 1


def test_copies_of_one_film_are_one_film_to_letterboxd():
    plan = sync.plan([_movie("1", "Lantern", "2007", played=True,
                             dates=["2023-01-01"]),
                      _movie("2", "Lantern", "2007", played=True,
                             dates=["2023-01-01"], section="Kids")], [], [])
    assert len(plan.rows) == 1


def test_a_row_sent_and_waiting_is_not_sent_again():
    waiting = [Row(Film("Lantern", "2007"), "2023-01-01", sent="2026-10-14")]
    plan = sync.plan([_movie("1", "Lantern", "2007", played=True,
                             dates=["2023-01-01"])], [], waiting)
    assert plan.rows == []


def test_only_the_marking_with_to_plex_only():
    plan = sync.plan([_movie("1", "Lantern", "2007", played=True,
                             dates=["2023-01-01"])], [], [], to_plex_only=True)
    assert plan.rows == []


# --- rows sent before ---------------------------------------------------------

def test_a_row_letterboxd_now_has_is_done_with():
    row = Row(Film("Lantern", "2007"), "2023-01-01", sent="2026-10-01")
    assert sync.settle_pending(
        [row], [Film("Lantern", "2007", dates=("2023-01-01",))], "") == ([], [])


def test_a_row_a_later_export_lacks_is_offered_again():
    row = Row(Film("Lantern", "2007"), "2023-01-01", sent="2026-10-01")
    assert sync.settle_pending([row], [], "2026-10-05") == ([], [row])


def test_a_row_the_feed_does_not_show_keeps_waiting():
    row = Row(Film("Lantern", "2007"), "2023-01-01", sent="2026-10-01")
    assert sync.settle_pending([row], [], "") == ([row], [])
    # An export from the day it was sent may have been made before the upload.
    assert sync.settle_pending([row], [], "2026-10-01") == ([row], [])


# --- a whole run --------------------------------------------------------------

class _Server:
    def __init__(self, *movies):
        self._movies = list(movies)
        self.marked, self.rated = [], []

    def asked_for(self, account):
        self.account = account
        return self

    def library(self, cache, whole=False):
        self.asked_whole = whole
        return Library(list(self._movies), {"movies": []}, cache is None or whole)

    def mark_played(self, movie):
        self.marked.append(movie.key)

    def rate(self, movie, rating):
        self.rated.append((movie.key, rating))


def _export(path):
    import zipfile
    with zipfile.ZipFile(path, "w") as handle:
        handle.writestr("watched.csv", "Date,Name,Year,Letterboxd URI\n"
                        "2020-07-08,Twelve Lanterns,1995,https://boxd.it/a\n")
        handle.writestr("ratings.csv", "Date,Name,Year,Letterboxd URI,Rating\n"
                        "2020-07-08,Twelve Lanterns,1995,https://boxd.it/a,3\n")
    return str(path)


@pytest.fixture
def run(monkeypatch, tmp_path):
    server = _Server(_movie("1", "12 Lanterns", "1995"),
                     _movie("2", "Hollow Night", "2018", played=True,
                            dates=["2023-01-01"]))
    monkeypatch.setattr(sync.plexserver, "from_environment",
                        lambda account: server.asked_for(account))
    monkeypatch.setattr(sync.tooldeps, "require_tools", lambda *a, **k: 0)
    monkeypatch.delenv("tmdbApiKey", raising=False)
    state, out = tmp_path / "state.json", tmp_path / "import.csv"

    def go(*argv):
        code = sync.main([*argv, "-k", str(state), "-o", str(out)],
                         script_dir=str(tmp_path))
        return code, server, state, out
    return go


def test_a_run_marks_rates_and_writes_and_a_second_sends_nothing_twice(run, tmp_path):
    export = _export(tmp_path / "export.zip")
    code, server, state, out = run(export)
    assert code == 0
    assert server.marked == ["1"] and server.rated == [("1", 6)]
    assert list(csv.reader(out.open()))[1][2:5] == ["Hollow Night", "2018", "2023-01-01"]
    assert len(letterboxd.read_state(str(state)).pending) == 1

    out.unlink()
    code, *_rest = run(export)
    assert code == 0 and not out.exists()


def test_a_preview_changes_nothing(run, tmp_path):
    code, server, state, out = run("-p", _export(tmp_path / "export.zip"))
    assert code == 0
    assert server.marked == [] and server.rated == []
    assert not state.exists() and not out.exists()


def test_the_feed_alone_is_refused_on_a_first_run(run):
    code, server, state, _out = run("someaccount")
    assert code == 1 and server.marked == [] and not state.exists()


def test_the_plex_library_is_kept_beside_the_state_and_f_reads_it_whole(run, tmp_path):
    export = _export(tmp_path / "export.zip")
    _code, server, state, _out = run(export)
    assert (tmp_path / "state-plex.json").is_file()
    run("-f", export)
    assert server.asked_whole


# --- ids from TMDb ------------------------------------------------------------

class _Match:
    def __init__(self, tmdb=""):
        self.tmdb, self.imdb, self.tag = tmdb, "", "{tmdb-%s}" % tmdb if tmdb else ""


def test_tmdb_names_only_what_titles_do_not_settle(monkeypatch):
    monkeypatch.setenv("tmdbApiKey", "k")
    asked = []

    def identify(title, year):
        asked.append(title)
        return _Match("671" if title == "The Stone of Wisdom" else "")

    plex = [_movie("1", "The Stone of Sorcery", "2001")._replace(
                film=Film("The Stone of Sorcery", "2001", tmdb="671")),
            _movie("2", "Lantern", "2007")]
    films, unnamed = sync.name_films(
        [Film("The Stone of Wisdom", "2001"), Film("Lantern", "2007"),
         Film("Not in Plex", "1990"), Film("Asked Before", "1990")],
        plex, ["Asked Before (1990)"], identify)
    assert asked == ["The Stone of Wisdom", "Not in Plex"]
    assert films[0].tmdb == "671"
    assert unnamed == ["Asked Before (1990)", "Not in Plex (1990)"]
    # With the id, the film Plex holds under another title is found.
    assert sorted(m.key for m, _f in sync.plan(plex, films, []).mark) == ["1", "2"]


def test_no_key_no_lookups(monkeypatch):
    monkeypatch.delenv("tmdbApiKey", raising=False)
    films = [Film("Not in Plex", "1990")]
    assert sync.name_films(films, [], [], lambda *a: 1 / 0) == (films, [])


# --- the watchlists -----------------------------------------------------------

def test_plex_s_watchlist_goes_to_letterboxd_less_what_it_has_or_watched():
    wishes = sync.plan_watchlist(
        [Wanted("c1", Film("Lantern", "2007", tmdb="3")),
         Wanted("c2", Film("Sandfall", "2021")),
         Wanted("c3", Film("Hollow Night", "2018")),
         Wanted("c4", Film("Copper Lamp", "1992"))],
        [Film("Sandfall", "2021")], [Film("Hollow Night", "2018")], [],
        [Row(Film("Copper Lamp", "1992"), sent="2026-10-14")])
    assert wishes.to_letterboxd == [Film("Lantern", "2007", tmdb="3")]


def test_letterboxd_s_watchlist_goes_to_plex_less_what_it_has_or_played():
    wishes = sync.plan_watchlist(
        [Wanted("c1", Film("Lantern", "2007"))],
        [Film("Lantern", "2007"), Film("Sandfall", "2021"), Film("Hollow Night", "2018")],
        [], [_movie("1", "Hollow Night", "2018", played=True)], [])
    assert wishes.to_plex == [Film("Sandfall", "2021")]


def test_only_plex_s_side_with_to_plex_only():
    wishes = sync.plan_watchlist([Wanted("c1", Film("Lantern", "2007"))], [], [], [],
                                 [], to_plex_only=True)
    assert wishes.to_letterboxd == []


def test_a_watchlist_film_sent_is_done_with_once_listed_or_watched():
    row = Row(Film("Lantern", "2007"), sent="2026-10-01")
    assert sync.settle_wanted([row], [Film("Lantern", "2007")], [], "") == ([], [])
    assert sync.settle_wanted([row], [], [Film("Lantern", "2007")], "") == ([], [])
    assert sync.settle_wanted([row], [], [], "") == ([row], [])
    assert sync.settle_wanted([row], [], [], "2026-10-05") == ([], [row])


def test_the_owner_s_plays_unless_another_account_is_named(run, tmp_path):
    export = _export(tmp_path / "export.zip")
    _code, server, _state, _out = run("-p", export)
    assert server.account == "1"
    run("-p", "-a", "3", export)
    assert server.account == "3"
