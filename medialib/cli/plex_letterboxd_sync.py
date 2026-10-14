"""plex-letterboxd-sync: what was watched, when, and how it was rated, kept
in step between a Plex server's movie libraries and a Letterboxd account.

The two directions are not alike, because Letterboxd takes nothing but a file:

* Letterboxd -> Plex marks every unplayed Plex film the account has logged as
  played, and gives a film Plex has no rating for Letterboxd's - on the
  server, directly. The day it was watched cannot go this way: Plex marks a
  film played as of the moment it is told to;
* Plex -> Letterboxd writes everything Letterboxd does not have yet into a CSV
  for its importer, which the user uploads: a diary entry for every play in
  Plex's history - a rewatch where it was not the first time - a rating where
  Letterboxd has none, and a film played with no play on record as a film
  watched.

A rating on both sides that disagrees is said, and neither is changed: which
of them is the newer is not something either side records.

Which Plex film is which Letterboxd film is `medialib/lib/filmmatch.py`, and
through it the same title fold the TMDb lookup uses - so a film written
differently on the two sides is still found.
"""

import datetime
import json
import os
import sys
from typing import NamedTuple

from medialib import commands
from medialib.lib import (
    clioptions,
    letterboxd,
    plexserver,
    runlog,
    titlematch,
    tmdblookup,
    tooldeps,
)
from medialib.lib.filmmatch import Film, FilmIndex, Found
from medialib.lib.letterboxd import Row

USAGE_HEAD = """Usage:
    {program} [options] <export.zip|account>
Argument:
    <export.zip|account>
                        A Letterboxd data export (Settings > Data > Export),
                        which holds everything the account has logged - or the
                        account's name, whose public RSS feed holds only the
                        fifty or so most recent diary entries.
Environment:
    plexUrl             The Plex server, e.g. http://192.168.1.10:32400
    plexToken           Its X-Plex-Token.
Options:"""

OPT_SPEC = """
s | <names> | Only the Plex movie libraries with these titles,
                    comma-separated. Default: every movie library.
l |  | Letterboxd -> Plex only: mark films played and rated, and
                    write no CSV.
o | <file> | Write the CSV for Letterboxd's importer to <file>. Default:
                    logs/letterboxd-import-<date>.csv beside this script.
k | <file> | Remember what Letterboxd has, and what was sent to it, in
                    <file>, and keep a copy of the Plex library beside it, so
                    the next run reads only what changed. Default:
                    data/plex-letterboxd-state.json beside this script.
a | <id> | Read the plays of the Plex account with this number on the
                    server rather than its owner's (1). The numbers are listed
                    at <plexUrl>/accounts.
f |  | Read the whole Plex library rather than what changed since the
                    last run - which a run also does by itself once a week, to
                    see films removed or marked unplayed.
p |  | Preview: say what would be marked, rated, added and
                    exported, and change nothing - not Plex, not the CSVs, not
                    the state file. The copy of the Plex library is kept.
h |  | Print this help page.
"""

OPT_VARS = ("s:sections l:toPlexOnly o:outOverride k:stateOverride a:account "
            "f:whole p:simulate")
OPT_COLUMN = 20
OPT_LONG = ("s:sections l:to-plex-only o:output k:state a:account f:full "
            "p:preview h:help")
OPT_CHECKS = """
a | nonNegInt | account number
"""


def spec(program: str) -> clioptions.Spec:
    return clioptions.with_verbosity(clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        checks=OPT_CHECKS,
        column=OPT_COLUMN,
    ))


def is_account(source: str) -> bool:
    """Whether the argument names an account rather than an export file."""
    return not (os.path.isfile(source) or source.lower().endswith(".zip")
                or os.sep in source)


