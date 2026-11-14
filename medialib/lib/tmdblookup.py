"""Plex/Jellyfin id tagging: a library's film folders named with their ids.

Ask an external API what a film is, and only rename when the answer is certain.

This is the walk and the renames. Which folders are films and what their names
say is :mod:`medialib.lib.filmfolders`; what a film is, asked of TMDb and the
IMDb datasets, is :mod:`medialib.lib.tmdbidentify` over
:mod:`medialib.lib.tmdbclient`; and the lists a run leaves behind are
:mod:`medialib.lib.tmdbworklists`.
"""

import functools
import os
import re
from collections.abc import Callable

from medialib.lib import (
    durationcheck,
    filmfolders,
    plexnames,
    safety,
    titlematch,
    tmdbidentify,
    tmdbworklists,
)


def without_its_own_year(title: str, year: str) -> str:
    """``title`` without a trailing " <year>" or " (<year>)" that is the film's
    own release year, so that adding " (<year>)" does not write it twice.

    A catalogue sometimes keeps the year in the title itself. A title that is
    nothing but a year, or ends in some other year, is the film's real name and
    is left whole.
    """
    if not year:
        return title
    match = re.match(r"^(.*\S) +(?:{0}|\({0}\))$".format(re.escape(year)),
                     title)
    return match.group(1) if match else title


class LongNames:
    """What the file name limit did to a run's renames, for the run to close
    with.

    Two outcomes, and they are not the same news. A commentary's transcript or
    its opus can be cut back into the track's own name - the transcription cuts
    that same stem at that same place when it writes one, so the file stays the
    file the next run looks for - and that is a rename to mention rather than
    one to hold back. Anything else the id tag pushes past the limit would have
    to be cut in the film's name, in the tag itself, or in the number that says
    which commentary a file is of, and none of those may move: that folder is
    left exactly as it was, listed here, and the rest of the library is renamed
    around it.
    """

    def __init__(self) -> None:
        self.cropped: list[tuple[str, str]] = []
        self.refused: list[tuple[str, str, str]] = []

    def crop(self, folder: str, name: str, target: str) -> None:
        self.cropped.append((os.path.join(folder, name), target))

    def refuse(self, folder: str, name: str, why: str = "") -> None:
        self.refused.append((folder, name, why))

    def report(self) -> list[str]:
        """The closing lines, or [] when the limit got in nobody's way.

        Silent when there is nothing to say: a run whose every name fitted says
        so by the library reading right, not by printing that it did.
        """
        lines: list[str] = []
        if self.cropped:
            lines.append(
                "WARNING: %d commentary file(s) were renamed with the track's "
                "name cut to fit the %d-byte file name limit:"
                % (len(self.cropped), plexnames.NAME_MAX_BYTES))
            for path, target in self.cropped:
                lines.append("  %s" % path)
                lines.append("    -> %s" % target)
        if self.refused:
            folders: dict[str, list[tuple[str, str]]] = {}
            for folder, name, why in self.refused:
                folders.setdefault(folder, []).append((name, why))
            lines.append(
                "ERROR: %d folder(s) were left unrenamed: a name goes past the "
                "%d-byte file name limit where nothing may be cut:"
                % (len(folders), plexnames.NAME_MAX_BYTES))
            for folder, names in folders.items():
                lines.append("  %s" % folder)
                for name, why in names:
                    lines.append("    %s%s" % (name, " (%s)" % why if why
                                               else ""))
        return lines


