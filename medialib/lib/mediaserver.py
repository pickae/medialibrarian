"""The media servers a library is kept for, and what every one of them is
asked the same way.

A server is one entry in :data:`PROVIDERS`: how it names films on disk, what
it can and cannot do, the settings it is reached with, and the module its
client lives in. The ``mediaServer`` setting picks one; nothing else in the
commands says which server it is talking to. A third server is a module
answering :class:`MediaServer` and one more entry here.

The films a server holds are read into the same shapes whichever server it
is - :class:`Movie`, :class:`Copy`, :class:`Wanted` - and a library read is
kept between runs in the same cache, so the sync reads only what changed.
"""

from __future__ import annotations

import datetime
import importlib
import os
from typing import NamedTuple, Protocol

from medialib.lib import clioptions
from medialib.lib.enums import shell_lower
from medialib.lib.filmmatch import Film

__all__ = ["MediaServerError", "Movie", "Copy", "Wanted", "Library",
           "Capabilities", "Naming", "Provider", "MediaServer", "PROVIDERS",
           "DEFAULT", "naming", "provider", "updated"]


class MediaServerError(Exception):
    """The server could not be asked, or gave an answer that is not one."""


class Movie(NamedTuple):
    """One film in a server's library: the server's key for it, and the film
    - with its rating, and the days it was played once the library has been
    read."""

    key: str
    film: Film
    played: bool
    section: str


class Copy(NamedTuple):
    """One version of a film in a library - a server merges several into one
    film - with its edition and the file or files it is kept in (several for
    a film split into parts)."""

    key: str
    film: Film
    section: str
    edition: str
    files: tuple


class Wanted(NamedTuple):
    """A film on the account's watchlist: the server's catalogue id, and the
    film."""

    key: str
    film: Film


class Library(NamedTuple):
    """A library read: its films, what to keep for the next read, and
    whether it was read whole."""

    movies: list
    cache: dict
    whole: bool
    watchlist: list = []


class Capabilities(NamedTuple):
    """What a server can be asked, where servers differ.

    ``play_history``: every day a film was played, rather than only the last.
    ``dated_marks``: a film marked played can be given the day it was watched.
    ``watchlist``: the account keeps a watchlist, with a catalogue to name a
    film it does not hold by. ``rating_in_ui``: the rating a run gives a film
    is one its own player shows.
    """

    play_history: bool
    dated_marks: bool
    watchlist: bool
    rating_in_ui: bool


class Naming(NamedTuple):
    """How a server's scanner wants a film named on disk.

    ``imdb`` and ``tmdb`` are the id tag, with ``%s`` for the number.
    ``edition`` is ``"tag"`` - ``{edition-Name}`` - or ``"label"``, a
    `` - Name`` after the whole name. ``sidecar_titles``: the server takes a
    subtitle's track name from the sidecar's own file name.
    """

    imdb: str
    tmdb: str
    edition: str
    sidecar_titles: bool


class Provider(NamedTuple):
    """One server medialib can keep a library for."""

    name: str
    display: str
    module: str
    naming: Naming
    capabilities: Capabilities
    # The settings it is reached with, the secret among them last - said
    # where a run cannot reach it.
    settings: tuple

    def client(self):
        return importlib.import_module(self.module)

    def from_settings(self, account: str = "") -> MediaServer | None:
        """The server its settings name, read for ``account`` - the server's
        own account where it is "" - or None while they are incomplete."""
        return self.client().from_environment(account)

    def local(self) -> MediaServer:
        """The server its settings name, or else the one on this machine."""
        return self.client().local()

    def hint(self) -> str:
        """What to set where the server cannot be reached."""
        return "set %s" % " and ".join(self.settings)


class MediaServer(Protocol):
    """What every server's client answers."""

    url: str

    def movie_sections(self) -> list[tuple[str, str]]: ...

    def library(self, cache: dict | None, whole: bool = False) -> Library: ...

    def movies(self, sections=()) -> list[Movie]: ...

    def copies(self) -> list[Copy]: ...

    # Says the day the server now has the film played on, where marking it
    # leaves one the next read would take for a day it was watched - "" where
    # it leaves none.
    def mark_played(self, movie: Movie, day: str = "") -> str: ...

    def rate(self, movie: Movie, rating: int) -> None: ...

    # Asked only of a server whose capabilities say it keeps a watchlist.
    def watchlist(self, known: list | None = None) -> list[Wanted]: ...

    def catalogue_key(self, film: Film) -> str: ...

    def add_to_watchlist(self, key: str) -> None: ...


