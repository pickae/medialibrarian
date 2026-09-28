"""Chapter marks for a film that has none, or only numbered ones, from the
ChapterDB archive.

ChapterDB was the database ChapterGrabber filled: chapter times read off a DVD or
Blu-ray, with the scene names from its menu typed in beside them. The site went
read-only and lives on as an archive at :data:`SITE`, whose search page and
per-set downloads still answer (its JSON API wants an account key, and sits
behind a wall that refuses a script). Nothing can be added to it any more, so a
recent film is simply not in it.

A set is only ever as good as the disc it was read from, and the archive holds
every disc of a film: a DVD and a Blu-ray, a theatrical and an extended cut, a
PAL transfer running 4% fast. Chapter times from the wrong one land every mark
in the wrong scene. So a set has to be **the same length as the file** before it
is looked at, within a couple of seconds - the only thing a rip and the disc it
came from agree on exactly - and then pass the rest of :func:`plausible`.

What a film is given, by what it already has:

  no chapters         the best set with names, else the best numbered one
  numbered chapters   a named set REPLACING them - but only one whose every
                      chapter starts where the film's own do, so that what
                      changes is the names and nothing else; marks the film
                      already has are not thrown away for different ones
  named chapters      nothing - and nothing is asked

Written with ``mkvpropedit``, in place: chapters are one element of the
container, and the film is not remuxed for them. ``--chapters`` replaces every
chapter the file had, so a film never ends up carrying two sets.
"""

from __future__ import annotations

import html
import itertools
import json
import os
import re
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import NamedTuple

from medialib.lib import plexnames, politepacing, subtitlefiles, titlematch

__all__ = ["SITE", "ChapterSet", "Existing", "add_chapters", "apply_set",
           "ask_archive",
           "choose", "empty_outcome", "examine", "existing_chapters",
           "film_title", "find_set", "findings", "is_named", "parse_set",
           "parse_search", "plausible", "search", "write_chapters"]

SITE = "https://chapterdb.plex.tv"

# Where the archive is asked instead, when set: the suite points it at a port
# nothing listens on, so a CLI case that runs the whole ingest cannot reach the
# real one.
SITE_VARIABLE = "chapterDbSite"

# The archive is one person's leftover server rather than a service with a
# stated limit, so it is asked at the pace a person clicking through it would
# ask, and no faster: a few seconds apart, give or take one.
MIN_REQUEST_INTERVAL = 3.0
REQUEST_JITTER = 1.0

# How many rows a search page is asked for, and how many pages are read. A
# short title matches a great deal - one common word is several hundred sets -
# and the pages come most-confirmed first, so a set past the last page read is
# one nobody vouched for.
PAGE_SIZE = 100
MAX_PAGES = 5

# How many sets are downloaded to be judged in full. The search page's own
# duration has already ruled out every other cut, so what is left is a handful
# of people who ripped the same disc.
MAX_CANDIDATES = 8

# How far a set's duration may be from the file's. A rip is the disc's title
# written out, so the two agree to a frame; the slack is for a DVD's IFO, which
# rounds, and a set typed in by hand. A different cut is minutes away, and a
# PAL transfer of a two-hour film five.
DURATION_TOLERANCE_SECONDS = 2.0

# The search page shows its durations to the second, cut rather than rounded,
# so its filter is wider than the final one and never refuses a set the XML
# would have passed.
LISTED_TOLERANCE_SECONDS = DURATION_TOLERANCE_SECONDS + 2.0

# The fewest marks worth writing. Two is a film and its end credits, which a
# player's seek bar already does better.
MIN_CHAPTERS = 3

# A first mark that is not at the start is a set someone typed out from a
# menu, starting at the first scene they could name.
MAX_FIRST_START_SECONDS = 1.0

# Where the last mark must at least reach, as a share of the film. A set whose
# marks all sit in the first minutes is a disc's menu loop, or times typed into
# the wrong field.
MIN_LAST_START_SHARE = 0.5

# How far a chapter of a named set may start from the film's own numbered one
# and still be the same mark. A disc's chapter falls on a frame, and a rip of
# it keeps that frame; the second is for a set read off a DVD, whose times are
# rounded. Chapters placed by anything else - every five minutes, by a scene
# cut - are nowhere near a disc's.
MARK_TOLERANCE_SECONDS = 1.0

