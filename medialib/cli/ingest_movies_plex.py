"""The Plex comparison: each film ingested looked for in the Plex library -
by the id tag the tagging gave it, by its title and year where it has none -
and, where the library has it already, said where, and held up against it
(see :mod:`medialib.lib.plexcompare`).

Nothing is changed, on disk or on the server: what to keep, and what to carry
from one file into the other, is left to whoever reads the verdicts. The mixed
ones are the reason for the phase. The closing count lists every film the
library has, MIXED first, each with where its library copy is.
"""

from __future__ import annotations

import os
import re

from medialib.cli import ingest_movies as rules
from medialib.lib import plexcompare, plexnames, plexserver

__all__ = ["compare_roots"]

# How each verdict is said, and counted at the end.
_VERDICTS = {
    "absent": ("not in the library", "not in the library"),
    "new": ("the NEW file is better in every respect: the existing one can go",
            "better than the library's"),
    "old": ("the EXISTING file is better in every respect: the new one adds "
            "nothing", "worse than the library's"),
    "same": ("equivalent: nothing to gain either way", "equivalent"),
    "mixed": ("MIXED: each has something the other lacks", "MIXED"),
    "different": ("a DIFFERENT VERSION of the film: keep both, as editions",
                  "a different version"),
    "unread": ("in the library, but not compared", "not compared"),
}


# The order the closing summary lists the duplicates in: the ones that need
# work first.
_INTEREST = ("mixed", "different", "new", "old", "same", "unread")


def compare_roots(roots: list, log) -> int:
    """Every film under each folder of ``roots`` compared with the library;
    1 where the server could not be asked at all."""
    server = plexserver.local()
    log("Plex: reading the library at %s" % server.url)
    try:
        copies = server.copies()
    except plexserver.PlexError as error:
        log("Plex: SKIPPED, the server could not be asked (%s)%s" % (
            error, "" if server.token else
            " - set plexToken to that server's X-Plex-Token"))
        return 1
    log("Plex: %d film version(s) in the library" % len(copies))
    index = _index(copies)
    tally: dict = {}
    for root in roots:
        for movie in films(root):
            verdict, places = _compare_film(movie, index, copies, log)
            tally.setdefault(verdict, []).append((movie, places))
    _report(tally, log)
    return 0


def films(root: str) -> list:
    """The main movie of every film folder under ``root``, improved or not:
    no bonus material, no copy an improvement kept."""
    found: list[str] = []
    for directory in [root] + rules._folders_below(root):
        if rules.is_bonus_folder(directory):
            continue
        base, _tag = plexnames.untagged_base(os.path.basename(directory.rstrip("/")))
        names = [name for name in rules._names_in(directory)
                 if os.path.isfile(os.path.join(directory, name))]
        found.extend(os.path.join(directory, name)
                     for name in plexnames.one_film_in(base, names))
    return found


