"""Which show a series folder is: TMDb first, TheTVDB as the fallback.

The ladder mirrors the film one (:mod:`medialib.lib.tmdbidentify`), with the
episodes on disk standing in for the film's runtime:

1. The folder's title and year against TMDb's ``/search/tv``, under every
   spelling :func:`titlematch.search_titles` gives; a candidate is kept when
   one of its own titles (its name, original name, alternative titles and
   translations) meets the folder's :func:`titlematch.title_keys`.
2. Narrowed by structure: a candidate needs a season for every season on
   disk, each holding at least as many episodes as the highest number on disk.
3. Where several still fit, the measured length of the episodes against the
   catalogue's runtimes; a show is named only when exactly one fits.
4. When TMDb named none, or none that fits, TheTVDB is asked the same way -
   and its show is tried in each of its orders (aired, DVD, absolute), since a
   library numbered by a DVD set fits only that one. The order that fit is
   reported: the server has to be told to read the show in it.

A show TMDb names keeps TMDb's aired numbering, and its TheTVDB id comes from
TMDb's own external ids without asking TheTVDB. Episode numbers on disk are
never changed to fit a catalogue.
"""

from __future__ import annotations

import functools
import os
from typing import NamedTuple

from medialib.lib import languages, titlematch, tmdbclient, tvdbclient

__all__ = ["Episode", "Show", "OnDisk", "Result", "identify", "by_tag",
           "fits", "imdb_of", "languages_of", "tmdb_show", "tvdb_show", "ORDER_NAMES"]

# How far an episode's measured length may stray from the catalogue's
# runtime and still be that episode: credits, recaps and a broadcast's
# minutes against a disc's.
RUNTIME_TOLERANCE = 0.25
RUNTIME_SLACK_MINUTES = 4

# How many search results of each query are looked at in detail.
MAX_CANDIDATES = 6

# What each of TheTVDB's orders is called on the servers, for the line that
# tells someone which order to set.
ORDER_NAMES = {
    "aired": "aired order",
    "default": "aired order",
    "official": "aired order",
    "dvd": "DVD order",
    "absolute": "absolute order",
}


class Episode(NamedTuple):
    """One episode as a catalogue has it."""

    number: int
    title: str = ""
    runtime: int = 0          # minutes, 0 when unknown
    aired: str = ""           # YYYY-MM-DD


class Show(NamedTuple):
    """A show as a catalogue has it.

    ``source`` is "tmdb" or "tvdb", ``order`` the episode order its seasons
    are in ("aired", or one of TheTVDB's), ``seasons`` {season: episode
    count}, and ``episodes`` {season: [Episode]} for the seasons read in
    full - the ones a run needs the titles of."""

    source: str
    id: str
    title: str
    year: str = ""
    titles: tuple = ()
    original_language: str = ""
    spoken: tuple = ()
    imdb: str = ""
    tmdb: str = ""
    tvdb: str = ""
    order: str = "aired"
    seasons: dict = {}
    episodes: dict = {}

    def tag_provider(self) -> tuple[str, str]:
        """(provider, number) the series folder is tagged with: TMDb's own
        where TMDb named it, else TheTVDB's."""
        if self.source == "tmdb":
            return "tmdb", self.tmdb or self.id
        return "tvdb", self.tvdb or self.id

    def episode(self, season: int, number: int) -> Episode | None:
        for episode in self.episodes.get(season, ()):
            if episode.number == number:
                return episode
        return None

    def by_date(self, day: str) -> tuple[int, int] | None:
        """The (season, episode) that aired on ``day``, where exactly one
        did."""
        found = [(season, episode.number)
                 for season, rows in self.episodes.items()
                 for episode in rows if episode.aired == day]
        return found[0] if len(found) == 1 else None


class OnDisk(NamedTuple):
    """What a series folder holds: {season: {episode numbers}}, and the
    measured length of episodes, {(season, episode): seconds}, where known."""

    episodes: dict
    durations: dict = {}


class Result(NamedTuple):
    """What the ladder came to: the show, or None, and the notes saying why
    - what was asked and what came back - for the near-miss list."""

    show: Show | None
    notes: tuple = ()
    ambiguous: tuple = ()


# --- the structure check ------------------------------------------------------

