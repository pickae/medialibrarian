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

import functools
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


version_key = functools.cmp_to_key(filevercmp)


def version_sorted(names):
    """``names`` in the order ``sort -V`` would put them."""
    return sorted(names, key=version_key)
