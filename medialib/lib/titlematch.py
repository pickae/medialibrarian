"""When two spellings name the same title, and which spellings to go asking under.

A library and a catalogue rarely disagree about a film. They disagree about how
its name is written: an accent someone's keyboard could not reach, an em dash
where the catalogue has a colon, "Part II" against "Part 2", a leading "The"
that fell off, "&" written out, an abbreviation with its points taken away. Each
of those is a different title to a string compare and the same film to a person.

The module answers that in one direction only: it FOLDS a title down to a set of
keys, and two titles are the same title when their key sets meet. Folding rather
than editing is what keeps it safe to widen - a new fold adds keys, so a title
that matched yesterday still matches, and the one thing a widening can do is
bring a second candidate into view, which every caller here answers by naming
nothing rather than by guessing between them.

The keys come from four independent readings of one title, and their
combinations:

* the **separator** it was written with, or none at all - which is one reading
  of an apostrophe, a dash, a point in an abbreviation and a joined-up compound;
* the **accents** it carries, dropped the way a transliteration drops them and
  spelled out the way a language writes them when it cannot reach them;
* the **articles** it carries, kept and dropped - the one it leads or trails
  with, and all of them wherever they sit;
* its **numerals**, roman read as arabic;
* the **filler** a local library puts in front of a name and a catalogue never
  carries.

Nothing here is about films. A title is a title, and the same fold serves a
book, an album or an episode.
"""

from __future__ import annotations

import functools
import re
import subprocess
import unicodedata

from medialib.lib.enums import DUPLICATE_MARKER_PATTERNS, shell_lower

__all__ = [
    "normalize_title",
    "title_keys",
    "equivalent",
    "search_titles",
    "strip_duplicate_marker",
    "reset_iconv_flavour",
]


# --- the fold to ASCII -------------------------------------------------------

# A library-sized run folds the same handful of titles over and over - a
# folder's own name against each of its files, and each file against every
# prefix of itself - and every fold that reaches iconv is a process. The cache
# is what makes asking cheap enough to ask often; :func:`reset_iconv_flavour`
# empties it, because a different iconv is a different answer.
_CACHE_SIZE = 4096


@functools.lru_cache(maxsize=_CACHE_SIZE)
def normalize_title(title: str) -> str:
    """Fold a title to a comparison key: transliterated, lower case, and every
    non-alphanumeric run collapsed to a single space.

    Only ever used to compare titles, never to rename - the on-disk name stays
    verbatim, and a name that is going to CHANGE is spelled the way whoever
    answered spells it.

    The transliteration is delegated to the host's ``iconv`` rather than
    re-implemented, because its table and its exit-code rule (a handful of
    codepoints transliterate AND fail, which resets the string to the original)
    are a property of glibc and the active locale, not of the title.

    Only where the host's iconv is glibc's, though - see
    :func:`_iconv_drops_accents`. The other widespread implementation, GNU
    libiconv, is what macOS and MSYS ship, and it SPELLS an accent out rather
    than dropping it: "Amélie" comes back "Am'elie", which folds on to
    "am elie" and no longer matches the "amelie" the same film's ASCII
    spelling gives. Those hosts, and a host with no iconv at all, fold in
    Python instead (:func:`_fold_without_iconv`).
    """
    if title.isascii():
        # Nothing for a transliteration to do, so it is not asked: ASCII goes
        # through ASCII//TRANSLIT unchanged and with a zero exit, and asking
        # anyway is a process per title. Most of a library is this case, and
        # the folder-against-file comparisons ask it once per name per word
        # boundary - so the short-circuit is the difference between a handful
        # of processes over a run and tens of thousands of them.
        return re.sub(r"[^a-z0-9]+", " ", shell_lower(title)).strip(" ")
    folded = None
    if _iconv_drops_accents():
        # No guard on the start, unlike everywhere else a tool is optional: the
        # probe above only answers true by having RUN iconv, so reaching here
        # means it was there a moment ago.
        proc = subprocess.run(
            ["iconv", "-f", "UTF-8", "-t", "ASCII//TRANSLIT"],
            input=title.encode("utf-8"), capture_output=True)
        if proc.returncode != 0:
            # iconv gave up somewhere: the original stands, exactly as the
            # shell's `|| s="$1"` does.
            folded = title
        else:
            folded = proc.stdout.decode("utf-8", "replace")
    if folded is None:
        folded = _fold_without_iconv(title)
    folded = shell_lower(folded)
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    return folded.strip(" ")