def fits(seasons: dict, on_disk: OnDisk) -> bool:
    """Whether a catalogue's seasons ({season: episode count}) cover every
    season and episode on disk. A disk's specials (season 0) are asked only
    of a catalogue that keeps specials."""
    for season, numbers in on_disk.episodes.items():
        if not numbers:
            continue
        if season == 0 and 0 not in seasons:
            continue
        if seasons.get(season, 0) < max(numbers):
            return False
    return True


def _runtime_fits(show: Show, on_disk: OnDisk) -> bool | None:
    """Whether the measured episodes run as long as the catalogue says, or
    None when there is nothing to compare."""
    compared = 0
    for (season, number), seconds in on_disk.durations.items():
        episode = show.episode(season, number)
        if episode is None or not episode.runtime or not seconds:
            continue
        minutes = seconds / 60.0
        allowed = max(RUNTIME_SLACK_MINUTES,
                      episode.runtime * RUNTIME_TOLERANCE)
        if abs(minutes - episode.runtime) > allowed:
            return False
        compared += 1
    return True if compared else None


# --- TMDb ---------------------------------------------------------------------

def _tmdb(path: str, params=()) -> dict:
    key = os.environ.get("tmdbApiKey", "")
    if not key:
        return {}
    return tmdbclient._as_json(tmdbclient._curl(
        tmdbclient._BASE + path, [("api_key", key)] + list(params)))


def _year(date) -> str:
    return str(date)[:4] if isinstance(date, str) and len(date) >= 4 \
        and date[:4].isdigit() else ""


def _tmdb_titles(detail: dict) -> tuple:
    titles = [detail.get("name"), detail.get("original_name")]
    alternatives = (detail.get("alternative_titles") or {}).get("results")
    for row in alternatives if isinstance(alternatives, list) else ():
        if isinstance(row, dict):
            titles.append(row.get("title"))
    translations = (detail.get("translations") or {}).get("translations")
    for row in translations if isinstance(translations, list) else ():
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict):
            titles.append(data.get("name"))
    seen: list = []
    for title in titles:
        if isinstance(title, str) and title.strip() and title not in seen:
            seen.append(title.strip())
    return tuple(seen)


def tmdb_show(tmdb_id: str, seasons_wanted=()) -> Show | None:
    """A TMDb show by its number, with the episodes of ``seasons_wanted``
    read in full - None where TMDb has no such show."""
    detail = _tmdb("/tv/%s" % tmdb_id,
                   [("append_to_response",
                     "alternative_titles,translations,external_ids")])
    if not detail.get("id"):
        return None
    seasons = {}
    for row in detail.get("seasons") or ():
        if isinstance(row, dict) and isinstance(row.get("season_number"), int):
            seasons[row["season_number"]] = int(row.get("episode_count") or 0)
    external = detail.get("external_ids") or {}
    spoken = tuple(code for code in (
        languages.code_from_tag(str(row.get("iso_639_1") or ""))
        for row in detail.get("spoken_languages") or ()
        if isinstance(row, dict)) if code)
    show = Show(
        "tmdb", str(detail["id"]), str(detail.get("name") or ""),
        _year(detail.get("first_air_date")), _tmdb_titles(detail),
        languages.code_from_tag(str(detail.get("original_language") or "")),
        spoken, str(external.get("imdb_id") or ""), str(detail["id"]),
        str(external.get("tvdb_id") or ""), "aired", seasons, {})
    return with_episodes(show, seasons_wanted)


def _tmdb_season(tmdb_id: str, season: int) -> list[Episode]:
    answer = _tmdb("/tv/%s/season/%d" % (tmdb_id, season))
    found = []
    for row in answer.get("episodes") or ():
        if isinstance(row, dict) and isinstance(row.get("episode_number"), int):
            found.append(Episode(row["episode_number"],
                                 str(row.get("name") or ""),
                                 int(row.get("runtime") or 0),
                                 str(row.get("air_date") or "")))
    return found


def with_episodes(show: Show, seasons_wanted) -> Show:
    """``show`` with the episodes of every season in ``seasons_wanted`` read
    in full (a TheTVDB show already has them all)."""
    if show.source != "tmdb":
        return show
    episodes = dict(show.episodes)
    for season in seasons_wanted:
        if season not in episodes and season in show.seasons:
            episodes[season] = _tmdb_season(show.tmdb, season)
    return show._replace(episodes=episodes)


