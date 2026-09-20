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
from collections import Counter

from medialib.lib import languages, titlematch
from medialib.lib.enums import PART_WORDS, SOURCE_PART_WORDS, shell_lower

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

# The number a trailing word ends in, read to tell a stacking token ("Part2")
# from a year grown onto one ("Part1981"): a part is never a date.
_NUMBER_TAIL = re.compile(r"[0-9]+$")

# A word that is nothing but separator, which is what stands between the keyword
# and its number when someone wrote "Part - 6".
_SEPARATOR = re.compile(r"^[._\-]+$")

# The same words loose in a name rather than as its trailing token: a
# "Part 1 - The First Half" is a part written the way a person writes one, and
# Plex stacks none of it - the keyword has to be the last thing in the name,
# with its number against it.
#
# Two spellings, because a name is split on its punctuation before either is
# asked: the keyword with its number grown onto it, and the keyword on its own
# with the number standing as the next word.
_A_PART_NUMBERED = re.compile(r"^(?:" + _WORDS + r")[0-9]+$", re.I)
_A_PART_WORD = re.compile(r"^(?:" + _WORDS + r")$", re.I)

# And the ones of those that name a SOURCE rather than a piece, which are the
# only ones a missing number does not condemn.
_A_SOURCE_WORD = re.compile(r"^(?:" + "|".join(SOURCE_PART_WORDS) + r")$", re.I)

# The year a film folder carries, as the last thing in its name. Read here so a
# file that left the year off can still be recognised as the folder's own film.
_YEAR_SUFFIX = re.compile(r"\s*\([12][0-9]{3}\)\s*$")

# The same year with the brackets a file often does not bother with. Never read
# on its own: a title may END in a year - "Rivertown 2049", "1915" - and
# taking it for the date would file the film under a name it does not have. It
# is only read where the FOLDER says the same year, which is what makes it the
# date said a second time rather than part of the name.
_BARE_YEAR_SUFFIX = re.compile(r"\s*([12][0-9]{3})\s*$")

# The same year wherever it sits in a name, which is where a tag goes in after:
# the end of the film's name, and the start of what the release says about
# itself. The LAST one, the way a folder's own name is read.
_YEAR_IN_NAME = re.compile(r"\([12][0-9]{3}\)")

# And the same year at the START of what is left over once a title has been
# matched, which is the folder's own year said a second time.
# Either bracket may be missing: a folder whose year lost one is repaired, and
# the file beside it lost the same one.
_YEAR_LEADING = re.compile(r"^\(?[12][0-9]{3}\)?")

# What an improved remux leaves the original under, lower-cased for the compare.
# A copy is never renamed: it is the film as it arrived, and a tag on it would
# offer Plex a second edition of every film that has one.
OLD_SUFFIX = " (old).mkv"

# The wrappers a hand-written version name tends to arrive in.
_WRAPPERS = (("(", ")"), ("[", "]"))

# The folders Plex reads bonus material out of, which are the only folders that
# belong INSIDE a film's own folder: everything there is that film's extras
# rather than another film.
# https://support.plex.tv/articles/local-files-for-trailers-and-extras/
BONUS_FOLDERS = ("Behind The Scenes", "Deleted Scenes", "Featurettes",
                 "Interviews", "Other", "Scenes", "Shorts", "Trailers")

# What a name puts BETWEEN two of its parts - the film and the version it is of,
# the film and its year - and never part of either part's own name. Whitespace
# is stripped with them and again after them, so a run of several comes off
# together.
SEPARATORS = " -:_|.–—"

# The longest a single file name may be, in bytes. Every filesystem this library
# is kept on stops at the same figure, and a rename past it fails outright with
# ENAMETOOLONG rather than being cut to fit - so a name that would pass it is
# worked out here, before anything is renamed.
NAME_MAX_BYTES = 255

