"""Tests for medialib.lib.letterboxd - Letterboxd's export, feed and import
formats, and the state file that remembers what is on it and what was sent."""

import csv
import json
import zipfile

import pytest

from medialib.lib import letterboxd
from medialib.lib.filmmatch import Film
from medialib.lib.letterboxd import Row, State

pytestmark = pytest.mark.fs


def _export(path, members: dict):
    with zipfile.ZipFile(path, "w") as handle:
        for name, text in members.items():
            handle.writestr(zipfile.ZipInfo(name, (2026, 10, 13, 21, 16, 0)), text)
    return str(path)


_DIARY = "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"


def test_the_export_puts_watched_diary_and_ratings_together(tmp_path):
    path = _export(tmp_path / "export.zip", {
        "watched.csv": "Date,Name,Year,Letterboxd URI\n"
                       "2020-07-08,Sélène,2001,https://boxd.it/a\n"
                       "2021-01-01,\"Moss, Rooms and Shelves\",2000,https://boxd.it/b\n",
        "diary.csv": _DIARY + "2024-01-02,Sélène,2001,https://boxd.it/a,4,,,2024-01-01\n",
        "reviews.csv": "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,"
                       "Watched Date\n"
                       "2024-03-02,Sélène,2001,https://boxd.it/a,4,Yes,Fine.,,2024-03-01\n",
        "ratings.csv": "Date,Name,Year,Letterboxd URI,Rating\n"
                       "2024-01-02,Sélène,2001,https://boxd.it/a,3.5\n",
        "deleted/diary.csv": _DIARY + "2024-01-02,Gone,1990,https://boxd.it/z,,,,2024-01-01\n",
        "watchlist.csv": "Date,Name,Year,Letterboxd URI\n2024-05-05,Sandfall,2021,"
                         "https://boxd.it/w\n",
    })
    export = letterboxd.read_export(path)
    assert export.made == "2026-10-13"
    assert export.films == [
        Film("Sélène", "2001", rating=7, dates=("2024-01-01", "2024-03-01"),
             logged="2020-07-08"),
        Film("Moss, Rooms and Shelves", "2000", logged="2021-01-01")]
    assert export.watchlist == [Film("Sandfall", "2021")]


def test_an_export_without_watched_csv_is_refused(tmp_path):
    with pytest.raises(ValueError):
        letterboxd.read_export(_export(tmp_path / "other.zip", {"profile.csv": "x\n"}))


def test_the_feed_names_films_with_their_day_and_rating():
    body = b"""<?xml version="1.0"?>
<rss xmlns:letterboxd="https://letterboxd.com" xmlns:tmdb="https://themoviedb.org">
<channel>
<item><letterboxd:filmTitle>Lantern</letterboxd:filmTitle>
<letterboxd:filmYear>1988</letterboxd:filmYear>
<letterboxd:watchedDate>2026-10-01</letterboxd:watchedDate>
<letterboxd:memberRating>4.5</letterboxd:memberRating>
<tmdb:movieId>149</tmdb:movieId></item>
<item><title>A list</title></item>
</channel></rss>"""
    assert letterboxd.parse_feed(body) == [
        Film("Lantern", "1988", tmdb="149", rating=9, dates=("2026-10-01",))]


def test_a_merge_adds_days_and_ids_to_a_known_film_and_keeps_the_rest():
    known = [Film("Twelve Lanterns", "1995", rating=6, dates=("2020-01-01",),
                  logged="2019-05-05"), Film("Hollow Night", "2018")]
    merged = letterboxd.merge(known, [
        Film("12 Lanterns", "1995", tmdb="7", rating=8, dates=("2026-10-01",)),
        Film("12 Lanterns", "1995", dates=("2026-10-02",)),
        Film("Sandfall", "2021")])
    assert merged == [
        Film("Twelve Lanterns", "1995", tmdb="7", rating=8,
             dates=("2020-01-01", "2026-10-01", "2026-10-02"), logged="2019-05-05"),
        Film("Hollow Night", "2018"), Film("Sandfall", "2021")]


def test_the_import_csv_has_a_column_for_each_thing_a_row_says(tmp_path):
    path = tmp_path / "import.csv"
    letterboxd.write_import(str(path), [
        Row(Film("Lantern", "1988", tmdb="149", imdb="tt0094625"), "2026-10-01",
            rating=7, rewatch=True),
        Row(Film("Hollow Night", "2018"))])
    assert list(csv.reader(path.open())) == [
        ["tmdbID", "imdbID", "Title", "Year", "WatchedDate", "Rating", "Rewatch"],
        ["149", "tt0094625", "Lantern", "1988", "2026-10-01", "3.5", "true"],
        ["", "", "Hollow Night", "2018", "", "", ""]]


def test_the_state_round_trips(tmp_path):
    path = tmp_path / "data" / "state.json"
    state = State([Film("Sélène", "2001", rating=7, dates=("2024-01-01",),
                        logged="2020-07-08")],
                  [Row(Film("Lantern", "1988", tmdb="149"), "2026-10-01", 7, True,
                       "2026-10-14")],
                  ["Not Named (1990)"], [Film("Sandfall", "2021")],
                  [Row(Film("Hollow Night", "2018"), sent="2026-10-14")])
    letterboxd.write_state(str(path), state)
    assert letterboxd.read_state(str(path)) == state


def test_a_file_that_is_not_a_state_is_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(["39 lanterns|1935"]))
    with pytest.raises(ValueError):
        letterboxd.read_state(str(path))


def test_no_state_yet_is_nothing_known(tmp_path):
    assert letterboxd.read_state(str(tmp_path / "missing.json")) == State([], [], [], [], [])


def test_the_watchlist_csv_names_each_film_once_per_row(tmp_path):
    path = tmp_path / "watchlist.csv"
    letterboxd.write_watchlist(str(path), [Film("Lantern", "2007", tmdb="3")])
    assert list(csv.reader(path.open())) == [["tmdbID", "imdbID", "Title", "Year"],
                                             ["3", "", "Lantern", "2007"]]


def test_an_export_s_films_take_the_ids_known_for_them():
    assert letterboxd.with_ids_of(
        [Film("Lantern", "2007"), Film("Sandfall", "2021")],
        [Film("Lantern", "2007", tmdb="3"), Film("Sandfall", "2020", tmdb="9")]) == [
        Film("Lantern", "2007", tmdb="3"), Film("Sandfall", "2021")]
