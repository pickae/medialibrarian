"""A Jellyfin server's movie libraries, read, marked watched and rated over
its own HTTP API.

The same few calls :mod:`medialib.lib.plexserver` makes, with curl, as JSON:
the user's libraries, every film in one of them with its ids, versions and the
user's data for it, the call that marks one film played, and the one that
rates it. The server, its API key and the user come from ``jellyfinUrl``,
``jellyfinApiKey`` and ``jellyfinUser``; the key goes to curl on stdin, in a
header, so it is in neither the process table nor a URL a log might keep.

Jellyfin differs from Plex in what it keeps, and the client says so through
its provider's capabilities rather than pretending otherwise:

* there is no play history - only whether a film was played, and when last.
  Each read adds that last day to the days the cache already remembers, so a
  play seen once is not lost when the film is played again;
* a film can be marked played as of a given day, which Plex cannot;
* the rating is the user's own, kept by the server out of the 10 Letterboxd
  rates by, though Jellyfin's own player does not show it;
* there is no watchlist, and no catalogue of films the server does not hold.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time
import urllib.parse

from medialib.lib import mediaserver, tmdbclient
from medialib.lib.filmmatch import Film
from medialib.lib.mediaserver import Copy, Library, Movie, Wanted

__all__ = ["JellyfinError", "Server", "from_environment", "local", "NAME",
           "LOCAL"]

# The provider this client is, in :data:`mediaserver.PROVIDERS`.
NAME = "jellyfin"

# Where a Jellyfin server answers on the machine it runs on.
LOCAL = "http://localhost:8096"

# What a film is read with: its ids, its file and versions, and the name it
# was released under where that is another.
_FIELDS = "ProviderIds,Path,MediaSources,OriginalTitle"

# A user named by the id Jellyfin gives it rather than by its name.
_USER_ID = re.compile(r"^[0-9a-f]{32}$")


class JellyfinError(mediaserver.MediaServerError):
    """The server could not be asked, or gave an answer that is not one."""


class Server:
    def __init__(self, url: str, key: str, user: str = "") -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.user = user
        self._user_id = user if _USER_ID.match(user) else ""


    def _ask(self, path: str, params=(), method: str = "GET", body=None):
        """``path`` on the server, with ``params`` in its query, as the JSON
        it answers - None where it answers nothing."""
        address = self.url + path
        if params:
            address += "?" + urllib.parse.urlencode(list(params))
        lines = ["url = " + tmdbclient._config_value(address),
                 "request = " + tmdbclient._config_value(method),
                 "fail", "silent", "show-error", "max-time = 120",
                 'header = "Accept: application/json"']
        if self.key:
            lines.append("header = " + tmdbclient._config_value(
                'Authorization: MediaBrowser Token="%s"' % self.key))
        if body is not None:
            lines.append('header = "Content-Type: application/json"')
            lines.append("data-binary = " + tmdbclient._config_value(json.dumps(body)))
        proc = subprocess.run(["curl", "--config", "-"],
                              input="\n".join(lines).encode("utf-8"),
                              capture_output=True)
        if proc.returncode != 0:
            raise JellyfinError("%s%s: %s" % (self.url, path, proc.stderr.decode(
                "utf-8", "replace").strip() or "curl exited %d" % proc.returncode))
        if not proc.stdout.strip():
            return None
        try:
            return json.loads(proc.stdout)
        except ValueError as error:
            raise JellyfinError("%s%s: not an answer from Jellyfin (%s)"
                                % (self.url, path, error)) from error

    def user_id(self) -> str:
        """The id of the user whose plays and ratings are read: ``user`` by
        name, or the server's one user where none is named."""
        if self._user_id:
            return self._user_id
        users = self._ask("/Users") or []
        if self.user:
            found = [user for user in users
                     if str(user.get("Name", "")).casefold() == self.user.casefold()]
        else:
            found = users if len(users) == 1 else []
        if len(found) != 1:
            raise JellyfinError(
                "%s: no user named \"%s\" - jellyfinUser is one of: %s"
                % (self.url, self.user, ", ".join(str(user.get("Name", ""))
                                                   for user in users))
                if self.user else
                "%s: the server has %d users - set jellyfinUser to the one "
                "whose films these are" % (self.url, len(users)))
        self._user_id = str(found[0].get("Id", ""))
        return self._user_id

    def movie_sections(self) -> list[tuple[str, str]]:
        """Every movie library the user sees, as (id, name)."""
        answer = self._ask("/UserViews", (("userId", self.user_id()),)) or {}
        return [(str(view.get("Id", "")), str(view.get("Name", "")))
                for view in answer.get("Items", ())
                if view.get("CollectionType") == "movies"]

    def _items(self, section: str) -> list:
        answer = self._ask("/Items", (
            ("userId", self.user_id()), ("ParentId", section),
            ("Recursive", "true"), ("IncludeItemTypes", "Movie"),
            ("Fields", _FIELDS), ("EnableUserData", "true"))) or {}
        return list(answer.get("Items", ()))

    def movies(self, sections=()) -> list[Movie]:
        """Every film in the movie libraries - only the ones named in
        ``sections``, where it names any - each with the last day it was
        played."""
        found: list[Movie] = []
        for key, title in self.movie_sections():
            if sections and title not in sections:
                continue
            found.extend(_movie(item, title) for item in self._items(key))
        return found

    def library(self, cache: dict | None, whole: bool = False) -> Library:
        """Every film in the movie libraries, read whole every time - one
        request a library, and the only read that sees every change - with
        the days played the cache remembers added to the last one the server
        says. A day the cache says a run stamped itself, by marking the film
        played, is no day the film was watched, and is left out."""
        now = int(time.time())
        sections = self.movie_sections()
        kept = cache if cache and mediaserver.cache_is_for(
            cache, NAME, self.url, sections) else None
        known = {entry["key"]: entry for entry in (kept or {}).get("movies", ())}
        movies = []
        for movie in self.movies():
            entry = known.get(movie.key, {})
            stamped = set(entry.get("marked", ()))
            days = (set(entry.get("dates", ())) | set(movie.film.dates)) - stamped
            movies.append(movie._replace(film=movie.film._replace(
                dates=tuple(sorted(days)))))
        fresh = mediaserver.cache(NAME, self.url, sections, now, now, movies, [])
        for entry in fresh["movies"]:
            stamped = known.get(entry["key"], {}).get("marked")
            if stamped:
                entry["marked"] = list(stamped)
        return Library(movies, fresh, True, [])

    def copies(self) -> list[Copy]:
        """Every version of every film in the movie libraries, with the file
        or files it is kept in. The first version is the film itself; the
        others carry the label their file name gives them."""
        found: list[Copy] = []
        for key, title in self.movie_sections():
            for item in self._items(key):
                movie = _movie(item, title)
                for at, source in enumerate(item.get("MediaSources") or ()):
                    path = str(source.get("Path", ""))
                    files = [path]
                    if at == 0 and int(item.get("PartCount") or 0) > 1:
                        files += self._parts(movie.key)
                    found.append(Copy(movie.key, movie.film, title,
                                      _edition(source, path), tuple(files)))
        return found

    def _parts(self, key: str) -> list[str]:
        """The files after the first of a film split into parts."""
        answer = self._ask("/Videos/%s/AdditionalParts" % key,
                           (("userId", self.user_id()),)) or {}
        return [str(item.get("Path", "")) for item in answer.get("Items", ())]

    def mark_played(self, movie: Movie, day: str = "") -> str:
        """Mark a film played, as of ``day`` - or today, where it names none
        - and say which day it was stamped with."""
        day = day or datetime.date.today().isoformat()
        self._ask("/UserPlayedItems/%s" % movie.key,
                  (("userId", self.user_id()), ("datePlayed", _noon(day))),
                  method="POST")
        return day

    def rate(self, movie: Movie, rating: int) -> None:
        """Give a film a rating out of 10, kept as the user's own."""
        self._ask("/UserItems/%s/UserData" % movie.key,
                  (("userId", self.user_id()),), method="POST",
                  body={"Rating": rating})

    # Jellyfin keeps no watchlist; the provider says so, and these are never
    # asked. They answer as an empty list would.
    def watchlist(self, known: list | None = None) -> list[Wanted]:
        return []

    def catalogue_key(self, film: Film) -> str:
        return ""

    def add_to_watchlist(self, key: str) -> None:
        raise JellyfinError("Jellyfin keeps no watchlist")


