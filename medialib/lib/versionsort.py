"""Version sort - the ordering `sort -V` produces on this machine.

Read that qualifier literally. `sort` here is **uutils coreutils**, not GNU, and
the two do not agree: GNU's `filevercmp` runs a second version comparison over the
whole names when the suffix-stripped stems tie, and uutils goes straight to byte
order, so GNU puts "1" before "01.mp3" and this machine does the reverse. The
scripts get whichever `sort` the host has installed, which means the order a
library is numbered in is a property of the host rather than of the code. Once
this rule lives here it stops being one.

The order that comes back decides which file becomes 01 and which becomes 02,
in every module that numbers a folder - so it is a shared rule rather than a
detail of one of them.

Version sort is not natural sort, and the differences are not academic. ``~``
sorts before everything, including the end of a string, so ``a~`` precedes ``a``.
A file suffix - a trailing run of ``.ext`` groups - is held back and compared only
after the rest, so ``foo.mp3`` and ``foo1.mp3`` order by ``foo`` against ``foo1``
rather than by the whole name. Non-alphanumerics sort after letters. Leading zeros
are skipped, but a longer digit run still wins.

The algorithm is gnulib's ``filevercmp`` with that one tie-break difference, and
it is checked against the real thing rather than against a reading of it:
the tests pipe a generated corpus through this and the installed ``sort -V``
and compare name by name.
"""

import re

__all__ = ["version_key", "filevercmp", "version_sorted"]

# A file suffix as filevercmp defines it: a run of ".ext" groups at the end, where
# each group starts with a letter or "~" and continues with letters, digits or "~".
# ".mp3" qualifies; ".3" does not, so "foo.3" has no suffix at all.
_SUFFIX = re.compile(r"(\.[A-Za-z~][A-Za-z0-9~]*)*$")


def _stem(text: str) -> str:
    """What precedes the file suffixes, or the whole string when that is empty.

    The pattern ends in ``*$``, so it matches - possibly empty - at the end of
    anything; the fallback is for a reader, not for a case that happens.
    """
    found = _SUFFIX.search(text)
    return (text[: found.start()] if found else text) or text


def _order(char: str) -> int:
    """filevercmp's collating value for one character outside a digit run."""
    if char.isascii() and char.isdigit():
        return 0
    if char.isascii() and char.isalpha():
        return ord(char)
    if char == "~":
        return -1
    return ord(char) + 256


def _verrevcmp(s1: str, s2: str) -> int:
    """The core comparison, run over the two strings with their suffixes removed."""
    len1, len2 = len(s1), len(s2)
    pos1 = pos2 = 0
    while pos1 < len1 or pos2 < len2:
        first_diff = 0
        # the non-digit stretch, compared by collating value
        while (pos1 < len1 and not s1[pos1].isdigit()) or (pos2 < len2 and not s2[pos2].isdigit()):
            c1 = 0 if pos1 == len1 else _order(s1[pos1])
            c2 = 0 if pos2 == len2 else _order(s2[pos2])
            if c1 != c2:
                return c1 - c2
            pos1 += 1
            pos2 += 1
        # leading zeros carry no value
        while pos1 < len1 and s1[pos1] == "0":
            pos1 += 1
        while pos2 < len2 and s2[pos2] == "0":
            pos2 += 1
        # the digit runs: the first differing digit decides, but only once both
        # runs have ended - a longer run is a bigger number whatever its digits
        while pos1 < len1 and s1[pos1].isdigit() and pos2 < len2 and s2[pos2].isdigit():
            if not first_diff:
                first_diff = ord(s1[pos1]) - ord(s2[pos2])
            pos1 += 1
            pos2 += 1
        if pos1 < len1 and s1[pos1].isdigit():
            return 1
        if pos2 < len2 and s2[pos2].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def filevercmp(a: str, b: str) -> int:
    """Negative, zero or positive as ``a`` sorts before, with, or after ``b``."""
    if a == b:
        return 0
    if not a or not b:
        return (0 if not b else 1) - (0 if not a else 1)

    # "." sorts first, then "..", then other names beginning with a dot, then
    # everything else. Only the leading run of dots is special; what follows is
    # compared normally.
    a_dot, b_dot = a.startswith("."), b.startswith(".")
    if a_dot != b_dot:
        return -1 if a_dot else 1
    if a_dot:
        for special in (".", ".."):
            if a == special or b == special:
                if a == b:
                    return 0
                return -1 if a == special else 1
        a, b = a[1:], b[1:]

    # Hold the suffixes back: they are compared only if what precedes them ties.
    a_stem = _stem(a)
    b_stem = _stem(b)
    result = _verrevcmp(a_stem, b_stem)
    if result:
        return result
    # Tied once the suffixes are set aside - "1" against "01.mp3", say. The order
    # is then the plain byte order of the whole names, which is what makes the
    # sort total. gnulib runs a second version comparison over the full strings
    # here and only then falls back to bytes; the sort on this machine does not,
    # and the sort on this machine is what a run gets.
    return (a > b) - (a < b)


