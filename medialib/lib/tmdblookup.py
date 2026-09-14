"""TMDb-backed IMDb id lookup, and Plex/Jellyfin id tagging.

Ask an external API what a film is, and only rename when the answer is certain.

The id lookup shells out to ``curl`` exactly the way the bash does. What is
rewritten is the glue - the JSON navigation (``json`` instead of ``jq``), the
certainty rule, and the folder rename - which is where a port can drift from
the original.

What counts as the same title is :mod:`medialib.lib.titlematch`, which folds a
written title to the keys it may be matched by; this module asks whether a
candidate's keys meet the folder's.
"""

import functools
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from typing import NamedTuple

from medialib.lib import durationcheck, plexnames, safety, titlematch
from medialib.lib.titlematch import normalize_title, title_keys

# The TMDb endpoint everything is relative to.
_BASE = "https://api.themoviedb.org/3"

# A movie folder is "Title (Year)" where the Year is 1xxx or 2xxx. The greedy
# title takes everything up to the LAST "(Year)", so "Batman (1999) (2005)"
# reads as title "Batman (1999)", year "2005".
_YEAR_RE = re.compile(r"^(.+) \(([12][0-9]{3})\)$")

# The same thing with one of its brackets lost - to a rename that cut the name
# short, or to a filesystem that would not take it. Both halves are repaired,
# and only ever where a bracket is still THERE: a trailing number with no
# bracket at all is as likely to be part of the title as a year, and "Blade
# Runner 2049" is not a film from the year 2049.
_HALF_YEAR = (
    re.compile(r"^(.+?)\s*\(\s*([12][0-9]{3})\s*$"),
    re.compile(r"^(.+?)\s+([12][0-9]{3})\s*\)\s*$"),
)


def repair_year(base: str) -> str:
    """``base`` with its year parentheses put back, or ``base`` unchanged."""
    if _YEAR_RE.match(base):
        return base
    for pattern in _HALF_YEAR:
        match = pattern.match(base)
        if match:
            return "%s (%s)" % (match.group(1).rstrip(), match.group(2))
    return base


def read_folder(name: str) -> tuple:
    """A folder name read as (base, tag, title, year), or four empties.

    The one place a folder name is taken apart, so what counts as a film folder
    is the same question wherever it is asked - the walk that decides which
    folders to descend into, and the tagging that decides what to look up.
    """
    base, tag = plexnames.untagged_base(name)
    base = repair_year(base)
    match = _YEAR_RE.match(base)
    if not match:
        return "", "", "", ""
    return base, tag, match.group(1), match.group(2)


# What TMDb asks of a caller: about ten requests a second. One film costs
# several - the search, an alternative-titles call per candidate, and the
# external-ids call - so a library goes through them far faster than a person
# would, and the limit is worth keeping to rather than finding out where the
# service draws it.
MAX_REQUESTS_PER_SECOND = 10.0

# The moment the last request went out, so the next can wait out the remainder
# of its slot. A list rather than a global name, the way the iconv answer above
# is kept, so it can be reset without a `global` statement.
_LAST_REQUEST: list[float] = []


def reset_rate_limit() -> None:
    """Forget when the last request went out, so the next one goes immediately.
    For a test that would otherwise pay the interval."""
    _LAST_REQUEST.clear()


def _wait_for_a_slot() -> None:
    """Hold the next request back until its slot comes round.

    A plain minimum interval rather than a rolling window: the calls here are
    sequential, so spacing each one evenly IS the rate, and it needs no history
    to be kept.
    """
    interval = 1.0 / MAX_REQUESTS_PER_SECOND
    now = time.monotonic()
    if _LAST_REQUEST:
        waiting = _LAST_REQUEST[0] + interval - now
        if waiting > 0:
            time.sleep(waiting)
            now = time.monotonic()
    _LAST_REQUEST[:] = [now]


# The escapes a curl config file's double-quoted value has, and all it has.
_CONFIG_ESCAPES = (("\\", "\\\\"), ('"', '\\"'), ("\t", "\\t"),
                   ("\n", "\\n"), ("\r", "\\r"), ("\v", "\\v"))


def _config_value(text: str) -> str:
    """One value as a curl config file spells it: double-quoted, with the six
    escapes that syntax has applied."""
    for character, escape in _CONFIG_ESCAPES:
        text = text.replace(character, escape)
    return '"' + text + '"'