def _noon(day: str) -> str:
    """A day as the moment Jellyfin is told a film was played: its noon, in
    UTC - the same calendar day read back anywhere from UTC-11 to UTC+11."""
    return day + "T12:00:00.000Z"


def _day(moment: str) -> str:
    """The local day of a moment Jellyfin writes - "2024-05-01T20:00:00.0000000Z"
    - or "" where it is not one."""
    text = moment.strip()
    if not text:
        return ""
    text = re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00")
    try:
        when = datetime.datetime.fromisoformat(text)
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    return when.astimezone().date().isoformat()


def _edition(source: dict, path: str) -> str:
    """The label a version was given - "" for the film itself, whose version
    Jellyfin names after its whole file."""
    name = str(source.get("Name", ""))
    stem = os.path.splitext(re.split(r"[\\/]", path)[-1])[0]
    return "" if not name or name == stem else name


def _movie(item: dict, section: str) -> Movie:
    ids = {str(key).lower(): str(value)
           for key, value in (item.get("ProviderIds") or {}).items() if value}
    original = str(item.get("OriginalTitle") or "")
    title = str(item.get("Name", ""))
    data = item.get("UserData") or {}
    try:
        rating = round(float(data.get("Rating") or 0))
    except (TypeError, ValueError):
        rating = 0
    played = bool(data.get("Played"))
    day = _day(str(data.get("LastPlayedDate") or "")) if played else ""
    film = Film(title, str(item.get("ProductionYear") or ""),
                tmdb=ids.get("tmdb", ""), imdb=ids.get("imdb", ""),
                aliases=(original,) if original and original != title else (),
                rating=rating, dates=(day,) if day else ())
    return Movie(str(item.get("Id", "")), film, played, section)


def local() -> Server:
    """The server ``jellyfinUrl`` names, or else the one on this machine."""
    return Server(os.environ.get("jellyfinUrl", "") or LOCAL,
                  os.environ.get("jellyfinApiKey", ""),
                  os.environ.get("jellyfinUser", ""))


def from_environment(account: str = "") -> Server | None:
    """The server ``jellyfinUrl`` and ``jellyfinApiKey`` name, read for
    ``account`` - a user's name - or ``jellyfinUser`` where that is "";
    None while the key is unset."""
    key = os.environ.get("jellyfinApiKey", "")
    if not key:
        return None
    return Server(os.environ.get("jellyfinUrl", "") or LOCAL, key,
                  account or os.environ.get("jellyfinUser", ""))