def tag_plex_ids(directory: str, log: Callable[[str], None],
                 skip_log: safety.SkipLog | None = None,
                 dry_run: bool = False, ids: dict | None = None,
                 unmatched: list | None = None, recursive: bool = False,
                 ambiguous: list | None = None,
                 planned: list | None = None,
                 near_misses: list | None = None,
                 aliases: list | None = None,
                 seen: set | None = None,
                 skip: set | None = None,
                 long_names: LongNames | None = None,
                 imdb: str = "",
                 tagged: dict | None = None) -> int:
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

    A folder whose year is written where nothing can read it is renamed onto a
    readable one first, so the rest of this reads a library whose film folders
    all say they are film folders.

    ``dry_run`` asks TMDb the same questions and works out the same names, then
    prints each rename instead of doing it: the name is the thing being
    previewed, so the lookup still happens. The repair above is held back with
    it, so a dry run says what it would rename such a folder to and nothing
    further: until that rename is made, there is no film folder there to look
    up.

    ``ids`` is a hand-written {folder name: tag} that answers BEFORE the network
    is asked, for the films TMDb cannot identify on its own. ``unmatched``, when
    a list is passed, collects the folder names that neither could name, and
    ``ambiguous`` the folders holding more than one film - both are left exactly
    as they are, and both lists are what the run writes its reports from.
    ``planned`` collects the renames a dry run would have made, for the third -
    the renames of the folders that went through ORDINARY naming, so that the
    four reports between them cover each folder exactly once. A folder one of
    the other lists is about is that list's to explain.
    ``near_misses`` collects what TMDb was asked and what it offered for every
    film it could NOT name, for the fourth - the one that says whether "no
    confident match" was the right answer. Only those: a folder another list
    already accounts for is that list's business, and a film in two of them is
    a film read twice. ``aliases`` collects the folders holding one film under
    several of its own titles, for the fifth: nothing is renamed for those, so
    no other list mentions them.

    ``seen`` collects the base name of every film folder this walk considered,
    which is what lets a caller holding a hand-written id list tell a row that
    named a folder from a row that named nothing: the ids are keyed by that
    same base, so whatever is not in here is not on disk. Collected for every
    folder rather than only the ones that consulted the list, because a film
    already carrying its id never asks for one and is still there.

    ``skip`` are folder base names not to ask the catalogue about at all. They
    are the rows a hand-written list carries with no id filled in: the pass that
    wrote that list is the one that already failed to name them, and asking the
    same question of the same catalogue is a network round trip whose answer is
    known. They still reach ``unmatched``, so a list rewritten from this run
    keeps the rows someone has yet to work through.

    ``long_names`` collects what the file name limit did - the commentary
    outputs cut back to fit it and the folders left alone because nothing in
    their names may be cut - for the run to close with. A folder over the limit
    is left exactly as it was and the ones after it are named as usual.

    ``recursive`` walks the whole tree rather than the one level the phase reads
    inside a full ingest, where the caller is pointed at the folder that holds
    the films and the phases around this one read that same one level.

    ``imdb`` is the local IMDb database each lookup ends on - see
    :mod:`medialib.lib.imdbdata` - or "" for TMDb alone.

    ``tagged`` collects {id tag: [folder path, ...]} for every film folder this
    walk knows the id of - the one it already carried and the one it was (or,
    on a dry run, would be) given alike - so that a caller can tell the same
    film kept in two folders. See :func:`duplicate_films`.
    """
    skip_log = skip_log if skip_log is not None else safety.SkipLog()
    long_names = long_names if long_names is not None else LongNames()
    if not os.environ.get("tmdbApiKey", ""):
        log("WARNING: tmdbApiKey not set, skipping IMDb id tagging")
        return 0

    filmfolders._repair_year_names(directory, recursive, skip_log, dry_run,
                                   log)
    for folder in filmfolders._candidates(directory, recursive):
        base, tag, title, year, edition = filmfolders.read_folder(folder.name)
        if not base:
            base, tag, title, year, edition = \
                filmfolders.read_undated_folder(folder)
        if not base or not filmfolders.is_film_folder(folder):
            continue
        if seen is not None:
            seen.add(base)

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

        # Usually the folder is saying the film's own words back: a loose film
        # is given a folder named after the file, so the release it names is on
        # the folder BECAUSE it is on the film. An echo carries nothing, and
        # handing it to a film that named no release is how a theatrical cut
        # sitting beside an extended one ends up under the other one's name.
        if edition in plexnames.editions_in(base, respelled):
            edition = ""

        # What the folder ALREADY said its film was, before anything was asked.
        # The only id that may be put on a file whose name this run cannot
        # account for: a freshly looked-up one says what the FOLDER is, and
        # stamping it on a stray is how a sequel gets filed under the film
        # before it.
        had_tag = tag
        asked = not tag
        by_hand = False
        found = tmdbidentify.Match()
        # The spellings of its own name this folder turns out to answer to
        # besides the one it is written under: the year a catalogue vouched is
        # the same film, and the name it was written under before the catalogue
        # respelled it. Both are collected here and read in one place below.
        also: tuple = ()
        redated = False
        glued = None
        if asked:
            # The hand-written id first: a film someone has already looked up is
            # not worth asking an API that has already failed to name it.
            tag = (ids or {}).get(base, "")
            by_hand = bool(tag)
            if not tag and base in (skip or ()):
                # Listed by hand and left blank: nothing is asked, and the row
                # is kept so it is still there to fill in.
                if unmatched is not None:
                    unmatched.append(base)
                continue
            if not tag:
                notes: list = []
                # "The Movie(1999)" is asked about as "The Movie" of 1999. What
                # the folder said is put back if nothing answers, so a title
                # that really does end that way is left exactly as it was.
                glued = None if year else filmfolders._GLUED_YEAR.match(title)
                if glued:
                    title, year = glued.group(1), glued.group(2)
                found, year, also = tmdbidentify._settle_the_year(
                    title, year, plexnames.years_disagreeing(base, respelled),
                    functools.partial(_folder_runtime, folder.path, base,
                                      respelled, on_disk, edition),
                    notes if near_misses is not None else None,
                    (_with_the_folder_above(directory, folder, title),
                     _without_the_folder_above(directory, folder, title)),
                    imdb,
                    functools.partial(tmdbidentify._written_year, [
                        os.path.join(folder.path, name) for name in names
                        if plexnames.is_movie_file(name)]))
                tag = found.tag
                if glued and not tag:
                    title, year, glued = glued.group(0), "", None
                # A folder that said no year, or one the catalogue has the film
                # nowhere near, takes the year the catalogue dates it to: it
                # is the one the match was made in.
                redated = bool(tag and found.year and year
                               and found.year != year)
                year = found.year or year
                # The catalogue's own spelling of whichever title matched is
                # what the whole folder is written under from here: it is the
                # one spelling of the several that is known to be right, and
                # the folder and its files disagreeing about which to use is
                # what made this hard to begin with. Worked out now and SAID
                # later, because a folder that turns out to be a muddle is
                # renamed to nothing and a line about renaming it would be a
                # line about something that did not happen.
                if tag:
                    spelled = "%s (%s)" % (
                        without_its_own_year(found.title or title, year), year)
                    if spelled != base:
                        # The name it had goes on the list of names it answers
                        # to, rather than the files being re-based onto the new
                        # one: the files matched the name the FOLDER had, and a
                        # catalogue's fuller title need not fold to anything
                        # they say - "Episode IV - Der Sturm" is not "Nordwind:
                        # Episode IV - Der Sturm". Reading them against the new
                        # name alone would leave this film's own files behind
                        # as strays.
                        also += (base,)
                        base = spelled
            if not tag:
                # Nothing answered to the FOLDER's name. Its files have names
                # of their own, and a library that holds a film under two
                # languages usually names the folder in a third - so the files
                # are asked in their own right, and a folder whose every film
                # comes back as one film IS that film. Asked here, before
                # anything judges the folder, because a folder judged first is
                # reported for holding films that are not its own and never
                # gets this far.
                settled, byfile = _named_by_its_files(base, year, folder.path,
                                                      names, notes, imdb)
                if settled:
                    _claim(tagged, settled, folder.path)
                    _flag_alias(aliases, folder.path, base, settled, byfile)
                    _tag_only(folder, names, settled, skip_log, dry_run, log,
                              None, functools.partial(
                                  _say_the_files_named_it, log, base, settled),
                              long_names=long_names)
                    continue

        # A folder whose films all spell the title one slip differently from
        # the folder itself. The slip is the folder's: the films are what
        # somebody was handed and the folder is what was written around them,
        # and more names say theirs than say its. Taken BEFORE the readings
        # below, so the folder is renamed onto its films rather than every
        # file that had it right being renamed onto the folder's mistake.
        # A folder tagged by an earlier run that wrote the year twice, because
        # the catalogue's title already ended in it: brought back to one.
        if had_tag and without_its_own_year(title, year) != title:
            also += (base,)
            base = "%s (%s)" % (without_its_own_year(title, year), year)
        if had_tag:
            theirs, said = plexnames.films_own_spelling(base, names)
            if theirs and said > 1:
                log('  "%s" is spelled "%s" by %d of its files - the folder '
                    "has the slip in it, and takes their spelling"
                    % (base, theirs, said))
                also += (base,)
                base = theirs

        # The names read once more, now that something has vouched for the one
        # they are being read against. Two readings are only safe against a
        # vouched-for name and so were held back from the first pass: the other
        # spellings this folder answers to, and the single letter somebody got
        # wrong. A name the first pass explained is explained the same way -
        # these only reach what it left over.
        if also or tag:
            corrected = plexnames.spelling_renames(base, names, also,
                                                   bool(tag))

        # A file that is this film under another of its titles AND says
        # something else besides - a language written after the year, for a
        # library that cannot mux them into one file. That something else is an
        # edition, and once it is written as one Plex collapses the files into
        # one film with a picker, so the title in front of it may take the
        # folder's spelling: nothing is lost that the edition does not keep.
        #
        # Never over a correction the NAMES themselves account for: what the
        # two readings disagree about is always the catalogue reaching further
        # than it should.
        for name, wanted in plexnames.alias_renames(
                base, names, found.aliases).items():
            corrected.setdefault(name, wanted)
        respelled = [corrected.get(name, name) for name in names]
        on_disk = {new: old for old, new in corrected.items()}

        # A folder holding one film under several of its titles. Only the
        # catalogue can say so, and what it buys is recognition and not a
        # rename: each file keeps the language its owner named it in, and gains
        # the id that says which film they all are.
        strays = plexnames.strays_in(base, respelled)
        known = plexnames.named_by_catalogue(strays, found.aliases, base) \
            if tag and strays else {}
        if strays and len(known) == len(strays):
            _claim(tagged, tag, folder.path)
            _flag_alias(aliases, folder.path, base, tag, known)
            # The FOLDER still takes the catalogue's spelling, the way every
            # other match here does - it was matched on its own name. Only the
            # files the catalogue vouched for keep theirs.
            _tag_only(
                folder, names, tag, skip_log, dry_run, log, None,
                functools.partial(_say_the_titles_met, log, base, len(known)),
                base, long_names)
            continue

        trouble = _what_is_wrong(base, respelled, also, bool(tag))
        if trouble:
            left = [on_disk.get(name, name) for name in trouble[1]]
            # Reported either way. A folder whose id was settled is still a
            # folder nothing could rename, and whether it REALLY could not is
            # the thing a person has to look at - an id going on quietly is
            # what would keep it off the list it belongs on.
            anyway = _tag_and_leave(folder, names, had_tag, skip_log, dry_run,
                                    log, None, long_names)
            reason = trouble[0] + (tmdbworklists.TAGGED_ANYWAY if anyway
                                   else "")
            _flag(ambiguous, folder.path, reason, left)
            if anyway:
                # _tag_and_leave only tags a folder whose ids are all one.
                _claim(tagged, (plexnames.ids_in(names)
                                | ({had_tag} if had_tag else set())).pop(),
                       folder.path)
            else:
                log('  "{}" {} - left as it is'.format(base, trouble[2]))
            continue

        if asked and not tag:
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
        # Which film this folder is, whatever becomes of its name below: a
        # rename refused for landing on its tagged twin is that very duplicate.
        _claim(tagged, tag, folder.path)
        wanted = plexnames.folder_name(base, tag)
        target = filmfolders._folder_spelling(
            os.path.dirname(folder.path) or ".", wanted)
        if wanted != folder.name and os.path.exists(target):
            skip_log.record(folder.path, target)
            log('  "{}" is already there, left "{}" untouched'
                .format(wanted, folder.name))
            continue

        # Said before the renames it explains, so a dry run reads as a film and
        # then what would happen to it. Only a folder this run ASKED about says
        # anything: one already tagged would repeat its line for the rest of
        # the library's life.
        if asked and glued:
            log('  "{}" has its year glued onto its title, and TMDb knows it '
                'as that - renaming it "{}"'.format(folder.name, base))
        elif asked and not filmfolders.read_folder(folder.name)[0]:
            log('  "{}" says no year, and TMDb dates it {} - renaming it "{}"'
                .format(folder.name, year, base))
        elif asked and redated:
            log('  "{}" is dated {} by TMDb, and its length agrees - renaming '
                'it "{}"'.format(folder.name, year, base))
        elif asked and base != filmfolders.read_folder(folder.name)[0]:
            log('  TMDb spells it "{}" - renaming "{}" onto it'.format(
                base.rsplit(" (", 1)[0], folder.name))
        if asked:
            _report(log, base, tag,
                    plexnames.editions_in(base, respelled, edition), by_hand)
        # A folder already carrying its id says nothing when it has nothing to
        # do - it would repeat its line for the rest of the library's life -
        # but the renames it DOES make are its files being brought into line
        # with an id it has had all along, and unexplained they read as renames
        # nobody asked for.
        announce = None if asked else functools.partial(
            _say_already_tagged, log, base, tag)
        _rename_in_place(folder, names, base, tag, target, skip_log, dry_run,
                         log, planned, found.aliases,
                         corrected, long_names, edition, announce)
    return 0


def _claim(tagged: dict | None, tag: str, path: str) -> None:
    """Note that the folder at ``path`` is the film ``tag`` names."""
    if tagged is not None and tag:
        tagged.setdefault(tag.lower(), []).append(path)


def duplicate_films(tagged: dict) -> list:
    """The films held in more than one folder, as [(tag, [folder, ...])].

    Only separate folders count: the several files INSIDE one folder are its
    versions, and a folder inside another film's folder belongs to it. Two
    folders anywhere else - beside each other, in different parts of the tree,
    on different disks - are the same film kept twice.
    """
    found = []
    for tag in sorted(tagged):
        folders = sorted({os.path.abspath(path) for path in tagged[tag]})
        apart = [path for path in folders
                 if not any(path.startswith(other + os.sep)
                            for other in folders if other != path)]
        if len(apart) > 1:
            found.append((tag, apart))
    return found


# What can be wrong with a folder that no id settles, as
# (reason for the report, the files it is about, how the run says it).
def _what_is_wrong(base: str, names: list, also=(), typos: bool = False):
    """The one thing this folder cannot be tagged for, or None.

    Asked in the order the answers are worth having: a second film in the folder
    is a different problem from one film written as parts, and each is reported
    under its own name.

    ``also`` and ``typos`` are the readings the names were respelled with, so
    that a name left unrespelled can be told apart from one no reading reaches.
    """
    # A folder holding a film that is not this folder's is left whole and
    # reported instead. A name that EXTENDS the folder's own - or that SAYS the
    # folder's own in another spelling - is one of this film's releases; one
    # that does neither is something else, and guessing which film it is is how
    # a second feature becomes an edition of the first.
    strays = plexnames.strays_in(base, names)
    twins = {name: plexnames.onto_base(base, name, also, typos)
             for name in strays}
    others = [name for name in strays if not twins[name]]
    if others:
        return ("holds a film that is not its own", others,
                "holds %d file(s) that are not this film" % len(others))
    # What is left DOES say this film, and was only left unrespelled because
    # its respelling is a name the folder already holds: the same film twice,
    # once under each spelling. Which of the two to keep is not a naming
    # question, and calling the second one a different film sends someone
    # looking for a film that is not there.
    if strays:
        both = sorted(set(strays) | {twins[name] for name in strays
                                     if twins[name] in names})
        return ("holds the same film twice, spelled two ways", both,
                "holds the same film twice, spelled two ways")

    # A part written the way a person writes one - "Part 1 - The First Half" -
    # would become an edition, and Plex would read three separate releases of
    # the film where there is one film in three files. Nothing here can make it
    # stack either: the token has to be last, and there is a title sitting after
    # it. A part written "Part 1" and nothing else IS fixable, and was fixed
    # before this was asked.
    in_parts, markers, misdated = [], [], []
    for stem in plexnames.movie_stems(base, names):
        edition = plexnames.read_stem(base, stem)[0]
        if plexnames.names_a_part(edition):
            in_parts.append(stem + ".mkv")
        elif plexnames.is_only_a_year(edition):
            misdated.append(stem + ".mkv")
        elif plexnames.is_only_a_marker(edition):
            markers.append(stem + ".mkv")
    if in_parts:
        return ("is one film in parts that do not stack", sorted(in_parts),
                "is in parts that Plex will not stack")
    # A digit wrong in a file's year, which reads as a bare number exactly the
    # way a copy's "(1)" does and is nothing like it. Which of the two dates is
    # the right one is not a thing to guess at - the folder's is the one TMDb
    # was asked about, but only someone who knows the film can say the file was
    # not a different cut of it.
    if misdated:
        return ("holds the same film under another year", sorted(misdated),
                "is dated one way by its folder and another by its files")
    # A "(1)" that could not simply be taken off, because taking it off would
    # put two files under one name. Which of the two to keep is not a naming
    # question, and an edition called "1" says nothing about either.
    if markers:
        return ("holds a duplicate marked only by a number", sorted(markers),
                "holds a duplicate marked only by a number")
    return None


def _tag_and_leave(folder, names: list, tag: str, skip_log: safety.SkipLog,
                   dry_run: bool, log: Callable[[str], None],
                   planned: list | None,
                   long_names: LongNames | None = None) -> bool:
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
    return _tag_only(
        folder, names, only, skip_log, dry_run, log, planned,
        lambda: log('  "{}" cannot be renamed, but every id in it says {} - '
                    "tagging only".format(folder.name, only)),
        long_names=long_names)


def _tag_only(folder, names: list, only: str, skip_log: safety.SkipLog,
              dry_run: bool, log: Callable[[str], None],
              planned: list | None,
              announce: Callable[[], None] | None = None,
              base: str = "",
              long_names: LongNames | None = None) -> bool:
    """Put ``only`` on the folder and on every name still without an id, and
    change nothing else about any of them.

    ``announce`` is called once, and only when there is something to do: a
    folder already carrying its id throughout is finished, and a line about it
    would be a line repeated for the rest of the library's life.

    False for a folder where the tag would push a name past the file name
    limit, having changed nothing: the id is exactly the thing that does not
    fit, so there is no tagging to be had here either.
    """
    cropped: list = []
    over_limit: list = []
    plan = plexnames.id_tag_renames(only, names, cropped, over_limit)
    wanted = plexnames.folder_name(
        base or plexnames.untagged_base(folder.name)[0], only)
    target = filmfolders._folder_spelling(
        os.path.dirname(folder.path) or ".", wanted)
    if not plexnames.fits_name(wanted):
        over_limit.append((folder.name, wanted))
    if over_limit:
        if long_names is not None:
            for name, _target in over_limit:
                long_names.refuse(folder.path, name)
        return False
    if wanted != folder.name and os.path.exists(target):
        skip_log.record(folder.path, target)
        return False
    if not plan and wanted == folder.name:
        return True
    if announce is not None:
        announce()
    if long_names is not None:
        for name, cut in cropped:
            long_names.crop(folder.path, name, cut)
    done = True
    for old, new in plan:
        if not _rename(os.path.join(folder.path, old),
                       os.path.join(folder.path, new), skip_log, dry_run, log,
                       planned, long_names):
            done = False
    if done:
        _rename(folder.path, target, skip_log, dry_run, log, planned,
                long_names)
    return True


def _folder_runtime(path: str, base: str, names: list,
                    on_disk: dict | None = None,
                    edition: str = "") -> float:
    """How long the film in this folder is, in seconds - or 0.0 when nothing
    here can say, which is most of the ways a folder can be arranged.

    A folder holding named editions says nothing on purpose: a catalogue states
    one runtime, and which of a theatrical and an extended cut it is for is
    exactly what is not known. A folder that names the edition itself, in
    ``edition``, is the same folder - the cut is named once instead of once per
    file, and the catalogue is no better placed to say which it timed. A split
    film is its parts added up, which IS what the catalogue states for it.
    Anything else is the one file, measured the way every other duration in
    this library is - the container's own figure.

    ``names`` are the names the folder WILL hold, which is what says how the
    films in it are arranged; ``on_disk`` maps any of those back to the name the
    file is under now, which is what can actually be opened.
    """
    if plexnames.editions_in(base, names, edition):
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
    title on each: "Nordwind" holding "Episode IV - Der Sturm", "The Keeper of
    the Keys" holding "The Circle of Keys". Neither half is the title
    a catalogue has, and put together they are.

    The mirror of the franchise a library repeats on every film IN it, which the
    search spellings already take OFF - a name can carry too little of the title
    as easily as too much.

    "" for a film sitting directly in the folder the run was pointed at: that
    one is the library, named by whoever typed it, and gluing it to every title
    underneath would ask about "Films Rivertown" once per film.
    """
    above = os.path.dirname(os.path.abspath(folder.path))
    if above == os.path.abspath(root):
        return ""
    # Without whatever tag an earlier run put on it, which is not part of any
    # title. It cannot be a film folder itself - the walk stops at one and does
    # not look inside - so what is left is a container someone named.
    name = plexnames.untagged_base(os.path.basename(above))[0].strip()
    return name + " " + title if name else ""