# How much of that a commentary's outputs keep back for their own suffixes: an
# ".opus" and an ".<language>.srt" both hang off one stem, so the stem is cut to
# leave room for the longest of them.
COMMENTARY_SUFFIX_BYTES = 8
COMMENTARY_STEM_MAX_BYTES = NAME_MAX_BYTES - COMMENTARY_SUFFIX_BYTES

# What hangs off that stem, and the only extensions a name may be cut for: the
# audio a commentary was extracted to, and the transcript made from it.
COMMENTARY_EXTENSIONS = ("opus", "srt")

# What a commentary's output puts after the film it belongs to: the track's
# number, then the track's own name. The number is what says which of a film's
# commentaries the file is of, and is the one part of the tail that may not be
# cut into.
_COMMENTARY_TAIL = re.compile(r"^ ([0-9]+) (\S.*)$", re.S)

# The language the transcription writes between a commentary's name and its
# extension: two or three letters, and nothing a track name would end on.
_LANGUAGE_EXT = re.compile(r"^\.[A-Za-z]{2,3}$")


def fits_name(name: str) -> bool:
    """Whether ``name`` is short enough to be a file name at all.

    Bytes and not characters: the limit is the filesystem's, and an accented
    title spends two of them on a letter that a count of characters spends one
    on.
    """
    return len(name.encode("utf-8", "surrogateescape")) <= NAME_MAX_BYTES


def cut_to_bytes(name: str, limit: int) -> str:
    """``name`` cut back until it fits ``limit`` bytes, or "" if it never does.

    Characters come off whole: a name cut mid-character would not be the name
    anything else works out for itself.
    """
    while name and len(name.encode("utf-8", "surrogateescape")) > limit:
        name = name[:-1]
    return name


def _split_suffix(tail: str) -> tuple:
    """A sidecar's tail as (what is named, the extensions on the end)."""
    named, extension = os.path.splitext(tail)
    head, language = os.path.splitext(named)
    if _LANGUAGE_EXT.match(language):
        return head, language + extension
    return named, extension


def crop_commentary(wanted: str, tail: str) -> str:
    """``wanted`` and ``tail`` joined and cut back to a name that fits, or "".

    One thing in a movie folder may be cut and one only: a commentary's own
    output, and only into the track's NAME. The transcription cuts that same
    stem at that same place when it writes one, so a file cut here is the file
    the next run goes looking for rather than one it writes a second time.

    Everything else has to survive whole - the film, its id tag, and the number
    that says which commentary the file is of - and "" is the answer for a name
    where the cut would reach any of them.
    """
    named, suffix = _split_suffix(tail)
    track = _COMMENTARY_TAIL.match(named)
    if not track:
        return ""
    if suffix.rsplit(".", 1)[-1].lower() not in COMMENTARY_EXTENSIONS:
        return ""
    # Cut where the transcription cuts, and then further for a suffix longer
    # than the room the stem limit keeps back for one.
    stem = cut_to_bytes(wanted + named, COMMENTARY_STEM_MAX_BYTES)
    while stem and not fits_name(stem + suffix):
        stem = stem[:-1]
    # The film, the tag and the track's number, which is as deep as a cut may
    # go - and a track left with no name at all is a cut that went that deep.
    kept = len(wanted) + len(" ") + len(track.group(1)) + len(" ")
    if len(stem) <= kept:
        return ""
    return stem + suffix


def _untagged_tail(tail: str) -> str:
    """What is left over after a stem, with any id tag taken out of it.

    :func:`plex_stem` is the one place an id is written, so a leftover that
    still carries one has it written twice. It happens where the stem a file
    matched stops SHORT of the file's own tag - a sidecar beside a film whose
    stem carries an edition matches the folder's bare name, and everything from
    its own tag onwards is leftover.

    The space the tag stood in goes with it, or a ".en.srt" comes back
    " .en.srt"; a leftover that goes on to say something keeps the one space
    that holds it off the stem.
    """
    if not ID_TAG_RE.search(tail):
        return tail
    cleaned = re.sub(r"\s+", " ", ID_TAG_RE.sub("", tail))
    if cleaned.startswith(" ") and cleaned[1:2] == ".":
        return cleaned[1:]
    return cleaned.rstrip() if cleaned.strip() else cleaned.strip()


