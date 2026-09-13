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

# A film's id tag, anywhere in the name. Plex reads the tag whether it sits on
# the folder or the file, and is indifferent to the order tags come in.
ID_TAG_RE = re.compile(r"\{(?:imdb|tmdb)-[^{}]*\}")

# The edition tag, which needs a Plex Pass to be DISPLAYED but is harmless
# without one.
EDITION_RE = re.compile(r"\{edition-([^{}]*)\}")

# The tokens Plex's scanner stacks on, as one trailing word: the keyword, an
# optional dot, and a number. Matched case-insensitively, the way the scanner
# does, so a "Part1" and a "cd2" are both recognised as written.
STACK_RE = re.compile(r"^(?:cd|dvd|part|pt|disk|disc)\.?[0-9]+$", re.I)

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
    _edition, part = _markers(stem)
    words = ID_TAG_RE.sub(" ", EDITION_RE.sub(" ", stem)).split()
    if part:
        words = words[:-1]
    return " ".join(words)


def _markers(stem: str) -> tuple:
    """The edition name and the stacking token ``stem`` carries, either of which
    may be ""."""
    match = EDITION_RE.search(stem)
    edition = match.group(1) if match else ""
    words = ID_TAG_RE.sub(" ", EDITION_RE.sub(" ", stem)).split()
    part = words[-1] if words and STACK_RE.match(words[-1]) else ""
    return edition, part


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
    edition, part = _markers(remainder)
    leftover = ID_TAG_RE.sub(" ", EDITION_RE.sub(" ", remainder)).split()
    if part:
        leftover = leftover[:-1]
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


def folder_renames(base: str, tag: str, names) -> list:
    """Every rename one movie folder needs, as (old name, new name) pairs.

    ``base`` is the folder without its id tag and ``tag`` the tag to carry;
    ``names`` is the folder's listing. A file that belongs to no film in the
    folder is left alone, as is every "(old)" copy, and a name that is already
    right produces no pair - so a second run over the same folder returns [].
    """
    stems = movie_stems(base, names)
    plan = []
    for name in sorted(names):
        if is_kept_copy(name):
            continue
        for stem in stems:
            if name.startswith(stem):
                break
        else:
            continue
        wanted = plex_stem(base, tag, *read_stem(base, stem))
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
