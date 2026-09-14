"""The names Plex reads a movie folder by, and the renames that produce them.

Three conventions live in one file name, and they do not commute:

* ``{imdb-ttXXXXXXX}`` / ``{tmdb-NNN}`` forces the match to one film.
* ``{edition-Name}`` separates different RELEASES of that film - a director's
  cut, a colourised print, a reissue with another soundtrack - which Plex
  collapses into one item with a named picker.
* ``Part1`` / ``cd1`` / ``disc1`` stacks a film SPLIT across several files back
  into one playable item.

The stacking suffix is matched at the END of the name, so it has to stay last:
an id or edition tag appended after it stops the parts stacking at all.

Everything in this module is a pure string operation over one folder's listing;
the caller does the renaming. It is written to be idempotent - feeding it names
it produced returns no work - because it runs on every ingest over a library
that is mostly already named.
"""

import os
import re

from medialib.lib import titlematch
from medialib.lib.enums import PART_WORDS

# A film's id tag, anywhere in the name. Plex reads the tag whether it sits on
# the folder or the file, and is indifferent to the order tags come in.
ID_TAG_RE = re.compile(r"\{(?:imdb|tmdb)-[^{}]*\}")

# The edition tag, which needs a Plex Pass to be DISPLAYED but is harmless
# without one.
EDITION_RE = re.compile(r"\{edition-([^{}]*)\}")

# The tokens Plex's scanner stacks on, as one trailing word: the keyword, an
# optional dot, and a number. Matched case-insensitively, the way the scanner
# does, so a "Part1" and a "cd2" are both recognised as written.
_WORDS = "|".join(PART_WORDS)
STACK_RE = re.compile(r"^(?:" + _WORDS + r")\.?[0-9]+$", re.I)

# The same token written with the number held off - "Part 1", "cd - 2". Plex
# stacks none of it, because its scanner wants the number against the keyword;
# but nothing else in the name is in question, so it is a spelling this module
# corrects rather than a folder it gives up on.
_LOOSE_PART = re.compile(r"^(" + _WORDS + r")[\s._-]*$", re.I)

# A word that is nothing but separator, which is what stands between the keyword
# and its number when someone wrote "Part - 6".
_SEPARATOR = re.compile(r"^[._\-]+$")

# The same words loose in a name rather than as its trailing token: a
# "Part 1 - The First Half" is a part written the way a person writes one, and
# Plex stacks none of it - the keyword has to be the last thing in the name,
# with its number against it.
_NAMES_A_PART = re.compile(r"^(?:" + _WORDS + r")\.?[0-9]*$", re.I)

# The year a film folder carries, as the last thing in its name. Read here so a
# file that left the year off can still be recognised as the folder's own film.
_YEAR_SUFFIX = re.compile(r"\s*\([12][0-9]{3}\)\s*$")

# The same year wherever it sits in a name, which is where a tag goes in after:
# the end of the film's name, and the start of what the release says about
# itself. The LAST one, the way a folder's own name is read.
_YEAR_IN_NAME = re.compile(r"\([12][0-9]{3}\)")

# What an improved remux leaves the original under, lower-cased for the compare.
# A copy is never renamed: it is the film as it arrived, and a tag on it would
# offer Plex a second edition of every film that has one.
OLD_SUFFIX = " (old).mkv"

# The wrappers a hand-written version name tends to arrive in.
_WRAPPERS = (("(", ")"), ("[", "]"))


def is_kept_copy(path: str) -> bool:
    """Whether ``path`` is the original an improved remux kept, rather than a
    film to work on: a copy is finished, and anything asked of it would redo
    what its living sibling has already had done.

    The suffix comes last, after every tag, so a copy made from an
    already-tagged film answers yes as readily as one made before the tagging.
    """
    return os.path.basename(path).lower().endswith(OLD_SUFFIX)


def film_key(stem: str) -> str:
    """``stem`` with every systematic tag taken off: the film it belongs to.

    Two files answering the same key are the same film - two editions of it, two
    parts of it, or one of each - which is what lets a folder holding several
    movie files be told from a folder holding several movies.
    """
    _edition, _part, eaten = _markers(stem)
    words = ID_TAG_RE.sub(" ", EDITION_RE.sub(" ", stem)).split()
    return " ".join(words[:len(words) - eaten])


