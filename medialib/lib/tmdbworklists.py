"""The worklist files a tagging run leaves beside the library: the renames it
made, the conflicts and duplicates it would not resolve, the ambiguous and
near-miss folders, the aliases, and the hand-written id list it reads back.

Each is plain text a person edits; the id list is one film per line, its
folder name and its id separated by a tab.
"""

import os
import re
from collections.abc import Callable

from medialib.lib import filmfolders, plexnames, titlematch

# What a reason gains when the folder could not be renamed but its id was known
# all the same. The names still need someone; the id is simply no longer
# missing too.
TAGGED_ANYWAY = " (id applied, names still to settle)"

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
        _ID_COMMENT + "",
        _ID_COMMENT + " Folders the other reports are about are not in here.",
        _ID_COMMENT + " Each of them is explained where it is listed, and the",
        _ID_COMMENT + " four files between them name every folder once.",
        "",
    ]
    for source, target in renames:
        lines.append(os.path.relpath(source, root))
        lines.append("    -> " + os.path.relpath(target, root))
    return _write_lines(path, lines, log)


def write_conflict_list(path: str, skips, root: str,
                        log: Callable[[str], None] | None = None) -> bool:
    """The renames held back because something already had the name.

    Nothing was overwritten and nothing was renamed: each pair is a folder or
    file still under its old name, beside the one that holds the new name -
    most often the same film already tagged.
    """
    lines = [
        _ID_COMMENT + " Renames the tagging held back because the name it",
        _ID_COMMENT + " wanted was already taken. Nothing was overwritten and",
        _ID_COMMENT + " nothing here was renamed. The name already there is",
        _ID_COMMENT + " most often the same film, tagged before - compare the",
        _ID_COMMENT + " two and keep one.",
        "",
    ]
    for source, target in skips:
        lines.append(os.path.relpath(source, root))
        lines.append("    -> " + os.path.relpath(target, root))
    return _write_lines(path, lines, log)


def write_duplicate_list(path: str, duplicates,
                         log: Callable[[str], None] | None = None) -> bool:
    """The films kept in more than one folder, as [(tag, [folder, ...])].

    Full paths, since the folders may be under different ones of those given.
    Said and nothing more: which copy to keep is not a thing a name can tell.
    """
    lines = [
        _ID_COMMENT + " Films kept in more than one folder - nearly always one",
        _ID_COMMENT + " kept twice by accident. Nothing was changed; which",
        _ID_COMMENT + " copy to keep is yours to decide.",
        "",
    ]
    for tag, folders in duplicates:
        lines.append(tag)
        lines.extend('    "%s"' % folder for folder in folders)
    return _write_lines(path, lines, log)


def _write_lines(path: str, lines: list,
                 log: Callable[[str], None] | None,
                 what: str | None = None) -> bool:
    """``lines`` as the whole of ``path``. A list that cannot be written is a
    warning through ``log``, naming it as ``what`` if given, and False - never
    an exception."""
    try:
        with open(path, "w", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as error:
        if log is not None:
            log("WARNING: could not write %s: %s" % (what or path, error))
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
        _ID_COMMENT + " 'holds the same film twice, spelled two ways': a file",
        _ID_COMMENT + " that is this film under another spelling, beside one",
        _ID_COMMENT + " already under the folder's. Which to keep is not a",
        _ID_COMMENT + " thing a tag may decide.",
        _ID_COMMENT + "",
        _ID_COMMENT + " 'one film in parts that do not stack': Plex reads a",
        _ID_COMMENT + " part from the END of the name - '<film> Part1' - and",
        _ID_COMMENT + " these have a title after the token, so it cannot.",
        _ID_COMMENT + " Tagged as editions they would read as separate films.",
        _ID_COMMENT + "",
        _ID_COMMENT + " 'holds the same film under another year': one of the",
        _ID_COMMENT + " two dates has a digit wrong. Which one is not a thing",
        _ID_COMMENT + " a tag may guess at.",
        _ID_COMMENT + "",
        _ID_COMMENT + " '" + TAGGED_ANYWAY.strip(" ()") + "': every id already",
        _ID_COMMENT + " in the folder said the same film, so the id went on to",
        _ID_COMMENT + " the folder and its files and nothing else moved. Plex",
        _ID_COMMENT + " can see which film these are; what it still cannot see",
        _ID_COMMENT + " is which release each file is.",
        _ID_COMMENT + "",
        _ID_COMMENT + "",
        _ID_COMMENT + " Each name is followed by what it READS AS once the",
        _ID_COMMENT + " spelling is folded away. Two lines that read the same",
        _ID_COMMENT + " are two names the matching refused to call one thing,",
        _ID_COMMENT + " which is a reading it does not have yet; two that read",
        _ID_COMMENT + " differently are two different films.",
        _ID_COMMENT + "",
        _ID_COMMENT + " Rename the files and run the tagging again.",
        "",
    ]
    for folder, reason, movies in folders:
        lines.append("%s  -  %s" % (os.path.relpath(folder, root), reason))
        lines += _how_close(
            filmfolders.read_folder(os.path.basename(folder))[0] or folder,
            movies)
        lines.append("")
    return _write_lines(path, lines, log)