# One accented letter and what a glibc iconv makes of it. The probe is a single
# letter on purpose: what is being told apart is the two tables' rule for a
# base-plus-accent, which is the case every title in a Latin-script library
# turns on.
_TRANSLIT_PROBE = "é"          # e with acute
_TRANSLIT_PROBE_GLIBC = "e"

# The answer to that probe, worked out once. A list rather than a global name
# so the reset below can empty it without a `global` statement, the way the
# other modules here keep their per-run state.
_ICONV_FLAVOUR: list[bool] = []


def reset_iconv_flavour() -> None:
    """Forget what the host's iconv was found to do, so the next fold asks
    again. For the cases that stand a different iconv on PATH.

    Empties the folded titles with it: they were folded by the iconv that is
    being forgotten, and keeping them would answer the old way forever.
    """
    _ICONV_FLAVOUR.clear()
    normalize_title.cache_clear()
    title_keys.cache_clear()


def _iconv_drops_accents() -> bool:
    """Whether this host's ``iconv`` transliterates the way the recorded
    behaviour expects: an accent DROPPED, "é" to "e".

    False for GNU libiconv, which answers "'e", and false when there is no
    iconv to ask. Asked once per process and remembered: a lookup over a movie
    library calls the fold once per folder, and the answer cannot change under
    a running command.
    """
    if not _ICONV_FLAVOUR:
        _ICONV_FLAVOUR.append(_probe_iconv())
    return _ICONV_FLAVOUR[0]


def _probe_iconv() -> bool:
    try:
        proc = subprocess.run(
            ["iconv", "-f", "UTF-8", "-t", "ASCII//TRANSLIT"],
            input=_TRANSLIT_PROBE.encode("utf-8"), capture_output=True)
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    return proc.stdout.decode("utf-8", "replace") == _TRANSLIT_PROBE_GLIBC


# The letters a decomposition cannot reach, with what glibc's table writes for
# each: they are single codepoints with no combining form, so NFKD leaves them
# whole and the ASCII pass would drop them altogether. Kept to the Latin
# alphabet's own oddities, which is what a film title in this library holds.
_LIGATURES = {
    "Æ": "AE", "æ": "ae",      # AE
    "Œ": "OE", "œ": "oe",      # OE
    "ß": "ss",                      # sharp s
    "Ø": "O", "ø": "o",        # O with stroke
    "Ð": "D", "ð": "d",        # eth
    "Đ": "D", "đ": "d",        # D with stroke
    "Ł": "L", "ł": "l",        # L with stroke
    "Þ": "TH", "þ": "th",      # thorn
    "Ħ": "H", "ħ": "h",        # H with stroke
    "Ŧ": "T", "ŧ": "t",        # T with stroke
    "ẞ": "SS",                      # capital sharp s
    "ı": "i", "İ": "I",        # Turkish dotless and dotted i
    "Ŋ": "N", "ŋ": "n",        # eng
    "ȷ": "j",                       # dotless j
    "ſ": "s",                       # long s
}

# The table is the Latin alphabet's own oddities and stops there. glibc has an
# opinion about another seventy-odd letters with no decomposition - African
# hooks, medieval Welsh digraphs, phonetic bars - and each of them drops here
# rather than folding, because a film title in this library is not written in
# any of them. It costs nothing on a glibc host, which never reaches this
# function at all.


def _fold_without_iconv(title: str) -> str:
    """The transliteration in Python: for a host whose iconv spells accents out
    rather than dropping them, and for one that has no iconv at all.

    Decompose, drop the combining marks, spell out the ligatures a
    decomposition cannot reach, and keep what is left of ASCII. That
    reproduces glibc's answer for the Latin script - which is what makes this
    a fallback and not a second behaviour: the same library folds to the same
    keys whichever host reads it.

    Where it still differs is a script glibc has an opinion about and Unicode
    does not - Cyrillic and Greek come back as "?" there and as nothing here.
    Both are degenerate keys either way, and both sides of every comparison
    are folded by this same function, so a title in one of those scripts
    matches or fails to match as a whole rather than half-folding.
    """
    spelled = "".join(_LIGATURES.get(character, character)
                      for character in title)
    decomposed = unicodedata.normalize("NFKD", spelled)
    kept = "".join(c for c in decomposed if not unicodedata.combining(c))
    return kept.encode("ascii", "ignore").decode("ascii")