# --- the same order as a key --------------------------------------------------
#
# ``filevercmp`` as a sort comparator costs a regex search per side and a Python
# loop over both strings - the whole PATH, shared directory and all - for every
# one of the n log n comparisons. The key below does that work once per name and
# leaves the comparisons to tuple comparison in C. ``filevercmp`` stays the
# definition, and the key is held to it by the tests.

# A digit run, ASCII only: ``\d`` would also take the Unicode digits, which are
# exactly the strings the key does not handle (see ``_fast_key``).
_DIGIT_RUN = re.compile(r"([0-9]+)")

# ``_order`` for every ASCII non-digit; anything else is ``ord + 256``.
_ASCII_ORDER = {chr(code): _order(chr(code)) for code in range(128)
                if not chr(code).isdigit()}

# What the end of a string compares as, past its last non-digit stretch: an
# empty digit run, then a non-digit stretch that ends at once. Appended to every
# key so that a key which runs out is not simply "smaller" - the end of a name
# sorts AFTER "~", and a bare tuple prefix would say otherwise.
_END = (0, "", 0)


def _fast_key(text: str):
    """A tuple that orders as ``filevercmp`` does, or None where it cannot.

    The stem becomes alternating stretches: a non-digit stretch as its collating
    values closed by 0 (what the end of the stretch compares as), a digit run as
    its length and digits once the leading zeros are gone. Trailing zeros that
    end the stem are dropped, since ``_verrevcmp`` skips them to nothing.

    None for a stem holding a non-ASCII digit, "³" or "٣" say. ``_verrevcmp``
    walks those as digits but collates them as ``ord + 256`` where they meet a
    letter, which no fixed tuple reproduces; those names take the comparator.
    """
    # filevercmp's own special cases, as a leading rank. Its empty-string branch
    # puts "" after everything, so that is what the rank says too.
    if not text:
        return (5,)
    if text[0] == ".":
        if text == ".":
            return (1,)
        if text == "..":
            return (2,)
        rank, text = 3, text[1:]
    else:
        rank = 4

    stem = _stem(text)
    if not stem.isascii() and any(
            char.isdigit() and not char.isascii() for char in stem):
        return None

    parts = _DIGIT_RUN.split(stem)
    if len(parts) > 1 and not parts[-1] and not parts[-2].lstrip("0"):
        del parts[-2:]
    flat = []
    for index, part in enumerate(parts):
        if index % 2:
            digits = part.lstrip("0")
            flat.append(len(digits))
            flat.append(digits)
        elif part.isascii():
            flat.extend(map(_ASCII_ORDER.__getitem__, part))
            flat.append(0)
        else:
            flat.extend(_ASCII_ORDER.get(char) or ord(char) + 256 for char in part)
            flat.append(0)
    flat.extend(_END)
    # The byte order of the whole name settles a tie, as in filevercmp.
    return (rank, tuple(flat), text)


class _VersionKey:
    """``functools.cmp_to_key(filevercmp)``, with the key precomputed.

    Two names with a fast key compare as tuples; a pair where either has none
    falls back to ``filevercmp`` itself, so the order is the same either way.
    """

    __slots__ = ("text", "key")
    __hash__ = None

    def __init__(self, text: str):
        self.text = text
        self.key = _fast_key(text)

    def _cmp(self, other) -> int:
        if self.key is not None and other.key is not None:
            return (self.key > other.key) - (self.key < other.key)
        return filevercmp(self.text, other.text)

    def __lt__(self, other):
        if self.key is not None and other.key is not None:
            return self.key < other.key
        return filevercmp(self.text, other.text) < 0

    def __gt__(self, other):
        return self._cmp(other) > 0

    def __le__(self, other):
        return self._cmp(other) <= 0

    def __ge__(self, other):
        return self._cmp(other) >= 0

    def __eq__(self, other):
        return self._cmp(other) == 0


version_key = _VersionKey


def version_sorted(names):
    """``names`` in the order ``sort -V`` would put them."""
    return sorted(names, key=version_key)