def _fits_or_crops(name: str, wanted: str, tail: str,
                   cropped: list | None, over_limit: list | None) -> str:
    """The name one file should take once the length limit has had its say, or
    "" for a name that cannot be brought under it.

    The two lists collect either outcome for the run to report: a commentary cut
    back into the track's name is a rename to mention, and a name that may not
    be cut is a folder to leave alone.
    """
    target = wanted + tail
    if fits_name(target):
        return target
    cut = crop_commentary(wanted, tail)
    if not cut:
        if over_limit is not None:
            over_limit.append((name, target))
        return ""
    if cropped is not None and cut != name:
        cropped.append((name, cut))
    return cut


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

    The separator that only INTRODUCED it comes off, and punctuation alone is
    not a name at all and answers "". What reaches this from :func:`read_stem`
    is whatever a stem had left over once the tags and the stacking token were
    accounted for, so a file written "<film> - Director's Cut" leaves the dash
    on the front of the name and "<film> - Part 1" leaves nothing but the dash.
    Written out those read "{edition-- Director's Cut}" and "{edition--}" - a
    picker entry wearing its own separator, and one with nothing in it standing
    between a split film's two halves and stacking.
    """
    text = " ".join(text.split())
    for opening, closing in _WRAPPERS:
        if text.startswith(opening) and text.endswith(closing):
            text = " ".join(text[1:-1].split())
            break
    text = text.replace("{", "(").replace("}", ")")
    text = text.strip(SEPARATORS).strip()
    if not any(character.isalnum() for character in text):
        return ""
    if text == text.lower():
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


def editions_in(base: str, names, edition: str = "") -> list:
    """The edition names the folder's films carry, in the order a report reads
    them out: alphabetical, and each one once.

    ``edition`` is a release only the FOLDER named, which every film in it that
    names none of its own is an instance of.
    """
    found = set()
    for stem in movie_stems(base, names):
        own, _part = read_stem(base, stem)
        if own or edition:
            found.add(own or edition)
    return sorted(found)


def years_disagreeing(base: str, names) -> list:
    """The years this folder's own films are dated that the FOLDER is not.

    A file dated one way and its folder another leaves the file's year standing
    where an edition would be, which is what :func:`is_only_a_year` reads. What
    that means is not a naming question - one of the two dates is wrong, or
    they are two different films - so this only says which other years are in
    play, and leaves the answering to whoever can ask a catalogue.
    """
    found = set()
    for stem in movie_stems(base, names):
        edition = read_stem(base, stem)[0]
        if is_only_a_year(edition):
            found.add(edition.strip())
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

    A keyword with no number after it is still one of these, with one
    exception: :data:`medialib.lib.enums.SOURCE_PART_WORDS`, the keywords that
    are also the name of a source. A "<film> (2001) DVD.mkv" beside a
    "<film> (2001) BluRay.mkv" is one film twice over, and the two words are
    doing the same job - the second was always an edition, and the first was
    being read as a part that had lost its number.

    A bare "disc", "cd" or "part" is not that. Those words name a PIECE and
    nothing else, so one standing without its number is a part that cannot
    stack, which is the thing to report rather than to write into an edition
    tag nobody chose.
    """
    words = [word for word in re.split(r"[^0-9A-Za-z]+", edition) if word]
    for index, word in enumerate(words):
        if _A_PART_NUMBERED.match(word):
            return True
        if not _A_PART_WORD.match(word):
            continue
        numbered = index + 1 < len(words) and words[index + 1].isdigit()
        if numbered or not _A_SOURCE_WORD.match(word):
            return True
    return False


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