# A name that only numbers its chapter, in the six languages - or no more than
# a number, a time, or nothing. Matched whole, so "Chapter 1: The Arrival" is a
# name. A roman numeral only after the word: on its own, "Mix" is one.
_ROMAN = r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_GENERIC = re.compile(
    r"(?:chapter|chap|ch|kapitel|chapitre|capitulo|capítulo|capitolo|"
    r"hoofdstuk|scene|scène|szene|escena|scena|part|teil|partie|parte|deel|"
    r"track)\s*[.:#-]?\s*(?:[0-9]+|" + _ROMAN + r")?\s*[.:]?|"
    r"[0-9]*\s*[.:]?|"
    r"[0-9]{1,2}(?::[0-9]{2}){1,2}(?:[.,][0-9]+)?",
    re.IGNORECASE)

# The words a disc label or a release name puts after the title: what makes
# "Nordwind (1998) HDDVD" or "Falcons.Forever.Rising.2007.EXTENDED" the
# film it is a set of. Cut at the year, which is where a title always ends.
_TRAILING_YEAR = re.compile(r"\s*[(\[]?[12][0-9]{3}[)\]]?(?:\s.*)?$")

_ROW = re.compile(
    r'<td[^>]*>\s*([^<]*?)\s*</td>\s*'
    r'<td><a href="/browse/([0-9]+)">([^<]*)</a></td>\s*'
    r'<td[^>]*>\s*([0-9]+):([0-9]+)\.([0-9]+)\s*</td>')

_PACER = politepacing.Pacer(MIN_REQUEST_INTERVAL, REQUEST_JITTER)


class Row(NamedTuple):
    """One line of a search page: enough to decide whether to download it."""
    set_id: str
    title: str
    source: str
    seconds: float


class ChapterSet(NamedTuple):
    """One set, as its XML download has it."""
    set_id: str
    title: str
    source: str
    language: str
    confirmations: int
    seconds: float
    chapters: tuple  # of (start seconds, name)

    @property
    def named(self) -> bool:
        return is_named([name for _start, name in self.chapters])


class Existing(NamedTuple):
    """What a film already has: its length, and its chapters' names and
    starts."""
    seconds: float
    names: tuple
    starts: tuple = ()

    @property
    def kind(self) -> str:
        """"none", "numbered" or "named" - which decides what may replace it."""
        if not self.names:
            return "none"
        # Any real name at all and the set is somebody's work: a file that
        # names half its chapters was named by hand, and is left alone.
        if any(not _GENERIC.fullmatch(name.strip()) for name in self.names):
            return "named"
        return "numbered"


# --- the network --------------------------------------------------------------

def reset_rate_limit() -> None:
    """Forget when the last request went out. For a test that would otherwise
    pay the interval."""
    _PACER.reset()


def ask_archive(path: str, params=()) -> str | None:
    """One GET of the archive, or ``None`` when it did not answer.

    Nothing secret goes with it, so unlike the TMDb lookup the query rides in
    argv.
    """
    _PACER.wait()
    site = os.environ.get(SITE_VARIABLE) or SITE
    argv = ["curl", "-fsSG", "--max-time", "30", site + path]
    for key, value in params:
        argv += ["--data-urlencode", "%s=%s" % (key, value)]
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")


def parse_search(page: str) -> list:
    """The rows of one search page."""
    rows = []
    for match in _ROW.finditer(page):
        source, set_id, title, hours, minutes, seconds = match.groups()
        rows.append(Row(set_id, html.unescape(title).strip(),
                        html.unescape(source),
                        int(hours) * 3600 + int(minutes) * 60 + int(seconds)))
    return rows


def search(title: str, fetch: Callable | None = None) -> list | None:
    """Every row the archive holds under ``title``, or ``None`` when it could
    not be asked at all - which is not the same answer as no rows."""
    fetch = fetch or ask_archive
    rows: list = []
    for page in range(1, MAX_PAGES + 1):
        body = fetch("/browse", [("title", title), ("pageSize", PAGE_SIZE),
                                 ("page", page)])
        if body is None:
            return None if page == 1 else rows
        found = parse_search(body)
        rows += found
        if len(found) < PAGE_SIZE:
            break
    return rows


def _seconds(stamp: str) -> float:
    """``HH:MM:SS[.fraction]`` as seconds; a malformed one as -1, which no
    check passes."""
    parts = stamp.strip().split(":")
    try:
        if len(parts) != 3:
            raise ValueError(stamp)
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return -1.0