def read_letterboxd(source: str, state: letterboxd.State) -> tuple:
    """What Letterboxd has, as (films, the day the export was made, the
    watchlist).

    An export is the whole of it and replaces what was known; a feed is the
    latest of it, merged into what was known, and has no day to say it is
    complete as of - "" - and no watchlist, so the one known stands.
    """
    if os.path.isfile(source):
        export = letterboxd.read_export(source)
        return (letterboxd.with_ids_of(export.films, state.films), export.made,
                letterboxd.with_ids_of(export.watchlist, state.watchlist))
    if not is_account(source):
        raise OSError('"%s" does not exist.' % source)
    return (letterboxd.merge(state.films, letterboxd.read_feed(source)), "",
            state.watchlist)


# --- ids for what a title does not settle -------------------------------------

# The rungs a Letterboxd film is taken as settled by without asking TMDb: an
# id, or the title in the same year. Anything weaker - a year apart, no year,
# a part of the name, a slip - is a reading of the title, and an id is what
# says whether the reading was right.
_SETTLED = ("id", "title")


def name_films(films: list, movies: list, unnamed: list,
               identify=tmdblookup.identify) -> tuple:
    """``films`` with TMDb's ids for the ones Plex's titles do not settle, as
    (films, the films TMDb could not name either).

    Asked of a film with no id that no Plex film matches, that two match
    equally, or that one matches only by a weaker reading. TMDb names a film
    only where it is certain, so an answer is an id both sides can be matched
    by - which finds the film Plex holds under another title altogether, and
    vetoes a reading that found the wrong one. A film TMDb cannot name is
    remembered, and not asked about again.
    """
    if not os.environ.get("tmdbApiKey", ""):
        runlog.detail("tmdbApiKey is not set: Letterboxd films are matched by "
                      "their titles alone")
        return films, unnamed
    plex = FilmIndex(movie.film for movie in movies)
    asked = set(unnamed)
    wanted = [at for at, film in enumerate(films)
              if not (film.tmdb or film.imdb) and str(film) not in asked
              and plex.find(film).how not in _SETTLED]
    if wanted:
        runlog.log("TMDb: looking up %d Letterboxd film(s) their titles do not "
                   "settle" % len(wanted))
    named = list(films)
    unnamed = list(unnamed)
    for at in wanted:
        film = named[at]
        match = identify(film.title, film.year)
        if match.tmdb or match.imdb:
            named[at] = film._replace(tmdb=match.tmdb, imdb=match.imdb)
            runlog.detail("%s - TMDb names it %s" % (film, match.tag))
        else:
            unnamed.append(str(film))
    return named, unnamed


# --- what Letterboxd has already ----------------------------------------------

def _has(film: Film | None, row: Row) -> bool:
    """Whether a Letterboxd film already carries what ``row`` says."""
    return film is not None \
        and (not row.date or row.date in film.dates) \
        and (not row.rating or film.rating == row.rating)


def settle_pending(pending: list, films: list, made: str) -> tuple:
    """The rows sent earlier, as (still waiting, offered again).

    A row Letterboxd now has is done with. One a full export made AFTER the
    day it was sent still does not have was never uploaded, and is dropped so
    that it is offered again; the day after is the margin, because an export
    and a CSV from the same day may have been made in either order. Every
    other row is still waiting, and is not offered twice.
    """
    index, sent = FilmIndex(films), FilmIndex(row.film for row in pending)
    waiting, again = [], []
    for row in pending:
        found = index.find_from(row.film, sent)
        if found.ambiguous or _has(found.film, row):
            continue
        if made and row.sent and row.sent < made:
            again.append(row)
        else:
            waiting.append(row)
    return waiting, again


# --- the plan -----------------------------------------------------------------

class Plan(NamedTuple):
    mark: list          # (movie, the Letterboxd film) - to mark played in Plex
    rate: list          # (movie, rating) - to rate in Plex
    rows: list          # Rows for Letterboxd's importer
    disagree: list      # (Plex film, Letterboxd film) rated differently
    ambiguous: list     # (Plex film, Found) matching two Letterboxd films