def _markers(stem: str) -> tuple:
    """The edition name, the stacking token, and how many words the token ate.

    Either of the first two may be "". The count is what the callers trim by,
    because a token is not always one word: "Part 1" is the same stacking token
    as "Part1", written with the number held off, and it is read as one and
    written back tight.
    """
    match = EDITION_RE.search(stem)
    edition = match.group(1) if match else ""
    words = ID_TAG_RE.sub(" ", EDITION_RE.sub(" ", stem)).split()
    if words and STACK_RE.match(words[-1]):
        return edition, words[-1], 1
    if words and words[-1].isdigit():
        index = len(words) - 2
        while index >= 0 and _SEPARATOR.match(words[index]):
            index -= 1
        loose = _LOOSE_PART.match(words[index]) if index >= 0 else None
        if loose:
            return edition, loose.group(1) + words[-1], len(words) - index
    return edition, "", 0


def read_stem(base: str, stem: str) -> tuple:
    """One movie file's stem read as (edition, part), against the folder's
    ``base`` - the folder name with its own id tag taken off.

    An edition already written as a tag is read back verbatim; one written the
    way a release names itself ("(Original Mono Track)", "colorized") is
    taken from whatever is left over once the tags and the stacking token are
    accounted for.
    """
    if not stem.startswith(base):
        return "", ""
    remainder = stem[len(base):]
    edition, part, eaten = _markers(remainder)
    leftover = ID_TAG_RE.sub(" ", EDITION_RE.sub(" ", remainder)).split()
    leftover = leftover[:len(leftover) - eaten]
    if not edition:
        edition = edition_name(" ".join(leftover))
    return edition, part


def edition_name(text: str) -> str:
    """A release's own way of naming itself, as an edition tag's value.

    Unwrapped, de-braced (the tag has no escape for its own delimiters) and
    given a capital where the whole thing was written in lower case - a folder's
    "colorized" reads as "Colorized" in Plex's picker, while a name that already
    has capitals of its own is left exactly as it was.
    """
    text = " ".join(text.split())
    for opening, closing in _WRAPPERS:
        if text.startswith(opening) and text.endswith(closing):
            text = " ".join(text[1:-1].split())
            break
    text = text.replace("{", "(").replace("}", ")")
    if text and text == text.lower():
        text = text[0].upper() + text[1:]
    return text


def plex_stem(base: str, tag: str, edition: str, part: str) -> str:
    """The name Plex wants, assembled in the one order that works: the film,
    its id, its edition, and the stacking token last."""
    stem = base + " " + tag if tag else base
    if edition:
        stem += " {edition-" + edition + "}"
    if part:
        stem += " " + part
    return stem


def movie_stems(base: str, names) -> list:
    """The distinct films-on-disk a folder holds, longest first.

    Taken from the .mkv files, which are the only names in a movie folder that
    cannot be a suffix of something else; every sidecar is then matched to the
    longest of these it starts with, which is what keeps a transcript called
    "<film> 2 Commentary.en.srt" with its own film rather than with the shorter
    name that also prefixes it.
    """
    stems = {base}
    for name in names:
        if not name.lower().endswith(".mkv") or is_kept_copy(name):
            continue
        stem = name[:-len(".mkv")]
        if stem == base or stem.startswith(base + " "):
            stems.add(stem)
    return sorted(stems, key=len, reverse=True)


def editions_in(base: str, names) -> list:
    """The edition names the folder's films carry, in the order a report reads
    them out: alphabetical, and each one once."""
    found = set()
    for stem in movie_stems(base, names):
        edition, _part = read_stem(base, stem)
        if edition:
            found.add(edition)
    return sorted(found)


def is_only_a_marker(edition: str) -> bool:
    """Whether an edition name is nothing but a number.

    A "(1)" beside a film is what a copy gets when two of the same name land in
    one folder - a duplicate marker, not a release anyone chose. As an edition
    tag it would put a picker in Plex offering "1", which says nothing about
    either file.
    """
    return edition.strip().isdigit()


def is_only_a_year(edition: str) -> bool:
    """Whether an edition name is nothing but a YEAR.

    A folder's film dated 1988 beside a file of it dated 1998 is one of them
    mistyped, and the leftover reads as a bare number - which is also what a
    copy's "(1)" reads as. They are not the same thing at all: one is a second
    file nobody meant to keep, the other is one file with a digit wrong, and
    reporting the second as the first sends someone looking for a duplicate
    that was never there.

    Asked before :func:`is_only_a_marker`, which every year would otherwise
    answer to as well.
    """
    text = edition.strip()
    return (len(text) == 4 and text.isdigit()
            and text[0] in "12")