def _tmdb_search(title: str, year: str) -> list[dict]:
    params = [("query", title), ("include_adult", "false")]
    if year:
        params.append(("first_air_date_year", year))
    rows = _tmdb("/search/tv", params).get("results")
    return [row for row in rows if isinstance(row, dict)] \
        if isinstance(rows, list) else []


def _tmdb_candidates(title: str, year: str, want: frozenset,
                     notes: list) -> list[Show]:
    ids: list[str] = []
    for query in titlematch.search_titles(title) or [title]:
        rows = _tmdb_search(query, year)
        if not rows and year:
            rows = _tmdb_search(query, "")
        notes.append('  TMDb asked for "%s"%s: %d result(s)'
                     % (query, " (%s)" % year if year else "", len(rows)))
        for row in rows[:MAX_CANDIDATES]:
            names = [row.get("name"), row.get("original_name")]
            if str(row.get("id")) in ids:
                continue
            if any(isinstance(name, str) and titlematch.title_keys(name) & want
                   for name in names) or len(rows) == 1:
                ids.append(str(row.get("id")))
        if ids:
            break
    shows = []
    for tmdb_id in ids:
        show = tmdb_show(tmdb_id)
        if show is None:
            continue
        if not any(titlematch.title_keys(name) & want for name in show.titles):
            notes.append("    %s - none of its titles is the folder's"
                         % _says(show))
            continue
        shows.append(show)
    return shows


# --- TheTVDB ------------------------------------------------------------------

def tvdb_show(tvdb_id: str, order: str = "default") -> Show | None:
    """A TheTVDB show by its number, every episode in ``order``."""
    record = tvdbclient.series_extended(tvdb_id)
    if not record:
        return None
    rows = tvdbclient.series_episodes(tvdb_id, order)
    episodes: dict = {}
    for row in rows:
        season, number = row.get("seasonNumber"), row.get("number")
        if not isinstance(season, int) or not isinstance(number, int):
            continue
        episodes.setdefault(season, []).append(Episode(
            number, str(row.get("name") or ""), int(row.get("runtime") or 0),
            str(row.get("aired") or "")))
    seasons = {season: max(episode.number for episode in rows)
               for season, rows in episodes.items() if rows}
    remote = {str(row.get("sourceName", "")).lower(): str(row.get("id", ""))
              for row in record.get("remoteIds") or ()
              if isinstance(row, dict)}
    titles = [record.get("name")] + [
        row.get("name") if isinstance(row, dict) else row
        for row in record.get("aliases") or ()]
    return Show(
        "tvdb", str(record.get("id") or tvdb_id), str(record.get("name") or ""),
        str(record.get("year") or _year(record.get("firstAired"))),
        tuple(title for title in titles if isinstance(title, str) and title),
        languages.code_from_tag(str(record.get("originalLanguage") or "")),
        (), remote.get("imdb", ""), remote.get("themoviedb.com", ""),
        str(record.get("id") or tvdb_id),
        "aired" if order == "default" else order, seasons, episodes)


def _tvdb_candidates(title: str, year: str, want: frozenset,
                     notes: list) -> list[str]:
    ids: list[str] = []
    for query in titlematch.search_titles(title) or [title]:
        rows = tvdbclient.search_series(query, year) \
            or (tvdbclient.search_series(query) if year else [])
        notes.append('  TheTVDB asked for "%s": %d result(s)'
                     % (query, len(rows)))
        for row in rows[:MAX_CANDIDATES]:
            names = [row.get("name")] + list(row.get("aliases") or ())
            tvdb_id = str(row.get("tvdb_id") or row.get("id") or "")
            tvdb_id = tvdb_id.removeprefix("series-")
            if tvdb_id and tvdb_id not in ids and any(
                    isinstance(name, str) and titlematch.title_keys(name) & want
                    for name in names):
                ids.append(tvdb_id)
        if ids:
            break
    return ids


# --- the ladder ---------------------------------------------------------------

def _says(show: Show) -> str:
    return '%s %s "%s"%s' % ("TMDb" if show.source == "tmdb" else "TheTVDB",
                             show.id, show.title,
                             " (%s)" % show.year if show.year else "")