class _Group(NamedTuple):
    """One film as Plex holds it - every copy of it, across libraries."""

    movies: list
    found: Found        # among Letterboxd's films
    sent: list          # rows already sent for it, waiting


def _plain(film: Film) -> Film:
    """A film as a row names it: title, year and ids."""
    return Film(film.title, film.year, film.tmdb, film.imdb)


def _groups(movies: list, films: list, pending: list) -> list:
    """The Plex films, each with what Letterboxd and the waiting rows say of
    it - the copies of one film together, so a film held twice is one film."""
    on_letterboxd = FilmIndex(films)
    in_plex = FilmIndex(movie.film for movie in movies)
    by_film: dict[Film, list] = {}
    for row in pending:
        by_film.setdefault(_plain(row.film), []).append(row)
    sent = FilmIndex(by_film)
    groups: dict[object, _Group] = {}
    for movie in movies:
        found = on_letterboxd.find_from(movie.film, in_plex)
        waiting = sent.find_from(movie.film, in_plex)
        rows = by_film[waiting.film] if waiting.film is not None else []
        if found.matched:
            key: object = ("letterboxd", id(found.film))
        else:
            key = ("plex", titlematch.normalize_title(movie.film.title),
                   movie.film.year)
        if key in groups:
            groups[key].movies.append(movie)
        else:
            groups[key] = _Group([movie], found, rows)
    return list(groups.values())


def _rows_for(group: _Group) -> tuple:
    """What Letterboxd lacks of one Plex film, as (rows, a disagreement)."""
    plex = max((movie.film for movie in group.movies),
               key=lambda film: (bool(film.tmdb), bool(film.imdb)))
    played = any(movie.played for movie in group.movies)
    plays = sorted({day for movie in group.movies for day in movie.film.dates})
    rating = max(movie.film.rating for movie in group.movies)
    theirs = group.found.film
    known_days = set(theirs.dates if theirs else ()) \
        | {row.date for row in group.sent}
    known_rating = (theirs.rating if theirs else 0) \
        or max((row.rating for row in group.sent), default=0)
    earliest = min(filter(None, (*(theirs.dates if theirs else ()),
                                 theirs.logged if theirs else "")), default="")

    film = _plain(plex)
    rows = [Row(film, day, rewatch=bool(plays[:index]) or bool(earliest)
                and earliest < day)
            for index, day in enumerate(plays) if day not in known_days]
    disagree = None
    if rating and known_rating and rating != known_rating:
        disagree = (plex, theirs)
    elif rating and not known_rating:
        if rows:
            rows[-1] = rows[-1]._replace(rating=rating)
        else:
            rows.append(Row(film, rating=rating))
    if played and not rows and theirs is None and not group.sent:
        rows.append(Row(film))
    return rows, disagree


def plan(movies: list, films: list, pending: list, to_plex_only=False) -> Plan:
    """Everything a run would do, worked out before any of it is done."""
    result = Plan([], [], [], [], [])
    for group in _groups(movies, films, pending):
        theirs = group.found.film
        if group.found.ambiguous:
            # Which of two films Plex's is, is not known - so neither what
            # Letterboxd has of it nor what to tell Plex.
            if not any(movie.played for movie in group.movies):
                result.ambiguous.append((group.movies[0].film, group.found))
            continue
        if theirs is not None:
            plex_rated = any(movie.film.rating for movie in group.movies)
            for movie in group.movies:
                if not movie.played:
                    result.mark.append((movie, group.found))
                if theirs.rating and not plex_rated:
                    result.rate.append((movie, theirs.rating))
        if not to_plex_only and (any(movie.played for movie in group.movies)
                                 or theirs is not None):
            rows, disagree = _rows_for(group)
            result.rows.extend(rows)
            if disagree:
                result.disagree.append(disagree)
    return result


# --- the watchlists -----------------------------------------------------------

