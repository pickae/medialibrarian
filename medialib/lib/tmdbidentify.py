"""What a film is: a folder's title and year asked of TMDb and the IMDb
datasets, and an answer given only when it is certain.

Every candidate is judged on its titles in every language, its release
years and its runtime against the film on disk; what counts as the same
title is :mod:`medialib.lib.titlematch`, and asking the service is
:mod:`medialib.lib.tmdbclient`. A rule here either settles the film or
leaves it unanswered - a near miss is reported, never renamed.
"""

import functools
import os
import re
import subprocess
from collections.abc import Callable
from typing import NamedTuple

from medialib.lib import imdbdata, titlematch, tmdbclient
from medialib.lib.titlematch import normalize_title, title_keys

# What an IMDb id looks like. The id ends up in folder names and paths, so a
# value from TMDb's JSON or the dataset is taken only in exactly this shape.
_IMDB_ID = re.compile(r"tt[0-9]+")


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

# How far it may be when nothing carrying the title came out within those: a
# stage show dated by its recording and released years later, a film dated by
# a re-release, a digit typed wrong. Only ever answered by a length - a
# candidate this far out is the film where the file's length fits it and rules
# out every other that carries the title, and the folder then takes the year
# the catalogue has it under.
FAR_YEARS = 3

# How many of the search results are worth asking about in detail. They arrive
# most popular first, and a film that is not in the first handful of answers to
# its own title is not one a certainty rule is going to settle - while every
# one of them costs a request.
MAX_CANDIDATES = 8

# The four documents a candidate is judged on, asked for with the candidate
# itself so that a candidate costs one request: its alternative titles, its
# title in every language it has been translated into, its IMDb id, and every
# country's release date.
_APPENDED = "alternative_titles,translations,external_ids,release_dates"


class _Candidate(NamedTuple):
    """One film TMDb offered, with everything the certainty rule asks of it."""

    years: frozenset        # every year some country released it in
    titles: frozenset       # its primary, original and alternative titles, folded
    spellings: tuple        # those same titles as TMDb writes them, in its order
    own: frozenset          # its own two titles alone, folded - see _the_best_evidence
    runtime: float          # minutes, 0.0 when TMDb does not say
    imdb: str               # "" when it has no usable IMDb id
    released: str = ""      # the year of its primary release date, or ""
    tmdb: str = ""          # TMDb's own number for it
    lead: frozenset = frozenset()  # its primary and original titles, folded


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
    # Every title the matched film is catalogued under, as the catalogue writes
    # them. A library that holds one film under two languages' names has no
    # other way to know they are one film: the names have nothing in common,
    # and the catalogue is the only thing that says they are the same.
    aliases: tuple = ()
    # The year the catalogue dates the matched film to, where the folder's own
    # is not the one to keep: it carried none, or one the catalogue does not
    # have the film anywhere near.
    year: str = ""
    # TMDb's own number for the film, which is what it is tagged with when
    # IMDb has never catalogued it - a television special, a concert disc.
    tmdb: str = ""

    @property
    def tag(self) -> str:
        """The id tag this match names a folder with, or "" for no match:
        IMDb's where there is one, and TMDb's own where there is not."""
        if self.imdb:
            return "{imdb-" + self.imdb + "}"
        return "{tmdb-" + self.tmdb + "}" if self.tmdb else ""


def tmdb_imdb_id(title: str, year: str,
                 runtime: Callable[[], float] | None = None) -> str:
    """The IMDb id (``ttXXXXXXX``) of a film, or "" - :func:`identify` without
    the spelling, for a caller that only wants to know which film this is."""
    return identify(title, year, runtime).imdb


def imdb_of_tmdb(tmdb_id: str) -> str:
    """The IMDb id TMDb records for one of its films, or "" - for a name whose
    tag is TMDb's own number, when what is being asked only knows IMDb's."""
    api_key = os.environ.get("tmdbApiKey", "")
    if not api_key or not tmdb_id.isdigit():
        return ""
    detail = tmdbclient._as_json(tmdbclient._curl(
        tmdbclient._BASE + "/movie/" + tmdb_id + "/external_ids",
        [("api_key", api_key)]))
    return _imdb_id(detail.get("imdb_id"))


_ID_TAG = re.compile(r"\{(imdb|tmdb)-([^{}]*)\}", re.I)

# TMDb's spelling of a language with no spoken content.
_NO_LANGUAGE = "xx"


class FilmLanguages(NamedTuple):
    """What TMDb says a film is spoken in, as ISO 639-1 codes (so "ja", "de",
    and "cn" for Cantonese). Either may be wrong, and the spoken ones list a
    line of dialogue as readily as a soundtrack's worth; both are empty when
    there was no key, no answer, or nothing TMDb knows."""

    original: str = ""
    spoken: tuple = ()


def film_languages(tag: str) -> FilmLanguages:
    """The original and spoken languages of the film an id tag names.

    ``tag`` is an id tag as a folder carries it, "{imdb-tt...}" or
    "{tmdb-NNN}": an IMDb id is found through TMDb's own lookup by external
    id, whose answer has the original language but not the spoken ones, so
    the film is asked for by its TMDb number after it.
    """
    api_key = os.environ.get("tmdbApiKey", "")
    match = _ID_TAG.search(tag or "")
    if not api_key or not match:
        return FilmLanguages()
    kind, value = match.group(1).lower(), match.group(2).strip().lower()
    if kind == "imdb":
        if not _IMDB_ID.fullmatch(value):
            return FilmLanguages()
        found = tmdbclient._as_json(tmdbclient._curl(
            tmdbclient._BASE + "/find/" + value,
            [("api_key", api_key), ("external_source", "imdb_id")]))
        movies = found.get("movie_results")
        first = movies[0] if isinstance(movies, list) and movies else {}
        value = (_id_token(first.get("id")) if isinstance(first, dict)
                 else "")
    if not value.isdigit():
        return FilmLanguages()
    document = tmdbclient._as_json(tmdbclient._curl(
        tmdbclient._BASE + "/movie/" + value, [("api_key", api_key)]))
    spoken = document.get("spoken_languages")
    return FilmLanguages(
        original=_language_code(document.get("original_language")),
        spoken=tuple(code for code in (
            _language_code(row.get("iso_639_1")) for row in
            (spoken if isinstance(spoken, list) else [])
            if isinstance(row, dict)) if code))