def parse_set(set_id: str, text: str) -> ChapterSet | None:
    """One set's XML download, or ``None`` when it is not one."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    # The namespace is read off the document rather than spelled out here:
    # every element of a set is in the one its root declares.
    namespace, _brace, local = root.tag.rpartition("}")
    if local != "chapterInfo":
        return None
    ns = namespace + "}" if namespace else ""
    try:
        confirmations = int(root.get("confirmations") or 0)
    except ValueError:
        confirmations = 0
    chapters = tuple(
        (_seconds(chapter.get("time") or ""), (chapter.get("name") or "").strip())
        for chapter in root.iter(ns + "chapter"))
    return ChapterSet(
        set_id=set_id,
        title=(root.findtext(ns + "title") or "").strip(),
        source=(root.findtext(ns + "source/" + ns + "type") or "").strip(),
        language=root.get("{http://www.w3.org/XML/1998/namespace}lang") or "",
        confirmations=confirmations,
        seconds=_seconds(root.findtext(ns + "source/" + ns + "duration")
                         or ""),
        chapters=chapters)


# --- the judgement ------------------------------------------------------------

def is_named(names) -> bool:
    """Whether a set's names say something: every one of them a real name,
    and no two the same. A set that names three chapters and numbers the rest
    is a set somebody gave up on, and its numbered ones would be written into
    the film as they are."""
    names = [name.strip() for name in names]
    if not names or any(_GENERIC.fullmatch(name) for name in names):
        return False
    return len({name.casefold() for name in names}) == len(names)


def _comparable(title: str) -> str:
    """A set's title the way a release or a disc label wrote it, read as a
    title: separators as spaces, and everything from the year on cut away."""
    spaced = " ".join(re.sub(r"[._]+", " ", title).split())
    return _TRAILING_YEAR.sub("", spaced) or spaced


def same_title(film: str, row_title: str) -> bool:
    """Whether a set is filed under this film's title."""
    return (titlematch.equivalent(film, row_title)
            or titlematch.equivalent(film, _comparable(row_title)))


def plausible(chapter_set: ChapterSet, film_seconds: float) -> str:
    """Why ``chapter_set`` cannot be this film's chapters, or "" when it can."""
    if chapter_set.seconds <= 0:
        return "states no duration"
    if abs(chapter_set.seconds - film_seconds) > DURATION_TOLERANCE_SECONDS:
        return "runs %.1fs, the film %.1fs" % (chapter_set.seconds,
                                               film_seconds)
    starts = [start for start, _name in chapter_set.chapters]
    if len(starts) < MIN_CHAPTERS:
        return "has %d chapter(s)" % len(starts)
    if any(start < 0 for start in starts):
        return "has a malformed time"
    if starts[0] > MAX_FIRST_START_SECONDS:
        return "does not start at the start"
    if any(later <= earlier for earlier, later in itertools.pairwise(starts)):
        return "is not in order"
    if starts[-1] >= min(film_seconds, chapter_set.seconds):
        return "has a chapter past the end"
    if starts[-1] < film_seconds * MIN_LAST_START_SHARE:
        return "has every chapter in the first half"
    return ""


def _at_the_end(start: float, end: float) -> bool:
    """Whether a mark sits at the very end: some discs close their title with
    an empty chapter, and a mark a player can only land on as the film stops
    is not one to write, nor one to compare. Only within a second of the end -
    a mark well past it is left for :func:`plausible` to refuse the set over."""
    return end - 1.0 <= start <= end + DURATION_TOLERANCE_SECONDS


def _trimmed(chapter_set: ChapterSet, film_seconds: float) -> ChapterSet:
    """``chapter_set`` without a mark at the very end."""
    end = min(film_seconds, chapter_set.seconds)
    kept = tuple(chapter for chapter in chapter_set.chapters
                 if not _at_the_end(chapter[0], end))
    return chapter_set._replace(chapters=kept)


def same_marks(chapter_set: ChapterSet, existing: Existing) -> bool:
    """Whether ``chapter_set`` has the chapters the film already has: as many,
    each starting within :data:`MARK_TOLERANCE_SECONDS` of the film's own.

    What makes replacing numbered chapters worth doing at all. The film's
    marks are the ones its rip kept, so a set that puts them anywhere else
    is a different authoring, or a different disc of the same length - and
    names are no reason to give up marks that were right for ones that may
    not be.
    """
    theirs = [start for start, _name in chapter_set.chapters]
    ours = [start for start in existing.starts
            if not _at_the_end(start, existing.seconds)]
    return len(theirs) == len(ours) and all(
        abs(one - other) <= MARK_TOLERANCE_SECONDS
        for one, other in zip(theirs, ours, strict=True))