# --- the words a fold has to know about --------------------------------------

# The symbols that are WORDS, spelled out before the fold takes them away. "&"
# and "and" are one title written twice, and the fold's own rule - every
# non-alphanumeric run becomes a space - would leave "Hansel Gretel" against
# "Hansel and Gretel" and no way back.
SYMBOL_WORDS = (
    ("&", " and "),
    ("＆", " and "),      # fullwidth ampersand
    ("+", " plus "),
    ("＋", " plus "),     # fullwidth plus
    ("@", " at "),
)

# The conjunctions of the languages this library holds, all read as one word.
# A Dutch folder's "Hans en Grietje" and a catalogue's "Hans & Grietje" are the
# same title, and after the substitution above the only thing still between them
# is which language the "and" was written in.
#
# Applied to an INTERIOR token only - never the first or the last - which is
# what keeps the short ones from eating a title. "E.T." folds to "e t", "I" and
# "Y" begin titles of their own, and none of them is a conjunction in the one
# position a conjunction sits in.
CONJUNCTIONS = {
    "and": "and", "und": "and", "et": "and", "en": "and", "y": "and",
    "e": "and", "ed": "and", "i": "and", "og": "and", "och": "and",
    "vs": "vs", "v": "vs", "versus": "vs", "gegen": "vs", "contre": "vs",
}

# The articles a title leads or trails with, over the same languages. Dropped
# and kept, never one or the other: "The Thing" and "Thing" are matched either
# way round, and so is a catalogue's "Shining, The".
ARTICLES = frozenset((
    "the", "a", "an",
    "le", "la", "les", "l", "un", "une", "des", "du", "de",
    "der", "die", "das", "den", "dem", "ein", "eine", "einer", "einen",
    "het", "een",
    "el", "los", "las", "una", "unos", "unas",
    "il", "lo", "gli", "i", "uno",
    "o", "os", "as",
))

# The words a local library puts in FRONT of a title and a catalogue never
# carries: what someone wrote to say what kind of thing the file is, rather than
# what the thing is called. Dropped as a leading run, and only while something
# is left to be the title.
FILLER_WORDS = frozenset((
    "movie", "movies", "film", "filme", "films", "feature", "featurefilm",
    "featurette", "featurettes", "special", "specials", "fullmovie",
    "thefilm", "themovie",
))

# The letters a language spells OUT when it cannot write the accent, rather than
# dropping the accent the way a transliteration does. A German keyboard's
# "Uebung" and a German title's "Übung" are one word; so are Danish "Aarhus" and
# "Århus", and Norwegian "oe" for "ø".
#
# A reading and not a correction: the plain fold already gives "ubung", and this
# adds "uebung" beside it, so a folder spelled either way meets a catalogue
# spelled either way. The ligatures above are not in here because the fold
# already spells those out - "Æ" is "ae" and "ß" is "ss" on both sides.
#
# One direction only, and it has to stay that way. Reading "ae" back as "a"
# would be the same rule run backwards, and it cannot be: "Michael" would
# answer to "Michal" and "Aeon Flux" to "Aon Flux". The accented spelling is
# what the two written ones have in common, and a catalogue carries it - so
# both of them meet it there, which is where they need to meet.
SPELLED_OUT = (
    ("ä", "ae"), ("Ä", "Ae"), ("ö", "oe"), ("Ö", "Oe"),
    ("ü", "ue"), ("Ü", "Ue"),
    ("å", "aa"), ("Å", "Aa"), ("ø", "oe"), ("Ø", "Oe"),
)

# A roman numeral as a whole token, in the one spelling that is a number:
# thousands, then the hundreds, tens and units each with their own subtractive
# form. Written out rather than "any run of roman letters", which is what keeps
# "did", "dim" and "civil" words rather than numbers.
#
# It does not keep "mix" one, and nothing short of a dictionary would: MIX is
# 1009 spelled correctly. The reading is additive, so what that costs is one
# extra key on a title nobody else has - a film called "Mix" would have to meet
# a film called "1009" for it to matter, and the certainty rule answers a
# meeting of two candidates by naming neither.
_ROMAN = re.compile(
    r"^m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$")
_ROMAN_VALUES = (("m", 1000), ("cm", 900), ("d", 500), ("cd", 400),
                 ("c", 100), ("xc", 90), ("l", 50), ("xl", 40),
                 ("x", 10), ("ix", 9), ("v", 5), ("iv", 4), ("i", 1))