def untitled_base(base: str, year: str = "") -> str:
    """``base`` without its "(Year)": what a file that left the year off says.

    The folder carries the year by convention and a file beside it often does
    not, so the two are compared both ways round before either is called a
    different film.

    ``year`` is the year the FOLDER carries, and giving it reads one more
    spelling: a year written without its brackets, which is how a file names
    itself when nothing ever put them there - "Den Ljusa Vagen 1992 Part1.mkv".
    Only that one year, never any other, for the reason
    :data:`_BARE_YEAR_SUFFIX` gives.
    """
    shortened = _YEAR_SUFFIX.sub("", base)
    if shortened != base or not year:
        return shortened
    match = _BARE_YEAR_SUFFIX.search(base)
    return base[:match.start()] if match and match.group(1) == year else base


def year_of(base: str) -> str:
    """The year a folder's name carries, or "".

    What :func:`untitled_base` has to be told to read a file's own spelling of
    the same year, and what says two names disagree about the date rather than
    about the film.
    """
    match = _YEAR_SUFFIX.search(base)
    return match.group(0).strip(" ()") if match else ""


def onto_base(base: str, name: str, also=(), typos: bool = False) -> str:
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

    ``also`` are further spellings of this FOLDER's own name - the franchise it
    wrote that the file did not, the year it is dated one side of - each of
    which a file may name this film under. They are the caller's to work out
    and to vouch for, because what says a second spelling is this same film is
    never in the two strings: it is the folder above saying the franchise, or a
    catalogue saying the two years are one film.

    ``typos`` allows the last reading of all, a single slip of the hand, on a
    name every other reading has refused. A guess, so the caller only sets it
    where something has already vouched for the name being guessed AT - see
    :func:`medialib.lib.titlematch.one_typo_apart`.
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
        for spelling in (base,) + tuple(also):
            if _names_this_film(spelling, head, typos) or _names_this_film(
                    spelling, titlematch.strip_duplicate_marker(head), typos):
                return base + _suffix_kept(head, name[cut:])
    return ""


# A number standing on its own, in words or in roman, with nothing holding it.
_A_NUMBER = re.compile(r"^(?:[0-9]+|[ivxlcdm]+)$", re.I)


def _numbers_another_film(rest: str) -> bool:
    """Whether what follows a matched title numbers a DIFFERENT film.

    "Falkenauge" matches the folder "Falkenauge I (1963)", because the first of a
    series is numbered three ways and meant identically - and that same reading
    makes "Falkenauge II (1964).mkv" match it too, with the "II" left over as
    though it were an edition. It is not an edition: it is the sequel, and
    absorbing it is precisely how a sequel gets filed under the film before it.

    Only a BARE number counts. One in brackets is a year or a copy's marker,
    and "Part 2" is a part - both of them say something about this film rather
    than naming another.
    """
    words = rest.split()
    # Cut at the first dot, or the number that IS the whole rest of the name
    # arrives wearing its extension: "Falkenauge 2.mkv" leaves "2.mkv".
    return bool(words) and bool(_A_NUMBER.match(words[0].split(".", 1)[0]))


def _boundaries(name: str) -> list:
    """Where a title could end inside ``name``, longest candidate first.

    A space, because the rest of the name is words; a dot, because a sidecar
    wears its language and its format there and a film its extension - "<film>.en.srt"
    has to be able to give up both of them to be recognised as that film.

    The END of the name is offered only where there is no extension to give up.
    A title is a title and knows nothing about files, so a fold has no reason
    to refuse one: read as words, "Falkenauge 2.mkv" is a title with a format
    written after it, and the readings that drop what follows a series number
    drop that as readily as a subtitle. What comes back is then the name with
    its extension gone, which is not a rename anybody meant.
    """
    ends = [] if os.path.splitext(name)[1] else [len(name)]
    return ends + [index for index in range(len(name) - 1, 0, -1)
                   if name[index] in " ."]