class Wishes(NamedTuple):
    to_letterboxd: list     # films for Letterboxd's watchlist import
    to_plex: list           # Letterboxd's films to add to Plex's watchlist


def settle_wanted(wanted: list, watchlist: list, films: list, made: str) -> tuple:
    """The films sent to the watchlist earlier, as (still waiting, offered
    again) - :func:`settle_pending`'s rule, where a film is done with once it
    is on the watchlist or has been watched, which takes it off one."""
    on_list, watched = FilmIndex(watchlist), FilmIndex(films)
    sent = FilmIndex(row.film for row in wanted)
    waiting, again = [], []
    for row in wanted:
        if any(_known(index, row.film, sent) for index in (on_list, watched)):
            continue
        if made and row.sent and row.sent < made:
            again.append(row)
        else:
            waiting.append(row)
    return waiting, again


def _known(index: FilmIndex, film: Film, origin: FilmIndex) -> bool:
    """Whether ``index`` holds ``film``, from ``origin`` - or might, as one of
    two it cannot choose between."""
    found = index.find_from(film, origin)
    return found.matched or bool(found.ambiguous)


def plan_watchlist(plex_wanted: list, watchlist: list, films: list, movies: list,
                   waiting: list, to_plex_only=False) -> Wishes:
    """What each watchlist lacks of the other's.

    A film already watched is not wanted on either: one Letterboxd has logged
    is not sent to its watchlist, and one Plex has played is not added to
    Plex's. Neither list loses anything here - a film taken off one is not
    taken off the other, because it is taken off by being watched, and the
    watching is what the rest of the run carries across.
    """
    wishes = Wishes([], [])
    if not to_plex_only:
        on_list, watched = FilmIndex(watchlist), FilmIndex(films)
        sent = FilmIndex(row.film for row in waiting)
        plex_list = FilmIndex(_plain(entry.film) for entry in plex_wanted)
        seen = set()
        for entry in plex_wanted:
            film = _plain(entry.film)
            if film not in seen and not any(_known(index, film, plex_list) for index in
                                             (on_list, watched, sent)):
                seen.add(film)
                wishes.to_letterboxd.append(film)
    in_plex_list = FilmIndex(entry.film for entry in plex_wanted)
    played = FilmIndex(movie.film for movie in movies if movie.played)
    lb_list = FilmIndex(watchlist)
    for film in watchlist:
        if not _known(in_plex_list, film, lb_list) and not _known(played, film, lb_list):
            wishes.to_plex.append(film)
    return wishes


def _wanted_in_plex(server, film: Film) -> bool:
    """Add a Letterboxd film to Plex's watchlist, and whether it was.

    Plex names it by its own catalogue's id, found by the film's TMDb id -
    which a Letterboxd export does not carry, so TMDb is asked for it first.
    """
    if not (film.tmdb or film.imdb) and os.environ.get("tmdbApiKey", ""):
        match = tmdblookup.identify(film.title, film.year)
        film = film._replace(tmdb=match.tmdb, imdb=match.imdb)
    try:
        key = server.catalogue_key(film)
        if not key:
            runlog.warn("%s: not found in Plex's catalogue, and not added to its "
                        "watchlist" % (film,))
            return True
        server.add_to_watchlist(key)
    except plexserver.PlexError as error:
        runlog.error("%s: %s" % (film, error))
        return False
    return True


def report_watchlist(wishes: Wishes, to_plex_only: bool) -> None:
    runlog.result("Watchlist, Letterboxd -> Plex: %d film(s) to add"
                  % len(wishes.to_plex))
    for film in wishes.to_plex:
        runlog.result("  " + str(film))
    if not to_plex_only:
        runlog.result("Watchlist, Plex -> Letterboxd: %d film(s) to import"
                      % len(wishes.to_letterboxd))
        for film in wishes.to_letterboxd:
            runlog.result("  " + str(film))


# --- saying it ----------------------------------------------------------------

