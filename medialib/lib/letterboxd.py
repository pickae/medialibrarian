"""What Letterboxd says an account has watched, when, and how it was rated -
and the file it imports more of that from.

Letterboxd has no public API to write to, and only two ways to read from:

* the data export (Settings > Data > Export), a zip of CSVs that between them
  hold every film the account has logged (``watched.csv``), every diary entry
  with the day it was watched (``diary.csv``, ``reviews.csv``) and every rating
  (``ratings.csv``) - by title and year, with no id at all. One film is one
  Letterboxd URI across all of them, which is how they are put together;
* the account's public RSS feed, which carries TMDb's id, but only the fifty
  or so most recent diary entries - and nothing of a film marked watched
  without one.

What goes back is a CSV in the format of Letterboxd's own importer
(https://letterboxd.com/import/), which the user uploads by hand: one row per
diary entry, or a row with no date for a film only marked watched or rated.

The feed's fifty entries are why there is a state file. It remembers two
things: everything known to be on Letterboxd, so a run that reads only the feed
still knows the rest; and every row written into an import CSV that Letterboxd
has not been seen to have yet, so a row is not offered twice while it waits to
be uploaded - and is offered again once a full export shows it never was.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import os
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from typing import NamedTuple

from medialib.lib.filmmatch import Film, FilmIndex

__all__ = ["Export", "Row", "State", "read_export", "read_feed", "parse_feed",
           "merge", "with_ids_of", "write_import", "write_watchlist", "read_state",
           "write_state"]


class Export(NamedTuple):
    """A data export: its films, the day it was made, and its watchlist."""

    films: list
    made: str
    watchlist: list = []


class Row(NamedTuple):
    """One line of an import CSV: a diary entry on ``date``, or with no date
    a film marked watched; with a ``rating`` out of 10, or 0 for none."""

    film: Film
    date: str = ""
    rating: int = 0
    rewatch: bool = False
    sent: str = ""          # the day it was written into a CSV, once it was


class State(NamedTuple):
    films: list
    pending: list           # Rows sent and not seen on Letterboxd yet
    # The films TMDb was asked about and could not name, as str(film), so a
    # run does not ask about them again.
    unnamed: list = []
    # The watchlist, as the last export had it; and the films sent to it in a
    # watchlist CSV and not seen on it yet, as Rows that carry no date.
    watchlist: list = []
    wanted: list = []


def _rating(stars: str) -> int:
    """Letterboxd's half stars as a rating out of 10, 0 for none."""
    try:
        return round(float(stars) * 2)
    except ValueError:
        return 0


def _member(archive: zipfile.ZipFile, name: str):
    """The archive's member called ``name`` - not the copy under ``deleted/``
    or ``orphaned/`` - or None."""
    for one in archive.namelist():
        if (one == name or one.endswith("/" + name)) \
                and not one.startswith(("deleted/", "orphaned/")):
            return one
    return None