def _language_code(value) -> str:
    """``value`` when it is a two-letter code naming a language, else ""."""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z]{2}", value) or value == _NO_LANGUAGE:
        return ""
    return value


def _imdb_id(value) -> str:
    """``value`` when it is an IMDb id, otherwise "" - which is what an id that
    is anything else is treated as: none at all."""
    if isinstance(value, str) and _IMDB_ID.fullmatch(value):
        return value
    return ""


def identify(title: str, year: str,
             runtime: Callable[[], float] | None = None,
             notes: list | None = None, also: tuple = (),
             imdb: str = "",
             written: Callable[[], str] | None = None) -> Match:
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

    A folder whose name turns out to hold SEVERAL of the film's titles rather
    than one of them - the "aka" a library writes when it cannot choose - is
    taken apart first, and each title asked about in turn, left to right. See
    :func:`_each_title`: that name is not a spelling of anything a catalogue
    holds, so it is not a query, and each of the titles in it is.

    ``also`` are further titles this folder might be the film of - the one the
    folder above it makes when its name is read as the first half of the title.
    They count both ways: each is asked about in its own right, and a candidate
    that carries one of them carries this film's title. The folder's own comes
    first either way, so a film findable under its own name never pays for them.

    ``imdb`` is the local IMDb database, asked last where TMDb could not answer
    under any spelling - see :func:`_in_the_imdb_lists`. "" asks TMDb alone.

    ``written`` is asked - like ``runtime``, only once candidates have to be
    weighed - for the year the film on the disk was written, or "": a film
    that came out after that is not the film. See :func:`_not_after_the_file`.
    """
    api_key = os.environ.get("tmdbApiKey", "")
    if not api_key:
        return Match()
    reading = _each_title((title,) + tuple(other for other in also if other))
    # A catalogue that writes the year INTO the title - "Kestrel Vane 2017" for
    # a special a folder calls "Kestrel Vane (2017)" - holds this title too, and
    # says the year while it is at it: a concert released decades after it
    # was recorded is named for the night, and so is the folder.
    titled = frozenset().union(*(title_keys(one) for one in reading))
    named_for_the_year = frozenset().union(
        *(title_keys("%s %s" % (one, year)) for one in reading if year)) \
        - titled
    want = titled | named_for_the_year
    if not want:
        return Match()

    latin = all(titlematch.in_latin_script(one) for one in reading)
    subtitles = tuple((title_keys(tail), _heading_words(one, tail))
                      for one, tail in ((one, titlematch.last_segment(one))
                                        for one in reading)
                      if _several_words(normalize_title(tail)))
    plainly = (frozenset(normalize_title(one) for one in reading)
               | frozenset(normalize_title("%s %s" % (one, year))
                           for one in reading if year)) - {""}
    # Whether any query so far was answered by a film carrying the title in
    # some reading. The readings that say the folder is wrong or short are for
    # a title nothing carries, and a wider query reaching past two films that
    # DO carry it - which is why the first query did not settle - is not that.
    carried: list = []
    runtime = _measured_once(runtime)
    written = _measured_once(written)
    for query in _queries(reading):
        found = _under(api_key, query, want, year, runtime, notes, plainly,
                       named_for_the_year, latin, subtitles, carried, written)
        if found.tag:
            return found
    if imdb:
        return _in_the_imdb_lists(imdb, want, year, runtime, notes, plainly,
                                  named_for_the_year, latin, written,
                                  bool(carried))
    return Match()


def _measured_once(measure: Callable | None):
    """``measure``, asked of the disk at most once however often it is
    wanted."""
    if measure is None:
        return None
    return functools.lru_cache(maxsize=1)(measure)


# How long a short can be - IMDb's own line is 45 minutes - with room for the
# credits and logos a release adds.
SHORT_MINUTES = 50


def _in_the_imdb_lists(database: str, want: frozenset, year: str,
                       runtime: Callable[[], float] | None,
                       notes: list | None = None,
                       plainly: frozenset = frozenset(),
                       named_for_the_year: frozenset = frozenset(),
                       latin: bool = False,
                       written: Callable[[], str] | None = None,
                       carried: bool = False) -> Match:
    """The certainty rule over what the local IMDb lists hold under this
    title, where TMDb has already been asked under every spelling and could
    not answer - see :mod:`medialib.lib.imdbdata`.

    The last question, because it is the one that reaches past a catalogue's
    search: TMDb has to FIND a film before the rule can weigh it, and a
    romanisation its search does not tokenise the same way - or an umlaut it
    does not fold - finds nothing at all. The lists are looked up by the same
    folded keys the rule itself compares, and they carry the runtimes TMDb
    so often does not, which is what a folder with no year is settled on.

    Every title they hold under the keys is a candidate, so nothing is ever
    over the budget. A short that states no runtime is not the answer for a
    feature on the disk: that it is a short says how long it is.

    Where nothing anywhere carries the title - not TMDb's answers under any
    spelling (``carried``), and not the lists under its keys - they are asked
    once more for the titles a single slip away, and those go to the one
    reading a slip has, :func:`_misspelt`, under the same rule. The only
    widening is in what is REACHED: TMDb's search answers nothing at all for
    a folder spelled a letter wrong, and the typo reading can only recognise a
    near miss somebody has handed it.
    """
    rows = imdbdata.titles_under(database, {key.replace(" ", "")
                                            for key in want})
    _note(notes, "asked the local IMDb lists: %d title(s)" % len(rows))
    found: list = []
    if rows:
        # The weaker readings are for a title nothing carries, and these are
        # the titles its own keys reach.
        settled = _settle(_imdb_candidates(rows, latin, runtime), [], want,
                          year, runtime, notes, plainly, named_for_the_year, (),
                          [True], written, found)
        if settled.tag or found:
            return settled
    if carried:
        return Match()
    rows = imdbdata.titles_near(database, {plain.replace(" ", "")
                                           for plain in plainly})
    _note(notes, "asked the local IMDb lists a slip away: %d title(s)"
          % len(rows))
    if not rows:
        return Match()
    return _settle(_imdb_candidates(rows, latin, runtime), [], want, year,
                   runtime, notes, plainly, named_for_the_year, (), [], written,
                   part_of_name=False)


def _imdb_candidates(rows: list, latin: bool,
                     runtime: Callable[[], float] | None) -> list:
    """The lists' titles as candidates, less the shorts that state no runtime
    where the disk holds a feature."""
    candidates = []
    for row in rows:
        candidate = _imdb_candidate(row, latin)
        if row.get("kind") == "short" and not candidate.runtime \
                and runtime is not None and runtime() > SHORT_MINUTES * 60:
            continue
        candidates.append(candidate)
    return candidates


def _imdb_candidate(row: dict, latin: bool = False) -> _Candidate:
    """One title of the local IMDb lists, read into what the rule asks.

    Its year is the one IMDb has - the year it came out, where TMDb has every
    country's - so a festival year and a release year a year apart are the
    runtime's to settle, as they are for any candidate outside the year.
    """
    lead = [str(row.get(field) or "") for field in ("original_title",
                                                    "primary_title")]
    written: list = []
    for text in lead + [str(t) for t in row.get("titles") or []]:
        if text and text not in written and \
                (not latin or titlematch.in_latin_script(text)):
            written.append(text)
    folded: set = set()
    for text in written:
        folded |= title_keys(text)
    year = str(row.get("year") or "")
    tconst = str(row.get("tconst") or "")
    return _Candidate(
        years=frozenset({year}) if year.isdigit() else frozenset(),
        titles=frozenset(key for key in folded if key),
        spellings=tuple(written),
        own=frozenset(normalize_title(text) for text in lead
                      if text and (not latin
                                   or titlematch.in_latin_script(text)))
        - {""},
        runtime=_minutes(row.get("minutes")),
        imdb=_imdb_id(tconst),
        released=year if year.isdigit() else "",
        lead=frozenset(normalize_title(text) for text in lead
                       if text and (not latin
                                    or titlematch.in_latin_script(text)))
        - {""})


def _settle_the_year(title: str, year: str, others: list,
                     runtime: Callable[[], float] | None = None,
                     notes: list | None = None, also: tuple = (),
                     imdb: str = "",
                     written: Callable[[], str] | None = None) -> tuple:
    """A film looked up where its folder and its files disagree about the year
    by one, as (the match, the year it is really under, the further spellings
    of the folder's name that settling it justified).

    A digit wrong in a date is the commonest thing to be wrong with a film
    folder, and it is also how a remake is told from its original - so it
    cannot be corrected without asking, and asking is cheap only because the
    question is never raised unless a file in the folder is actually dated the
    other way.

    Both years are asked, and what comes back decides which of three things
    this is:

    * two different films. A 1953 remake of a 1952 film is exactly what a
      year-apart pair looks like, and the folder is left for someone to split;
    * one film, dated either way. Whichever year found it is the year, and the
      other spelling of the folder's name is one this film answers to - which
      is what lets the file carrying it come home instead of being reported;
    * nothing found under the folder's year and the film found under the
      file's. Then the FOLDER is the one with the digit wrong, and the film is
      named and dated the way the catalogue has it.

    The third is why this returns a year at all: everything downstream writes
    the folder from it.
    """
    found = identify(title, year, runtime, notes, also, imdb, written)
    for other in [one for one in others if one != year and _near(one, year)]:
        _note(notes, 'its files are dated %s where the folder says %s: '
              "asking under that year too" % (other, year))
        second = identify(title, other, runtime, notes, also, imdb, written)
        if found.tag and second.tag and found.tag != second.tag:
            _note(notes, "    %s and %s are two different films" % (year, other))
            continue
        if found.tag:
            return found, year, ("%s (%s)" % (title, other),)
        if second.tag:
            return second, other, ("%s (%s)" % (title, year),)
    return found, year, ()


def _each_title(reading: tuple) -> tuple:
    """The same readings with any name that is SEVERAL names replaced by the
    names it holds, left to right.

    A library that could not choose between a film's titles writes both, with
    an "aka" between them: "Falcons Forever aka Nordwind Rising". Nothing can
    be looked up under that. It is not a spelling of either title - no
    catalogue holds it, no fold reaches it, and the search is handed a query no
    film was ever released under - so the name has to come apart before any of
    the ladders below see it, which is why this stands here and not among them.

    The whole name is dropped rather than kept as a further reading. Each of
    the titles in it IS a name the film has, so asking under all of them at
    once would buy a query that cannot answer and two requests to find that out
    - and :func:`medialib.lib.titlematch.titles_written_together` only divides
    a name where the marker plainly holds two titles apart, so a title with the
    word in it is never taken to pieces to begin with.

    Left to right, and the first that settles is the answer: that is the order
    somebody wrote them in, and the one they led with is the one they thought
    the film was called. Each is asked in its own right and each counts as this
    film's title - the same arrangement ``also`` already has, and for the same
    reason, because two names for one film is exactly what the folder is
    claiming.
    """
    found: list = []
    for one in reading:
        found.extend(titlematch.titles_written_together(one) or [one])
    return tuple(found)


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
           notes: list | None = None,
           plainly: frozenset = frozenset(),
           named_for_the_year: frozenset = frozenset(),
           latin: bool = False,
           subtitles: tuple = (),
           carried: list | None = None,
           written: Callable[[], str] | None = None) -> Match:
    """The certainty rule over one query's results.

    ``query`` is what TMDb is asked; ``want`` stays the folder's own keys
    throughout, because what is being asked about is still that film - a wider
    query is a way of REACHING the candidate, never a looser test of it.

    ``plainly`` is the folder's title folded and nothing more - no reading, no
    widening - and it is what tells a strong match from a weak one. See
    :func:`_the_best_evidence`.

    ``named_for_the_year`` are the keys of the title with the folder's year
    written after it. A candidate carrying one of those is dated in that year
    by its own name, whatever year it was released in.

    ``latin`` says the folder is named in Latin letters, and the candidates'
    titles in other scripts are no evidence either way - see
    :func:`_candidate`. ``subtitles`` are what stands after the last break in
    the folder's name - its keys, and the words of what stands in front of it
    - for :func:`_one_part_of_the_name`. ``carried`` is shared by every
    query one folder is asked under, and says whether any of them was answered
    by a film carrying its title: once one has, the two last readings are not
    asked again under a wider one.
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
    worth = _worth_asking(rows, year, want, plainly)
    asking, over_budget = worth[:MAX_CANDIDATES], worth[MAX_CANDIDATES:]
    _note_the_passed_over(notes, rows, asking, over_budget)
    candidates = [_candidate(api_key, row, latin) for row in asking]
    return _settle(candidates, over_budget, want, year, runtime, notes,
                   plainly, named_for_the_year, subtitles, carried, written)


def _settle(candidates: list, over_budget: list, want: frozenset, year: str,
            runtime: Callable[[], float] | None,
            notes: list | None = None,
            plainly: frozenset = frozenset(),
            named_for_the_year: frozenset = frozenset(),
            subtitles: tuple = (),
            carried: list | None = None,
            written: Callable[[], str] | None = None,
            found: list | None = None,
            part_of_name: bool = True) -> Match:
    """The certainty rule over one set of candidates, wherever they came from.

    ``over_budget`` are the search results that were never made candidates,
    which a folder with no year cannot be certain without. ``found``, when a
    list is passed, is told whether any candidate carried the title, as
    ``carried`` is. ``part_of_name`` is off for candidates reached a slip
    away, which have had the one guess they are allowed. The rest are
    :func:`_under`'s.
    """
    named = [row for row in candidates if want & row.titles]
    carried = carried if carried is not None else []
    if named:
        carried.append(True)
        if found is not None:
            found.append(True)
    if not named and not carried:
        named = _misspelt(candidates, plainly, notes)
    if not named and not carried and part_of_name:
        named = _one_part_of_the_name(candidates, want, plainly, subtitles,
                                      notes)
    _note_the_unnamed(notes, candidates, named)
    named = _not_after_the_file(named, written, notes)
    named = _that_can_answer(named, notes)
    named = _the_best_evidence(named, plainly, notes)
    if not year:
        return _undated(named, over_budget, want, plainly, runtime, notes)
    dated = _not_a_rerelease([row for row in named if year in row.years
                              or row.titles & named_for_the_year],
                             year, notes)
    if len(dated) == 1:
        return _matched(dated[0], want, plainly, notes)

    # Several films of one title and one year - a remake released the same year,
    # or one release recorded twice - or, where the year settled nothing at all,
    # the ones that came out either side of it.
    contenders = dated or [row for row in named
                           if any(_near(y, year) for y in row.years)]
    if not contenders:
        redated = _redated_by_runtime(named, year, runtime, notes)
        if redated is not None:
            return _matched(redated, want, plainly, notes, redate=True)
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
        return Match()
    return _matched(settled, want, plainly, notes)


# The year Matroska was first released. A container dated earlier than that was
# not dated by whoever wrote it - a missing clock, a default - and says nothing
# about when the file was made.
FIRST_WRITTEN_YEAR = 2002


def _not_after_the_file(named: list, written: Callable[[], str] | None,
                        notes: list | None = None) -> list:
    """The candidates that had come out by the time the film on the disk was
    written.

    A container records when it was written, and a film cannot have been
    written to disk before it existed. A folder with no year was offered a
    fairy tale from 1952 and one from 2024, and its length fitted the new one
    better - on a file written in 2020. Only ever one way: a remux or a copy
    makes the date LATER, so the date can rule a film out and never in.
    """
    year = written() if written is not None and named else ""
    if not year:
        return named
    kept = [row for row in named if not row.years or min(row.years) <= year]
    for row in named:
        if row not in kept:
            _note(notes, "    %s - set aside: it came out after %s, when the "
                  "film on the disk was written" % (_says(row), year))
    return kept


def _written_year(paths) -> str:
    """The earliest year any of these files' containers say they were written
    in, or "" when none says one worth believing."""
    years = []
    for path in paths:
        try:
            done = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format_tags=creation_time", "-of", "default=nk=1:nw=1",
                 path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            continue
        stamp = done.stdout.decode("utf-8", "replace").strip()[:4]
        if stamp.isdigit() and int(stamp) >= FIRST_WRITTEN_YEAR:
            years.append(stamp)
    return min(years) if years else ""


def _redated_by_runtime(named: list, year: str,
                        runtime: Callable[[], float] | None,
                        notes: list | None = None):
    """The one candidate carrying the title that came out up to
    :data:`FAR_YEARS` from the folder's year and that the file's length picks
    out, or None.

    Only asked where nothing carrying the title came out near the year at all,
    so it never stands between two candidates the year could tell apart. The
    length has to fit it AND rule out every other candidate this far out, which
    is the same test a length is put to everywhere here.
    """
    far = [row for row in named
           if any(_near(y, year, FAR_YEARS) for y in row.years)]
    if not far:
        return None
    seconds = runtime() if runtime is not None else 0.0
    settled = _settled_by_runtime(far, seconds)
    if settled is None or not settled.released:
        _note(notes, "    nothing carrying the title came out near %s, and the "
              "length has to vouch for one further out:" % year)
        _note_the_lengths(notes, far, seconds)
        return None
    _note(notes, "    %s - came out %s, not %s, and the length picks it out"
          % (_says(settled), settled.released, year))
    return settled


def _undated(named: list, over_budget: list, want: frozenset,
             plainly: frozenset,
             runtime: Callable[[], float] | None,
             notes: list | None = None) -> Match:
    """The certainty rule for a folder that says no year at all.

    The year is what most of the rule narrows on, and with none the length of
    the film on the disk is the whole of the evidence left: a candidate is the
    answer only where it carries the title, the file's length fits it, and it
    RULES OUT every other candidate carrying the title - the same test the
    length is put to everywhere else here, over every year at once.

    A search result carrying the title that the budget never reached is one
    nobody measured, and so is not ruled out: the answer stays "not certain"
    rather than being the best of the ones that happened to be asked about. A
    title a dozen films share - the fairy tale filmed once a decade - is
    exactly where that happens, and where naming one of them by its length
    alone would be a guess.
    """
    if not named:
        return Match()
    unasked = [row for row in over_budget
               if any(title_keys(str(t)) & want for t in _row_titles(row))]
    if unasked:
        _note(notes, "    no year to narrow on, and %d more result(s) carrying "
              "the title were never asked about" % len(unasked))
        return Match()
    seconds = runtime() if runtime is not None else 0.0
    settled = _settled_by_runtime(named, seconds)
    if settled is None:
        _note(notes, "    no year to narrow on, so the length has to pick one "
              "out:")
        _note_the_lengths(notes, named, seconds)
        return Match()
    if not settled.released:
        _note(notes, "    %s - the length picks it out, and TMDb has no date "
              "for it to name the folder with" % _says(settled))
        return Match()
    _note(notes, "    %s - no year to narrow on, and the length picks it out"
          % _says(settled))
    return _matched(settled, want, plainly, notes, redate=True)


def _misspelt(candidates: list, plainly: frozenset,
              notes: list | None = None) -> list:
    """The candidates that carry this title MISSPELT, and only where nothing
    carries it at all.

    The last reading, and the only one in the whole lookup that says the folder
    is WRONG rather than that it is written differently. What it answers is the
    slip - "Thinner Then Air" for "Thinner Than Air", "Thinner Tahn Air" for
    the same - and, for free, the two families no table will ever hold: an
    americanism, and a character that is the shape of another without being
    one.

    Three things make it safe enough to ask, and each of them is somebody
    else's rule:

    * it is asked last. A single candidate carrying the title as written, or
      under any reading of it, means this is never reached - which is the whole
      of "no alternative that is closer";
    * what counts as misspelt is
      :func:`medialib.lib.titlematch.one_slip_apart`, which refuses a short
      title and any difference touching a digit - so a year, a sequel's number
      and a series' roman numeral are never slips;
    * two candidates a slip away are still two, and the certainty rule below
      answers a pair by naming neither. Nothing here chooses between guesses.

    It costs no request of its own: TMDb's own search is what turned a
    misspelt query into the film, and this only lets the answer be recognised.
    """
    near = [row for row in candidates
            if any(titlematch.one_slip_apart(spelling, plain)
                   for spelling in row.spellings for plain in plainly)]
    for row in near:
        _note(notes, "    %s - carries this title misspelt, and nothing "
              "carries it as written" % _says(row))
    return near


# How many words a name has to have before one catalogue title running on from
# it, or leading up to it, says anything: "Island" begins and ends a great many,
# and so does "The Alps" - the article is not a word of it that says anything.
_SEVERAL_WORDS = 2


def _several_words(folded: str) -> bool:
    """Whether a folded name has enough words besides its articles to be
    anchored on."""
    return len([word for word in folded.split()
                if word not in titlematch.ARTICLES]) >= _SEVERAL_WORDS


def _heading_words(title: str, tail: str) -> frozenset:
    """The words of what stands in front of ``tail`` in ``title`` - the whole
    of ``title`` where there is no ``tail`` - that could say which film it is.

    Not an article, and not a word that only says what kind of thing the file
    is: "Movie" in front of two subtitles is not the two sharing a heading.
    """
    head = title[:len(title) - len(tail)] if tail else title
    return frozenset(word for word in normalize_title(head).split()
                     if word not in titlematch.ARTICLES
                     and word not in titlematch.FILLER_WORDS
                     and word not in titlematch.LABEL_WORDS
                     and word not in titlematch.TRAILING_LABELS)


def _one_part_of_the_name(candidates: list, want: frozenset,
                          plainly: frozenset = frozenset(),
                          subtitles: tuple = (),
                          notes: list | None = None) -> list:
    """The candidates whose name holds the folder's title as one part of it,
    and only where nothing carries it at all.

    A folder carries a documentary's short name - "Minimal Rooms" - where a
    catalogue carries the long one, "Minimal Rooms: A Film About Empty
    Shelves"; a folder carries an opera's name where the catalogue
    carries the house's, "The Harbour Opera: Moss Rooms"; a franchise's
    folder carries "Harbour Tales - Movie 02 - Moonlight Wake" where the
    catalogue has "Harbour Tales II: Moonlight Wake". Nothing a fold does
    reaches from one to the other, because the part left out is words of the
    title. Three shapes count, and each is anchored:

    * a whole segment of the catalogue's name - what stands before its first
      " - " or ": ", or after its last - that is the folder's title;
    * the catalogue's name beginning or ending with the whole of the folder's
      title, where that title is more than a word: a subtitle cut short, or a
      franchise written in front with no break before it;
    * the catalogue's last segment - or its whole name, where it has no break
      - being the folder's own last segment, where the folder has one of more
      than a word, and what the two have in front of it sharing a word: the
      franchise and the number are written two ways, and still written. What
      stands in front of a concert's "Live at the Pier" is the band, and a
      different band in front of it is a different night.

    Never words somewhere in the middle. Like the misspelling, it is asked
    only where no candidate carries the title in any reading, and the rules
    below still have to settle on exactly one: two films that each have the
    folder's title as half their name are two, and are answered by naming
    neither.
    """
    anchored = [plain for plain in plainly if _several_words(plain)]
    near = []
    for row in candidates:
        for spelling in row.spellings:
            head, _rest = titlematch.leading_segment(spelling)
            tail = titlematch.last_segment(spelling)
            whole = normalize_title(spelling)
            heading = _heading_words(spelling, tail)
            if any(part and title_keys(part) & want for part in (head, tail)) \
                    or any(title_keys(tail or spelling) & keys
                           and heading & words for keys, words in subtitles) \
                    or any(whole.startswith(plain + " ")
                           or whole.endswith(" " + plain)
                           for plain in anchored):
                near.append(row)
                break
    for row in near:
        _note(notes, "    %s - carries this title as one part of its name, and "
              "nothing carries it whole" % _says(row))
    return near


def _that_can_answer(named: list, notes: list | None = None) -> list:
    """The candidates likeliest to BE the answer: the ones carrying an IMDb id,
    wherever any of them does.

    An entry IMDb never catalogued, standing beside one it did, is far likelier
    a stray - an idol video, a fan upload, a second entry for a release - than
    the film, and all it can do there is make the pair look uncertain. A folder
    called "Re Made" was offered the film it is and an idol video with no id,
    both lengths fitting, and was told the question could not be settled.

    Worse than standing beside a fit: a candidate TMDb states no runtime for is
    never ruled OUT by a length either, so one that could not have been the
    answer kept the answer at "not certain" all by itself.

    Where NO candidate has an IMDb id they all stand, and whichever the rules
    settle on is named by TMDb's own number.
    """
    answerable = [row for row in named if row.imdb]
    if not answerable or len(answerable) == len(named):
        return named
    for row in named:
        if not row.imdb:
            _note(notes, "    %s - set aside: no IMDb id, so it could not be "
                  "the answer to a question asking for one" % _says(row))
    return answerable


# How a candidate may carry the folder's title, strongest reading first, with
# what to say about the candidates a stronger one leaves behind.
#
# Each rung is contained in the one below it, so the first that anything
# reaches is the best evidence there is and the rest of the ladder is already
# in it.
_EVIDENCE = (
    (lambda row, plainly: bool(row.lead & plainly),
     "it carries the title only as a translation, and another carries it as "
     "its primary or original title"),
    (lambda row, plainly: bool(row.own & plainly),
     "it is one of the OTHER names this film is known by, and another "
     "carries the title as its own"),
    (lambda row, plainly: any(normalize_title(spelling) in plainly
                              for spelling in row.spellings),
     "it carries the title only through a reading, and another carries it "
     "as written"),
)


def _the_best_evidence(named: list, plainly: frozenset,
                       notes: list | None = None) -> list:
    """The candidates carrying the folder's title on the strongest evidence any
    of them has, or all of them.

    Two candidates are not equally likely just because both survived the rules,
    and weighing them as though they were is what leaves an obvious film
    unnamed. There are three ways to be the weaker of a pair:

    * **through a reading.** A folder called "1815" is offered the film "1815",
      which folds to exactly what the folder does, alongside a "Bramble '15"
      that only reached the list at all through the article reading;
    * **as an also-known-as.** A folder called "Bleak Friday" is offered the
      film of that name, whose own title it is, alongside an unrelated film
      that carries "Bleak Friday" somewhere down its list of alternative
      titles - a thing a catalogue's alternatives are full of, because they
      hold every market's poster name. Neither its primary title nor its
      original one has a word of the folder's in it, and it was still weighed
      as the equal of the film itself.

    * **as a translation.** A folder called "Gull Tide" is offered the film of
      that name alongside a documentary whose primary title is "Gull Tide: A
      Portrait" but whose translations write it plain "Gull Tide". Both
      lengths fit, and a translation is one of a film's own names, so both
      carried it as their own and neither was the weaker.

    A film's OWN titles are its original one, the one the catalogue leads with
    in the asking language, and its translations - which is why this does not
    strand a film found under a translation: the catalogue's English title is
    one of its own two, so a folder named in English meets a Swedish film
    there and not among its alternatives. A translation loses only to a
    candidate holding the title as its primary or original one.

    It narrows and never widens: two films that really do share a title on the
    same rung are still two, and still answered by naming neither.
    """
    for carries, beaten in _EVIDENCE:
        best = [row for row in named if carries(row, plainly)]
        if not best:
            continue
        if len(best) == len(named):
            return named
        for row in named:
            if row not in best:
                _note(notes, "    %s - set aside: %s" % (_says(row), beaten))
        return best
    return named


def _not_a_rerelease(dated: list, year: str, notes: list | None = None) -> list:
    """The candidates that CAME OUT in the wanted year, or all of them.

    A film's year is every year any country released it in, which is what lets
    a festival premiere and a release the year after both answer for a folder.
    It also lets a re-release answer, and a re-release is timed to something -
    usually a remake: "A Light Is Lit" from 1976 went back into cinemas in 2018
    because "A Light Is Lit" from 2018 came out, and a folder named for the
    remake was offered both and told they were equally likely.

    So where some candidates were FIRST released around the wanted year and
    others only reached it decades later, the others are set aside. Around, and
    not exactly on, because a premiere and a general release a year apart are
    the case the year rule exists for and must keep answering.

    Narrows and never widens: where every candidate is equally old, or none is,
    they all stand and the length decides as before.
    """
    original = [row for row in dated
                if row.years and _near(min(row.years), year)]
    if not original or len(original) == len(dated):
        return dated
    for row in dated:
        if row not in original:
            _note(notes, "    %s - set aside: it first came out in %s, so %s "
                  "is a re-release" % (_says(row), min(row.years), year))
    return original


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
    """The titles a candidate actually carries, without the literal word
    "null" for the ones it does not."""
    return [t for t in candidate.spellings if t and t != "null"]


def _note_the_passed_over(notes: list | None, rows: list, asking: list,
                          over_budget: list) -> None:
    """The results that never cost a request of their own, and why.

    The ones most worth reading in the list: a film whose document was never
    asked for is one the search itself offered and the cheap filter set aside.

    Two different reasons, and telling them apart is the point. One is that the
    result was nothing like this film; the other is that it was worth asking
    about and the budget had already gone on results that were closer - which
    is a line to read as "raise the budget", not as "the rule is too narrow".
    """
    if notes is None:
        return
    crowded = {id(row) for row in over_budget}
    kept = {id(row) for row in asking} | crowded
    for row in rows:
        if not isinstance(row, dict) or id(row) in kept:
            continue
        _note(notes, '    passed over: %s - neither its title nor its year '
              "was close" % _as_offered(row))
    for row in over_budget:
        _note(notes, "    passed over: %s - %d result(s) were closer, which is "
              "as many as are asked about" % (_as_offered(row), len(asking)))


def _as_offered(row: dict) -> str:
    """One search result as the near-miss list names it, out of what the search
    itself said - which is all that is known about a result nothing was asked
    about."""
    release = row.get("release_date")
    return '"%s" (%s)' % (row.get("title"), (release or "")[:4] or "no year")


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


def _matched(candidate: _Candidate, want: frozenset,
             plainly: frozenset = frozenset(),
             notes: list | None = None, redate: bool = False) -> Match:
    """A settled candidate as (id, the spelling to write it under).

    The spelling is the first of the candidate's own titles that the folder's
    keys actually met, in TMDb's order - original title, primary title, then the
    alternatives. Taking the first MATCHING one and not the first one is what
    keeps a French film French: the English title a catalogue leads with folds
    to keys the French folder never had, so it is not a spelling this folder was
    found under and is not one it is renamed to.

    "" for a spelling no file may be named after - one carrying a path separator
    or a NUL. Trade/Off is a real film and not a real folder, and the id is still
    worth having.

    A candidate settled on by :func:`_misspelt` has no title the folder's keys
    meet - that is what it means to be misspelt - so the spelling is the one the
    slip was made in. Which is the whole point of naming it: the
    folder is spelt the way the catalogue spells it, and the slip goes away.

    ``redate`` is for a match the folder's own year did not make - it had
    none, or one too far out - and hands back the year the catalogue dates the
    film to, for the folder to be named with.

    A candidate with no IMDb id is named by TMDb's own number instead - the
    German television special and the concert disc IMDb never catalogued are
    still films TMDb knows, and Plex reads a "{tmdb-...}" tag as readily as an
    IMDb one. It is SAID here, so the report shows which folders carry the
    weaker of the two tags.
    """
    if not candidate.imdb:
        _note(notes, "    %s - this is the film, and TMDb holds no IMDb id for "
              "it, so it is named by TMDb's own" % _says(candidate))
    year = candidate.released if redate else ""
    tmdb = candidate.tmdb
    meeting = [spelling for spelling in candidate.spellings
               if title_keys(spelling) & want]
    if meeting:
        spelling = _nearest_spelling(meeting, plainly)
        return Match(candidate.imdb, spelling if _nameable(spelling) else "",
                     candidate.spellings, year, tmdb)
    for spelling in candidate.spellings:
        if any(titlematch.one_slip_apart(spelling, plain) for plain in plainly):
            return Match(candidate.imdb,
                         spelling if _nameable(spelling) else "",
                         candidate.spellings, year, tmdb)
    return Match(candidate.imdb, "", candidate.spellings, year, tmdb)


# How much of a catalogue spelling has to be the folder's own words for the
# folder to be renamed onto it.
LIKE_THE_FOLDER = 0.5


def _nearest_spelling(meeting: list, plainly: frozenset) -> str:
    """Of the spellings that met the folder's keys, the one written most like
    the folder - the catalogue's order where they are equally like it.

    A key a whole franchise shares - the "Harbour Tales 6" in front of every
    market's title for the sixth film - meets the folder through the
    Portuguese title as readily as through the one the folder was written
    in, and the first of them in the catalogue's order is not the one to
    rename a folder onto.

    "" where none of them is written much like the folder at all - fewer than
    half their words in common. The folder is named in a language neither
    catalogue holds the film under, which is not a thing to correct: it keeps
    its name, and gains the id.
    """
    wanted = [set(plain.split()) for plain in plainly]
    squeezed = {plain.replace(" ", "") for plain in plainly}

    def likeness(spelling: str) -> tuple:
        # Written with its umlauts, and with them spelled out: "Mühlwiese"
        # is the folder's "Muehlwiese" written by another keyboard.
        folds = {normalize_title(spelling),
                 normalize_title(titlematch.spelled_out(spelling))}
        exact = any(fold.replace(" ", "") in squeezed for fold in folds)
        shared = max((len(words & plain) / len(words | plain)
                      for words in (set(fold.split()) for fold in folds)
                      for plain in wanted if words | plain), default=0.0)
        return (exact, shared)
    best = max(meeting, key=likeness)
    exact, shared = likeness(best)
    return best if exact or shared >= LIKE_THE_FOLDER else ""


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
    body = tmdbclient._curl(tmdbclient._BASE + "/search/movie", params)
    if body is None:
        return None
    results = tmdbclient._as_json(body).get("results")
    return results if isinstance(results, list) else []


def _worth_asking(rows, year: str, want: frozenset,
                  plainly: frozenset = frozenset()) -> list:
    """The search results worth a request of their own, the likeliest first.

    Two ways to be worth one, both answered from what the search already said:
    a title that already matches, or a primary release year near the wanted
    one. The second is the ordinary candidate - the film whose only release
    near this year is in a country the primary date is not, and whose full
    document is the one thing that can say so. A folder with no year has no
    year for a result to be near, and every result is that ordinary candidate:
    the search found it under something, and the title it carries may be one
    only its translations hold.

    The first comes ahead of it, and that ordering is the whole point of the
    function. TMDb answers most popular first, and a common word of a title
    fills the first page with films that merely contain it: a folder called
    "Sprint" was offered twenty results, and the budget below was spent on the
    eight most popular films with "sprint" somewhere in their names while the
    two called exactly that sat at the bottom of the page unread. Popularity is the
    catalogue's ordering and carries no opinion about which film this folder
    holds; carrying the folder's title is the closest thing to one there is.

    The ones left are ordered by how NEAR their names are - see
    :func:`_how_near_the_name_is`. A film reached by a reading this module does
    not have yet cannot land in the first list by definition, and those are
    precisely the ones the budget has to reach for a miss to be fixable: the
    result that shares three words of the folder's four is worth a document
    before the one that shares one, whatever the two of them are worth to the
    catalogue's readers. Ordering only, and never a filter - a result is
    eligible for exactly the two reasons above, and this decides which of the
    eligible ones the budget is spent on.
    """
    titled: list = []
    dated: list = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("id") in seen:
            continue
        release = row.get("release_date")
        release = release if isinstance(release, str) else ""
        if any(title_keys(str(t)) & want for t in _row_titles(row)):
            seen.add(row.get("id"))
            titled.append(row)
        elif _near(release[:4], year) or not year:
            seen.add(row.get("id"))
            dated.append(row)
    wanted = [one.split() for one in plainly]
    # A stable sort, so results the measure cannot tell apart keep the order
    # the catalogue put them in - which is the only opinion left at that point.
    dated.sort(key=lambda row: _how_near_the_name_is(row, wanted), reverse=True)
    return titled + dated


def _how_near_the_name_is(row: dict, wanted: list) -> tuple:
    """How near a result's name is to the folder's, as (does one name begin the
    other, how much of the two names is the same name).

    A tuple because the two are not worth adding up. The first is a SHAPE and
    the second a count, and where they disagree the shape is right - so they
    sort the way they read, and neither needs a weight invented for it.

    **One name beginning the other** is two names that run together from the
    left until one of them stops: "Sunfall Reckoning" against "Sunfall
    Reckoning Redux". That is the shape of a subtitle, of a market's title run
    on after the film's own, and of a label somebody added - which is to say it
    is the shape of the near misses this module has readings for, and of the
    ones it has no reading for yet. Scattered words that happen to coincide are
    not that shape however many of them there are.

    **How much of the two names is the same name** is the words they share
    against the words they do not, and it says the rest: a result carrying more
    of the folder's words is nearer, and a result saying less besides them is
    nearer - "Hard Border" is a better use of a request than "Wad: surviving on
    the border of water and land", and both carry the whole of a folder called
    "Border".

    A word count and not a fold, because a fold has already been asked here and
    said no. What is measured is how near a MISS is.

    ``wanted`` is the folder's own title as words, once per spelling it answers
    to; whichever of the result's own two titles answers best is the one that
    counts. (0, 0.0) where nothing can be measured, which puts the result
    behind everything that can be.
    """
    best = (0, 0.0)
    for title in _row_titles(row):
        words = normalize_title(str(title)).split()
        for name in wanted:
            if not words or not name:
                continue
            begins = words[:len(name)] == name or name[:len(words)] == words
            shared = len(set(words) & set(name)) / len(set(words) | set(name))
            best = max(best, (int(begins), shared))
    return best


def _candidate(api_key: str, row: dict, latin: bool = False) -> _Candidate:
    """One search result and its own document, read into what the rule asks.

    ``latin`` is for a folder named in Latin letters, and leaves out every
    title the catalogue has written in another script: what those fold to is
    the digits and the Latin-shaped letters in them, and a Thai title reduced
    to "09" met a folder called "Movie 09 - ..." on nothing but the number -
    and would have been the spelling the folder was renamed onto.
    """
    detail = tmdbclient._as_json(tmdbclient._curl(
        tmdbclient._BASE + "/movie/" + _id_token(row.get("id")),
        [("api_key", api_key), ("append_to_response", _APPENDED)]))
    readable = (lambda text: titlematch.in_latin_script(text)) if latin \
        else (lambda _text: True)
    folded: set = set()
    written: list = []
    for t in _detail_titles(detail) + _row_titles(row):
        text = str(t)
        if not readable(text):
            continue
        folded |= title_keys(text)
        if text not in written:
            written.append(text)
    return _Candidate(
        years=frozenset(_release_years(row, detail)),
        titles=frozenset(t for t in folded if t),
        spellings=tuple(written),
        own=frozenset(normalize_title(str(t))
                      for t in _row_titles(detail) + _row_titles(row)
                      + _translated_titles(detail)
                      if readable(str(t))) - {""},
        runtime=_minutes(detail.get("runtime")),
        imdb=_imdb_id((detail.get("external_ids") or {}).get("imdb_id")),
        released=_primary_year(detail) or _primary_year(row),
        tmdb=_id_token(row.get("id")).replace("null", ""),
        lead=frozenset(normalize_title(str(t))
                       for t in _row_titles(detail) + _row_titles(row)
                       if readable(str(t))) - {""})


def _primary_year(document: dict) -> str:
    """The year of the release date a TMDb document leads with, or ""."""
    date = document.get("release_date")
    if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
        return date[:4]
    return ""


def _row_titles(row: dict) -> list:
    """A search result's own two titles. A missing one is the literal word
    "null", so it is compared - and missed - like any other title rather than
    skipped."""
    titles = []
    for field in ("title", "original_title"):
        value = row.get(field)
        titles.append("null" if value is None else value)
    return titles


def _detail_titles(detail: dict) -> list:
    """A film document's titles: its own two, and every alternative it carries.

    An alternative with no title is skipped rather than read as "null", which
    is the one place the two differ.

    The ORIGINAL title leads, because this order is also the order a spelling to
    rename onto is picked in: a film's own name is the one to keep where the
    folder was named from it.
    """
    titles = list(reversed(_row_titles(detail)))
    alternatives = (detail.get("alternative_titles") or {}).get("titles") or []
    for entry in alternatives:
        if isinstance(entry, dict) and entry.get("title") is not None:
            titles.append(entry["title"])
    return titles + _translated_titles(detail)


def _translated_titles(detail: dict) -> list:
    """The film's title in each language TMDb has a translation for.

    Not the same list as the alternative titles, and the one the search reads:
    a Czech fairy tale is found under its German title because that title is
    its German translation, while its alternatives hold only the English and
    Slovak ones - so the search hands back the film and nothing in the document
    said why. A translation that leaves the title empty keeps the original, and
    says nothing.
    """
    translations = (detail.get("translations") or {}).get("translations") or []
    titles = []
    for entry in translations:
        data = entry.get("data") if isinstance(entry, dict) else None
        title = data.get("title") if isinstance(data, dict) else None
        if isinstance(title, str) and title.strip():
            titles.append(title)
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


def _near(year: str, wanted: str, apart: int = NEAR_YEARS) -> bool:
    """Whether two years are the same or next door - or no more than ``apart``
    from each other - and both are years."""
    if not (year.isdigit() and wanted.isdigit()):
        return False
    return abs(int(year) - int(wanted)) <= apart


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
    """The id as a URL path segment: a missing id is the literal word "null",
    not nothing.

    Digits or that word, and nothing else. The value comes out of the API's own
    JSON and is spliced into a URL PATH, where a "/" or a "#" in it asks for
    something other than what this meant to ask for; a TMDb id that is not a
    number is not an id.
    """
    if value is None:
        return "null"
    text = str(value)
    return text if text.isdigit() else "null"
