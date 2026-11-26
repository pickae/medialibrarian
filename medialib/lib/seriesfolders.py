"""Reading a show's folder: its title, year and id, its seasons, its loose
episodes and its extras - and the guard that tells a show from a film.

A show is kept the way Plex and Jellyfin both read it:

    Show Title (2005) {tmdb-123}/
        Season 01/
            Show Title (2005) - s01e01 - Pilot.mkv
        Season 00/              (the specials)
        Featurettes/            (series-level extras)

Everything here is a read of names and listings: nothing is renamed or moved.
The phases that do that are ``ingest-series``'s.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

from medialib.lib import enums, episodes, plexnames

__all__ = ["SeriesFolder", "read_series_folder", "is_video", "videos_in",
           "season_folders", "loose_episodes", "bonus_folders",
           "looks_like_series", "looks_like_film", "series_folders",
           "FEATURE_SECONDS"]

# The videos a show is kept in, before the layout has made Matroska of them.
_VIDEO_EXTENSIONS = frozenset(("mkv",) + tuple(enums.SOURCE_VIDEO_EXTENSIONS))

# "Title (2005)" and what may follow it; and a year written bare at the end.
_PAREN_YEAR = re.compile(r"^(.*?)\s*\(([12][0-9]{3})\)\s*(.*)$")
_BARE_YEAR = re.compile(r"^(.*\S)[\s._-]+([12][0-9]{3})$")

# How long a single video has to run to be a feature film rather than an
# episode: longer than any hour-long drama's episode with its credits.
FEATURE_SECONDS = 70 * 60


class SeriesFolder(NamedTuple):
    """A series folder's name, read: the name without its id tag, the tag,
    the title, the year ("" where none is written)."""

    base: str
    tag: str
    title: str
    year: str


def read_series_folder(name: str) -> SeriesFolder:
    """A series folder's name read as (base, tag, title, year).

    The tag is the first id tag the name carries, written for either server;
    the year the one in brackets after the title, or a bare one ending the
    name ("Show 2005"); the title is what stands before it, with the dots and
    underscores a release writes for spaces read as spaces when the name has
    no space of its own.
    """
    base, tag = plexnames.untagged_base(name)
    text = base.strip()
    if " " not in text:
        text = text.replace(".", " ").replace("_", " ")
    match = _PAREN_YEAR.match(text)
    if match:
        return SeriesFolder(base, tag, match.group(1).strip(), match.group(2))
    match = _BARE_YEAR.match(text)
    if match:
        return SeriesFolder(base, tag, match.group(1).strip(" .-_"),
                            match.group(2))
    return SeriesFolder(base, tag, text.strip(), "")


def is_video(name: str) -> bool:
    """A video file the show might hold an episode in - not the copy an
    improvement kept."""
    return ("." in name
            and enums.lower_extension_of(name) in _VIDEO_EXTENSIONS
            and not plexnames.is_kept_copy(name))


def _entries(path: str) -> tuple[list[str], list[str]]:
    """(files, folders) directly in ``path``, by name; hidden entries left
    out."""
    files, folders = [], []
    try:
        with os.scandir(path) as listing:
            for entry in listing:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    folders.append(entry.name)
                elif entry.is_file(follow_symlinks=False):
                    files.append(entry.name)
    except OSError:
        pass
    return sorted(files), sorted(folders)


def videos_in(path: str) -> list[str]:
    """The videos directly in ``path``, by name."""
    return [name for name in _entries(path)[0] if is_video(name)]


def season_folders(path: str) -> list[tuple[int, str]]:
    """The season folders of a series folder, as (season, path), by season;
    "Specials" is season 0."""
    found = []
    for name in _entries(path)[1]:
        season = episodes.season_of_folder(name)
        if season is not None:
            found.append((season, os.path.join(path, name)))
    return sorted(found)


def loose_episodes(path: str) -> list[str]:
    """The videos sitting directly in a series folder, outside any season."""
    return [os.path.join(path, name) for name in videos_in(path)]


def bonus_folders(path: str) -> list[str]:
    """The extras folders directly in ``path`` - a series or a season
    folder - named the way the servers read them."""
    return [os.path.join(path, name) for name in _entries(path)[1]
            if plexnames.is_bonus_folder_name(name)]


def looks_like_series(path: str) -> bool:
    """Whether a folder is a show rather than a film or a shelf of them.

    Yes when it holds a season folder with a video in it, or when most of
    the videos directly in it carry an episode token (``S01E02``, ``1x02``).
    A folder that is itself named for a season ("Season 1") is a show's
    season, and is a show for the guard's purposes too.
    """
    files, folders = _entries(path)
    if episodes.season_of_folder(os.path.basename(path.rstrip("/\\"))) \
            not in (None, 0) and any(is_video(name) for name in files):
        return True
    for name in folders:
        if episodes.season_of_folder(name) is None:
            continue
        season_path = os.path.join(path, name)
        if videos_in(season_path) or _holds_video_below(season_path):
            return True
    videos = [name for name in files if is_video(name)]
    if not videos:
        return False
    tokened = sum(1 for name in videos if episodes.has_episode_token(name))
    return tokened * 2 > len(videos)


def _holds_video_below(path: str) -> bool:
    return any(is_video(name)
               for _parent, _dirs, names in os.walk(path) for name in names)


def looks_like_film(path: str, duration=None) -> bool:
    """Whether a folder handed to ``ingest-series`` holds a film instead: one
    feature-length video, no season folders and no episode token.

    ``duration`` answers a video's length in seconds; without it, or where
    it cannot tell (0), a lone video with no token is taken for a film only
    when it is the folder's only video and is named like the folder.
    """
    if season_folders(path):
        return False
    videos = videos_in(path)
    if len(videos) != 1 or episodes.has_episode_token(videos[0]):
        return False
    if duration is not None:
        seconds = duration(os.path.join(path, videos[0]))
        if seconds:
            return seconds >= FEATURE_SECONDS
    folder = read_series_folder(os.path.basename(path.rstrip("/\\")))
    stem = os.path.splitext(videos[0])[0]
    return bool(folder.title) and stem.lower().startswith(folder.title.lower())


def series_folders(root: str) -> list[str]:
    """The folders directly under ``root`` that are shows, by name."""
    return [os.path.join(root, name) for name in _entries(root)[1]
            if looks_like_series(os.path.join(root, name))]