def _suffix_kept(head: str, tail: str) -> str:
    """``tail`` with any language suffix the cut swallowed put back in front.

    A cut may fall either side of a sidecar's ".xx", because giving the language
    up is sometimes the only way the name can be RECOGNISED as this film - see
    :func:`_boundaries`. What is recognised away is not thereby renamed away:
    the language is the one thing the file says that no other file in the folder
    says, and a ".en.srt" that comes back ".srt" is a subtitle Plex can no longer
    tell the language of.
    """
    dot = head.rfind(".")
    if dot < 0 or not languages.code_from_tag(head[dot + 1:]):
        return tail
    return head[dot:] + tail


def _names_this_film(base: str, head: str, typos: bool = False) -> bool:
    """Whether ``head`` is the folder's own film said differently - with the
    year on either side of the comparison, or on neither.

    ``typos`` adds the one reading that is a guess, and adds it last: a name no
    fold could bring home is asked whether it is this one with a single slip in
    it. Both comparisons again, because the year is as likely to be the thing
    the file left off here as anywhere else.
    """
    if titlematch.equivalent(base, head):
        return True
    bare = untitled_base(base)
    if bare != base and titlematch.equivalent(bare, head):
        return True
    return bool(typos) and (titlematch.one_typo_apart(base, head)
                            or (bare != base
                                and titlematch.one_typo_apart(bare, head)))


def alias_renames(base: str, names, aliases) -> dict:
    """{name: the name it should take} for a file that is this film under
    another of its titles AND says something else besides.

    The something else is an edition: a library that cannot mux every language
    into one file keeps one file per language and writes the language after the
    year. That suffix is what tells the files apart, and once it becomes an
    ``{edition-}`` tag Plex collapses them into one film with a picker - so the
    title in front of it is free to take the folder's spelling, and nothing is
    lost by its doing so.

    A file whose WHOLE name is this film under another title is not in here: its
    title is the only thing distinguishing it, and rewriting that would throw
    away which language the file is. :func:`named_by_catalogue` recognises those
    and leaves them alone.
    """
    keys = frozenset().union(*(titlematch.title_keys(t) for t in aliases)) \
        if aliases else frozenset()
    held = set(names)
    renames: dict[str, str] = {}
    for name in sorted(names):
        wanted = _onto_base_by_alias(base, name, keys)
        if wanted and wanted != name and wanted not in held:
            renames[name] = wanted
            held.add(wanted)
    return renames


def _onto_base_by_alias(base: str, name: str, keys: frozenset) -> str:
    """``name`` respelled onto ``base`` when a PROPER prefix of it is one of the
    catalogue's titles for this film, or "".

    Proper: something has to be left over. A name the catalogue accounts for
    entirely is the language case, not the edition case.
    """
    if not keys or is_kept_copy(name) or name.startswith(base):
        return ""
    year = year_of(base)
    for cut in _boundaries(name):
        head = untitled_base(name[:cut].rstrip(), year)
        rest = name[cut:]
        if not head or not _says_more_than_the_year(rest):
            continue
        if _numbers_another_film(rest):
            continue
        # On several words, and never on one. A one-word meeting is a word of
        # the SAME title, not a title: a prefix as short as "II" carries its
        # own word into the fold of the longer title it is a part of, and a
        # title written in a script the fold cannot read leaves the same bare
        # word behind - its "II", its "2". Meeting a catalogue title on that
        # one word means the head is a PREFIX of the title the name says, and
        # the leftover is the title's own continuation - which is how
        # "II: The Stone (1982).de.srt" came back "Hollow Creek II: The Stone
        # (1982) Stone (1982).de.srt", a name no later run can take apart.
        shared = titlematch.title_keys(head) & keys
        if shared and any(" " in key for key in shared):
            return base + rest
    return ""


def _says_more_than_the_year(rest: str) -> bool:
    """Whether what follows a matched title is an EDITION rather than nothing.

    The year is not one: the folder carries it already, and a remainder of
    " (1979).mkv" appended to a base that ends in "(1979)" writes the year
    twice. Neither is a bare extension - that is the whole-name match, which
    this function's caller must leave alone.
    """
    rest = _YEAR_LEADING.sub("", rest.strip(), count=1).strip()
    return bool(rest) and not rest.startswith(".")


