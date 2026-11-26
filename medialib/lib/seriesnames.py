"""The names Plex and Jellyfin read a show by: the series folder, the season
folders, and each episode's file - written for the server the
``mediaServerNaming`` setting names, and read back either way.

    Plex       Show Title (2005) {tmdb-123}/Season 01/
               Show Title (2005) - s01e02 - Episode Title.mkv
    Jellyfin   Show Title (2005) [tmdbid-123]/Season 01/
               Show Title (2005) S01E02 Episode Title.mkv

The id tag goes on the series folder only: both servers read it there, and
the episode names stay shorter for it. The series title stays in every
episode name all the same - an episode copied out of its folder still says
which show it is, and the subtitle sites match on the file name alone.

Pure string work, like :mod:`medialib.lib.plexnames`: the caller renames.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

from medialib.lib import episodes, mediaserver, plexnames

__all__ = ["EpisodeName", "style", "safe_title", "series_folder_name",
           "season_folder_name", "episode_stem", "episode_name",
           "read_episode_name", "series_key", "series_folder_of",
           "INLINE_EXTRA_SUFFIXES", "inline_extra_name", "EPISODE_NAME_BYTES"]

# The characters no file name may carry on one of the systems a library is
# shared from, and what each is written as instead. A colon is the common
# one: "Show: The Next Chapter" becomes "Show - The Next Chapter", the way
# Plex's own guide writes it.
_UNSAFE = (("/", "-"), ("\\", "-"), (":", " -"), ("?", ""), ("*", ""),
           ('"', "'"), ("<", ""), (">", ""), ("|", "-"))

# The room an episode's name leaves for what is added to it later: a
# subtitle's ".en.forced.srt", a commentary transcript's ".2 Commentary".
_SIDECAR_ROOM = 32
EPISODE_NAME_BYTES = plexnames.NAME_MAX_BYTES - _SIDECAR_ROOM

# What an episode's own extra is called after its episode, by the folder the
# same extra would sit in at series or season level. Both servers read these.
INLINE_EXTRA_SUFFIXES = {
    "Behind The Scenes": "-behindthescenes",
    "Deleted Scenes": "-deleted",
    "Featurettes": "-featurette",
    "Interviews": "-interview",
    "Scenes": "-scene",
    "Shorts": "-short",
    "Trailers": "-trailer",
    "Other": "-other",
}

# An episode name read back, either spelling: the series part, the token,
# and what follows it.
_INLINE_EXTRA = re.compile(
    r"-(behindthescenes|deleted|featurette|interview|scene|short|trailer|"
    r"other)([0-9]*)$", re.I)
_PART = re.compile(r"(?:\s+-\s+|-)(?:pt|part|cd|disc|disk|dvd)([0-9]+)$", re.I)
_PLEX_VERSION = re.compile(r"\s+\[([^\[\]]+)\]$")
_LABEL_VERSION = re.compile(r"\s+-\s+([^-]+)$")


class EpisodeName(NamedTuple):
    """An episode's file name, read: the series part before the token, the
    season, the episodes, the title after the token, the version (the
    converted copy's "AV1") and the part of a split episode."""

    series: str
    season: int
    episodes: tuple
    title: str
    version: str
    part: str


def style() -> mediaserver.SeriesNaming:
    """How the server the names are written for wants an episode named."""
    return mediaserver.naming().series


def safe_title(title: str) -> str:
    """A catalogue's title as a file name may carry it."""
    for character, written in _UNSAFE:
        title = title.replace(character, written)
    return " ".join(title.split()).strip(" .")


def series_folder_name(title: str, year: str, tag: str = "") -> str:
    """``Show Title (2005) {tmdb-123}``: the title, the year where there is
    one, and the id tag in the server's spelling."""
    base = safe_title(title) + (" (%s)" % year if year else "")
    return plexnames.folder_name(base, tag)


def season_folder_name(season: int) -> str:
    """``Season 01`` - the English word on both servers, for any show; the
    specials are ``Season 00``."""
    return "Season %02d" % season


def episode_stem(series_base: str, season: int, numbers, title: str = "",
                 part: str = "", version: str = "",
                 naming: mediaserver.SeriesNaming | None = None) -> str:
    """One episode's file name without its extension.

    ``series_base`` is the series folder's name without its tag
    ("Show (2005)"); ``numbers`` the episodes the file holds; ``title`` the
    catalogue's title of the first; ``part`` a split episode's number;
    ``version`` the label a second copy of it carries ("AV1").
    """
    naming = naming or style()
    token = episodes.write_episode_token(season, numbers, naming.token)
    stem = series_base + naming.before + token
    if title:
        stem += naming.after + safe_title(title)
    if part:
        stem += (" - pt%s" if naming.token == "plex" else "-part%s") % part
    if version:
        stem += (" [%s]" % version if naming.version == "bracket"
                 else " - %s" % version)
    return stem


def episode_name(series_base: str, season: int, numbers, title: str,
                 extension: str = ".mkv", part: str = "", version: str = "",
                 naming: mediaserver.SeriesNaming | None = None,
                 folder: str = "", path_limit: int = 0) -> str:
    """:func:`episode_stem` with its extension, the title shortened - and
    only the title - where the name would not fit: a file name the systems
    a library is shared from cannot write, or with ``path_limit``, a whole
    path under ``folder`` longer than that."""
    def build(cut: str) -> str:
        return episode_stem(series_base, season, numbers, cut, part,
                            version, naming) + extension

    title = safe_title(title)
    name = build(title)
    budget = EPISODE_NAME_BYTES
    if path_limit and folder:
        budget = min(budget, path_limit - len(os.path.join(folder, "")))
    while title and (len(name.encode("utf-8")) > budget
                     or (path_limit and folder
                         and len(os.path.join(folder, name)) > path_limit)):
        words = title.split()
        title = (" ".join(words[:-1]) if len(words) > 1
                 else plexnames.cut_to_bytes(title,
                                             len(title.encode("utf-8")) - 1))
        title = title.rstrip(" -,.;")
        name = build(title)
    return name


def read_episode_name(name: str) -> EpisodeName | None:
    """An episode's file name read back, written for either server - or
    None for a name that carries no episode token."""
    stem, _extension = os.path.splitext(name)
    span = episodes.episode_span(stem)
    if span is None:
        return None
    found = episodes.episodes_of(stem)
    season = found[0][0]
    numbers = tuple(number for _season, number in found)
    series = stem[:span[0]].rstrip(" -._")
    rest = stem[span[1]:]
    part = ""
    version = ""
    match = _PLEX_VERSION.search(rest)
    if match and plexnames.CONVERTED_EDITION in match.group(1):
        version, rest = match.group(1), rest[:match.start()]
    else:
        match = _LABEL_VERSION.search(rest)
        if match and match.group(1).strip() == plexnames.CONVERTED_EDITION:
            version, rest = match.group(1).strip(), rest[:match.start()]
    match = _PART.search(rest)
    if match:
        part, rest = match.group(1), rest[:match.start()]
    title = rest.strip(" -._")
    return EpisodeName(series, season, numbers, title, version, part)


def series_folder_of(episode: str) -> str:
    """The series folder an episode sits in: its folder's parent when its
    folder is a season, else its own folder."""
    folder = os.path.dirname(os.path.abspath(episode))
    if episodes.season_of_folder(os.path.basename(folder)) is not None:
        return os.path.dirname(folder)
    return folder


def series_key(episode: str) -> str:
    """The key a record kept across runs knows one episode file by: the
    series' id (read off the series folder, as the Plex spelling), the
    season-and-episode token, and the part of a split episode - "" for an
    episode whose series carries no id, or a name with no token.

    The counterpart of :func:`plexnames.id_key`, which reads a film's id off
    its own folder: an episode's folder is its season."""
    folder = os.path.basename(series_folder_of(episode))
    match = plexnames.ID_TAG_RE.search(folder)
    read = read_episode_name(os.path.basename(episode))
    if not match or read is None:
        return ""
    token = episodes.write_episode_token(read.season, read.episodes, "plex")
    return " ".join(word for word in (
        plexnames.canonical_id(match.group(0)), token,
        "pt" + read.part if read.part else "") if word)


def inline_extra_name(episode_stem_: str, category: str, number: int,
                      extension: str = ".mkv") -> str:
    """An episode's own extra named after it: ``<episode>-deleted.mkv``, and
    ``-deleted2`` for the second of a kind."""
    suffix = INLINE_EXTRA_SUFFIXES.get(category, "-other")
    return episode_stem_ + suffix + (str(number) if number > 1 else "") \
        + extension


def is_inline_extra(name: str) -> bool:
    """Whether a file is an episode's own extra, named after it."""
    return bool(_INLINE_EXTRA.search(os.path.splitext(name)[0]))