def _fold(title: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", title.casefold())


def _index(copies: list) -> dict:
    index: dict = {}
    for copy in copies:
        for key in ({"{imdb-%s}" % copy.film.imdb} if copy.film.imdb else set()) \
                | ({"{tmdb-%s}" % copy.film.tmdb} if copy.film.tmdb else set()):
            index.setdefault(key, []).append(copy)
    return index


def _found(movie: str, index: dict, copies: list) -> tuple:
    """The library's copies of the film, and how they were found. A copy kept
    in the film's own folder is the film itself, or a version beside it on
    purpose, and is not a duplicate."""
    directory = os.path.dirname(os.path.abspath(movie))
    stem = os.path.splitext(os.path.basename(movie))[0]
    match = (plexnames.ID_TAG_RE.search(stem)
             or plexnames.ID_TAG_RE.search(os.path.basename(directory)))
    if match:
        candidates, how = index.get(match.group(0), []), "by its id " + match.group(0)
    else:
        base, _tag = plexnames.untagged_base(os.path.basename(directory))
        year = plexnames.year_of(base)
        title = _fold(plexnames.untitled_base(base, year))
        candidates = [copy for copy in copies if year and copy.film.year == year
                      and title in {_fold(name) for name in copy.film.titles}]
        how = "by title and year (the film carries no id)"
    elsewhere = [copy for copy in candidates
                 if not any(os.path.dirname(os.path.abspath(path)) == directory
                            for path in copy.files)]
    return elsewhere, how


def _compare_film(movie: str, index: dict, copies: list, log) -> tuple:
    """The film's verdict, and the files of its library copies."""
    log("Plex: %s" % os.path.basename(movie))
    found, how = _found(movie, index, copies)
    if not found:
        log("  not in the library (looked for %s)" % how)
        return "absent", []
    base, _tag = plexnames.untagged_base(os.path.basename(os.path.dirname(movie)))
    edition, part = plexnames.read_stem(base, os.path.splitext(
        os.path.basename(movie))[0])
    new = None if part else plexcompare.read_profile(movie, edition)
    verdicts = []
    for copy in found:
        log("  in the library (%s), found %s:" % (copy.section, how))
        for path in copy.files:
            log("    " + path)
        verdicts.append(_against(movie, new, part, copy, log))
    places = [path for copy in found for path in copy.files]
    # The film's own verdict: the most interesting of its copies'.
    for verdict in _INTEREST:
        if verdict in verdicts:
            return verdict, places
    return "unread", places


def _against(movie: str, new, part: str, copy, log) -> str:
    if part or len(copy.files) > 1:
        log("  a film split into parts: not compared")
        return "unread"
    if new is None:
        log("  the new file could not be read: not compared")
        return "unread"
    old = plexcompare.read_profile(copy.files[0], copy.edition) \
        if os.path.isfile(copy.files[0]) else None
    if old is None:
        log("  the library's file cannot be read from here (is its disk "
            "mounted?): not compared")
        return "unread"
    comparison = plexcompare.compare(new, old)
    log("  " + _VERDICTS[comparison.verdict][0])
    for reason in comparison.versions:
        log("    " + reason)
    for said, points in (("new", comparison.new_better),
                         ("existing", comparison.old_better)):
        for point in points:
            log("    %s has %s" % (said, _the(point)))
    if comparison.verdict != "different" and (
            comparison.verdict in ("mixed", "old")
            or abs(new.seconds - old.seconds) >= plexcompare.SAME_LENGTH):
        _sync(new, old, log)
    return comparison.verdict


def _the(point: str) -> str:
    """A point of the comparison as what one file has: "the better
    resolution: ..." for a weighing, "an isolated score track, ..." for a
    thing only it has."""
    if point.split(":")[0] in ("resolution", "HDR", "commentaries", "chapters") \
            or point.split(":")[0].endswith(" audio"):
        return "the better " + point
    return point


def _sync(new, old, log) -> None:
    gap = new.seconds - old.seconds
    if abs(gap) >= plexcompare.SAME_LENGTH:
        log("    the lengths differ by %.1fs (logos in front, most likely)" % gap)
    pair = plexcompare.audio_pair(new, old)
    if pair is None:
        log("    sync not measured: one of them has no soundtrack")
        return
    try:
        offset = plexcompare.measure_offset(new, old, pair)
    except ImportError:
        log("    sync not measured: numpy is not installed")
        return
    if not offset.measured:
        log("    sync not measured: no stretch of the two soundtracks matched")
        return
    if not offset.consistent:
        log("    WARNING: the offset changes along the film (%s): cut differently "
            "inside it, not only in front - muxing across needs a closer look"
            % ", ".join("%s at %s" % ("%+d ms" % ms if ms is not None else "no match",
                                      _clock(at))
                        for at, ms in zip(offset.at, offset.ms, strict=True)))
        return
    ms = offset.ms_median
    if abs(ms) < plexcompare.NEGLIGIBLE_MS:
        log("    in sync: the existing file's tracks can be muxed in as they are")
        return
    log("    WARNING: out of sync by %+d ms - mux the existing file's tracks in "
        "with mkvmerge --sync -1:%d in front of it" % (ms, ms))


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def _report(tally: dict, log) -> None:
    if not tally:
        log("Plex: no film found")
        return
    log("Plex: " + ", ".join("%d %s" % (len(tally[verdict]), said)
                             for verdict, (_long, said) in _VERDICTS.items()
                             if verdict in tally))
    for verdict in _INTEREST:
        for movie, places in tally.get(verdict, ()):
            log("Plex: %s: %s" % ("MIXED, needs a look" if verdict == "mixed"
                                  else _VERDICTS[verdict][1], movie))
            for path in places:
                log("Plex:   the library's: " + path)