def named_by_catalogue(names, aliases, base: str = "") -> dict:
    """{name: the catalogue's own writing of the title it answers to}, for the
    names a CATALOGUE says are this film - and which nothing about the strings
    could have said.

    "Die Blaue Stunde" and "L'Ora Blu" have not a
    syllable in common with "The Blue Hour" or with each other, and
    are one film in three languages. No rule over the text will ever join them;
    the alternative titles the catalogue holds do.

    Recognition only. A name this accounts for keeps every character it has -
    which of a film's languages a file is named in is a thing its owner chose,
    and a lookup is no reason to overwrite it.

    ``aliases`` are the titles as the catalogue writes them, and one of those is
    what comes back - not the key that matched. A report is read by a person,
    and a fold is not a title: "Die Blaue Stunde" folds to a key
    with "and" in the middle of it, which is not a name anything has.

    ``base`` is the folder's own name, and what it is for is the year: a file
    named in another language carries its date the way its owner wrote it, in
    brackets or without them, and either way that date is the folder's and not
    part of the title a catalogue would recognise. Its stacking token and its
    tags come off for the same reason - "Den Ljusa Vagen 1992 Part1" is one
    piece of a film whose title is two of those words.
    """
    found = {}
    year = year_of(base)
    for name in names:
        stem = film_key(os.path.splitext(name)[0])
        keys = titlematch.title_keys(untitled_base(stem, year))
        for written in aliases:
            if titlematch.title_keys(written) & keys:
                found[name] = written
                break
    return found


def spelling_renames(base: str, names, also=(), typos: bool = False) -> dict:
    """{name: the name it should be spelled as}, for the names that differ.

    Only the ones a spelling explains: a name this folder's film cannot be read
    out of is not in here at all, and is what :func:`strays_in` goes on to
    report. A rename that would land on a name the folder already holds is
    dropped rather than made - the two files are then genuinely two, whatever
    their names say, and that is a thing for someone to look at.

    ``also`` and ``typos`` are :func:`onto_base`'s, and they are asked
    differently. A spelling in ``also`` is a name something has VOUCHED for, so
    it stands beside the folder's own and is read at the same time - which
    matters, because it is usually the longer reading of the two: a file dated
    "(1952)" answers to a vouched-for "Film (1952)" whole, and to the folder's
    own "Film (1953)" only by giving the date up as a leftover.

    A typo is a guess, so it is asked in a SECOND pass over the names the first
    could not explain at all. That order is the whole of "only where nothing
    closer was found": the guess never gets to claim a name from under a
    reading, nor the name a reading was going to take, because the first pass
    has already held it.
    """
    held = set(names)
    corrected: dict[str, str] = {}
    for guessing in (False, True) if typos else (False,):
        for name in sorted(names):
            if name in corrected:
                continue
            wanted = onto_base(base, name, also, guessing)
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


def _word_counter(words) -> Counter:
    """A name's words as a multiset, compared the way titles are compared:
    case folded, and the brackets a year often wears stripped away, so a
    "(1981)" and a bare "1981" are the same word."""
    return Counter(word.casefold().strip("()[]") for word in words)