def _without_the_folder_above(root: str, folder, title: str) -> str:
    """This film's title with the folder above it taken OFF the front, or "".

    The other way a library writes a franchise on two folders: "Marekk Halvor"
    holding "Marek Halvor - Sunfall (2019)", where the film's own name is
    everything after the artist, and the artist is said twice on the way down
    and not at all in the catalogue.

    The folder above is what makes it safe to drop, and nothing else could.
    Plenty of real titles have a " - " or a ": " in them and mean both halves;
    the one thing that says a prefix is a franchise rather than half a name is
    somebody having already used it as a heading. A misspelling of it counts -
    "Marekk" over "Marek" is the same heading written twice by one hand,
    which is exactly what the case is made of.
    """
    above = os.path.dirname(os.path.abspath(folder.path))
    if above == os.path.abspath(root):
        return ""
    name = plexnames.untagged_base(os.path.basename(above))[0].strip()
    heading, rest = titlematch.leading_segment(title)
    if not name or not rest:
        return ""
    return rest if titlematch.equivalent(heading, name) \
        or titlematch.one_slip_apart(heading, name) else ""


def _named_by_its_files(base: str, year: str, path: str, names: list,
                        notes: list | None = None, imdb: str = "") -> tuple:
    """The id every film in this folder answers to, and what each answered
    under - or ("", {}) when they do not agree, or cannot all be named.

    For the folder whose OWN name is the one nothing knows. A library that
    holds a film under two languages usually names the folder in a third, and
    that third is the one being looked up: the files are the only names here a
    catalogue has ever heard of.

    Asked of every film in the folder and not just one, because a folder whose
    films answer to two different ids is a folder holding two films - which is
    exactly the thing an id must not be guessed over. Every one of them has to
    answer, and all of them to the same id.

    Only the files whose names DIFFER from the folder's are asked: one that
    says what the folder says has already been looked up under it.
    """
    movies = sorted(name for name in names if plexnames.is_movie_file(name))
    if not movies:
        return "", {}
    named = filmfolders._YEAR_RE.match(base)
    folder_title = named.group(1) if named else base
    found: dict[str, str] = {}
    ids = set()
    for name in movies:
        stem = plexnames.film_key(os.path.splitext(name)[0])
        match = filmfolders._YEAR_RE.match(stem)
        title, its_year = (match.group(1), match.group(2)) if match \
            else (stem, year)
        if titlematch.equivalent(title, folder_title):
            continue
        tmdbidentify._note(notes, 'asked under its own file "%s":' % name)
        settled = tmdbidentify.identify(
            title, its_year, functools.partial(_one_film_runtime, path, name),
            notes, imdb=imdb,
            written=functools.partial(tmdbidentify._written_year,
                                      [os.path.join(path, name)]))
        if not settled.tag:
            return "", {}
        ids.add(settled.tag)
        found[name] = settled.title or title
    if len(ids) != 1 or not found:
        return "", {}
    return ids.pop(), found