def names_a_part(edition: str) -> bool:
    """Whether an edition name is really a PART said in words.

    "Part 1 - The First Half", "Part 2", "disc 3": the keyword is in
    there, but not as the trailing token Plex stacks on, and what Plex would
    make of it as an edition is three separate releases of one film rather than
    one film in three files. Nothing here can turn it into a stacking token
    either - the token has to come last, and there is a title sitting after it -
    so the folder is left alone for someone to name.
    """
    return any(_NAMES_A_PART.match(word)
               for word in re.split(r"[^0-9A-Za-z]+", edition) if word)


def strays_in(base: str, names) -> list:
    """The movie files in a folder that are not this folder's film.

    A name that extends the folder's own is one of that film's releases - a
    version, an edition, a part - whatever word the release used for itself. A
    name that does not is something else entirely, and which film it is is a
    thing only someone who knows it can say: a folder holding one is left whole
    and reported rather than guessed at.
    """
    return sorted(
        name for name in names
        if is_movie_file(name)
        and not (name[:-len(".mkv")] == base
                 or name[:-len(".mkv")].startswith(base + " ")))


def untitled_base(base: str) -> str:
    """``base`` without its "(Year)": what a file that left the year off says.

    The folder carries the year by convention and a file beside it often does
    not, so the two are compared both ways round before either is called a
    different film.
    """
    return _YEAR_SUFFIX.sub("", base)


def onto_base(base: str, name: str) -> str:
    """``name`` with its title respelled the way the folder spells it, or "".

    A file whose name only DIFFERS from the folder's - an accent the keyboard
    could not reach, "Part II" for "Part 2", a dropped "The", an apostrophe
    stripped out, a year the file never carried - is one of this folder's films
    written by a different hand, and the difference is a spelling to correct
    rather than a second film to report.

    The longest leading run of words equivalent to the folder's name is what is
    replaced, so everything the file said ABOUT its release - its edition, its
    part, its language suffix - comes through untouched. "" when no prefix of
    the name names this film, which is the answer for a file that really is
    something else.

    A copy's marker comes off either way: a "(1)", a "(another copy)" or a
    " - Copy" is what a file manager writes when two files of one name land in
    one folder, it says nothing about the film, and a name is neither a stray
    nor an edition for carrying one. Only where the name it leaves is free -
    :func:`spelling_renames` will not put two files under one name, and a folder
    that really does hold the same film twice keeps both and is reported.
    """
    if is_kept_copy(name):
        return ""
    if name.startswith(base):
        # Already this film, and the only thing that can be wrong with it is a
        # marker a file manager left: everything else after the base is what the
        # release says about itself, and is read rather than corrected.
        stem, extension = os.path.splitext(name)
        stripped = titlematch.strip_duplicate_marker(stem)
        if stripped != stem and len(stripped) >= len(base):
            return stripped + extension
        return ""
    for cut in _boundaries(name):
        head = name[:cut].rstrip()
        if not head:
            continue
        if _numbers_another_film(name[cut:]):
            continue
        if _names_this_film(base, head) or _names_this_film(
                base, titlematch.strip_duplicate_marker(head)):
            return base + name[cut:]
    return ""


# A number standing on its own, in words or in roman, with nothing holding it.
_A_NUMBER = re.compile(r"^(?:[0-9]+|[ivxlcdm]+)$", re.I)


def _numbers_another_film(rest: str) -> bool:
    """Whether what follows a matched title numbers a DIFFERENT film.

    "Winnetou" matches the folder "Winnetou I (1963)", because the first of a
    series is numbered three ways and meant identically - and that same reading
    makes "Winnetou II (1964).mkv" match it too, with the "II" left over as
    though it were an edition. It is not an edition: it is the sequel, and
    absorbing it is precisely how a sequel gets filed under the film before it.

    Only a BARE number counts. One in brackets is a year or a copy's marker,
    and "Part 2" is a part - both of them say something about this film rather
    than naming another.
    """
    words = rest.split()
    # Cut at the first dot, or the number that IS the whole rest of the name
    # arrives wearing its extension: "Winnetou 2.mkv" leaves "2.mkv".
    return bool(words) and bool(_A_NUMBER.match(words[0].split(".", 1)[0]))