# How many keys one title may produce. The readings multiply, and a title that
# answers to every one of them at once is a title nothing is going to be certain
# about anyway; the cap is what keeps a pathological name from costing a
# library-sized run its afternoon.
MAX_KEYS = 128


def _spelled_out(title: str) -> str:
    """``title`` with every accent a language writes out written out."""
    for accented, spelling in SPELLED_OUT:
        title = title.replace(accented, spelling)
    return title


def _tokens(title: str) -> list[str]:
    """The folded words of a title, with the symbols that are words spelled out
    and the conjunctions read as one."""
    text = title
    for symbol, word in SYMBOL_WORDS:
        text = text.replace(symbol, word)
    words = normalize_title(text).split()
    return [CONJUNCTIONS.get(word, word) if 0 < index < len(words) - 1 else word
            for index, word in enumerate(words)]


def _without_article(words: list[str]) -> list[str]:
    """``words`` with a leading or trailing article taken off, or ``words``.

    Both ends, because the two conventions for the same title put it at
    opposite ones: "The Shining" on disk, "Shining, The" in a catalogue that
    sorts by the first real word.
    """
    if len(words) > 1 and words[0] in ARTICLES:
        return words[1:]
    if len(words) > 1 and words[-1] in ARTICLES:
        return words[:-1]
    return words


def _without_articles(words: list[str]) -> list[str]:
    """``words`` with EVERY article taken out, while two words are left.

    An article does not only go missing from the front. "The Lord of the Rings"
    is written "Lord of the Rings", "Lord of Rings" and "The Lord of Rings" by
    three different hands, and taking all of them out of both sides is what
    makes those one title however many went missing from either.

    The two-word floor is there to keep RECALL, not to keep the module honest -
    a widening cannot make it name the wrong film, because a second candidate
    coming into view is answered by naming neither. What it can do is take a
    certain match away: "La La Land" worn down to "Land" carries a key a real
    and different film already has, and a folder holding one of them would stop
    being nameable at all. So a title this reading would wear down to a single
    word keeps its articles.

    The floor is this reading's own. The leading-article reading above still
    takes one article off a two-word title, because that IS the convention a
    catalogue varies by - which is also what makes "Los Angeles" answer to
    "Angeles".
    """
    kept = [word for word in words if word not in ARTICLES]
    return kept if len(kept) >= 2 else words


def _without_filler(words: list[str]) -> list[str]:
    """``words`` with its leading filler run taken off, while a title remains."""
    cut = 0
    while cut < len(words) - 1 and words[cut] in FILLER_WORDS:
        cut += 1
    return words[cut:]


def _arabic_numerals(words: list[str]) -> list[str]:
    """``words`` with every roman numeral token written as a number.

    One direction and not both: a number has exactly one roman spelling, so
    folding the roman ONTO the arabic makes "Part II" and "Part 2" one key,
    while folding the other way would have to pick a spelling and would meet
    nothing that was not already met.
    """
    return [str(_roman_value(word)) if _ROMAN.match(word) and word else word
            for word in words]


def _roman_value(numeral: str) -> int:
    """What a roman numeral counts to. Only ever called on one :data:`_ROMAN`
    has already accepted, so there is no invalid spelling to reject."""
    total = 0
    rest = numeral
    for glyph, value in sorted(_ROMAN_VALUES, key=lambda pair: -pair[1]):
        while rest.startswith(glyph):
            total += value
            rest = rest[len(glyph):]
    return total


def _widen(forms: list, reading) -> list:
    """Every form ``forms`` holds, plus what ``reading`` makes of each - once.

    Additive on purpose: a reading is a way the title MIGHT have been written,
    not a correction of it, so both stay.
    """
    widened = list(forms)
    for words in forms:
        other = reading(words)
        if other != words and other and other not in widened:
            widened.append(other)
    return widened