def choose(sets, existing: Existing) -> ChapterSet | None:
    """The set this film should be given, or ``None``.

    Only sets :func:`plausible` passes, and of those a named one over a
    numbered one - a numbered one only for a film that has no chapters at all,
    and for a film with numbered ones only a named set with
    :func:`same_marks`.
    Then English names, since there is no knowing which of the others a
    library would read; then the set more people confirmed; then the closer
    length.
    """
    if existing.kind == "named":
        return None
    fitting = [candidate for candidate in
               (_trimmed(each, existing.seconds) for each in sets)
               if not plausible(candidate, existing.seconds)]
    if existing.kind == "numbered":
        fitting = [candidate for candidate in fitting
                   if candidate.named and same_marks(candidate, existing)]
    if not fitting:
        return None
    return min(fitting, key=lambda candidate: (
        not candidate.named, candidate.language != "eng",
        -candidate.confirmations, abs(candidate.seconds - existing.seconds)))


# --- the film -----------------------------------------------------------------

def film_title(movie: str) -> str:
    """The title a tagged film's folder carries, or "" for a folder the
    tagging has not named: an untagged folder's title is a guess, and chapters
    from a namesake of the same length would be written in as fact."""
    folder = os.path.basename(os.path.dirname(os.path.abspath(movie)))
    base, tag = plexnames.untagged_base(folder)
    if not tag:
        return ""
    return plexnames.untitled_base(base, plexnames.year_of(base)).strip()


def existing_chapters(movie: str) -> Existing | None:
    """The film's length and its chapters' names and starts, or ``None``
    when ffprobe cannot read it."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-show_entries",
             "format=duration", "-of", "json", movie],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    try:
        probe = json.loads(done.stdout.decode("utf-8", "replace"))
        seconds = float(probe.get("format", {}).get("duration") or 0)
    except (ValueError, AttributeError):
        return None
    chapters = probe.get("chapters") or []
    try:
        starts = tuple(float(chapter.get("start_time") or 0)
                       for chapter in chapters)
    except ValueError:
        return None
    names = tuple((chapter.get("tags") or {}).get("title", "")
                  for chapter in chapters)
    return Existing(seconds, names, starts)


def _stamp(seconds: float) -> str:
    nanoseconds = round(seconds * 1e9)
    whole, fraction = divmod(nanoseconds, 1_000_000_000)
    return "%02d:%02d:%02d.%09d" % (whole // 3600, whole // 60 % 60,
                                    whole % 60, fraction)


def chapters_xml(chapter_set: ChapterSet) -> str:
    """The set as a Matroska chapter file. A numbered set is written with names
    of this library's own, so every numbered film reads alike."""
    language = chapter_set.language or "und"
    chapters = ET.Element("Chapters")
    edition = ET.SubElement(chapters, "EditionEntry")
    for number, (start, name) in enumerate(chapter_set.chapters, 1):
        atom = ET.SubElement(edition, "ChapterAtom")
        ET.SubElement(atom, "ChapterTimeStart").text = _stamp(start)
        display = ET.SubElement(atom, "ChapterDisplay")
        ET.SubElement(display, "ChapterString").text = (
            name if chapter_set.named else "Chapter %02d" % number)
        ET.SubElement(display, "ChapterLanguage").text = language
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(chapters, encoding="unicode") + "\n")