def _one_film_runtime(path: str, name: str) -> float:
    """How long one movie file is, for a lookup asking about that file alone."""
    return durationcheck.total_duration([os.path.join(path, name)])


def _say_the_files_named_it(log: Callable[[str], None], base: str,
                            tag: str) -> None:
    """The line a folder gets when its own name said nothing and its files
    said everything."""
    log('  "{}" is not a name TMDb knows, but every film in it is {} - '
        "tagging only".format(base, tag))


def _say_the_titles_met(log: Callable[[str], None], base: str,
                        how_many: int) -> None:
    """The line a folder gets when the catalogue's own titles are what held it
    together."""
    log('  "{}" holds {} file(s) TMDb knows as other titles of this film - '
        "tagging only".format(base, how_many))


def _flag_alias(aliases: list | None, path: str, base: str, tag: str,
                known: dict) -> None:
    """Record a folder whose files are this film under other titles of its own.

    Nothing was renamed for it, so there is no rename list to read it out of -
    this is the only place it is written down, and the only way to see that the
    recognition is working.
    """
    if aliases is not None:
        aliases.append((path, base, tag, dict(known)))


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


def _say_already_tagged(log: Callable[[str], None], base: str,
                        tag: str) -> None:
    """Said once, and only by a folder that turns out to have renames to make:
    it was tagged before this run and what follows is its files catching up."""
    log('  already {}: "{}" - bringing its files into line'.format(tag, base))


