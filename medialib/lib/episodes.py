"""What a television file or folder name says about its season and episodes.

The readings every command that looks at a show shares: find-gaps asks which
episodes a run is missing, and ingest-series asks which season a loose file
belongs in and what its episode is called. One set of patterns, so the two can
never disagree about what "S01E02" is.

Three spellings of an episode are read - ``S01E02`` (with the range and list
forms after it), ``1x02``, and a bare ``Episode 2`` / ``Ep. 2`` when the folder
already says which season it is - plus the dates a daily show is named by.
And one is written: :func:`write_episode_token`, the token a server reads.
"""

from __future__ import annotations

import datetime
import re

__all__ = ["EPISODE", "MORE_EPISODES", "CROSS_EPISODE", "SEASON_FOLDER",
           "NAMES_SEASON", "episodes_of", "season_of_folder", "date_of",
           "numbered_episode", "write_episode_token", "has_episode_token",
           "episode_span"]

# A season and episode anywhere in a name: "S01E02", "s1e2", "S02,E13",
# "S01 E02", and the episodes after it of a file holding more than one -
# "S01E01E02", "S01E01-E03". Not inside a word: "bs01e02" is not an episode.
EPISODE = re.compile(
    r"(?<![A-Za-z0-9])[Ss]([0-9]{1,3})[ ._,-]*[Ee]([0-9]{1,4})"
    r"((?:[ ._]*-?[ ._]*[Ee][0-9]{1,4})*)(?![0-9])")
MORE_EPISODES = re.compile(r"(-?)[ ._]*[Ee]([0-9]{1,4})")
# And the other spelling of the same: "1x05".
CROSS_EPISODE = re.compile(
    r"(?<![A-Za-z0-9])([0-9]{1,2})[xX]([0-9]{2,3})(?![0-9])")

# A folder that is one season of a show: "Season 2", "S02", "Staffel 1",
# "Series 3", "Saison 4".
SEASON_FOLDER = re.compile(
    r"^(?:season|series|staffel|saison|seizoen|temporada|stagione|s)"
    r"[ ._-]*([0-9]{1,3})(?![0-9])", re.IGNORECASE)
# A folder whose name says which season it holds, anywhere in it: "Season 2",
# "Show S02 1080p". Not inside a word: "Jobs 2" names no season.
NAMES_SEASON = re.compile(
    r"(?<![A-Za-z0-9])(?:season|series|staffel|saison|seizoen|temporada|"
    r"stagione|s)[ ._-]*([0-9]{1,3})(?![0-9])", re.IGNORECASE)

# The folders a show keeps its specials in.
_SPECIALS_FOLDER = re.compile(r"^(?:specials?|extras? episodes?)$", re.I)

# "Episode 4", "Ep 4", "Ep. 04", "Folge 4", "Afl. 4" - a number the folder's
# season gives a season to.
_NUMBERED = re.compile(
    r"(?<![A-Za-z0-9])(?:episode|ep|folge|aflevering|afl|épisode|episodio)"
    r"[ ._-]*\.?[ ._-]*([0-9]{1,4})(?![0-9])", re.IGNORECASE)

# A date in a name, the way a daily show is named: 2011-11-15, 2011.11.15,
# 2011 11 15, and the European order 15-11-2011.
_ISO_DATE = re.compile(
    r"(?<![0-9])([12][0-9]{3})[ ._-]([01][0-9])[ ._-]([0-3][0-9])(?![0-9])")
_EUROPEAN_DATE = re.compile(
    r"(?<![0-9])([0-3][0-9])[ ._-]([01][0-9])[ ._-]([12][0-9]{3})(?![0-9])")


def episodes_of(name: str) -> list[tuple[int, int]]:
    """Every (season, episode) a name says it holds, in order."""
    match = EPISODE.search(name)
    if match:
        season, first = int(match.group(1)), int(match.group(2))
        found = [(season, first)]
        previous = first
        for more in MORE_EPISODES.finditer(match.group(3)):
            number = int(more.group(2))
            # A dash between two episodes is a range: E01-E03 is three.
            if more.group(1) and number > previous:
                found += [(season, n) for n in range(previous + 1, number + 1)]
            else:
                found.append((season, number))
            previous = number
        return found
    match = CROSS_EPISODE.search(name)
    if match:
        return [(int(match.group(1)), int(match.group(2)))]
    return []


def episode_span(name: str) -> tuple[int, int] | None:
    """Where in ``name`` its episode token stands, as (start, end), or None.
    What a rename cuts the release's own words after."""
    match = EPISODE.search(name) or CROSS_EPISODE.search(name)
    return (match.start(), match.end()) if match else None


def has_episode_token(name: str) -> bool:
    """Whether a name carries a season-and-episode token of its own."""
    return bool(episodes_of(name))


def season_of_folder(name: str) -> int | None:
    """The season a folder holds, read off its name - "Season 1", "S01",
    "Staffel 2", "Series 3" - with 0 for a "Specials" folder; None for a
    folder that names no season."""
    stripped = name.strip()
    if _SPECIALS_FOLDER.match(stripped):
        return 0
    match = SEASON_FOLDER.match(stripped)
    # Only where the number ends a word: "S1m0ne (2002)" is a film.
    if match and not stripped[match.end():match.end() + 1].isalnum():
        return int(match.group(1))
    return None


def numbered_episode(name: str) -> int | None:
    """The episode number a name gives without a season - "Episode 4",
    "Ep. 04" - or None. Only worth asking inside a season folder."""
    match = _NUMBERED.search(name)
    return int(match.group(1)) if match else None


def date_of(name: str) -> datetime.date | None:
    """The calendar day a name carries, the way a daily show is named
    ("2011-11-15", "15.11.2011"), or None - a run of digits that is no day
    included."""
    match = _ISO_DATE.search(name)
    if match:
        year, month, day = (int(group) for group in match.groups())
    else:
        match = _EUROPEAN_DATE.search(name)
        if not match:
            return None
        day, month, year = (int(group) for group in match.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def write_episode_token(season: int, episodes, style: str = "plex") -> str:
    """The token a server reads one file's episodes by.

    ``style`` "plex" writes ``s01e02`` and a range ``s01e02-e03``; "jellyfin"
    the same in capitals, ``S01E02-E03``. Several episodes are written as the
    range from the first to the last: both servers read a range, and neither
    reads a list with holes in it.
    """
    numbers = sorted(set(int(number) for number in episodes))
    if not numbers:
        raise ValueError("no episode to write")
    width = max(2, len(str(numbers[-1])))
    token = "s%02de%0*d" % (season, width, numbers[0])
    if len(numbers) > 1:
        token += "-e%0*d" % (width, numbers[-1])
    return token.upper() if style == "jellyfin" else token