def _settle(shows: list[Show], on_disk: OnDisk, notes: list) -> Result:
    fitting = []
    for show in shows:
        if fits(show.seasons, on_disk):
            fitting.append(show)
        else:
            notes.append("    %s - its seasons do not hold the episodes on "
                         "disk" % _says(show))
    if len(fitting) <= 1:
        return Result(fitting[0] if fitting else None, tuple(notes))
    by_runtime = []
    for show in fitting:
        show = with_episodes(show, on_disk.episodes)
        verdict = _runtime_fits(show, on_disk)
        if verdict is not False:
            by_runtime.append((verdict, show))
        else:
            notes.append("    %s - its episodes run another length"
                          % _says(show))
    measured = [show for verdict, show in by_runtime if verdict]
    if len(measured) == 1:
        return Result(measured[0], tuple(notes))
    left = measured or [show for _verdict, show in by_runtime]
    if len(left) == 1:
        return Result(left[0], tuple(notes))
    notes.append("    %d shows fit, and nothing on disk tells them apart"
                 % len(left))
    return Result(None, tuple(notes), tuple(left))


def identify(title: str, year: str, on_disk: OnDisk,
             tvdb: bool = True) -> Result:
    """The show a series folder titled ``title`` (``year`` where known)
    holding ``on_disk`` is - see the module's ladder."""
    notes: list = ['"%s"%s' % (title, " (%s)" % year if year else "")]
    want = titlematch.title_keys(title)
    if not want:
        return Result(None, tuple(notes + ["  no title to ask by"]))
    if os.environ.get("tmdbApiKey"):
        shows = _tmdb_candidates(title, year, want, notes)
        result = _settle(shows, on_disk, notes)
        if result.show is not None or result.ambiguous:
            return result
    if not tvdb or not tvdbclient.available():
        return Result(None, tuple(notes))
    for tvdb_id in _tvdb_candidates(title, year, want, notes):
        for order in tvdbclient.SEASON_TYPES:
            show = tvdb_show(tvdb_id, order)
            if show is None:
                break
            if fits(show.seasons, on_disk) \
                    and _runtime_fits(show, on_disk) is not False:
                notes.append("    %s fits in its %s"
                             % (_says(show), ORDER_NAMES.get(show.order,
                                                             show.order)))
                return Result(show, tuple(notes))
        notes.append("    TheTVDB %s - fits none of its orders" % tvdb_id)
    return Result(None, tuple(notes))


def by_tag(provider: str, number: str, on_disk: OnDisk | None = None) -> Show | None:
    """The show an id names - a tag a folder already carries, or one written
    into the id list: TMDb's number, TheTVDB's (tried in each order until
    one fits what is on disk), or an IMDb id found through TMDb."""
    number = number.strip()
    seasons = sorted(on_disk.episodes) if on_disk else ()
    if provider == "tmdb" and number.isdigit():
        return tmdb_show(number, seasons)
    if provider == "imdb":
        found = _tmdb("/find/" + number.lower(),
                      [("external_source", "imdb_id")]).get("tv_results")
        if isinstance(found, list) and found and isinstance(found[0], dict):
            return tmdb_show(str(found[0].get("id")), seasons)
        return None
    if provider == "tvdb" and number.isdigit():
        first = None
        for order in tvdbclient.SEASON_TYPES:
            show = tvdb_show(number, order)
            if show is None:
                return first
            first = first or show
            if on_disk is None or fits(show.seasons, on_disk):
                return show
        return first
    return None


@functools.lru_cache(maxsize=256)
def imdb_of(provider: str, number: str) -> str:
    """A show's IMDb id from its TMDb or TheTVDB number, asked once per run
    - "" where the catalogue holds none."""
    if provider == "tmdb" and number.isdigit():
        ids = _tmdb("/tv/%s/external_ids" % number)
        return str(ids.get("imdb_id") or "")
    if provider == "tvdb" and number.isdigit():
        record = tvdbclient.series_extended(number)
        for row in record.get("remoteIds") or ():
            if isinstance(row, dict) and str(row.get("id", "")).startswith("tt"):
                return str(row["id"])
    return ""


def languages_of(show: Show | None):
    """What the show is spoken in, as the remux's language rule takes it."""
    from medialib.lib import tmdbidentify
    if show is None:
        return tmdbidentify.FilmLanguages()
    return tmdbidentify.FilmLanguages(original=show.original_language,
                                      spoken=show.spoken)