def _rename_in_place(folder, names: list, base: str, tag: str, target: str,
                     skip_log: safety.SkipLog, dry_run: bool,
                     log: Callable[[str], None],
                     planned: list | None = None, aliases=(),
                     corrected: dict | None = None,
                     long_names: LongNames | None = None,
                     edition: str = "",
                     announce: Callable[[], None] | None = None) -> None:
    """One folder's films and sidecars, then the folder itself.

    ``announce`` is said once before the first rename and not at all where
    there is none, for the caller that has a line to print only about a folder
    that turns out to have work in it.

    That order and no other: renaming the folder first would move every path
    underneath it out from under the names just worked out.

    An empty ``tag`` still does the rest of the work: which release a file is
    is on the disk already, whether or not TMDb could say which film they are
    all versions of.

    ``edition`` is a release only the FOLDER named, for the films in it that
    name none of their own. Empty where the films say it themselves, which is
    the ordinary case: there the folder simply sheds words it was only echoing.

    ``corrected`` are the respellings the caller has already worked out and
    reported on. Handing them over rather than letting them be worked out again
    is what keeps the renames and the reports one answer: the caller read this
    folder under everything the catalogue told it, and a second reading would
    only know what the two names say.

    A name the id tag would push past the file name limit stops the folder
    outright, before a single file has moved: the tag is not a thing that may be
    cut, and half a folder renamed is worse than none of it. The folder goes
    into the closing report and the run carries on to the next film.
    """
    cropped: list = []
    over_limit: list = []
    plan = plexnames.folder_renames(base, tag, names, aliases, corrected,
                                    cropped, over_limit, edition)
    if not plexnames.fits_name(os.path.basename(target)):
        over_limit.append((folder.name, os.path.basename(target)))
    if over_limit:
        if long_names is not None:
            for name, _wanted in over_limit:
                long_names.refuse(folder.path, name)
        log('  "{}" cannot be named without going past the {}-byte file name '
            "limit - left as it is, with its files".format(
                base, plexnames.NAME_MAX_BYTES))
        return
    if long_names is not None:
        for name, cut in cropped:
            long_names.crop(folder.path, name, cut)
    if announce is not None and (plan or os.path.basename(target)
                                 != folder.name):
        announce()
    done = True
    for old, new in plan:
        if not _rename(os.path.join(folder.path, old),
                       os.path.join(folder.path, new), skip_log, dry_run, log,
                       planned, long_names):
            done = False
    # Only over a folder whose files all landed: a folder renamed over
    # half-renamed contents files them under a name they do not carry.
    if done:
        _rename(folder.path, target, skip_log, dry_run, log, planned,
                long_names)


