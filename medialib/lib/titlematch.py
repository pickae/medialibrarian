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
  of an apostrophe, a dash, a point in an abbreviation and a joined-up compound,
  and, with the possessive "s" the fold leaves standing, of a series named with
  one and without;
* the **accents** it carries, dropped the way a transliteration drops them and
  spelled out the way a language writes them when it cannot reach them;
* the **articles** it carries, kept and dropped - the one it leads or trails
  with, and all of them wherever they sit;
* its **numerals**, roman read as arabic; a trailing "1" dropped, because the
  first film of a series is numbered three ways and meant identically; and a
  number with title on BOTH sides of it dropped, because what surrounds it
  still says which film it is;
* the **filler** a local library puts in front of a name and a catalogue never
  carries - or writes INTO the middle of it, which is the same word doing the
  same nothing in another position.

One thing here is not a fold, and is kept apart from them for that reason:
:func:`one_typo_apart` answers whether two titles are one title with a single
letter wrong. A fold is a reading of what somebody wrote and cannot be wrong;
a typo is the claim that they wrote it wrong, which is a guess - so it is
never a key, it is asked only where every fold has already failed, and it is
refused on a short name and on any difference that touches a digit.

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
    "one_typo_apart",
    "leading_segment",
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
    than dropping it: "Sélène" comes back "Am'elie", which folds on to
    "am elie" and no longer matches the "selene" the same film's ASCII
    spelling gives. Those hosts, and a host with no iconv at all, fold in
    Python instead (:func:`_fold_without_iconv`).

    A letter that is the SHAPE of a Latin one is read as that letter first -
    see :data:`CONFUSABLES` - because no transliteration will ever do it: to
    iconv a Cyrillic TE is Cyrillic, and to a reader it is a T.
    """
    # Before anything else, including the short-circuit below: a name whose only
    # non-ASCII character is a letter from the wrong keyboard becomes ASCII here
    # and costs no process at all.
    title = title.translate(CONFUSABLES)
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

# The letters of another alphabet that are the SAME SHAPE as a Latin one, and
# what they are the shape of. A file called "Тempest - Rising" whose first letter
# is a Cyrillic TE is the same name as one spelled with a Latin T - it is the
# same name on the screen - and the fold, which has no opinion about Cyrillic
# beyond dropping it, made "hor" of it and called the file a different film.
#
# Only the letters that are visually IDENTICAL, which is what makes the
# substitution safe to make before anything else: a title really written in
# Cyrillic folds to the same keys on both sides of every comparison whether
# these are applied or not, so nothing that matched stops matching, while a
# Latin title with one letter from the wrong keyboard starts.
# https://www.unicode.org/reports/tr39/
CONFUSABLES = str.maketrans({
    # Cyrillic capitals
    "А": "A", "В": "B", "Е": "E", "К": "K",
    "М": "M", "Н": "H", "О": "O", "Р": "P",
    "С": "C", "Т": "T", "У": "Y", "Х": "X",
    "Ѕ": "S", "І": "I", "Ј": "J", "Ԛ": "Q",
    "Ԝ": "W",
    # Cyrillic smalls
    "а": "a", "е": "e", "о": "o", "р": "p",
    "с": "c", "у": "y", "х": "x", "ѕ": "s",
    "і": "i", "ј": "j", "ԛ": "q", "ԝ": "w",
    # Greek capitals
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z",
    "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
    "Υ": "Y", "Χ": "X",
    # Greek smalls
    "ο": "o", "ν": "v",
})

# The symbols that are WORDS, spelled out before the fold takes them away. "&"
# and "and" are one title written twice, and the fold's own rule - every
# non-alphanumeric run becomes a space - would leave "Jonas Greta" against
# "Jonas and Greta" and no way back.
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
# and kept, never one or the other: "The Shape" and "Shape" are matched either
# way round, and so is a catalogue's "Beacon, The".
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

# The same label written INTO a title rather than in front of it: a library's
# "Sunstriker the Movie - Ember" against a catalogue's "Sunstriker: Ember".
# It is the filler above in another position, and it says the same nothing.
#
# A much shorter list than the filler, and deliberately. A leading run can be
# cut on sight - whatever stands in front of the title is not the title - while
# an interior word has title on both sides of it and might BE the title: a
# "Special" or a "Feature" in the middle of a name is as likely to be a word of
# it as a label on it. These four are not: nobody writes "the Movie" into the
# middle of a name meaning anything by it.
INTERJECTED_WORDS = frozenset(("movie", "film", "themovie", "thefilm"))

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
# would be the same rule run backwards, and it cannot be: "Rafael" would
# answer to "Rafal" and "Aeon Vale" to "Aon Vale". The accented spelling is
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
    """The folded words of a title, with the symbols that are words spelled
    out."""
    text = title
    for symbol, word in SYMBOL_WORDS:
        text = text.replace(symbol, word)
    return normalize_title(text).split()


def _one_conjunction(words: list[str]) -> list[str]:
    """``words`` with every conjunction read as the same one.

    A reading and not part of the fold, because two of these words are numerals
    as well: "Falkenauge I (1963)" has its "I" in a conjunction's place and is not
    a conjunction at all - read as one it folds to "falkenauge and 1963", and the
    numeral it really is never gets read at all. Widening keeps both meanings.
    """
    return [CONJUNCTIONS.get(word, word) if 0 < index < len(words) - 1 else word
            for index, word in enumerate(words)]


def _without_the_first(words: list[str]) -> list[str]:
    """``words`` without a trailing "1", while a title remains.

    The first film of a series is numbered three ways and meant identically:
    "Falkenauge I", "Falkenauge 1" and plain "Falkenauge" are one film, and a library
    and a catalogue rarely agree on which. Only ONE of them - a trailing "2" is
    the sequel, and dropping it would file the sequel under the original.

    Runs after the numerals are read, so the roman "I" has already become the
    "1" this looks for.
    """
    if len(words) > 1 and words[-1] == "1":
        return words[:-1]
    return words


def _without_article(words: list[str]) -> list[str]:
    """``words`` with a leading or trailing article taken off, or ``words``.

    Both ends, because the two conventions for the same title put it at
    opposite ones: "The Beacon" on disk, "Beacon, The" in a catalogue that
    sorts by the first real word.
    """
    if len(words) > 1 and words[0] in ARTICLES:
        return words[1:]
    if len(words) > 1 and words[-1] in ARTICLES:
        return words[:-1]
    return words


def _without_articles(words: list[str]) -> list[str]:
    """``words`` with EVERY article taken out, while two words are left.

    An article does not only go missing from the front. "The Keeper of the Keys"
    is written "Keeper of the Keys", "Keeper of Keys" and "The Keeper of Keys" by
    three different hands, and taking all of them out of both sides is what
    makes those one title however many went missing from either.

    The two-word floor is there to keep RECALL, not to keep the module honest -
    a widening cannot make it name the wrong film, because a second candidate
    coming into view is answered by naming neither. What it can do is take a
    certain match away: "Bel Bel Ville" worn down to "Land" carries a key a real
    and different film already has, and a folder holding one of them would stop
    being nameable at all. So a title this reading would wear down to a single
    word keeps its articles.

    The floor is this reading's own. The leading-article reading above still
    takes one article off a two-word title, because that IS the convention a
    catalogue varies by - which is also what makes "Los Robles" answer to
    "Robles".
    """
    kept = [word for word in words if word not in ARTICLES]
    return kept if len(kept) >= 2 else words


def _without_filler(words: list[str]) -> list[str]:
    """``words`` with its leading filler run taken off, while a title remains."""
    cut = 0
    while cut < len(words) - 1 and words[cut] in FILLER_WORDS:
        cut += 1
    return words[cut:]


def _without_the_interjection(words: list[str]) -> list[str]:
    """``words`` without a label that has title on BOTH sides of it.

    "Sunstriker the Movie - Ember and the Clockwork Wonder" is the film a
    catalogue holds as "Sunstriker: Ember and the Clockwork Wonder", and the
    only thing between them is a label somebody wrote into the middle of the
    name. Both sides, for the same reason the inner number is dropped on both
    sides: what surrounds the label still says which film this is.

    The article in front of the label is not this reading's business.
    :func:`_without_articles` already has one that takes every article out, and
    the two widenings meet - so "the movie" and "movie" reduce alike without
    this one having to know that "the" was part of the label.
    """
    kept = [word for index, word in enumerate(words)
            if not (0 < index < len(words) - 1 and word in INTERJECTED_WORDS)]
    return kept if len(kept) > 1 else words


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


def _without_the_inner_number(words: list[str]) -> list[str]:
    """``words`` without a number that has title on BOTH sides of it.

    "Steel Halo 2: Silence" is written "Steel Halo Silence" by a library that
    left the number out, and the two are one film - because everything around
    the number still says which film it is. That is what makes the number
    droppable here and not at the end: "Falkenauge 2" reduced to "Falkenauge"
    would be the sequel wearing the original's name, and there is
    nothing else in it to say otherwise.

    Runs after the numerals are read, so a roman "II" in the middle of a title
    is dropped as readily as an arabic one.
    """
    kept = [word for index, word in enumerate(words)
            if not (0 < index < len(words) - 1 and _an_ordinal(word))]
    return kept if len(kept) > 1 else words


def _an_ordinal(word: str) -> bool:
    """Whether a number is a film's place in its series rather than a year.

    A four-digit 19xx or 20xx in the middle of a name is the year, and the year
    is the one thing besides the title that says which film this is - dropping
    it would widen a name onto every other year's.
    """
    return word.isdigit() and not (len(word) == 4 and word[0] in "12")


def _without_the_possessive(words: list[str]) -> list[str]:
    """``words`` without the "s" an apostrophe left standing on its own.

    A series' name is written with the possessive and without it - "Wilder and
    Bright's Watch This" against a folder called "Wilder and Bright Watch This"
    - and the fold turns the apostrophe into a space, so the "s" survives as a
    word of its own and the two titles differ by it.

    Only a bare "s", which is a thing the fold makes and not a thing anybody
    writes: a title with a real word "s" in it does not exist, and one that did
    would only gain a key.
    """
    kept = [word for word in words if word != "s"]
    return kept if kept and kept != words else words


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
    title written "N.E.S.T.", "N E S T" or "NEST" folds to one squeezed key, and
    so do "Iron-Wolf" and "Iron Wolf", "A Cat's Tale" and "A Cats Tale", and
    every dash, comma, point and exclamation mark between two words.
    """
    forms = [_tokens(title)]
    written = _tokens(_spelled_out(title))
    if written != forms[0]:
        forms.append(written)
    for reading in (_one_conjunction, _without_article, _without_articles,
                    _without_filler, _without_the_interjection,
                    _without_the_possessive,
                    _arabic_numerals, _without_the_first,
                    _without_the_inner_number):
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


