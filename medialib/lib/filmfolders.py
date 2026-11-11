"""Reading a library's film folders: which folders are films, what a folder
name says, and the year spellings that are repaired on the disk before
anything is looked up.

A movie folder is "Title (Year)", optionally tagged with its id; one with
no year is read too, from the films inside it. The walk that decides which
folders to descend into and the tagging that decides what to look up both
ask here, so the two never disagree about what a film folder is.
"""

import os
import re
from collections.abc import Callable

from medialib.lib import plexnames, safety

# A movie folder is "Title (Year)" where the Year is 1xxx or 2xxx. The greedy
# title takes everything up to the LAST "(Year)", so "Nighthawk (1999) (2005)"
# reads as title "Nighthawk (1999)", year "2005".
#
# Whatever follows the year is what the RELEASE says about itself, read exactly
# as the same words are when a file rather than a folder carries them: a
# "The Movie (1999) Extended Edition" is this film's extended edition and not a
# folder of some other shape.
_YEAR_RE = re.compile(r"^(.+) \(([12][0-9]{3})\)(.*)$")

# The five ways a folder writes a year that no reader here can see - or that it
# reads as half of the title. None of them is a matching question: each is a
# name that is wrong on the disk, and the folder is renamed onto the right one
# before anything is looked up.
#
# The space before the bracket written as some other separator -
# "The Movie-(1999)" - which stops the name being a film folder at all; or
# written as several spaces, which leaves the title carrying a trailing one into
# everything it is asked under. Then a space written INSIDE the brackets,
# "(1999 )". Then one of the brackets lost, to a rename that cut the name short
# or to a filesystem that would not take it. Each is repaired, and only ever
# where a bracket is still THERE: a trailing number with no bracket at all is
# as likely to be part of the title as a year, and "The Movie 2049" is not a
# film from the year 2049.
_SEPARATOR_RUN = "[" + re.escape(plexnames.SEPARATORS.replace(" ", "")) + "]+"
_BROKEN_YEAR = (
    re.compile(r"^(.+?)" + _SEPARATOR_RUN + r"\(([12][0-9]{3})\)$"),
    re.compile(r"^(.+?)\s{2,}\(([12][0-9]{3})\)$"),
    re.compile(r"^(.+?)\s*\((?:\s+([12][0-9]{3})\s*|([12][0-9]{3})\s+)\)$"),
    re.compile(r"^(.+?)\s*\(\s*([12][0-9]{3})\s*$"),
    re.compile(r"^(.+?)\s+([12][0-9]{3})\s*\)\s*$"),
)

# A year glued onto the title with no space at all - "The Movie(1999)". Not one
# of the repairs above: a bracketed year written straight after a word can be
# part of a title, so the disk is not touched on the name's word alone. The
# film is ASKED about as that title and that year, and only a match renames it.
_GLUED_YEAR = re.compile(r"^(.*\S)\(([12][0-9]{3})\)$")


def repair_year(base: str) -> str:
    """``base`` with the brackets around its year spelled the way everything
    here reads them, or ``base`` unchanged.

    A name already written that way matches none of the four and comes back as
    it is, which is what the lazy title and the anchored year buy: the run
    before the bracket has to be wrong for any of them to fire.
    """
    for pattern in _BROKEN_YEAR:
        match = pattern.match(base)
        if match:
            return "%s (%s)" % (match.group(1).rstrip(),
                                match.group(2) or match.group(3))
    return base


def read_folder(name: str) -> tuple:
    """A folder name read as (base, tag, title, year, edition), or five empties.

    The one place a folder name is taken apart, so what counts as a film folder
    is the same question wherever it is asked - the walk that decides which
    folders to descend into, and the tagging that decides what to look up.

    ``base`` stops at the year, and anything the folder said after it comes back
    as ``edition`` instead: a folder is the film and its id and nothing else,
    and which release it holds is a thing its films carry. Mostly it is the
    film's own words said twice, because the phase that gives a loose film a
    folder names that folder after the file.
    """
    base, tag = plexnames.untagged_base(name)
    match = _YEAR_RE.match(base)
    if not match:
        return "", "", "", "", ""
    return ("%s (%s)" % (match.group(1), match.group(2)), tag,
            match.group(1), match.group(2),
            plexnames.edition_name(match.group(3)))