def _boundaries(name: str) -> list:
    """Where a title could end inside ``name``, longest candidate first.

    A space, because the rest of the name is words; a dot, because a sidecar
    wears its language and its format there and a film its extension - "<film>.en.srt"
    has to be able to give up both of them to be recognised as that film.
    """
    return [len(name)] + [index for index in range(len(name) - 1, 0, -1)
                          if name[index] in " ."]


def _names_this_film(base: str, head: str) -> bool:
    """Whether ``head`` is the folder's own film said differently - with the
    year on either side of the comparison, or on neither."""
    if titlematch.equivalent(base, head):
        return True
    bare = untitled_base(base)
    return bare != base and titlematch.equivalent(bare, head)


def named_by_catalogue(names, aliases) -> dict:
    """{name: the catalogue's own writing of the title it answers to}, for the
    names a CATALOGUE says are this film - and which nothing about the strings
    could have said.

    "Das Krokodil und sein Nilpferd" and "Io sto con gli ippopotami" have not a
    syllable in common with "I'm For The Hippopotamus" or with each other, and
    are one film in three languages. No rule over the text will ever join them;
    the alternative titles the catalogue holds do.

    Recognition only. A name this accounts for keeps every character it has -
    which of a film's languages a file is named in is a thing its owner chose,
    and a lookup is no reason to overwrite it.

    ``aliases`` are the titles as the catalogue writes them, and one of those is
    what comes back - not the key that matched. A report is read by a person,
    and a fold is not a title: "Das Krokodil und sein Nilpferd" folds to a key
    with "and" in the middle of it, which is not a name anything has.
    """
    found = {}
    for name in names:
        keys = titlematch.title_keys(untitled_base(os.path.splitext(name)[0]))
        for written in aliases:
            if titlematch.title_keys(written) & keys:
                found[name] = written
                break
    return found


def spelling_renames(base: str, names) -> dict:
    """{name: the name it should be spelled as}, for the names that differ.

    Only the ones a spelling explains: a name this folder's film cannot be read
    out of is not in here at all, and is what :func:`strays_in` goes on to
    report. A rename that would land on a name the folder already holds is
    dropped rather than made - the two files are then genuinely two, whatever
    their names say, and that is a thing for someone to look at.
    """
    held = set(names)
    corrected: dict[str, str] = {}
    for name in sorted(names):
        wanted = onto_base(base, name)
        if wanted and wanted != name and wanted not in held:
            corrected[name] = wanted
            held.add(wanted)
    return corrected


def ids_in(names) -> set:
    """Every id tag the folder's movie files carry, as a set.

    One entry and nothing else means every film in the folder was tagged, by
    hand or by an earlier run, as the SAME film - which is the one thing that
    can be said about a folder whose names cannot be made to agree.
    """
    return {match.group(0)
            for name in names if is_movie_file(name)
            for match in [ID_TAG_RE.search(name)] if match}


def folder_renames(base: str, tag: str, names) -> list:
    """Every rename one movie folder needs, as (old name, new name) pairs.

    ``base`` is the folder without its id tag and ``tag`` the tag to carry;
    ``names`` is the folder's listing. A file that belongs to no film in the
    folder is left alone, as is every "(old)" copy, and a name that is already
    right produces no pair - so a second run over the same folder returns [].

    A name that says this folder's film in a different spelling is brought onto
    the folder's own first, and then read as any other name is - so a file that
    arrived as "le comte de monte cristo pt 1.mkv" leaves with the folder's
    capitals, the folder's accents and a stacking token Plex can see.
    """
    corrected = spelling_renames(base, names)
    respelled = [corrected.get(name, name) for name in names]
    stems = movie_stems(base, respelled)
    plan = []
    for name in sorted(names):
        if is_kept_copy(name):
            continue
        spelled = corrected.get(name, name)
        for stem in stems:
            if spelled.startswith(stem):
                break
        else:
            continue
        wanted = plex_stem(base, tag, *read_stem(base, stem))
        target = wanted + spelled[len(stem):]
        if target != name:
            plan.append((name, target))
    return plan


