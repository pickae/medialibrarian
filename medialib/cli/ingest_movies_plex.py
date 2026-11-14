"""The Plex comparison: each film ingested looked for in the Plex library -
by the id tag the tagging gave it, by its title and year where it has none -
and, where the library has it already, said where, and held up against it
(see :mod:`medialib.lib.plexcompare`).

On its own (-p) nothing is changed, on disk or on the server: each film says
what an ingest would take from the library's copy (:mod:`medialib.lib.plexcarry`),
and what it would not and why. In a full ingest the carry-over has been made
by then (:mod:`medialib.cli.ingest_movies_carry`), and the verdict is of the
film as the run leaves it - a MIXED one still MIXED for what could not be
carried, said under it. The closing count lists every film the library has,
MIXED first, each with where its library copy is.
"""

from __future__ import annotations

import os
import re

from medialib.cli import ingest_movies as rules
from medialib.lib import (
    chapterdb,
    commentarytranscription,
    plexcarry,
    plexcompare,
    plexnames,
    plexserver,
)

__all__ = ["compare_roots", "folder_key", "timing_of", "transcripts_beside"]

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


def compare_roots(roots: list, log, keeps_for=None, carried=None) -> int:
    """Every film under each folder of ``roots`` compared with the library;
    1 where the server could not be asked at all.

    ``keeps_for`` says, of a film, which of the library copy's soundtracks
    its improved copy would keep (every language, when not given) - what the
    carry-over an ingest would make is worked out with. ``carried`` are the
    carry-overs a full ingest made, by :func:`folder_key`: a film with one is
    said with what it took rather than what it would."""
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
            verdict, places = _compare_film(movie, index, copies, log,
                                            keeps_for, carried or {})
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


def folder_key(movie: str) -> tuple:
    """A film as its carry-over is known by from the moment it is made to the
    end of the run: its folder's inode, which the tagging's renames keep."""
    try:
        found = os.stat(os.path.dirname(os.path.abspath(movie)))
    except OSError:
        return ()
    return found.st_dev, found.st_ino


def _fold(title: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", title.casefold())


def _index(copies: list) -> dict:
    index: dict = {}
    for copy in copies:
        for key in ({"{imdb-%s}" % copy.film.imdb} if copy.film.imdb else set()) \
                | ({"{tmdb-%s}" % copy.film.tmdb} if copy.film.tmdb else set()):
            index.setdefault(key, []).append(copy)
    return index


def _found(movie: str, index: dict, copies: list, tag: str = "") -> tuple:
    """The library's copies of the film, and how they were found. A copy kept
    in the film's own folder is the film itself, or a version beside it on
    purpose, and is not a duplicate. ``tag`` is the id the tagging will give a
    folder that carries none yet."""
    directory = os.path.dirname(os.path.abspath(movie))
    stem = os.path.splitext(os.path.basename(movie))[0]
    match = (plexnames.ID_TAG_RE.search(stem)
             or plexnames.ID_TAG_RE.search(os.path.basename(directory)))
    found_tag = match.group(0) if match else tag
    if found_tag:
        candidates, how = index.get(found_tag, []), "by its id " + found_tag
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


def _compare_film(movie: str, index: dict, copies: list, log,
                  keeps_for=None, carried=None) -> tuple:
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
    made = (carried or {}).get(folder_key(movie))
    verdicts = []
    for copy in found:
        log("  in the library (%s), found %s:" % (copy.section, how))
        for path in copy.files:
            log("    " + path)
        verdicts.append(_against(
            movie, new, part, copy, log, keeps_for,
            made if made is not None and made.library in copy.files else None))
    places = [path for copy in found for path in copy.files]
    # The film's own verdict: the most interesting of its copies'.
    for verdict in _INTEREST:
        if verdict in verdicts:
            return verdict, places
    return "unread", places


def _against(movie: str, new, part: str, copy, log, keeps_for=None,
             made: plexcarry.Plan | None = None) -> str:
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
    if made is not None and made.timing is not None:
        timing, untimed = made.timing, ""
    else:
        timing, untimed = timing_of(new, old)
    comparison = plexcompare.compare(new, old, timing)
    log("  " + _VERDICTS[comparison.verdict][0])
    for reason in comparison.versions:
        log("    " + reason)
    for said, points in (("new", comparison.new_better),
                         ("existing", comparison.old_better)):
        for point in points:
            log("    %s has %s" % (said, _the(point)))
    if timing is not None:
        log("    " + timing.describe(new.seconds))
    elif untimed and comparison.verdict != "different":
        log("    not in sync: " + untimed)
    if made is not None:
        for line in made.carried:
            log("    carried from it: " + line)
        for line in made.left:
            log("    not carried: " + line)
    elif comparison.verdict in ("mixed", "old"):
        would = plexcarry.plan(
            new, old, timing, untimed,
            keeps_for(movie) if keeps_for is not None else lambda _audio: True,
            transcripts_beside(old.path), chapterdb.existing_chapters(old.path))
        for line in would.carried:
            log("    an ingest would carry from it: " + line)
        for line in would.left:
            log("    an ingest would not carry: " + line)
    return comparison.verdict


def timing_of(new, old) -> tuple:
    """Where the old file's moments fall in the new one, and "" - or None,
    and why the two cannot be put in sync."""
    if not plexcompare.worth_measuring(new, old):
        return None, (plexcompare.editions_differ(new, old)
                      or "their lengths are a version apart")
    pair = plexcompare.audio_pair(new, old)
    if pair is None:
        return None, "one of them has no soundtrack"
    try:
        offset = plexcompare.measure_offset(new, old, pair)
    except ImportError:
        return None, "numpy is not installed to measure it"
    if not offset.measured:
        return None, "no stretch of the two soundtracks matched"
    timing = offset.timing()
    if timing is None:
        return None, ("the offset changes along the film (%s): cut "
                      "differently inside it" % ", ".join(
                          "%s at %s" % ("%+d ms" % ms if ms is not None
                                        else "no match", _clock(at))
                          for at, ms in zip(offset.at, offset.ms, strict=True)))
    return timing, ""


def transcripts_beside(library: str):
    """What finds a commentary's transcripts beside the library's copy at
    ``library``: ``(srt, two-letter language)`` for each, by the track number
    the transcription named them after."""
    directory = os.path.dirname(library)
    stem = os.path.splitext(os.path.basename(library))[0]
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []

    def find(audio) -> tuple:
        prefix = commentarytranscription.commentary_prefix(stem, audio.track)
        best: dict = {}
        for name in names:
            if not (name.startswith(prefix) and name.endswith(".srt")):
                continue
            language = name[:-len(".srt")].rpartition(".")[2]
            if len(language) != 2 or not language.isalpha():
                continue
            if len(name) >= len(best.get(language, "")):
                best[language] = name
        return tuple((os.path.join(directory, name), language)
                     for language, name in sorted(best.items()))
    return find


def _the(point: str) -> str:
    """A point of the comparison as what one file has: "the better
    resolution: ..." for a weighing, "an isolated score track, ..." for a
    thing only it has."""
    if point.split(":")[0] in ("resolution", "HDR", "commentaries", "chapters") \
            or point.split(":")[0].endswith(" audio"):
        return "the better " + point
    return point


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