def _folder_spelling(directory: str, name: str) -> str:
    """The path of a child of ``directory``: a bare "." writes "./name", a
    trailing slash is not doubled, anything else joins with one slash."""
    if directory == ".":
        return "./" + name
    if directory.endswith("/"):
        return directory + name
    return directory + "/" + name


def _candidates(directory: str, recursive: bool) -> list:
    """The folders to consider: one level for a full ingest, the whole tree for
    a run that was pointed at a library rather than at a film folder."""
    if not recursive:
        return [entry for entry in os.scandir(directory)
                if entry.is_dir(follow_symlinks=False)]
    return _film_folders(directory)


def _repair_year_names(directory: str, recursive: bool,
                       skip_log: safety.SkipLog, dry_run: bool,
                       log: Callable[[str], None]) -> None:
    """Rename every folder whose year is written where nothing can read it.

    Walked and renamed BEFORE a single thing is looked up, because a folder
    written "The Movie-(1999)" is not a film folder to anything here: the
    tagging passes it over, and the walk that looks for films goes looking
    INSIDE it. On the disk rather than at the moment it is read, so the name is
    right whether or not anything is ever found under it.

    The same walk the tagging does, so the two agree about which folders are
    the films: one level for a full ingest, the whole tree for a run pointed at
    a library.
    """
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        base, tag = plexnames.untagged_base(entry.name)
        repaired = repair_year(base)
        if repaired != base:
            _repair_one(entry, directory, plexnames.folder_name(repaired, tag),
                        skip_log, dry_run, log)
            continue
        if recursive and not is_film_folder(entry):
            _repair_year_names(entry.path, recursive, skip_log, dry_run, log)


def _repair_one(entry, directory: str, wanted: str,
                skip_log: safety.SkipLog, dry_run: bool,
                log: Callable[[str], None]) -> None:
    """One folder onto the name its year is readable under."""
    target = _folder_spelling(directory, wanted)
    if safety.would_hide(target) or os.path.exists(target):
        skip_log.record(entry.path, target)
        log('  "{}" writes its year where nothing can read it, and "{}" is '
            "already there - left as it is".format(entry.name, wanted))
        return
    if dry_run:
        log('  "{}" writes its year where nothing can read it'
            .format(entry.name))
        log('    would rename: "{}" -> "{}"'.format(entry.name, wanted))
        return
    os.rename(entry.path, target)
    log('  "{}" wrote its year where nothing could read it - renamed it "{}"'
        .format(entry.name, wanted))


def is_film_folder(entry) -> bool:
    """Whether this directory is one film's own folder, rather than one to look
    for films inside.

    A folder named "Title (Year)" IS a film: what sits inside one is that
    film's own material - its extras, its Subs - and not more films.

    A folder that says something after its year is that film too, but only
    while what is inside it is one film's. The words after a year are how a
    release names itself, and they are also how a shelf of films names the
    thing they have in common - "<film> (1999) Collection", "<film> (1999)
    Trilogy" - so the disk is asked rather than the name, and a folder that
    holds films rather than a film is one to look inside. Read as a film it
    would be looked up once, renamed onto whatever that one answer was, and
    every film under it lost inside it.

    Which the disk says two ways, and both have to hold: the film is here, and
    nothing is under this folder that is not this film's own. See
    :func:`_holds_one_films_material`.

    A folder that says no year at all is a film too, where the disk says so
    the same way and the film in it is named for the folder - see
    :func:`read_undated_folder`.
    """
    base, _tag, _title, _year, edition = read_folder(entry.name)
    if not base:
        return bool(read_undated_folder(entry)[0])
    if not edition:
        return True
    return _holds_one_films_material(entry.path)