def write_near_miss_list(path: str, folders, root: str,
                         log: Callable[[str], None] | None = None) -> bool:
    """What TMDb was asked, what it offered, and why none of it was the answer.

    The list to read before trusting the other three: every folder in it was
    left alone, and this is the only place that says whether it should have
    been. A page of candidates that are plainly the film is a reading this does
    not have yet; a page of films that merely share a word is the rule working.
    """
    lines = [
        _ID_COMMENT + " Every film TheMovieDB could not name, and how close",
        _ID_COMMENT + " it came: what was asked, what came back, and why none",
        _ID_COMMENT + " of it was certain enough to rename on.",
        _ID_COMMENT + "",
        _ID_COMMENT + " Only those. A folder one of the other lists accounts",
        _ID_COMMENT + " for is that list's business, and a folder read twice",
        _ID_COMMENT + " is a folder looked at twice.",
        "",
    ]
    for folder, reason, notes in folders:
        lines.append("%s  -  %s" % (os.path.relpath(folder, root), reason))
        lines += ["    " + note for note in notes]
        lines.append("")
    return _write_lines(path, lines, log)


def write_alias_list(path: str, folders, root: str,
                     log: Callable[[str], None] | None = None) -> bool:
    """The folders held together by the catalogue's own alternative titles.

    Nothing was renamed for any of them, so no other list mentions them: this
    is where to see that the recognition is working, and the only place a film
    kept under two languages' names is written down at all.
    """
    lines = [
        _ID_COMMENT + " Folders holding one film under several of its own",
        _ID_COMMENT + " titles - the same film named in another language,",
        _ID_COMMENT + " which nothing about the names themselves could say.",
        _ID_COMMENT + " TheMovieDB lists each of these as a title of the film",
        _ID_COMMENT + " the folder was matched to.",
        _ID_COMMENT + "",
        _ID_COMMENT + " Every one of them KEPT its name - which of a film's",
        _ID_COMMENT + " languages a file is named in is a thing you chose, and",
        _ID_COMMENT + " a lookup is no reason to overwrite it. All that was",
        _ID_COMMENT + " added is the id, so Plex knows they are one film.",
        "",
    ]
    for folder, base, tag, known in folders:
        lines.append("%s  -  %s" % (os.path.relpath(folder, root), tag))
        lines.append("    matched as: " + base)
        for name in sorted(known):
            lines.append('    "%s"' % name)
            lines.append("        is a title TMDb holds it under: "
                         + known[name])
        lines.append("")
    return _write_lines(path, lines, log)


def read_id_list(path: str, blank: list | None = None) -> dict | None:
    """A hand-filled id list as {folder name: tag}, or None if it cannot be
    read at all.

    None rather than nothing, because the two are not the same answer and the
    caller acts on them differently: a file with no ids in it yet is a morning's
    work not started, and a file that is not there is a path typed wrong - which
    read as "no ids" would throw away every id in the real file and ask the
    catalogue all the questions it has already failed.

    ``blank`` collects the rows with no id yet. They are neither refused nor
    dropped: the file is filled in over several sittings, and a row still blank
    is a film the catalogue could not name and nobody has looked up, which is
    the one thing worth knowing about it.
    """
    found: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip() or line.lstrip().startswith(_ID_COMMENT):
            continue
        name, _tab, written = line.partition("\t")
        tag = id_tag_for(written)
        if not name.strip():
            continue
        if tag:
            found[name.strip()] = tag
        elif blank is not None:
            blank.append(name.strip())
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
        _ID_COMMENT + "   ingest-movies -i <this file> <library>",
        _ID_COMMENT + " adding -w once the dry run reads right. Lines still",
        _ID_COMMENT + " blank are simply the ones still to do.",
        "",
    ]
    lines += [name + "\t" + tag for name, tag in sorted((ids or {}).items())]
    lines += [name + "\t" for name in names]
    return _write_lines(path, lines, log, "the id list " + path)


def _how_close(base: str, names: list) -> list:
    """How near each file a folder was left for came to being that folder's own
    film, as the fold that decided it.

    Two lines that read the same are two names this refused to call one thing,
    which is a reading that is missing; two that read differently are two
    different films, which is the answer the folder was left for.
    """
    lines = ["    the folder reads as: " + _reads_as(base)]
    for name in names:
        lines.append("    %s" % name)
        lines.append("        reads as: " + _reads_as(os.path.splitext(name)[0]))
    return lines


def _reads_as(stem: str) -> str:
    """One name folded to what it SAYS, with the systematic tags off.

    An id or an edition tag is not part of any title, and folded in it would be
    the loudest thing on the line - two names that differ only in that one
    already carries its id would read as two different films.
    """
    return titlematch.normalize_title(plexnames.film_key(stem))