@functools.lru_cache(maxsize=_CACHE_SIZE)
def title_keys(title: str) -> frozenset:
    """Every key a title may be matched by.

    Two titles name the same thing when these sets meet - which is what
    :func:`equivalent` asks, and what a catalogue lookup asks of each candidate
    it was offered.

    Each key comes in two spellings, the words joined by a space and the words
    joined by nothing. That pair is the whole of the punctuation question: a
    title written "S.W.A.T.", "S W A T" or "SWAT" folds to one squeezed key, and
    so do "Spider-Man" and "Spider Man", "A Cat's Tale" and "A Cats Tale", and
    every dash, comma, point and exclamation mark between two words.
    """
    forms = [_tokens(title)]
    written = _tokens(_spelled_out(title))
    if written != forms[0]:
        forms.append(written)
    for reading in (_without_article, _without_articles, _without_filler,
                    _arabic_numerals):
        forms = _widen(forms, reading)
    keys = set()
    for words in forms:
        keys.add(" ".join(words))
        keys.add("".join(words))
        if len(keys) >= MAX_KEYS:
            break
    keys.discard("")
    return frozenset(keys)


def equivalent(one: str, other: str) -> bool:
    """Whether two written titles name the same title."""
    return bool(title_keys(one) & title_keys(other))


# --- what to go asking under -------------------------------------------------

# Where a name that begins with something other than the title breaks: a
# franchise someone repeated on every film in it, a label, a collection. What
# stands AFTER one of these is worth asking about on its own.
_SEGMENT = re.compile(r"\s[-–—]\s|:\s|\s\|\s")

# How many spellings one title is worth asking a catalogue about. Every one of
# them is a request, and a title that is not found under the first few is not
# one more query is going to settle.
MAX_QUERIES = 5


def search_titles(title: str) -> list:
    """The spellings to ask a catalogue under, the written one first.

    :func:`title_keys` says whether an ANSWER is the title; this says what to
    ask, which is a different question - a catalogue matches text of its own and
    has never heard of these keys. Each entry is a plausible way the same film
    is catalogued, and the list is short because each one costs a request.

    The written title always leads, so a name that is already right is found by
    the first query and the rest are never paid for.

    Two spellings that FOLD to one are one query and not two: a catalogue's
    search is no more troubled by an accent or a capital than the fold is, so
    asking it twice would buy a second request and the same answer. What earns a
    query is a spelling with different WORDS in it.
    """
    stripped = _filler_stripped(title) or title
    found = [title]
    folds = {normalize_title(title)}
    for spelling in (stripped, _last_segment(stripped),
                     _arabic_spelling(stripped), normalize_title(title)):
        folded = normalize_title(spelling) if spelling else ""
        if folded and folded not in folds:
            found.append(spelling)
            folds.add(folded)
    return found[:MAX_QUERIES]


def _filler_stripped(title: str) -> str:
    """``title`` without the leading "Movie" / "Special" / "Featurefilm" a local
    library wrote in front of it, and without the punctuation that followed."""
    rest = title
    while True:
        head, _sep, tail = rest.partition(" ")
        if not tail or shell_lower(head).strip(".:-") not in FILLER_WORDS:
            break
        rest = tail.lstrip(" -:.–—|")
    return rest if rest != title else ""


def _last_segment(title: str) -> str:
    """What stands after the LAST " - " or ": " in ``title``.

    The franchise someone repeated on every film in it - "James Bond - Goldfinger",
    "Star Wars: A New Hope" - is not on the catalogue's side of the name, and
    what is left once every such prefix is gone is the film's own title.

    Empty when the title has no such break, and empty when the break is the only
    thing that made it one: a tail this cuts to nothing has nothing to ask
    about.
    """
    tail = _SEGMENT.split(title)[-1].strip()
    return tail if tail and tail != title.strip() else ""


def _arabic_spelling(title: str) -> str:
    """``title`` with its roman numerals written as numbers, for a catalogue
    that has the film under "Part 2" where the disk says "Part II"."""
    words = title.split()
    rewritten = [str(_roman_value(shell_lower(word)))
                 if _ROMAN.match(shell_lower(word)) and word else word
                 for word in words]
    return " ".join(rewritten) if rewritten != words else ""


# --- the marker a copy carries -----------------------------------------------

_DUPLICATE_MARKER = re.compile(
    r"(?:" + "|".join(DUPLICATE_MARKER_PATTERNS) + r")$", re.I)


def strip_duplicate_marker(stem: str) -> str:
    """``stem`` without the marker a file manager gave a second copy of it.

    One marker and not a run of them: "Film (1) (2)" is a copy of a copy, and
    what it is a copy OF is still the name with one taken off, which the caller
    can ask about again if it wants to.
    """
    return _DUPLICATE_MARKER.sub("", stem).rstrip()