# --- the one letter somebody got wrong ---------------------------------------

# How short a title may be and still have a typo read out of it. One letter in
# three is most of a short name, and the short names are where the accidents
# are: "Ash" and "Ashe", "Orb" and "Orbs", "Kite" and "Kites". Six characters
# is the floor, counted on the fold, so the spaces count and the punctuation
# does not.
MIN_TYPO_LENGTH = 6


def one_typo_apart(one: str, other: str) -> bool:
    """Whether two written titles are one title with a single letter wrong.

    The last thing asked and never the first. Every reading in this module says
    what somebody MIGHT have written and cannot be wrong about it; this says
    they wrote it wrong, which is a guess, and a guess is only worth making
    where nothing else has answered.

    What it buys, besides the plain slip, is the two families of difference no
    table will ever hold: an americanism ("color" against "colour", "traveled"
    against "travelled") and a character that is the SHAPE of another and is
    not one - the ones :data:`CONFUSABLES` has not heard of yet.

    Three things make it safe enough to ask at all:

    * the edit is a LETTER. A digit is never a slip - "Falkenauge 2" and
      "Falkenauge 3" are one letter apart and two films, and so are a film's
      year and the year before it. This is also why the year needs no rule of
      its own: it is digits, so nothing in it can be a typo;
    * neither title is shorter than :data:`MIN_TYPO_LENGTH`;
    * one edit exactly, and the titles are not already the same - a title that
      matches has been matched, and this was not asked.
    """
    return _one_letter_apart(normalize_title(one), normalize_title(other))