def _curl(url: str, params) -> str | None:
    """One ``curl -fsSG`` call, the module's only way out to the network.

    ``params`` is an ordered (key, value) pair list, each becoming a
    ``--data-urlencode key=value``. Returns the body on success, or ``None``
    when curl fails (the ``-f`` makes an HTTP error a non-zero exit, which is
    how the caller tells "no answer" from an answer).

    Paced to :data:`MAX_REQUESTS_PER_SECOND` before it goes out, which is where
    the whole module's traffic is metered: every question reaching TMDb is one
    of these calls.

    Handed to curl through ``--config -`` rather than as argv, because one of
    those params is the API key and argv is not private: /proc/<pid>/cmdline is
    readable by every account on the machine, and a run over a library opens
    that window once per candidate folder. On stdin it reaches curl and nothing
    else.
    """
    _wait_for_a_slot()
    lines = ["url = " + _config_value(url), "get", "fail", "silent",
             "show-error"]
    for key, value in params:
        lines.append("data-urlencode = "
                     + _config_value("{}={}".format(key, value)))
    proc = subprocess.run(["curl", "--config", "-"],
                          input="\n".join(lines).encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _as_json(text: str | None):
    """A TMDb body as a dict, or an empty dict when it is not valid JSON.

    The bash pipes every body through ``jq`` with a ``?``/``//`` guard, which
    reads a non-object as "nothing"; an empty dict is the same here.
    """
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# How far a film on disk may be from the runtime TMDb states and still be the
# same film, whichever of the two is larger. The five minutes are what the two
# figures are MADE of rather than any disagreement about the film: TMDb rounds
# to whole minutes, and a release carries its distributor's logos and its
# credits where a catalogue counts the feature. The fraction is for a long one,
# where a PAL transfer's 4% speed-up is four minutes on its own.
RUNTIME_TOLERANCE_SECONDS = 300.0
RUNTIME_TOLERANCE_FRACTION = 0.07

# How far the folder's year may be from a release year when the runtime is what
# vouches for the match: the off-by-one of a production or festival year
# written where a release year was meant, and no further.
NEAR_YEARS = 1

# How many of the search results are worth asking about in detail. They arrive
# most popular first, and a film that is not in the first handful of answers to
# its own title is not one a certainty rule is going to settle - while every
# one of them costs a request.
MAX_CANDIDATES = 8

# The three documents a candidate is judged on, asked for with the candidate
# itself so that a candidate costs one request: its alternative titles, its
# IMDb id, and every country's release date.
_APPENDED = "alternative_titles,external_ids,release_dates"


class _Candidate(NamedTuple):
    """One film TMDb offered, with everything the certainty rule asks of it."""

    years: frozenset        # every year some country released it in
    titles: frozenset       # its primary, original and alternative titles, folded
    spellings: tuple        # those same titles as TMDb writes them, in its order
    runtime: float          # minutes, 0.0 when TMDb does not say
    imdb: str               # "" when it has no usable IMDb id


class Match(NamedTuple):
    """What a lookup settled on: the id to tag with, and the spelling to use.

    ``title`` is the catalogue's own writing of whichever title MATCHED, which
    is not the same as the catalogue's primary title: a French film found under
    its French name is named in French, because that is the name the match was
    made on. It is "" when nothing was matched, and when the catalogue's
    spelling cannot be a file name.
    """

    imdb: str = ""
    title: str = ""


def tmdb_imdb_id(title: str, year: str,
                 runtime: Callable[[], float] | None = None) -> str:
    """The IMDb id (``ttXXXXXXX``) of a film, or "" - :func:`identify` without
    the spelling, for a caller that only wants to know which film this is."""
    return identify(title, year, runtime).imdb


def identify(title: str, year: str,
             runtime: Callable[[], float] | None = None,
             notes: list | None = None, also: tuple = ()) -> Match:
    """A film's id and catalogue spelling, ONLY when the match is unambiguous.

    Returns an empty :class:`Match` (never a half-certain id) when it is not
    certain enough to rename on, so a caller can gate on an empty result and
    leave the film untouched.

    Certain, in the ordinary case: exactly one film that carries the wanted
    title - on its primary, original or any alternative - and was released in
    the wanted year. A film's year is every year ANY country released it in
    rather than only the one TMDb prints as its primary date, because a
    festival premiere and a home release a year apart are one film with two
    true years and a folder may have been named from either.

    "Carries the wanted title" is :mod:`medialib.lib.titlematch`'s question, not
    a string compare: an accent, an em dash, a roman numeral, a dropped article
    and a written-out "&" are all the same title said differently, and each of
    them is what a library and a catalogue routinely disagree about.

    ``runtime`` is asked - and only asked - when that rule does not settle it:
    how long the film on the disk is, in seconds, or 0.0 when nothing can say.
    A length is what stands in for the year when the year has been given up on,
    and for the title when two films share one: a candidate is picked out of
    several only when the file's length fits it and RULES OUT every other, so a
    candidate nobody can measure keeps the answer at "not certain" rather than
    losing to one that could be.

    A title nothing was found under is asked again under the other spellings
    :func:`medialib.lib.titlematch.search_titles` offers - the filler a local
    library wrote in front of it, the franchise repeated on every film in it,
    its roman numerals as numbers. Only asked again, though: each spelling goes
    through the same certainty rule, and the first that settles is the answer.

    ``notes``, when a list is passed, collects what was asked and what came
    back: the queries, the films TMDb offered under each, and the reason every
    one of them was not the answer. Nothing decides on it - it is what a dry run
    writes down so that "no confident match" can be read rather than taken on
    trust, and so a rule that is too narrow shows up as a page of candidates
    that were obviously the film.

    ``also`` are further titles this folder might be the film of - the one the
    folder above it makes when its name is read as the first half of the title.
    They count both ways: each is asked about in its own right, and a candidate
    that carries one of them carries this film's title. The folder's own comes
    first either way, so a film findable under its own name never pays for them.
    """
    api_key = os.environ.get("tmdbApiKey", "")
    if not api_key:
        return Match()
    reading = (title,) + tuple(other for other in also if other)
    want = frozenset().union(*(title_keys(one) for one in reading))
    if not want:
        return Match()

    for query in _queries(reading):
        found = _under(api_key, query, want, year, runtime, notes)
        if found.imdb:
            return found
    return Match()


def _queries(reading: tuple) -> list:
    """Every spelling to ask about, over every title this folder may be of.

    Deduplicated by FOLD across all of them, so a longer reading that only adds
    punctuation to a shorter one is not a second request for the same answer.
    """
    asked: list = []
    folds = set()
    for one in reading:
        for query in titlematch.search_titles(one):
            folded = normalize_title(query)
            if folded and folded not in folds:
                asked.append(query)
                folds.add(folded)
    return asked


def _under(api_key: str, query: str, want: frozenset, year: str,
           runtime: Callable[[], float] | None,
           notes: list | None = None) -> Match:
    """The certainty rule over one query's results.

    ``query`` is what TMDb is asked; ``want`` stays the folder's own keys
    throughout, because what is being asked about is still that film - a wider
    query is a way of REACHING the candidate, never a looser test of it.
    """
    rows = _search(api_key, query, year)
    if rows is None:
        _note(notes, 'asked "%s": the search itself failed' % query)
        return Match()
    if not rows:
        # Nothing at all for that year: ask again without one, and let the
        # rules below decide what the year is worth. This is the search that
        # finds a film whose folder was named from a year no release of it
        # happened in.
        rows = _search(api_key, query, "") or []

    _note(notes, 'asked "%s": %d result(s)' % (query, len(rows)))
    worth = _worth_asking(rows, year, want)
    _note_the_passed_over(notes, rows, worth)
    candidates = [_candidate(api_key, row) for row in worth]
    named = [row for row in candidates if want & row.titles]
    _note_the_unnamed(notes, candidates, named)
    dated = [row for row in named if year in row.years]
    if len(dated) == 1:
        return _matched(dated[0], want)

    # Several films of one title and one year - a remake released the same year,
    # or one release recorded twice - or, where the year settled nothing at all,
    # the ones that came out either side of it.
    contenders = dated or [row for row in named
                           if any(_near(y, year) for y in row.years)]
    if not contenders:
        for row in named:
            _note(notes, "    %s - carries the title, but came out %s, not %s"
                  % (_says(row), ", ".join(sorted(row.years)) or "nowhere",
                     year))
        return Match()
    # The one thing that reads the disk, and it is read here and nowhere
    # earlier: the probe is paid for only by the films the plain rule could not
    # name on its own.
    seconds = runtime() if runtime is not None else 0.0
    settled = _settled_by_runtime(contenders, seconds)
    if settled is None:
        _note_the_lengths(notes, contenders, seconds)
    return _matched(settled, want) if settled is not None else Match()


def _note(notes: list | None, line: str) -> None:
    if notes is not None:
        notes.append(line)


def _says(candidate: _Candidate) -> str:
    """One candidate as a line of the near-miss list names it.

    Named by the first title it has that is a title: a document the detail call
    could not fetch leaves the literal word "null" where each of its two
    titles should be, and a list of films called "null" says nothing about
    any of them.
    """
    titles = _written(candidate)
    return '"%s" (%s) %s' % (titles[0] if titles else "no title",
                             ", ".join(sorted(candidate.years)) or "no year",
                             candidate.imdb or "no imdb id")


def _written(candidate: _Candidate) -> list:
    """The titles a candidate actually carries, without jq's word for the ones
    it does not."""
    return [t for t in candidate.spellings if t and t != "null"]


def _note_the_passed_over(notes: list | None, rows: list, worth: list) -> None:
    """The results that never cost a request of their own, and why.

    The ones most worth reading in the list: a film whose document was never
    asked for is one the search itself offered and the cheap filter set aside.
    """
    if notes is None:
        return
    kept = {id(row) for row in worth}
    for row in rows:
        if not isinstance(row, dict) or id(row) in kept:
            continue
        release = row.get("release_date")
        _note(notes, '    passed over: "%s" (%s) - neither its title nor its '
              "year was close" % (row.get("title"),
                                  (release or "")[:4] or "no year"))


def _note_the_unnamed(notes: list | None, candidates: list, named: list) -> None:
    """The candidates whose titles did not meet the folder's, with the titles
    they do carry - which is the line that says a reading is missing."""
    if notes is None:
        return
    met = {id(row) for row in named}
    for row in candidates:
        if id(row) not in met:
            _note(notes, "    %s - no title of its meets this folder's: %s"
                  % (_says(row), "; ".join(_written(row)[:4])))


def _note_the_lengths(notes: list | None, contenders: list,
                      seconds: float) -> None:
    """Why a length settled nothing, which is either that several fit or that
    one of them could not be measured at all."""
    if notes is None:
        return
    if seconds <= 0:
        _note(notes, "    %d candidate(s) left and nothing on disk to measure"
              % len(contenders))
        return
    for row in contenders:
        verdict = _runtime_agrees(seconds, row.runtime)
        _note(notes, "    %s - TMDb says %s, the disk says %s: %s"
              % (_says(row), "%g min" % row.runtime if row.runtime
                 else "nothing", "%g min" % round(seconds / 60.0, 1),
                 {True: "fits", False: "does not fit",
                  None: "cannot be compared"}[verdict]))


def _matched(candidate: _Candidate, want: frozenset) -> Match:
    """A settled candidate as (id, the spelling to write it under).

    The spelling is the first of the candidate's own titles that the folder's
    keys actually met, in TMDb's order - original title, primary title, then the
    alternatives. Taking the first MATCHING one and not the first one is what
    keeps a French film French: the English title a catalogue leads with folds
    to keys the French folder never had, so it is not a spelling this folder was
    found under and is not one it is renamed to.

    "" for a spelling no file may be named after - one carrying a path separator
    or a NUL. Face/Off is a real film and not a real folder, and the id is still
    worth having.
    """
    for spelling in candidate.spellings:
        if title_keys(spelling) & want:
            return Match(candidate.imdb,
                         spelling if _nameable(spelling) else "")
    return Match(candidate.imdb, "")


def _nameable(title: str) -> bool:
    """Whether a catalogue's spelling can be a name on this filesystem."""
    return bool(title.strip()) and not set(title) & {"/", "\\", "\0"}


def _search(api_key: str, title: str, year: str):
    """The search results for a title, in TMDb's order: a list, or None when
    the call itself failed - which is not the same as an answer of none."""
    params = [("api_key", api_key), ("query", title)]
    if year:
        params.append(("year", year))
    params.append(("include_adult", "false"))
    body = _curl(_BASE + "/search/movie", params)
    if body is None:
        return None
    results = _as_json(body).get("results")
    return results if isinstance(results, list) else []


def _worth_asking(rows, year: str, want: frozenset) -> list:
    """The search results worth a request of their own, in the order they came.

    Two ways to be worth one, both answered from what the search already said:
    a primary release year near the wanted one, or a title that already matches.
    The first is the ordinary candidate; the second is the film whose only
    release near this year is in a country the primary date is not, and whose
    full document is the one thing that can say so.
    """
    keep: list = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("id") in seen:
            continue
        release = row.get("release_date")
        release = release if isinstance(release, str) else ""
        titled = any(title_keys(str(t)) & want for t in _row_titles(row))
        if _near(release[:4], year) or titled:
            seen.add(row.get("id"))
            keep.append(row)
        if len(keep) >= MAX_CANDIDATES:
            break
    return keep


def _candidate(api_key: str, row: dict) -> _Candidate:
    """One search result and its own document, read into what the rule asks."""
    detail = _as_json(_curl(_BASE + "/movie/" + _id_token(row.get("id")),
                            [("api_key", api_key),
                             ("append_to_response", _APPENDED)]))
    folded: set = set()
    written: list = []
    for t in _detail_titles(detail) + _row_titles(row):
        text = str(t)
        folded |= title_keys(text)
        if text not in written:
            written.append(text)
    imdb = (detail.get("external_ids") or {}).get("imdb_id")
    return _Candidate(
        years=frozenset(_release_years(row, detail)),
        titles=frozenset(t for t in folded if t),
        spellings=tuple(written),
        runtime=_minutes(detail.get("runtime")),
        imdb=imdb if isinstance(imdb, str) and imdb.startswith("tt") else "")


def _row_titles(row: dict) -> list:
    """A search result's own two titles. A missing one is the literal word
    "null" the way ``jq -r`` prints it, so it is compared - and missed - like
    any other title rather than skipped."""
    titles = []
    for field in ("title", "original_title"):
        value = row.get(field)
        titles.append("null" if value is None else value)
    return titles


def _detail_titles(detail: dict) -> list:
    """A film document's titles: its own two, and every alternative it carries.

    An alternative with no title is skipped rather than read as "null", which
    is the one place the two differ - ``.title // empty`` in the shell.

    The ORIGINAL title leads, because this order is also the order a spelling to
    rename onto is picked in: a film's own name is the one to keep where the
    folder was named from it.
    """
    titles = list(reversed(_row_titles(detail)))
    alternatives = (detail.get("alternative_titles") or {}).get("titles") or []
    for entry in alternatives:
        if isinstance(entry, dict) and entry.get("title") is not None:
            titles.append(entry["title"])
    return titles


def _release_years(row: dict, detail: dict) -> set:
    """Every year this film was released in, anywhere.

    The primary date both documents carry, plus each country's own in
    ``release_dates`` - which is what a premiere in one year and a release in
    the next comes down to, and what lets either of them answer for the folder.
    """
    years = set()
    for source in (row, detail):
        date = source.get("release_date")
        if isinstance(date, str) and len(date) >= 4:
            years.add(date[:4])
    countries = (detail.get("release_dates") or {}).get("results") or []
    for country in countries:
        if not isinstance(country, dict):
            continue
        for release in country.get("release_dates") or []:
            date = release.get("release_date") if isinstance(release, dict) else None
            if isinstance(date, str) and len(date) >= 4:
                years.add(date[:4])
    return years


def _minutes(value) -> float:
    """A stated runtime in minutes, or 0.0 for one that says nothing - which is
    what a missing, null or zero runtime all are."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


def _near(year: str, wanted: str) -> bool:
    """Whether two years are the same or next door, and both are years."""
    if not (year.isdigit() and wanted.isdigit()):
        return False
    return abs(int(year) - int(wanted)) <= NEAR_YEARS


def _runtime_agrees(seconds: float, minutes: float):
    """Whether a file of ``seconds`` is the film TMDb states ``minutes`` for:
    True, False, or None when either side does not say."""
    if seconds <= 0 or minutes <= 0:
        return None
    stated = minutes * 60.0
    allowed = max(RUNTIME_TOLERANCE_SECONDS, stated * RUNTIME_TOLERANCE_FRACTION)
    return abs(seconds - stated) <= allowed


def _settled_by_runtime(candidates: list, seconds: float):
    """The one candidate the file's own length picks out, or None.

    Picked out means both halves: exactly one it fits, and no other left
    unmeasured. A candidate TMDb states no runtime for is not ruled out by a
    length, so as long as one is standing the answer is still "not certain" -
    which is the rule this module is for.
    """
    verdicts = [(row, _runtime_agrees(seconds, row.runtime)) for row in candidates]
    fits = [row for row, verdict in verdicts if verdict is True]
    unknown = [row for row, verdict in verdicts if verdict is None]
    if len(fits) == 1 and not unknown:
        return fits[0]
    return None


def _id_token(value):
    """The id the way ``jq -r`` spells it in a URL: a missing id is the literal
    word "null", not nothing - the bash builds its path from what jq printed.

    Digits or that word, and nothing else. The value comes out of the API's own
    JSON and is spliced into a URL PATH, where a "/" or a "#" in it asks for
    something other than what this meant to ask for; a TMDb id that is not a
    number is not an id.
    """
    if value is None:
        return "null"
    text = str(value)
    return text if text.isdigit() else "null"


def _folder_spelling(directory: str, name: str) -> str:
    """The way ``find`` spells a child of ``directory``: a bare "." writes
    "./name", a trailing slash is not doubled, anything else joins with one
    slash."""
    if directory == ".":
        return "./" + name
    if directory.endswith("/"):
        return directory + name
    return directory + "/" + name


# The id list's own spelling: one film per line, its folder name and its id
# separated by a tab, "#" comments and blank lines ignored. A tab because a
# film's name is full of everything else - spaces, brackets, dashes, colons -
# and is the one character a folder name cannot contain.
_ID_COMMENT = "#"

_ID_FORMS = re.compile(r"^\{?(?:(imdb)-)?(tt[0-9]+)\}?$|^\{?(?:(tmdb)-)?([0-9]+)\}?$",
                       re.I)


def id_tag_for(text: str) -> str:
    """The tag a hand-written id becomes, or "" when it is not one.

    Written the way someone has it to hand: "tt0000001", "imdb-tt0000001",
    "{imdb-tt0000001}", a bare TMDb number, or "tmdb-12345". An IMDb id is a
    "tt" and digits; anything else that is only digits is TMDb's.
    """
    match = _ID_FORMS.match(text.strip())
    if not match:
        return ""
    if match.group(2):
        return "{imdb-" + match.group(2).lower() + "}"
    return "{tmdb-" + match.group(4) + "}"


def write_rename_list(path: str, renames, root: str,
                      log: Callable[[str], None] | None = None) -> bool:
    """Every rename a dry run would have made, as a file to read through.

    The same lines the run printed, kept where they can be searched and diffed
    rather than scrolled through: a library of any size prints thousands of
    them, and the point of a dry run is to be able to check them.
    """
    lines = [
        _ID_COMMENT + " Every rename the tagging would make, in the order it",
        _ID_COMMENT + " would make them: a folder's files first, then the",
        _ID_COMMENT + " folder itself. Nothing here has happened - run the",
        _ID_COMMENT + " tagging again with -w to carry these out.",
        "",
    ]
    for source, target in renames:
        lines.append(os.path.relpath(source, root))
        lines.append("    -> " + os.path.relpath(target, root))
    try:
        with open(path, "w", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as error:
        if log is not None:
            log("WARNING: could not write %s: %s" % (path, error))
        return False
    return True


def write_ambiguous_list(path: str, folders, root: str,
                         log: Callable[[str], None] | None = None) -> bool:
    """The folders holding more than one film, as a report to read and act on.

    Not a file to feed back: what to do about two films in one folder is to
    give the files names that say which release each one is, and only someone
    who knows them can. The report says where they are and what is in them.
    """
    lines = [
        _ID_COMMENT + " Folders the tagging left alone, and why.",
        _ID_COMMENT + "",
        _ID_COMMENT + " 'holds a film that is not its own': a movie file whose",
        _ID_COMMENT + " name does not extend the folder's. Which film it is is",
        _ID_COMMENT + " not something a tag may guess.",
        _ID_COMMENT + "",
        _ID_COMMENT + " 'one film in parts that do not stack': Plex reads a",
        _ID_COMMENT + " part from the END of the name - '<film> Part1' - and",
        _ID_COMMENT + " these have a title after the token, so it cannot.",
        _ID_COMMENT + " Tagged as editions they would read as separate films.",
        _ID_COMMENT + "",
        _ID_COMMENT + " Rename the files and run the tagging again.",
        "",
    ]
    for folder, reason, movies in folders:
        lines.append("%s  -  %s" % (os.path.relpath(folder, root), reason))
        lines += ["    " + name for name in movies]
        lines.append("")
    try:
        with open(path, "w", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as error:
        if log is not None:
            log("WARNING: could not write %s: %s" % (path, error))
        return False
    return True


def write_near_miss_list(path: str, folders, root: str,
                         log: Callable[[str], None] | None = None) -> bool:
    """What TMDb was asked, what it offered, and why none of it was the answer.

    The list to read before trusting the other three: every folder in it was
    left alone, and this is the only place that says whether it should have
    been. A page of candidates that are plainly the film is a reading this does
    not have yet; a page of films that merely share a word is the rule working.
    """
    lines = [
        _ID_COMMENT + " Every folder the tagging left alone, and how close it",
        _ID_COMMENT + " came. Nothing here has happened and nothing here is a",
        _ID_COMMENT + " worklist - it is what a dry run saw, so that \"no",
        _ID_COMMENT + ' confident match" can be read rather than taken on',
        _ID_COMMENT + " trust.",
        "",
    ]
    for folder, reason, notes in folders:
        lines.append("%s  -  %s" % (os.path.relpath(folder, root), reason))
        lines += ["    " + note for note in notes]
        lines.append("")
    try:
        with open(path, "w", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as error:
        if log is not None:
            log("WARNING: could not write %s: %s" % (path, error))
        return False
    return True


def read_id_list(path: str) -> dict:
    """A hand-filled id list as {folder name: tag}.

    A line with no id yet is skipped rather than refused - the file is meant to
    be filled in over several sittings, and the ones still blank are simply the
    ones still to do.
    """
    found: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return found
    for line in lines:
        if not line.strip() or line.lstrip().startswith(_ID_COMMENT):
            continue
        name, _tab, written = line.partition("\t")
        tag = id_tag_for(written)
        if name.strip() and tag:
            found[name.strip()] = tag
    return found


def write_id_list(path: str, names, ids: dict | None = None,
                  log: Callable[[str], None] | None = None) -> bool:
    """The id list as it stands: what has been filled in, then what has not.

    ``ids`` is written back verbatim. It is the file's whole point - the ids
    someone looked up by hand are the only record of them anywhere, and a film
    named from one still answers "identified" on the next run only because the
    line is still there. ``names`` are the ones still to do, each left blank.

    Rewritten whole rather than appended to, so a film that has since been
    identified by TMDb itself drops off the list instead of lingering on it.
    """
    lines = [
        _ID_COMMENT + " Films TheMovieDB could not identify on its own.",
        _ID_COMMENT + " Put the id after the tab - tt0000001, imdb-tt0000001",
        _ID_COMMENT + " or a bare TMDb number - and run the tagging again with",
        _ID_COMMENT + "   ingest-movies -t -i <this file> <library>",
        _ID_COMMENT + " adding -w once the dry run reads right. Lines still",
        _ID_COMMENT + " blank are simply the ones still to do.",
        "",
    ]
    lines += [name + "\t" + tag for name, tag in sorted((ids or {}).items())]
    lines += [name + "\t" for name in names]
    try:
        with open(path, "w", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as error:
        if log is not None:
            log("WARNING: could not write the id list %s: %s" % (path, error))
        return False
    return True


def _candidates(directory: str, recursive: bool) -> list:
    """The folders to consider: one level for a full ingest, the whole tree for
    a run that was pointed at a library rather than at a film folder."""
    if not recursive:
        return [entry for entry in os.scandir(directory)
                if entry.is_dir(follow_symlinks=False)]
    return _film_folders(directory)


def _film_folders(directory: str) -> list:
    """Every film folder at or below ``directory``, in the filesystem's own
    order - the order the shell's ``find`` walks, so the lines they each log
    come out in the same sequence.

    A folder named "Title (Year)" IS a film and is not descended into: what
    sits inside one is that film's own material - its extras, its Subs - and
    not more films. Everything else is descended through, which is what finds
    the films in a library that keeps them a level down, in an "Unsorted" or a
    box set, rather than directly under the root it was handed.
    """
    found: list = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        if read_folder(entry.name)[0]:
            found.append(entry)
        else:
            found.extend(_film_folders(entry.path))
    return found


def tag_plex_ids(directory: str, log: Callable[[str], None],
                 skip_log: safety.SkipLog | None = None,
                 dry_run: bool = False, ids: dict | None = None,
                 unmatched: list | None = None, recursive: bool = False,
                 ambiguous: list | None = None,
                 planned: list | None = None,
                 near_misses: list | None = None) -> int:
    """Name each confidently-matched movie folder, its films and their sidecars
    the way Plex reads them: the folder and every file carry the id tag, an
    edition carries its own, and a split film keeps its stacking token last.

    Renaming only, each name beside what it already was - the files inside the
    folder first, then the folder itself - so nothing is copied anywhere.

    The on-disk name is kept verbatim; only the tags are added. A folder that
    already carries an "{imdb-...}" or "{tmdb-...}" tag is not looked up again,
    but its files ARE brought into line with it, without a second ask of the
    network. The "<film> (old).mkv" an improved remux keeps is never renamed: it
    is the film as it arrived, and tagging it would offer Plex a second edition
    of every film that has one.

    ``dry_run`` asks TMDb the same questions and works out the same names, then
    prints each rename instead of doing it: the name is the thing being
    previewed, so the lookup still happens.

    ``ids`` is a hand-written {folder name: tag} that answers BEFORE the network
    is asked, for the films TMDb cannot identify on its own. ``unmatched``, when
    a list is passed, collects the folder names that neither could name, and
    ``ambiguous`` the folders holding more than one film - both are left exactly
    as they are, and both lists are what the run writes its reports from.
    ``planned`` collects the renames a dry run would have made, for the third.
    ``near_misses`` collects what TMDb was asked and what it offered for every
    folder that ended up in one of the other two lists, for the fourth - the one
    that says whether "no confident match" was the right answer.

    ``recursive`` walks the whole tree rather than the one level the phase reads
    inside a full ingest, where the caller is pointed at the folder that holds
    the films and the phases around this one read that same one level.
    """
    skip_log = skip_log if skip_log is not None else safety.SkipLog()
    if not os.environ.get("tmdbApiKey", ""):
        log("WARNING: tmdbApiKey not set, skipping IMDb id tagging")
        return 0

    for folder in _candidates(directory, recursive):
        base, tag, title, year = read_folder(folder.name)
        if not base:
            continue

        names = [entry.name for entry in os.scandir(folder.path)
                 if entry.is_file(follow_symlinks=False)]
        # Read against the names the folder WOULD hold once every spelling of
        # its own film has been brought onto its own: a file that differs from
        # the folder by an accent, a roman numeral or a copy's "(1)" is that
        # film written by another hand, and asking whether the folder is a
        # muddle before correcting those would report half the library.
        corrected = plexnames.spelling_renames(base, names)
        respelled = [corrected.get(name, name) for name in names]
        # And a way back, because nothing has been renamed yet: a report naming
        # a file by the name it would have had names no file at all, and the
        # runtime probe would go looking for one that is not there.
        on_disk = {new: old for old, new in corrected.items()}
        trouble = _what_is_wrong(base, respelled)
        if trouble:
            if not _tag_and_leave(folder, names, tag, skip_log, dry_run, log,
                                  planned):
                left = [on_disk.get(name, name) for name in trouble[1]]
                _flag(ambiguous, folder.path, trouble[0], left)
                _flag(near_misses, folder.path, trouble[0],
                      _how_close(base, left))
                log('  "{}" {} - left as it is'.format(base, trouble[2]))
            continue

        asked = not tag
        by_hand = False
        if asked:
            # The hand-written id first: a film someone has already looked up is
            # not worth asking an API that has already failed to name it.
            tag = (ids or {}).get(base, "")
            by_hand = bool(tag)
            if not tag:
                notes: list = []
                found = identify(
                    title, year,
                    functools.partial(_folder_runtime, folder.path, base,
                                      respelled, on_disk),
                    notes if near_misses is not None else None,
                    also=(_with_the_folder_above(directory, folder, title),))
                tag = "{imdb-" + found.imdb + "}" if found.imdb else ""
                # The catalogue's own spelling of whichever title matched is
                # what the whole folder is written under from here: it is the
                # one spelling of the several that is known to be right, and
                # the folder and its files disagreeing about which to use is
                # what made this hard to begin with.
                if tag and found.title:
                    base = _as_folder(base, found.title, year, log)
            if not tag:
                if unmatched is not None:
                    unmatched.append(base)
                if not by_hand:
                    _flag(near_misses, folder.path, "no confident TMDb match",
                          notes or ["nothing was asked"])
                _report(log, base, "", [], False)
                continue

        # The folder's own name is settled BEFORE a single file is touched:
        # renaming the files under a folder that cannot take its name leaves
        # them spelled for a folder they are not in, beside the folder they are
        # spelled for.
        wanted = plexnames.folder_name(base, tag)
        target = _folder_spelling(os.path.dirname(folder.path) or ".", wanted)
        if wanted != folder.name and os.path.exists(target):
            skip_log.record(folder.path, target)
            log('  "{}" is already there, left "{}" untouched'
                .format(wanted, folder.name))
            continue

        # Said before the renames it explains, so a dry run reads as a film and
        # then what would happen to it. Only a folder this run ASKED about says
        # anything: one already tagged would repeat its line for the rest of
        # the library's life.
        if asked:
            _report(log, base, tag, plexnames.editions_in(base, respelled),
                    by_hand)
        _rename_in_place(folder, names, base, tag, target, skip_log, dry_run,
                         log, planned)
    return 0


def _as_folder(base: str, title: str, year: str,
               log: Callable[[str], None]) -> str:
    """The folder name a catalogue's spelling gives, and a line about it.

    ``base`` back unchanged when the spelling is the one already on disk, which
    is the ordinary case and says nothing.
    """
    wanted = "%s (%s)" % (title, year)
    if wanted != base:
        log('  TMDb spells it "{}" - renaming "{}" onto it'.format(title, base))
    return wanted


# What can be wrong with a folder that no id settles, as
# (reason for the report, the files it is about, how the run says it).
def _what_is_wrong(base: str, names: list):
    """The one thing this folder cannot be tagged for, or None.

    Asked in the order the answers are worth having: a second film in the folder
    is a different problem from one film written as parts, and each is reported
    under its own name.
    """
    # A folder holding a film that is not this folder's is left whole and
    # reported instead. A name that EXTENDS the folder's own - or that SAYS the
    # folder's own in another spelling - is one of this film's releases; one
    # that does neither is something else, and guessing which film it is is how
    # a second feature becomes an edition of the first.
    strays = plexnames.strays_in(base, names)
    if strays:
        return ("holds a film that is not its own", strays,
                "holds %d file(s) that are not this film" % len(strays))

    # A part written the way a person writes one - "Part 1 - The First Half" -
    # would become an edition, and Plex would read three separate releases of
    # the film where there is one film in three files. Nothing here can make it
    # stack either: the token has to be last, and there is a title sitting after
    # it. A part written "Part 1" and nothing else IS fixable, and was fixed
    # before this was asked.
    in_parts, markers = [], []
    for stem in plexnames.movie_stems(base, names):
        edition = plexnames.read_stem(base, stem)[0]
        if plexnames.names_a_part(edition):
            in_parts.append(stem + ".mkv")
        elif plexnames.is_only_a_marker(edition):
            markers.append(stem + ".mkv")
    if in_parts:
        return ("is one film in parts that do not stack", sorted(in_parts),
                "is in parts that Plex will not stack")
    # A "(1)" that could not simply be taken off, because taking it off would
    # put two files under one name. Which of the two to keep is not a naming
    # question, and an edition called "1" says nothing about either.
    if markers:
        return ("holds a duplicate marked only by a number", sorted(markers),
                "holds a duplicate marked only by a number")
    return None


def _tag_and_leave(folder, names: list, tag: str, skip_log: safety.SkipLog,
                   dry_run: bool, log: Callable[[str], None],
                   planned: list | None) -> bool:
    """Tag a folder nothing can rename, when its own files already say which
    film it is. False when they do not, and the folder is reported instead.

    What cannot be guessed here is which file is which release; which FILM they
    are is not a guess at all when every id already written in the folder is the
    same id. So the id goes on to the folder and on to every name still without
    one, and not another character moves.

    One disagreeing id stops it dead, and deliberately: two ids in a folder is
    how a sequel ends up filed under the film before it, and that is a mistake
    to be shown rather than a spelling to be tidied.
    """
    settled = plexnames.ids_in(names) | ({tag} if tag else set())
    if len(settled) != 1:
        return False
    only = settled.pop()
    plan = plexnames.id_tag_renames(only, names)
    wanted = folder.name if tag else plexnames.folder_name(
        plexnames.untagged_base(folder.name)[0], only)
    target = _folder_spelling(os.path.dirname(folder.path) or ".", wanted)
    if wanted != folder.name and os.path.exists(target):
        skip_log.record(folder.path, target)
        return False
    if not plan and wanted == folder.name:
        return True
    log('  "{}" cannot be renamed, but every id in it says {} - tagging only'
        .format(folder.name, only))
    for old, new in plan:
        _rename(os.path.join(folder.path, old), os.path.join(folder.path, new),
                skip_log, dry_run, log, planned)
    _rename(folder.path, target, skip_log, dry_run, log, planned)
    return True


def _folder_runtime(path: str, base: str, names: list,
                    on_disk: dict | None = None) -> float:
    """How long the film in this folder is, in seconds - or 0.0 when nothing
    here can say, which is most of the ways a folder can be arranged.

    A folder holding named editions says nothing on purpose: a catalogue states
    one runtime, and which of a theatrical and an extended cut it is for is
    exactly what is not known. A split film is its parts added up, which IS what
    the catalogue states for it. Anything else is the one file, measured the way
    every other duration in this library is - the container's own figure.

    ``names`` are the names the folder WILL hold, which is what says how the
    films in it are arranged; ``on_disk`` maps any of those back to the name the
    file is under now, which is what can actually be opened.
    """
    if plexnames.editions_in(base, names):
        return 0.0
    movies = plexnames.one_film_in(base, names)
    if not movies:
        return 0.0
    return durationcheck.total_duration(
        [os.path.join(path, (on_disk or {}).get(name, name))
         for name in movies])
def _with_the_folder_above(root: str, folder, title: str) -> str:
    """This film's title with the folder above it read as its first half, or "".

    A library that keeps a franchise in a folder of its own writes half the
    title on each: "Star Wars" holding "Episode IV - A New Hope", "The Lord of
    the Rings" holding "The Fellowship of the Ring". Neither half is the title
    a catalogue has, and put together they are.

    The mirror of the franchise a library repeats on every film IN it, which the
    search spellings already take OFF - a name can carry too little of the title
    as easily as too much.

    "" for a film sitting directly in the folder the run was pointed at: that
    one is the library, named by whoever typed it, and gluing it to every title
    underneath would ask about "Films Casablanca" once per film.
    """
    above = os.path.dirname(os.path.abspath(folder.path))
    if above == os.path.abspath(root):
        return ""
    # Without whatever tag an earlier run put on it, which is not part of any
    # title. It cannot be a film folder itself - the walk stops at one and does
    # not look inside - so what is left is a container someone named.
    name = plexnames.untagged_base(os.path.basename(above))[0].strip()
    return name + " " + title if name else ""


def _how_close(base: str, names: list) -> list:
    """How near each file a folder was left for came to being that folder's own
    film, as the fold that decided it.

    Two lines that read the same are two names this refused to call one thing,
    which is a reading that is missing; two that read differently are two
    different films, which is the answer the folder was left for.
    """
    lines = ["the folder reads as: " + titlematch.normalize_title(base)]
    for name in names:
        lines.append('"%s" reads as: %s'
                     % (name, titlematch.normalize_title(
                         os.path.splitext(name)[0])))
    return lines


def _flag(ambiguous: list | None, path: str, reason: str, names) -> None:
    """Record a folder the tagging will not touch, and why."""
    if ambiguous is not None:
        ambiguous.append((path, reason, list(names)))


def _report(log: Callable[[str], None], base: str, tag: str,
            editions: list, by_hand: bool = False) -> None:
    """What this folder got, in one line - including the folder that got
    nothing, which is the answer people go looking for."""
    named = ", ".join(editions)
    source = "from the id list" if by_hand else "match"
    if tag and editions:
        log('  {}: "{}" -> {}, editions: {}'.format(source, base, tag, named))
    elif tag:
        log('  {}: "{}" -> {}'.format(source, base, tag))
    else:
        log('  no confident TMDb match: "{}" - left as it is'.format(base))


def _rename_in_place(folder, names: list, base: str, tag: str, target: str,
                     skip_log: safety.SkipLog, dry_run: bool,
                     log: Callable[[str], None],
                     planned: list | None = None) -> None:
    """One folder's films and sidecars, then the folder itself.

    That order and no other: renaming the folder first would move every path
    underneath it out from under the names just worked out.

    An empty ``tag`` still does the rest of the work: which release a file is
    is on the disk already, whether or not TMDb could say which film they are
    all versions of.
    """
    for old, new in plexnames.folder_renames(base, tag, names):
        _rename(os.path.join(folder.path, old),
                os.path.join(folder.path, new), skip_log, dry_run, log,
                planned)
    _rename(folder.path, target, skip_log, dry_run, log, planned)


def _rename(source: str, target: str, skip_log: safety.SkipLog,
            dry_run: bool = False,
            log: Callable[[str], None] | None = None,
            planned: list | None = None) -> None:
    """A rename that refuses to land on something that is already there, or to
    hide what it moves - and that only SAYS what it would do on a dry run.

    ``planned`` collects what a dry run would have done, so the run can hand it
    over as a file rather than as output that scrolls away.
    """
    if source == target:
        return
    if safety.would_hide(target) or os.path.exists(target):
        skip_log.record(source, target)
        return
    if dry_run:
        if log is not None:
            log('    would rename: "{}" -> "{}"'.format(
                os.path.basename(source), os.path.basename(target)))
        if planned is not None:
            planned.append((source, target))
        return
    os.rename(source, target)