def _rows(archive: zipfile.ZipFile, name: str) -> list:
    member = _member(archive, name)
    if member is None:
        return []
    with archive.open(member) as handle:
        return list(csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig")))


def read_export(path: str) -> Export:
    """Every film in a Letterboxd export, with its diary days and its rating.

    ValueError when the zip holds no ``watched.csv`` - an export of some other
    kind, or not an export at all.
    """
    with zipfile.ZipFile(path) as archive:
        watched = _member(archive, "watched.csv")
        if watched is None:
            raise ValueError("%s holds no watched.csv" % path)
        made = datetime.date(*archive.getinfo(watched).date_time[:3]).isoformat()
        films: dict[str, dict] = {}

        def film(row: dict) -> dict:
            key = row.get("Letterboxd URI") or "%s|%s" % (row["Name"], row.get("Year"))
            return films.setdefault(key, {"title": row["Name"],
                                          "year": (row.get("Year") or "").strip(),
                                          "dates": set(), "rating": 0, "logged": ""})

        for row in _rows(archive, "watched.csv"):
            if row.get("Name"):
                film(row)["logged"] = row.get("Date") or ""
        for name in ("diary.csv", "reviews.csv"):
            for row in _rows(archive, name):
                if row.get("Name") and row.get("Watched Date"):
                    film(row)["dates"].add(row["Watched Date"])
        for row in _rows(archive, "ratings.csv"):
            if row.get("Name"):
                film(row)["rating"] = _rating(row.get("Rating") or "")
        watchlist = [Film(row["Name"], (row.get("Year") or "").strip())
                     for row in _rows(archive, "watchlist.csv") if row.get("Name")]
    return Export([Film(one["title"], one["year"], rating=one["rating"],
                        dates=tuple(sorted(one["dates"])), logged=one["logged"])
                   for one in films.values()], made, watchlist)


def feed_url(user: str) -> str:
    return "https://letterboxd.com/%s/rss/" % user


def read_feed(user: str) -> list[Film]:
    """The films in an account's public RSS feed.

    OSError when the feed cannot be fetched - no such account, or no network.
    """
    proc = subprocess.run(["curl", "-fsSL", "--max-time", "30", feed_url(user)],
                          capture_output=True)
    if proc.returncode != 0:
        raise OSError(proc.stderr.decode("utf-8", "replace").strip()
                      or "curl exited %d" % proc.returncode)
    return parse_feed(proc.stdout)


def parse_feed(body: bytes) -> list[Film]:
    """The diary entries an RSS body lists, one film each; a list or a review
    that names no film is passed over."""
    films = []
    for item in ET.fromstring(body).iter("item"):
        # By local name, so a change of namespace URI does not lose the fields.
        fields = {element.tag.rsplit("}", 1)[-1]: (element.text or "").strip()
                  for element in item}
        if fields.get("filmTitle"):
            date = fields.get("watchedDate", "")
            films.append(Film(fields["filmTitle"], fields.get("filmYear", ""),
                              tmdb=fields.get("movieId", ""),
                              rating=_rating(fields.get("memberRating", "")),
                              dates=(date,) if date else ()))
    return films


def _combined(one: Film, other: Film) -> Film:
    """One film as both say it: the ids either has, every day either was
    watched, and ``other``'s rating where it has one - it is the newer."""
    return one._replace(
        tmdb=one.tmdb or other.tmdb, imdb=one.imdb or other.imdb,
        rating=other.rating or one.rating,
        dates=tuple(sorted(set(one.dates) | set(other.dates))),
        logged=min(filter(None, (one.logged, other.logged)), default=""))


def merge(known: list, new: list) -> list:
    """``known`` with what ``new`` adds to it: a film already known gains its
    days and its rating, and a film not known yet joins it.

    A film that might be either of two known ones is not merged into either,
    and is not added beside them as a third.
    """
    films = list(known)
    index, arriving = FilmIndex(films), FilmIndex(new)
    # The index keeps the films as they were, so where each one is stays
    # findable once an earlier merge has replaced it in the list.
    at = {id(film): position for position, film in enumerate(films)}
    for film in new:
        found = index.find_from(film, arriving)
        if found.matched:
            position = at[id(found.film)]
            films[position] = _combined(films[position], film)
        elif not found.ambiguous:
            films.append(film)
    return films


def with_ids_of(films: list, known: list) -> list:
    """``films`` - an export's, which carry no ids - with the ids ``known``
    has for the same title and year."""
    ids = {(film.title, film.year): (film.tmdb, film.imdb)
           for film in known if film.tmdb or film.imdb}
    return [film._replace(tmdb=ids[(film.title, film.year)][0],
                          imdb=ids[(film.title, film.year)][1])
            if not (film.tmdb or film.imdb) and (film.title, film.year) in ids
            else film for film in films]


def write_import(path: str, rows) -> None:
    """``rows`` as a CSV Letterboxd's importer takes."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["tmdbID", "imdbID", "Title", "Year", "WatchedDate",
                         "Rating", "Rewatch"])
        for row in rows:
            writer.writerow([row.film.tmdb, row.film.imdb, row.film.title,
                             row.film.year, row.date,
                             "%g" % (row.rating / 2) if row.rating else "",
                             "true" if row.rewatch else ""])


def write_watchlist(path: str, films) -> None:
    """``films`` as a CSV Letterboxd's watchlist import takes."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["tmdbID", "imdbID", "Title", "Year"])
        for film in films:
            writer.writerow([film.tmdb, film.imdb, film.title, film.year])


def _film_entry(film: Film) -> dict:
    return {"title": film.title, "year": film.year, "tmdb": film.tmdb,
            "imdb": film.imdb, "rating": film.rating, "dates": list(film.dates),
            "logged": film.logged}


def _film(entry: dict) -> Film:
    return Film(entry.get("title", ""), entry.get("year", ""),
                entry.get("tmdb", ""), entry.get("imdb", ""),
                rating=int(entry.get("rating", 0)),
                dates=tuple(entry.get("dates", ())), logged=entry.get("logged", ""))


def read_state(path: str) -> State:
    """What is remembered about Letterboxd; nothing where nothing is yet.

    ValueError for a file that is not a state this module wrote.
    """
    if not os.path.isfile(path):
        return State([], [], [], [], [])
    with open(path, encoding="utf-8") as handle:
        entries = json.load(handle)
    if not isinstance(entries, dict) or not isinstance(entries.get("films"), list):
        raise ValueError("%s is not a state this command wrote" % path)
    return State([_film(entry) for entry in entries["films"]],
                 [_row(entry) for entry in entries.get("pending", ())],
                 list(entries.get("unnamed", ())),
                 [_film(entry) for entry in entries.get("watchlist", ())],
                 [_row(entry) for entry in entries.get("wanted", ())])


def _row(entry: dict) -> Row:
    return Row(_film(entry["film"]), entry.get("date", ""),
               int(entry.get("rating", 0)), bool(entry.get("rewatch")),
               entry.get("sent", ""))


def _row_entry(row: Row) -> dict:
    return {"film": _film_entry(row.film), "date": row.date, "rating": row.rating,
            "rewatch": row.rewatch, "sent": row.sent}


def write_state(path: str, state: State) -> None:
    films = sorted(state.films, key=lambda film: (film.title.lower(), film.year))
    entries = {"films": [_film_entry(film) for film in films],
               "pending": [_row_entry(row) for row in state.pending],
               "unnamed": sorted(set(state.unnamed)),
               "watchlist": [_film_entry(film) for film in state.watchlist],
               "wanted": [_row_entry(row) for row in state.wanted]}
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