def _one_letter_apart(one: str, other: str) -> bool:
    """The comparison itself, over two titles already folded.

    Worked out from the ends rather than with an edit-distance table: what is
    left once the common head and the common tail are taken off the two titles
    IS the edit, and a single substitution leaves one letter on each side while
    a single insertion or deletion leaves one letter on one side and nothing on
    the other. Anything else is two edits or more, and is not asked about
    further.
    """
    if one == other or abs(len(one) - len(other)) > 1:
        return False
    if min(len(one), len(other)) < MIN_TYPO_LENGTH:
        return False
    shortest = min(len(one), len(other))
    head = 0
    while head < shortest and one[head] == other[head]:
        head += 1
    tail = 0
    while tail < shortest - head and one[-1 - tail] == other[-1 - tail]:
        tail += 1
    edit = one[head:len(one) - tail] + other[head:len(other) - tail]
    return len(edit) == (2 if len(one) == len(other) else 1) and edit.isalpha()


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
                     _uninterjected(stripped),
                     _arabic_spelling(stripped), _unnumbered(stripped),
                     normalize_title(title)):
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


def leading_segment(title: str) -> tuple:
    """``title`` as (what stands before its FIRST " - " or ": ", what stands
    after it), or ("", "") when it has no such break.

    The other end of :func:`_last_segment`, and for a caller that needs to know
    what the prefix SAID rather than only that there was one: what is worth
    dropping off the front of a title is the franchise, the artist or the
    collection somebody repeated, and the only thing that can say a prefix is
    one of those is something outside the title agreeing.
    """
    match = _SEGMENT.search(title)
    if not match:
        return "", ""
    return title[:match.start()].strip(), title[match.end():].strip()


