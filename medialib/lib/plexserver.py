"""A Plex Media Server's movie libraries, read, marked watched and rated over
its own HTTP API.

Five calls are all it takes, so they are made with curl rather than through a
client library: the list of libraries, every film in one of them with its ids
and rating, the play history, the "scrobble" that marks one film played, and
the call that rates one. The server and its token come
from ``plexUrl`` and ``plexToken``; the token goes to curl on stdin, in a
header, so it is in neither the process table nor a URL a log might keep.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import NamedTuple

from medialib.lib import tmdblookup
from medialib.lib.filmmatch import Film

__all__ = ["PlexError", "Server", "Movie", "Library", "Wanted", "from_environment"]

# The identifier a scrobble or a rating names the library by - the one every
# Plex server answers to for its own libraries.
_LIBRARY = "com.plexapp.plugins.library"

# The server's own number for the account that owns it, which is the account
# a server's token normally belongs to - and whose plays are read, unless the
# server is told another. On a shared server, somebody else's plays are not
# this account's watching.
OWNER = "1"

# How long a library read once is trusted before it is read whole again. The
# filters a read since the last one asks with see a film added, changed,
# played or rated - and not a film removed, nor one marked unplayed, which only
# a whole read finds.
FULL_EVERY = 7 * 24 * 3600

# How far before the last read the next one reaches back, so a change made
# while the last one was running is not missed. Reading a change twice is
# harmless: a film read again replaces itself, and a day played is a set.
_OVERLAP = 3600

# The account's watchlist is not on the server: it is the account's, kept by
# plex.tv, and the films on it are named by Plex's own catalogue - which is
# also where a film not in any library is found by its TMDb id.
_DISCOVER = "https://discover.provider.plex.tv"
_METADATA = "https://metadata.provider.plex.tv"

# How many watchlist entries one request asks for.
_PAGE = 200

# The filters a film changed since a moment is asked for by - one request
# each, since Plex joins its filters with "and".
_CHANGED = ("addedAt", "updatedAt", "lastViewedAt", "lastRatedAt")


class PlexError(Exception):
    """The server could not be asked, or gave an answer that is not one."""


class Movie(NamedTuple):
    """One film in a Plex library: the server's key for it, and the film -
    with its rating, and the days it was played once :meth:`Server.movies`
    has read the history."""

    key: str
    film: Film
    played: bool
    section: str


class Wanted(NamedTuple):
    """A film on the account's watchlist: Plex's catalogue id, and the film."""

    key: str
    film: Film


class Server:
    def __init__(self, url: str, token: str, account: str = OWNER) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.account = account

    def _ask(self, path: str, params=(), method: str = "GET") -> ET.Element:
        """``path`` on the server - or, where it is a whole address, there."""
        address = path if path.startswith("https://") else self.url + path
        lines = ["url = " + tmdblookup._config_value(address), "get",
                 "request = " + tmdblookup._config_value(method),
                 "fail", "silent", "show-error", "max-time = 120",
                 "header = " + tmdblookup._config_value(
                     "X-Plex-Token: " + self.token),
                 'header = "Accept: application/xml"']
        for key, value in params:
            lines.append("data-urlencode = "
                         + tmdblookup._config_value("%s=%s" % (key, value)))
        proc = subprocess.run(["curl", "--config", "-"],
                              input="\n".join(lines).encode("utf-8"),
                              capture_output=True)
        if proc.returncode != 0:
            raise PlexError("%s%s: %s" % (self.url, path, proc.stderr.decode(
                "utf-8", "replace").strip() or "curl exited %d"
                % proc.returncode))
        try:
            return ET.fromstring(proc.stdout) if proc.stdout.strip() \
                else ET.Element("MediaContainer")
        except ET.ParseError as error:
            raise PlexError("%s%s: not an answer from Plex (%s)"
                            % (self.url, path, error)) from error

    def movie_sections(self) -> list[tuple[str, str]]:
        """Every movie library on the server, as (key, title)."""
        return [(directory.get("key", ""), directory.get("title", ""))
                for directory in self._ask("/library/sections").iter("Directory")
                if directory.get("type") == "movie"]

    def library(self, cache: dict | None, whole: bool = False) -> Library:
        """Every film in the movie libraries, read from ``cache`` - what the
        last run kept - and what changed on the server since; or read whole,
        where ``whole`` asks for it, where there is no cache, where it is from
        another server or other libraries, or where it is older than
        :data:`FULL_EVERY`."""
        now = int(time.time())
        sections = self.movie_sections()
        if whole or not cache or cache.get("server") != self.url \
                or [list(pair) for pair in sections] != cache.get("sections") \
                or now - int(cache.get("full", 0)) > FULL_EVERY:
            movies = self.movies()
            wanted = self.watchlist()
            return Library(movies, _cache(self.url, sections, now, now, movies, wanted),
                           True, wanted)

        since = str(int(cache["read"]) - _OVERLAP)
        by_key = {movie.key: movie for movie in map(_unpacked, cache["movies"])}
        for key, title in sections:
            for field in _CHANGED:
                for video in self._ask("/library/sections/%s/all" % key,
                                       (("type", "1"), ("includeGuids", "1"),
                                        (field + ">>", since))).iter("Video"):
                    movie = _movie(video, title)
                    known = by_key.get(movie.key)
                    by_key[movie.key] = movie._replace(film=movie.film._replace(
                        dates=known.film.dates)) if known else movie
            for rating_key, days in self._plays(key, since).items():
                played = by_key.get(rating_key)
                if played is not None:
                    by_key[rating_key] = played._replace(film=played.film._replace(
                        dates=tuple(sorted(set(played.film.dates) | days))))
        movies = list(by_key.values())
        wanted = self.watchlist([Wanted(entry["key"], _unpacked_film(entry))
                                 for entry in cache.get("watchlist", ())])
        return Library(movies, _cache(self.url, sections, int(cache["full"]), now,
                                      movies, wanted), False, wanted)

    def movies(self, sections=()) -> list[Movie]:
        """Every film in the movie libraries - only the ones titled in
        ``sections``, where it names any - each with the days it was played."""
        found: list[Movie] = []
        for key, title in self.movie_sections():
            if sections and title not in sections:
                continue
            container = self._ask("/library/sections/%s/all" % key,
                                  (("type", "1"), ("includeGuids", "1")))
            plays = self._plays(key)
            for video in container.iter("Video"):
                movie = _movie(video, title)
                days = plays.get(movie.key)
                found.append(movie._replace(film=movie.film._replace(
                    dates=tuple(sorted(days)))) if days else movie)
        return found

    def _plays(self, section: str, since: str = "") -> dict[str, set]:
        """The days each film in a library was played to the end, by its key
        - only since the moment ``since`` names, where it names one.

        From the play history and not from a film's "last viewed": marking a
        film played - which is what this module's own :meth:`mark_played`
        does - sets that to the moment it was marked, and leaves no play in
        the history. So what the history holds is when it was watched.
        """
        days: dict[str, set] = {}
        asking = [("librarySectionID", section), ("accountID", self.account)]
        if since:
            asking.append(("viewedAt>", since))
        history = self._ask("/status/sessions/history/all", asking)
        for video in history:
            viewed = video.get("viewedAt", "")
            if video.get("type") == "movie" and viewed.isdigit() \
                    and video.get("accountID", self.account) == self.account:
                days.setdefault(video.get("ratingKey", ""), set()).add(
                    _day(viewed))
        return days

    def watchlist(self, known: list | None = None) -> list[Wanted]:
        """The films on the account's watchlist - its shows left out.

        With ``known`` - the watchlist as a run read it before - only what was
        added since is read: newest first, up to the first film already
        known. A film taken off the list since is not seen that way, and is
        left for the next whole read.
        """
        known_keys = {entry.key for entry in known or ()}
        wanted: list[Wanted] = []
        start = 0
        while True:
            page = self._ask(_DISCOVER + "/library/sections/watchlist/all",
                             (("includeGuids", "1"), ("sort", "watchlistedAt:desc"),
                              ("X-Plex-Container-Start", str(start)),
                              ("X-Plex-Container-Size", str(_PAGE))))
            entries = [entry for entry in page if entry.tag in ("Video", "Directory")]
            for entry in entries:
                if entry.get("ratingKey", "") in known_keys:
                    return wanted + list(known or ())
                if entry.get("type") == "movie":
                    wanted.append(Wanted(entry.get("ratingKey", ""),
                                         _movie(entry, "").film))
            start += len(entries)
            if not entries or start >= int(page.get("totalSize", start) or start):
                return wanted + list(known or ()) if known else wanted

    def catalogue_key(self, film: Film) -> str:
        """Plex's catalogue id for a film, by its TMDb or IMDb id - "" where
        it has neither, or the catalogue does not know it."""
        for scheme, value in (("tmdb", film.tmdb), ("imdb", film.imdb)):
            if value:
                found = [video.get("ratingKey", "") for video in
                         self._ask(_METADATA + "/library/metadata/matches",
                                   (("type", "1"), ("guid", "%s://%s" % (scheme, value))))
                         if video.tag == "Video"]
                if len(found) == 1:
                    return found[0]
        return ""

    def add_to_watchlist(self, key: str) -> None:
        self._ask(_DISCOVER + "/actions/addToWatchlist", (("ratingKey", key),),
                  method="PUT")

    def mark_played(self, movie: Movie) -> None:
        self._ask("/:/scrobble", (("identifier", _LIBRARY), ("key", movie.key)))

    def rate(self, movie: Movie, rating: int) -> None:
        """Give a film a rating out of 10 - Plex's five stars, halves
        included."""
        self._ask("/:/rate", (("identifier", _LIBRARY), ("key", movie.key),
                              ("rating", str(rating))), method="PUT")


class Library(NamedTuple):
    """A library read: its films, what to keep for the next read, and
    whether it was read whole."""

    movies: list
    cache: dict
    whole: bool
    watchlist: list = []


def _cache(url: str, sections: list, full: int, read: int, movies: list,
           wanted: list) -> dict:
    return {"server": url, "sections": [list(pair) for pair in sections],
            "full": full, "read": read, "movies": [_packed(movie) for movie in movies],
            "watchlist": [dict(_packed_film(entry.film), key=entry.key)
                          for entry in wanted]}


def _packed_film(film: Film) -> dict:
    return {"title": film.title, "year": film.year, "tmdb": film.tmdb,
            "imdb": film.imdb, "aliases": list(film.aliases),
            "rating": film.rating, "dates": list(film.dates)}


def _unpacked_film(entry: dict) -> Film:
    return Film(entry["title"], entry["year"], entry["tmdb"], entry["imdb"],
                tuple(entry["aliases"]), rating=entry["rating"],
                dates=tuple(entry["dates"]))


def _packed(movie: Movie) -> dict:
    return dict(_packed_film(movie.film), key=movie.key, section=movie.section,
                played=movie.played)


def _unpacked(entry: dict) -> Movie:
    return Movie(entry["key"], _unpacked_film(entry), entry["played"],
                 entry["section"])


def updated(cache: dict, movie: Movie) -> None:
    """Keep in ``cache`` what this run changed about a film on the server."""
    for at, entry in enumerate(cache["movies"]):
        if entry["key"] == movie.key:
            cache["movies"][at] = _packed(movie)


def _day(epoch: str) -> str:
    return datetime.datetime.fromtimestamp(int(epoch)).date().isoformat()


def _movie(video: ET.Element, section: str) -> Movie:
    ids: dict[str, str] = {}
    for guid in video.iter("Guid"):
        scheme, _, value = (guid.get("id") or "").partition("://")
        ids.setdefault(scheme, value)
    original = video.get("originalTitle", "")
    try:
        rating = round(float(video.get("userRating", "")))
    except ValueError:
        rating = 0
    film = Film(video.get("title", ""), video.get("year", ""),
                tmdb=ids.get("tmdb", ""), imdb=ids.get("imdb", ""),
                aliases=(original,) if original else (), rating=rating)
    played = int(video.get("viewCount", "0") or 0) > 0
    return Movie(video.get("ratingKey", ""), film, played, section)


def from_environment(account: str = OWNER) -> Server | None:
    """The server ``plexUrl`` and ``plexToken`` name, read for ``account``'s
    plays - or None while either is unset."""
    url = os.environ.get("plexUrl", "")
    token = os.environ.get("plexToken", "")
    return Server(url, token, account) if url and token else None