PROVIDERS: dict[str, Provider] = {
    "plex": Provider(
        "plex", "Plex", "medialib.lib.plexserver",
        Naming("{imdb-%s}", "{tmdb-%s}", "tag", sidecar_titles=False),
        Capabilities(play_history=True, dated_marks=False, watchlist=True,
                     rating_in_ui=True),
        ("plexUrl", "plexToken")),
    "jellyfin": Provider(
        "jellyfin", "Jellyfin", "medialib.lib.jellyfinserver",
        Naming("[imdbid-%s]", "[tmdbid-%s]", "label", sidecar_titles=True),
        Capabilities(play_history=False, dated_marks=True, watchlist=False,
                     rating_in_ui=False),
        ("jellyfinUrl", "jellyfinUser", "jellyfinApiKey")),
}

DEFAULT = "plex"


def _named(variable: str, value: str) -> Provider:
    name = shell_lower(value.strip())
    if name not in PROVIDERS:
        raise clioptions.UsageError(
            'The %s setting names "%s". It is one of %s.'
            % (variable, value.strip(), ", ".join(PROVIDERS)))
    return PROVIDERS[name]


def provider() -> Provider:
    """The server the ``mediaServer`` setting names - refused, like a bad
    option, where it names none medialib knows."""
    return _named("mediaServer", os.environ.get("mediaServer", "") or DEFAULT)


def naming() -> Provider:
    """The server the names are written for: ``mediaServerNaming`` where it
    is set, and the server the commands talk to where it is not."""
    value = os.environ.get("mediaServerNaming", "")
    return _named("mediaServerNaming", value) if value.strip() else provider()


# --- the cache a library read is kept in --------------------------------------

def cache(provider_name: str, url: str, sections: list, full: int, read: int,
          movies: list, wanted: list) -> dict:
    return {"provider": provider_name, "server": url,
            "sections": [list(pair) for pair in sections],
            "full": full, "read": read, "movies": [packed(movie) for movie in movies],
            "watchlist": [dict(packed_film(entry.film), key=entry.key)
                          for entry in wanted]}


def cache_is_for(kept: dict | None, provider_name: str, url: str,
                 sections: list) -> bool:
    """Whether a kept cache is this server's, of these libraries. A cache
    from before servers were named in it is Plex's."""
    if not kept:
        return False
    return kept.get("provider", "plex") == provider_name \
        and kept.get("server") == url \
        and [list(pair) for pair in sections] == kept.get("sections")


def packed_film(film: Film) -> dict:
    return {"title": film.title, "year": film.year, "tmdb": film.tmdb,
            "imdb": film.imdb, "aliases": list(film.aliases),
            "rating": film.rating, "dates": list(film.dates)}


def unpacked_film(entry: dict) -> Film:
    return Film(entry["title"], entry["year"], entry["tmdb"], entry["imdb"],
                tuple(entry["aliases"]), rating=entry["rating"],
                dates=tuple(entry["dates"]))


def packed(movie: Movie) -> dict:
    return dict(packed_film(movie.film), key=movie.key, section=movie.section,
                played=movie.played)


def unpacked(entry: dict) -> Movie:
    return Movie(entry["key"], unpacked_film(entry), entry["played"],
                 entry["section"])


def updated(kept: dict, movie: Movie, stamped: str = "") -> None:
    """Keep in ``kept`` what this run changed about a film on the server -
    and ``stamped``, the day marking it played left on the server, which a
    later read is not to take for a day it was watched."""
    for at, entry in enumerate(kept["movies"]):
        if entry["key"] == movie.key:
            marked = set(entry.get("marked", ())) | ({stamped} if stamped else set())
            kept["movies"][at] = packed(movie)
            if marked:
                kept["movies"][at]["marked"] = sorted(marked)


def day_of(epoch: str) -> str:
    return datetime.datetime.fromtimestamp(int(epoch)).date().isoformat()