def _last_segment(title: str) -> str:
    """What stands after the LAST " - " or ": " in ``title``.

    The franchise someone repeated on every film in it - "Agent Ward - Blackfeather",
    "Nordwind: Der Sturm" - is not on the catalogue's side of the name, and
    what is left once every such prefix is gone is the film's own title.

    Empty when the title has no such break, and empty when the break is the only
    thing that made it one: a tail this cuts to nothing has nothing to ask
    about.
    """
    tail = _SEGMENT.split(title)[-1].strip()
    return tail if tail and tail != title.strip() else ""


def _uninterjected(title: str) -> str:
    """``title`` without the "the Movie" somebody wrote into the middle of it,
    for a catalogue that holds the film without it.

    "" when the title carries no such label, which is what keeps this from
    being a query: a spelling is only worth asking about where the thing it
    corrects was there to correct.
    """
    words = title.split()
    kept: list[str] = []
    for index, word in enumerate(words):
        if 0 < index < len(words) - 1 \
                and normalize_title(word) in INTERJECTED_WORDS:
            if kept and normalize_title(kept[-1]) in ARTICLES:
                kept.pop()
            continue
        kept.append(word)
    return " ".join(kept) if kept != words and len(kept) > 1 else ""


def _unnumbered(title: str) -> str:
    """``title`` without a trailing "I" or "1", for a catalogue that holds the
    first of a series under its bare name."""
    words = title.split()
    if len(words) > 1 and shell_lower(words[-1]) in ("i", "1"):
        return " ".join(words[:-1])
    return ""


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