def _rename(source: str, target: str, skip_log: safety.SkipLog,
            dry_run: bool = False,
            log: Callable[[str], None] | None = None,
            planned: list | None = None,
            long_names: LongNames | None = None) -> bool:
    """A rename that refuses to land on something that is already there, or to
    hide what it moves - and that only SAYS what it would do on a dry run.

    ``planned`` collects what a dry run would have done, so the run can hand it
    over as a file rather than as output that scrolls away.

    True unless the rename was attempted and the system refused it, which is
    what tells the caller to stop working on this folder. A rename that was not
    needed and one that was held back to save what is already there are both
    outcomes rather than failures, and both answer True.
    """
    if source == target:
        return True
    if safety.would_hide(target) or os.path.exists(target):
        skip_log.record(source, target)
        return True
    if dry_run:
        if log is not None:
            log('    would rename: "{}" -> "{}"'.format(
                os.path.basename(source), os.path.basename(target)))
        if planned is not None:
            planned.append((source, target))
        return True
    try:
        os.rename(source, target)
    except OSError as error:
        # One film's rename, and never the run's: the library behind this
        # folder still has its names to gain, and a traceback here would cost
        # it every one of them.
        if long_names is None:
            raise
        long_names.refuse(os.path.dirname(source), os.path.basename(source),
                          str(error))
        return False
    return True