def conform_to_folder(base: str, tag: str, stem: str) -> tuple:
    """A mangled movie name brought back to the FOLDER's own spelling, or a
    verdict that it cannot be.

    ``base`` is the folder's name without its id tag, ``tag`` the tag the folder
    carries, and ``stem`` the file's name with its extension off - a name that
    never got the dot before its "mkv", read as the words it is. The file is
    accepted as the folder's own film only if it carries that folder's tag, and
    once the tag and a trailing stacking token are taken off what is left must
    be the folder's own words plus nothing but a year said a second time. Then
    the name is rebuilt the way Plex spells it - the film, its id, and the
    token last - and handed back.

    What comes back is ``(new stem, unfixable)``. ``("", False)`` is not the
    folder's film to conform - it carries no tag, or a DIFFERENT tag, so the
    caller leaves it to the phases that match films to folders. ``(stem, True)``
    is a film the folder's spelling does not account for: a word of its own
    (an edition, a release marker) or one of the folder's words missing, and the
    caller reports it rather than guess at what it is.
    """
    if not tag:
        return "", False
    match = ID_TAG_RE.search(stem)
    if not match or match.group(0) != tag:
        return "", False
    words = ID_TAG_RE.sub(" ", stem).split()

    # The stacking token sits last in a name and is read off before the rest is
    # judged. A number of three digits or more is a year, never a part, so a
    # token carrying one is not read as a token at all - it stays in place, and
    # a word that is not a number makes the name unfixable below.
    part = ""
    if words and STACK_RE.match(words[-1]):
        number = _NUMBER_TAIL.search(words[-1])
        if number is not None and len(number.group(0)) < 3:
            part = words[-1]
            words.pop()
    elif len(words) >= 2 and words[-1].isdigit() and len(words[-1]) < 3:
        loose = _LOOSE_PART.match(words[-2])
        if loose is not None:
            part = loose.group(1) + words[-1]
            del words[-2:]

    have = _word_counter(words)
    folder = _word_counter(base.split())
    if folder - have:
        # A word the folder says that the film never did: not the folder's film
        # misspelt, but a different name, and the caller is not here to guess.
        return "", True
    if any(not word.isdigit() for word in have - folder):
        # A real word left over once the folder's are accounted for - an
        # edition, a release marker - the spelling the folder gives does not
        # cover, and cannot be rebuilt from it.
        return "", True
    return plex_stem(base, tag, "", part), False


def folder_renames(base: str, tag: str, names, aliases=(),
                   corrected: dict | None = None,
                   cropped: list | None = None,
                   over_limit: list | None = None,
                   edition: str = "") -> list:
    """Every rename one movie folder needs, as (old name, new name) pairs.

    ``base`` is the folder without its id tag and ``tag`` the tag to carry;
    ``names`` is the folder's listing. A file that belongs to no film in the
    folder is left alone, as is every "(old)" copy, and a name that is already
    right produces no pair - so a second run over the same folder returns [].

    ``aliases`` are the catalogue's own titles for this film, which is what
    lets a file named in another language and carrying an edition after it -
    "<film in English> (1968) English.mkv" - be read as this film's English
    edition rather than as a second film.

    A name that says this folder's film in a different spelling is brought onto
    the folder's own first, and then read as any other name is - so a file that
    arrived as "le seigneur de valmont pt 1.mkv" leaves with the folder's
    capitals, the folder's accents and a stacking token Plex can see.

    ``corrected`` is those respellings when the caller has already worked them
    out, and passing them is how a caller that read the folder under WIDER
    readings than this function has - a franchise the folder above says, a year
    a catalogue vouched for, a letter somebody got wrong - renames what it
    reported rather than a second, narrower answer to the same question.

    ``cropped`` and ``over_limit``, when lists are passed, collect what the file
    name limit did to the plan: the commentary outputs whose track name had to
    be cut back to fit, and the names the id tag pushes past the limit that may
    not be cut at all. Neither is in the plan under its long name - the first is
    in it cut, the second is not in it at all - so a caller that asks for
    neither list still gets a plan that can be carried out.

    ``edition`` is a release only the FOLDER named, for a film that names none
    of its own. A film that names its own keeps that one - it is the nearer of
    the two answers.
    """
    if corrected is None:
        corrected = spelling_renames(base, names)
        # Never over a correction the NAMES themselves account for. What the
        # two readings disagree about is always the catalogue reaching further
        # than it should: a name the spelling already explains is explained.
        for name, wanted in alias_renames(base, names, aliases).items():
            corrected.setdefault(name, wanted)
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
        own, part = read_stem(base, stem)
        wanted = plex_stem(base, tag, own or edition, part)
        target = _fits_or_crops(name, wanted, _untagged_tail(
            spelled[len(stem):]), cropped, over_limit)
        if target and target != name:
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