def read_undated_folder(entry) -> tuple:
    """A folder with no year in its name read as :func:`read_folder` reads one
    that has one, or five empties when it is not a film's folder.

    "Moss Harbour" holding "Moss Harbour.mkv" and its subtitles is a film whose
    folder nobody dated - the commonest shape a documentary, a concert, a
    special or a television fairy tale arrives in. Read as anything else it
    is walked into, found to hold no film folders, and never asked about or
    reported at all: from the outside that looks like a catalogue with no
    answer, when nothing was asked.

    Only where the disk leaves no doubt: the film is here, nothing is here
    that is not this film's own material, and every film file in it is named
    for the folder. A folder of films, or a folder holding some other film,
    is still the shelf it looks like. A folder already carrying an id tag is
    left as it has always been - it has its answer - and so is one whose year
    is there and only written where nothing can read it, which is
    :func:`repair_year`'s to put right.

    The year comes back empty unless the film files themselves all carry the
    one year, in which case that is the year and the folder is read as though
    it had said so.
    """
    title, tag = plexnames.untagged_base(entry.name)
    title = title.strip()
    if not title or tag or _YEAR_RE.match(title) \
            or repair_year(title) != title \
            or plexnames.is_bonus_folder_name(title) \
            or not _holds_one_films_material(entry.path):
        return "", "", "", "", ""
    try:
        names = [item.name for item in os.scandir(entry.path)
                 if item.is_file(follow_symlinks=False)]
    except OSError:
        return "", "", "", "", ""
    if plexnames.strays_in(title, names):
        return "", "", "", "", ""
    years = plexnames.years_disagreeing(title, names)
    if len(years) == 1:
        return "%s (%s)" % (title, years[0]), "", title, years[0], ""
    return title, "", title, "", ""


def _holds_one_films_material(path: str) -> bool:
    """Whether what sits in this folder is one film and nothing but that film's
    own material.

    The film itself directly in it, and below it only the folders Plex reads
    extras out of - a "Featurettes", a "Deleted Scenes" - each holding files
    and no folders of its own. One level, and named the way Plex names them.

    Anything else and this is a folder of FILMS: another film's folder under it
    is exactly what a box set looks like, and a nesting deeper than Plex reads
    is a tree nobody laid out for Plex at all. Neither is a thing to look up
    under the one name this folder carries.

    Strictly, which costs a folder still holding the "Subs" a release shipped:
    it is read as a shelf and walked into rather than looked up, and the films
    under it - there are none - are what gets asked about instead. The ingest's
    own subtitle phase lifts that folder before this runs. A plain
    "Title (Year)" is never asked any of this: its name left no doubt, so what
    is under it is its own business, Subs and all.
    """
    try:
        inside = list(os.scandir(path))
    except OSError:
        return False
    film = False
    for item in inside:
        if item.is_dir(follow_symlinks=False):
            if not plexnames.is_bonus_folder_name(item.name):
                return False
            if _holds_a_folder(item.path):
                return False
        elif item.is_file(follow_symlinks=False):
            film = film or plexnames.is_movie_file(item.name)
    return film


def _holds_a_folder(path: str) -> bool:
    """Whether a folder sits inside this one - which is the second level a
    film's own material never has. A folder that cannot be read answers yes:
    what is not seen is not vouched for."""
    try:
        return any(item.is_dir(follow_symlinks=False)
                   for item in os.scandir(path))
    except OSError:
        return True


def _film_folders(directory: str) -> list:
    """Every film folder at or below ``directory``, in the filesystem's own
    order, so the lines they each log come out in the same sequence.

    Everything that is not one is descended through, which is what finds the
    films in a library that keeps them a level down, in an "Unsorted" or a box
    set, rather than directly under the root it was handed.
    """
    found: list = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        if is_film_folder(entry):
            found.append(entry)
        else:
            found.extend(_film_folders(entry.path))
    return found