def retag(stem: str, tag: str) -> str:
    """``stem`` given ``tag``, with every other character of it where it was.

    For the folder nothing can rename: what is wrong with its names is not
    something a tag may guess at, but the id itself is known, and Plex reads it
    off whichever name carries it. So the tag goes in and the name is otherwise
    untouched.

    Where it goes is where it goes everywhere else - straight after the year,
    which is the end of the film's name and the start of what the release says
    about itself. A name with no year takes it before its edition tag and before
    its stacking token, the two things that have to stay after it, and at the
    end when it has neither.

    A stem that already carries an id is left alone: it is the agreement that
    was checked, and rewriting it would say the check had found a disagreement.
    """
    if not tag or ID_TAG_RE.search(stem):
        return stem
    cuts = []
    for match in _YEAR_IN_NAME.finditer(stem):
        cuts = [match.end()]
    edition = EDITION_RE.search(stem)
    if edition:
        cuts.append(edition.start())
    _edition, part, eaten = _markers(stem)
    if part:
        cuts.append(_word_start(stem, eaten))
    cut = min(cuts) if cuts else len(stem)
    head = stem[:cut].rstrip()
    tail = stem[cut:].strip()
    return head + " " + tag + (" " + tail if tail else "")


def _word_start(text: str, words: int) -> int:
    """Where the last ``words`` whitespace-separated words of ``text`` begin."""
    index = len(text)
    for _ in range(words):
        index = len(text[:index].rstrip())
        space = text.rfind(" ", 0, index)
        if space < 0:
            return 0
        index = space
    return index


def id_tag_renames(tag: str, names) -> list:
    """Every rename that does nothing but put ``tag`` on this folder's files.

    The answer for a folder whose names cannot be made to agree and whose id is
    not in doubt: each film keeps the name it has, each sidecar follows its own
    film the way it always does, and the one thing that changes is that Plex can
    now see which film they are.
    """
    stems = sorted({name[:-len(".mkv")] for name in names if is_movie_file(name)},
                   key=len, reverse=True)
    plan = []
    for name in sorted(names):
        if is_kept_copy(name):
            continue
        for stem in stems:
            if name.startswith(stem):
                break
        else:
            continue
        wanted = retag(stem, tag)
        if wanted != stem:
            plan.append((name, wanted + name[len(stem):]))
    return plan


def split_tags(name: str) -> tuple:
    """``name`` as (the part that is a title, the tags that follow it).

    Every rule the name cleaner has is wrong about a tag: "{edition-Director's
    Cut}" with its apostrophe cleaned away is a DIFFERENT edition, and its
    sidecars are left behind under the old spelling. A name with no tag comes
    back whole.
    """
    starts = [match.start() for match in
              (ID_TAG_RE.search(name), EDITION_RE.search(name)) if match]
    if not starts:
        return name, ""
    cut = min(starts)
    return name[:cut].rstrip(), name[cut:]


def folder_name(base: str, tag: str) -> str:
    """The folder's own name: the film and its id, and neither of the two
    per-file conventions - a folder holds every edition and every part."""
    return base + " " + tag if tag else base


def untagged_base(folder: str) -> tuple:
    """A folder name read as (base, tag it already carries).

    A library tagged by an earlier run hands its own id back rather than being
    looked up again, which is what lets a folder whose files were missed be put
    right without asking the network a second time.
    """
    match = ID_TAG_RE.search(folder)
    if not match:
        return folder, ""
    base = folder[:match.start()] + " " + folder[match.end():]
    return " ".join(base.split()), match.group(0)


def is_movie_file(name: str) -> bool:
    """A playable film in a movie folder - not the copy an improvement kept."""
    return name.lower().endswith(".mkv") and not is_kept_copy(name)


def one_film_in(base: str, names) -> list:
    """The movie files of a folder that holds ONE film, or [].

    One file is that film. Several are only ever accepted when each reduces to
    the folder's own name once its id, edition and stacking tokens are taken off
    - which is to say when they are that film's editions and parts, written
    systematically. Anything else is a folder whose feature cannot be told from
    its other contents, and the caller leaves it alone.
    """
    movies = sorted(name for name in names if is_movie_file(name))
    if len(movies) <= 1:
        return movies
    keys = {film_key(os.path.splitext(name)[0]) for name in movies}
    return movies if keys == {film_key(base)} else []