def id_tag_renames(tag: str, names, cropped: list | None = None,
                   over_limit: list | None = None) -> list:
    """Every rename that does nothing but put ``tag`` on this folder's files.

    The answer for a folder whose names cannot be made to agree and whose id is
    not in doubt: each film keeps the name it has, each sidecar follows its own
    film the way it always does, and the one thing that changes is that Plex can
    now see which film they are.

    ``cropped`` and ``over_limit`` collect what the file name limit did to the
    plan, exactly as they do for a folder being renamed outright.
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
        if wanted == stem:
            continue
        target = _fits_or_crops(name, wanted, name[len(stem):],
                                cropped, over_limit)
        if target and target != name:
            plan.append((name, target))
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


def folder_for(stem: str) -> str:
    """The folder one loose film belongs in, read off the film's own name.

    The film and its id, and not the release: the words a file puts after its
    year say which CUT it is, and a folder holds every cut of one film. So a
    "<film> (1999) Extended Edition.mkv" keeps those words - Plex reads the
    edition off the file - and is given a "<film> (1999)" to sit in, which is
    the folder the theatrical copy beside it is given too.

    The same cut written out twice, once on the folder and once on the file, is
    what :func:`folder_renames` then has to unpick, and is where a theatrical
    cut ends up wearing an extended one's name. Not writing it twice is the
    cheaper half of that.

    A name with no year says nothing that can be cut at, and stands as the
    folder's name whole.
    """
    base, tag = untagged_base(stem)
    years = list(_YEAR_IN_NAME.finditer(base))
    if not years:
        return stem
    return folder_name(base[:years[-1].end()], tag)


def is_bonus_folder_name(name: str) -> bool:
    """Whether a folder of this name holds bonus material rather than a film.

    Matched on the name's END, so a disc's "Movie Featurettes" counts as much
    as a plain "Featurettes". Two ways in: a name ending in "extras" in any
    case - the one spelling common enough in the wild to catch before anything
    has been renamed - or one ending in a folder name in the case Plex spells
    it, which is also the case this library writes.
    """
    if shell_lower(name).endswith("extras"):
        return True
    return any(name.endswith(candidate) for candidate in BONUS_FOLDERS)


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


def films_own_spelling(base: str, names) -> tuple:
    """(the title the folder's films spell, how many of its files said so),
    where that differs from the folder's own by a single slip - ("", 0)
    otherwise.

    The single-slip reading is a guess, and it reads a folder and a file as one
    name without saying which of the two got it right. Nothing in the two
    strings can say. What can is how many names each spelling has behind it: a
    folder is one name written once, and the files under it are what somebody
    was handed, so a spelling every file agrees on and the folder does not is
    the folder's slip rather than theirs.

    The spelling is taken from the MOVIE files, which carry no language suffix
    to read around; the count is every file that goes on to say it, sidecars
    included, because each is a name that would have to be wrong too.

    ("", 0) where the films do not agree among themselves: an edition or a
    stacking token puts more than the title between the two names, and which
    part of that the slip is in is not a thing to guess at.
    """
    spellings = {re.sub(r"\s+", " ",
                        ID_TAG_RE.sub("", os.path.splitext(name)[0])).strip()
                 for name in names if is_movie_file(name)}
    if len(spellings) != 1:
        return "", 0
    theirs = spellings.pop()
    if not theirs or theirs == base or titlematch.equivalent(theirs, base):
        return "", 0
    if not titlematch.one_typo_apart(theirs, base):
        return "", 0
    said = sum(1 for name in names
               if re.sub(r"\s+", " ",
                         ID_TAG_RE.sub("", name)).strip().startswith(theirs))
    return theirs, said


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