def _said(plex: Film, found: Found) -> str:
    """A Plex film, with the Letterboxd film it was matched to where the two
    are not written alike."""
    if found.film is None or str(found.film) == str(plex):
        return str(plex)
    return "%s  <- Letterboxd: %s" % (plex, found.film)


def _stars(rating: int) -> str:
    return "%g stars" % (rating / 2)


def _row_said(row: Row) -> str:
    parts = [row.date or "watched, no date"]
    if row.rewatch:
        parts.append("rewatch")
    if row.rating:
        parts.append(_stars(row.rating))
    return "  %s: %s" % (row.film, ", ".join(parts))


def report(result: Plan, to_plex_only: bool) -> None:
    runlog.result("Letterboxd -> Plex: %d film(s) to mark played, %d to rate"
                  % (len(result.mark), len(result.rate)))
    for movie, found in result.mark:
        runlog.result("  " + _said(movie.film, found))
        runlog.detail("    matched by %s" % found.how)
    for movie, rating in result.rate:
        runlog.result("  %s: %s" % (movie.film, _stars(rating)))
    if result.ambiguous:
        runlog.warn("%d unplayed Plex film(s) match more than one Letterboxd "
                    "film, and are left unmarked:" % len(result.ambiguous))
        for film, found in result.ambiguous:
            runlog.warn("  %s - %s" % (film, "; ".join(map(str, found.ambiguous))))
    if result.disagree:
        runlog.warn("%d film(s) are rated differently on the two sides, and "
                    "neither rating is changed:" % len(result.disagree))
        for plex, theirs in result.disagree:
            runlog.warn("  %s: Plex %s, Letterboxd %s"
                        % (plex, _stars(plex.rating), _stars(theirs.rating)))
    if not to_plex_only:
        runlog.result("Plex -> Letterboxd: %d row(s) to import" % len(result.rows))
        for row in result.rows:
            runlog.result(_row_said(row))


# --- the run ------------------------------------------------------------------

def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, commands.program_name(__spec__.name), commands.script_dir())