def write_chapters(movie: str, chapter_set: ChapterSet) -> bool:
    """Replace every chapter ``movie`` has with ``chapter_set``, in place,
    and say whether the film now has them.

    mkvpropedit exits 1 for warnings with the edit made, and 2 when it could
    not make it.
    """
    handle, path = tempfile.mkstemp(suffix=".xml", prefix="chapters-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(chapters_xml(chapter_set))
        try:
            done = subprocess.run(["mkvpropedit", "--quiet", movie,
                                   "--chapters", path],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL)
        except OSError:
            return False
        if done.returncode not in (0, 1):
            return False
    finally:
        os.remove(path)
    # Read back, because a zero exit only says mkvpropedit had no complaint:
    # the film is what is supposed to have changed.
    written = existing_chapters(movie)
    return written is not None and len(written.names) == len(
        chapter_set.chapters)


def _ascii(title: str) -> str:
    """The title with its accents dropped: how most of the archive, typed on
    an English keyboard, spells it."""
    return (unicodedata.normalize("NFKD", title).encode("ascii", "ignore")
            .decode("ascii"))


def find_set(title: str, existing: Existing,
             fetch: Callable | None = None) -> tuple:
    """(the set for this film or ``None``, whether the archive answered)."""
    fetch = fetch or ask_archive
    queries = [title] + ([_ascii(title)] if _ascii(title) != title else [])
    rows: list = []
    for query in queries:
        found = search(query, fetch)
        if found is None:
            return None, False
        rows = [row for row in found if same_title(title, row.title)
                and abs(row.seconds - existing.seconds)
                <= LISTED_TOLERANCE_SECONDS]
        if rows:
            break
    sets = []
    for row in rows[:MAX_CANDIDATES]:
        body = fetch("/browse/%s.xml" % row.set_id)
        parsed = parse_set(row.set_id, body) if body is not None else None
        if parsed is not None:
            sets.append(parsed)
    return choose(sets, existing), True


def examine(movie: str) -> tuple:
    """(verdict, title, existing) for one film, without asking the archive
    anything: "untagged", "failed" or "kept" when it is not to be looked up,
    and "" when it is - by ``title``, for a film with ``existing``."""
    title = film_title(movie)
    if not title:
        return "untagged", "", None
    existing = existing_chapters(movie)
    if existing is None or existing.seconds <= 0:
        return "failed", title, None
    if existing.kind == "named":
        return "kept", title, existing
    return "", title, existing


def findings(directory: str, log: Callable[[str], None],
             fetch: Callable | None = None):
    """Every film under ``directory`` as ``(movie, verdict, existing, set)``:
    the verdicts :func:`examine` comes to, "unmatched" for a film the archive
    has no fitting set for, and "found" with the set it has.

    ``fetch`` is how the archive is asked, :func:`ask_archive` unless a caller
    has a way of its own - pages it asked for ahead of need. The first time the
    archive does not answer, the rest of the films are not asked about: it is
    down, or it has started refusing, and either way asking again per film
    only waits out the timeout a few hundred times.
    """
    for movie in subtitlefiles.subtitle_movies(directory):
        verdict, title, existing = examine(movie)
        if verdict == "failed":
            log("WARNING: could not read the length of, skipping: " + movie)
        if verdict:
            yield movie, verdict, existing, None
            continue
        chosen, answered = find_set(title, existing, fetch)
        if not answered:
            log("WARNING: the chapter archive at %s did not answer - no more "
                "films are looked up this run"
                % (os.environ.get(SITE_VARIABLE) or SITE))
            return
        yield (movie, "unmatched" if chosen is None else "found", existing,
               chosen)


def apply_set(movie: str, existing: Existing, chosen: ChapterSet,
              log: Callable[[str], None]) -> str:
    """Write the set a film was found, and say what came of it: "added",
    "replaced" or "failed"."""
    verdict = "replaced" if existing.kind == "numbered" else "added"
    what = "%d %s chapters (set %s, %s, %d confirmation(s))" % (
        len(chosen.chapters), "named" if chosen.named else "numbered",
        chosen.set_id, chosen.source or "unknown source",
        chosen.confirmations)
    if write_chapters(movie, chosen):
        log("Chapters %s, %s: %s" % (verdict, what, movie))
        return verdict
    log("WARNING: mkvpropedit could not write the chapters, left as it was: "
        + movie)
    return "failed"


def empty_outcome() -> dict:
    """The films by what came of them, before any has come to anything."""
    return {"added": [], "replaced": [], "unmatched": [], "kept": [],
            "untagged": [], "failed": []}


def add_chapters(directory: str, log: Callable[[str], None],
                 fetch: Callable | None = None) -> dict:
    """Give every tagged film under ``directory`` the chapters :func:`choose`
    picks for it, one film at a time, and return the films by what came of
    them. ``fetch`` is :func:`findings`'."""
    outcome = empty_outcome()
    for movie, verdict, existing, chosen in findings(directory, log, fetch):
        if verdict == "found":
            verdict = apply_set(movie, existing, chosen, log)
        outcome[verdict].append(movie)
    return outcome