def main(argv: list, program: str = "plex-letterboxd-sync",
         script_dir: str = "") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
        clioptions.settle_verbosity(result)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        sys.stderr.write(clioptions.usage_error_text(declaration, error.message))
        return 1

    if clioptions.args_out_of_range(len(result.positionals), 1, 1):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    values = result.values
    simulate = "p" in result.given
    to_plex_only = "l" in result.given
    sections = tuple(name.strip() for name in
                     (values["sections"] or "").split(",") if name.strip())
    script_dir = script_dir or commands.script_dir()
    state_path = values["stateOverride"] or os.path.join(
        script_dir, "data", "plex-letterboxd-state.json")
    today = datetime.date.today().isoformat()
    out_path = values["outOverride"] or commands.logs_file(
        script_dir, "letterboxd-import-%s.csv" % today)

    if tooldeps.require_tools(program, ["curl"]):
        return 1
    server = plexserver.from_environment(values["account"] or plexserver.OWNER)
    if server is None:
        runlog.error("Set plexUrl and plexToken to the Plex server and its token.")
        return 1

    try:
        state = letterboxd.read_state(state_path)
        if not state.films and is_account(result.positionals[0]):
            # The feed is the latest fifty entries: with nothing remembered
            # beside it, every film Plex has played would look new.
            runlog.error("Nothing is known about Letterboxd yet - the first run "
                         "needs a data export (Settings > Data > Export), not "
                         "the feed.")
            return 1
        films, made, lb_watchlist = read_letterboxd(result.positionals[0], state)
    except (OSError, ValueError) as error:
        runlog.error(str(error))
        return 1
    runlog.log("Letterboxd: %d film(s) known" % len(films))
    waiting, again = settle_pending(state.pending, films, made)
    if again:
        runlog.warn("%d row(s) sent before this export are not in it, and are "
                    "offered again" % len(again))
    if waiting:
        runlog.log("%d row(s) sent earlier are not on Letterboxd yet, and are "
                   "not sent again" % len(waiting))
    wanted_waiting, wanted_again = settle_wanted(state.wanted, lb_watchlist, films,
                                                 made)
    if wanted_again:
        runlog.warn("%d watchlist film(s) sent before this export are not on its "
                    "watchlist, and are offered again" % len(wanted_again))
    if wanted_waiting:
        runlog.log("%d watchlist film(s) sent earlier are not on Letterboxd's "
                   "watchlist yet, and are not sent again" % len(wanted_waiting))

    cache_path = os.path.splitext(state_path)[0] + "-plex.json"
    try:
        library = server.library(_read_cache(cache_path), "f" in result.given)
        movies = [movie for movie in library.movies
                  if not sections or movie.section in sections]
        runlog.log("Plex: %d film(s), %s" % (len(movies), "read whole"
                   if library.whole else "changes since the last run read"))
        films, unnamed = name_films(films, library.movies, state.unnamed)
        result_plan = plan(movies, films, waiting, to_plex_only)
        report(result_plan, to_plex_only)
        wishes = plan_watchlist(library.watchlist, lb_watchlist, films,
                                library.movies, wanted_waiting, to_plex_only)
        report_watchlist(wishes, to_plex_only)
        if simulate:
            # The copy of the library is kept even so: it is what Plex said,
            # not anything this run would change, and keeping it is what makes
            # the next run - a preview or not - read only what changed.
            _write_cache(cache_path, library.cache)
            runlog.log("Preview - nothing was changed.")
            return 0
        failed = 0
        for movie, _found in result_plan.mark:
            if _changed(movie, server.mark_played, movie):
                plexserver.updated(library.cache, movie._replace(played=True))
            else:
                failed += 1
        for movie, rating in result_plan.rate:
            if _changed(movie, server.rate, movie, rating):
                plexserver.updated(library.cache, movie._replace(
                    film=movie.film._replace(rating=rating)))
            else:
                failed += 1
        for film in wishes.to_plex:
            failed += not _wanted_in_plex(server, film)
    except plexserver.PlexError as error:
        runlog.error(str(error))
        return 1
    _write_cache(cache_path, library.cache)

    sent = [row._replace(sent=today) for row in result_plan.rows]
    if sent:
        letterboxd.write_import(out_path, sent)
        runlog.result('Wrote "%s" - upload it at https://letterboxd.com/import/'
                      % out_path)
    wanted = [Row(film, sent=today) for film in wishes.to_letterboxd]
    if wanted:
        watchlist_path = os.path.splitext(values["outOverride"])[0] + "-watchlist.csv" \
            if values["outOverride"] else commands.logs_file(
                script_dir, "letterboxd-watchlist-%s.csv" % today)
        letterboxd.write_watchlist(watchlist_path, wishes.to_letterboxd)
        runlog.result('Wrote "%s" - upload it with the import on your Letterboxd '
                      'watchlist page' % watchlist_path)
    letterboxd.write_state(state_path, letterboxd.State(
        films, waiting + sent, unnamed, lb_watchlist, wanted_waiting + wanted))
    return 1 if failed else 0


def _changed(movie, call, *args) -> bool:
    """One change on the server, and whether it was made. A failure is said
    and not fatal: a film removed since the library was last read whole cannot
    be marked, and the rest still can."""
    try:
        call(*args)
    except plexserver.PlexError as error:
        runlog.error("%s: %s" % (movie.film, error))
        return False
    return True


def _read_cache(path: str) -> dict | None:
    """The Plex library as the last run kept it, or None - and None for a
    copy that cannot be read, which only costs a whole read."""
    try:
        with open(path, encoding="utf-8") as handle:
            cache = json.load(handle)
    except (OSError, ValueError):
        return None
    return cache if isinstance(cache, dict) else None


def _write_cache(path: str, cache: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(cli())
