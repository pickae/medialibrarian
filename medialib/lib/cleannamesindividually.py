"""The per-name engine behind the folder and file cleaning.

One raw name goes in; a leading number or date prefix and the cleaned remainder
come back. The passes, in the order they run:

1. normalise the spacing and separators around a leading numbering prefix
   ("01 -Title" -> "01 Title", "( 01" -> "(01", "(1 2)" -> "(1-2");
2. replace and wipe the punctuation the encoders leave behind;
3. remove caller-supplied fragments, where word protection keeps a fragment
   from being torn out of the middle of a word;
4. split off the leading date/number prefix, guarded so a rule never bites
   into a word or into the middle of a number range;
5. collapse whitespace and trim the trailing separators.

Pure string in / string out: no files, no tools.
"""

import os
import re

from medialib import commands
from medialib.lib.affix import affix_char_class, align_affix_to_run_boundary
from medialib.lib.enums import BRACKET_CLOSE, BRACKET_OPEN, DATE_PREFIX_PATTERN

__all__ = [
    "clean_names_individually",
    "digits_not_greater",
    "fragments_file_for",
    "normalize_number_separator",
]

# The numbering separators: dash, point, underscore, comma, semicolon, and the
# multi-byte em dash. The en dash is NOT one of them - it is a letter to
# affix_char_class and passes through the cleaner untouched, exactly as the
# bash original treats it.
_SEP_CLASS = r"([-._,;]|\u2014)"

# The bracket treatment here: an OPENING bracket hugs the number to its RIGHT
# ("( 01" -> "(01"), a CLOSING bracket the number to its LEFT ("01 )" -> "01)"),
# and the opposite pairings keep their space. The WHICH lives in enums.py.
_OBRK_CLASS = r"[(\[{<]"
_CBRK_CLASS = r"[)\]}>]"

# token kind -> (regex fragment, wrap in a group, adds a group, is an anchor,
# forces the empty joiner). sep/sep2 count a capture group but are NOT anchors:
# the anchors are the first two of num/alpha/obrk/cbrk, in order of appearance.
_TOKEN = {
    "num": (r"([0-9])", False, True, True, False),
    # bash's [[:alpha:]] under C.UTF-8: a Unicode letter, which is \w minus
    # digits and underscore.
    "alpha": (r"([^\W\d_])", False, True, True, False),
    "obrk": (_OBRK_CLASS, True, True, True, True),
    "cbrk": (_CBRK_CLASS, True, True, True, True),
    "sp": (r"\s+", False, False, False, False),
    "sep": (_SEP_CLASS + "+", False, True, False, False),
    "sep2": (_SEP_CLASS + "{2,}", False, True, False, False),
}

_SPECS = (
    ("interior", ("num", "sp", "sep", "alpha")),
    ("interior", ("num", "sep2", "alpha")),
    ("once", ("obrk", "sp", "num")),
    ("once", ("obrk", "sep", "sp", "num")),
    ("once", ("num", "sp", "cbrk")),
    ("once", ("num", "sp", "sep", "cbrk")),
)


def _compile(tokens):
    """The ERE for a token sequence, with the anchor group numbers and joiner.

    A pattern has exactly two anchors; everything between them is squeezed out
    and the anchors re-joined. The joiner is a single space between a number and
    a letter, and nothing as soon as a bracket is involved, so the bracket hugs
    the number ("(01", "01)").
    """
    rx = ""
    groups = 0
    joiner = " "
    anchors = []
    for tok in tokens:
        fragment, wrap, counted, is_anchor, tightens = _TOKEN[tok]
        if wrap:
            fragment = f"({fragment})"
        rx += fragment
        if counted:
            groups += 1
            if is_anchor:
                anchors.append(groups)
        if tightens:
            joiner = ""
    return re.compile(rx), anchors[0], anchors[1], joiner


def _apply(tokens, s):
    """Rewrite s against the pattern until it no longer matches."""
    rx, first, second, joiner = _compile(tokens)
    while True:
        m = rx.search(s)
        if m is None:
            return s
        s = s[: m.start()] + m.group(first) + joiner + m.group(second) + s[m.end():]


def _reverse_interior(tokens):
    """Keep the two end anchors in place, reverse the fillers between them."""
    return (tokens[0],) + tuple(reversed(tokens[1:-1])) + (tokens[-1],)


def _digits_not_greater(a: str, b: str) -> bool:
    """Whether the digit string a is not a larger NUMBER than b.

    Compared as stripped strings, not with arithmetic: a leading zero must not
    read as octal and a long enough run must not overflow. "007" and "7" are
    the same number, so "5 out of 5" stays a range.
    """
    a = a.lstrip("0") or "0"
    b = b.lstrip("0") or "0"
    if len(a) != len(b):
        return len(a) < len(b)
    return not a > b


digits_not_greater = _digits_not_greater


def _brackets_correspond(open_ch: str, close_ch: str) -> bool:
    """Whether the closing bracket is the one paired with the opening one, by
    index into the central pairing - "(1 2]" is a mismatched pair and stays."""
    return BRACKET_CLOSE[BRACKET_OPEN.index(open_ch)] == close_ch


_RANGE_SPECS = (
    ("pair", rf"{_OBRK_CLASS}([0-9]+)\s+([0-9]+){_CBRK_CLASS}"),
    ("bare", r"([0-9]+)\s+-([0-9]+)"),
    ("bare", r"([0-9]+)-\s+([0-9]+)"),
)


def _tighten_number_range(s: str) -> str:
    """Turn a bracketed pair of non-descending numbers into a range.

    "(1 2)" is "parts 1 to 2", so the space becomes a dash and the token reads
    as one unit. The walk left to right STEPS OVER a candidate that fails a
    condition rather than retrying it, or the loop would never end. The bare
    shapes are the two lopsided spellings of an already dashed range ("1 -2",
    "1- 2"); a dash flanked by spaces on both sides is left to the " - " rule.
    """
    for mode, pattern in _RANGE_SPECS:
        rx = re.compile(pattern)
        head = ""
        tail = s
        while True:
            m = rx.search(tail)
            if m is None:
                break
            n1, n2 = m.group(1), m.group(2)
            tight = ""
            if _digits_not_greater(n1, n2):
                if mode == "pair":
                    open_ch = m.group(0)[0]
                    close_ch = m.group(0)[-1]
                    if _brackets_correspond(open_ch, close_ch):
                        tight = f"{open_ch}{n1}-{n2}{close_ch}"
                else:
                    tight = f"{n1}-{n2}"
            if tight:
                head += tail[: m.start()] + tight
            else:
                head += tail[: m.end()]
            tail = tail[m.end():]
        s = head + tail
    return s


def normalize_number_separator(s: str) -> str:
    """Squeeze the fillers around a leading numbering prefix, both orientations.

    Each base pattern runs forwards and then, for the interior ones, in its
    mirror; the direction-specific bracket patterns run once, because an
    opening bracket hugs to its right and a closing one to its left. The
    bracketed range tightening runs last, once the brackets are in place.
    """
    for mode, tokens in _SPECS:
        s = _apply(tokens, s)
        if mode != "once":
            mirror = _reverse_interior(tokens)
            if mirror != tokens:
                s = _apply(mirror, s)
    return _tighten_number_range(s)


# The punctuation passes, in the exact order the original applies them: the
# " - " replacement runs before the yt-dlp "｜" one, and the apostrophe is
# wiped before any later pass could see it.
_PUNCT = (
    (".", " "),
    (" - ", " "),
    ("_", " "),
    ("`", " "),
    ("'", ""),
    ('"', ""),
    ("\u201d", ""),
    ("\u201c", ""),
    ("\t", ""),
    ("\r", ""),
    ("\n", ""),
    ("\uff5c", " - "),
    ("\uff1a", " - "),
    # The other colon a Windows-safe renamer writes, beyond the original: "∶" is
    # not ASCII punctuation, so affix_char_class would call it a letter and a
    # shared "∶ Title" suffix would stop one character short of it.
    ("\u2236", " - "),
    ("\u29f8", "-"),
    ("#", "Ep."),
    ("\uff02", " "),
    ("\uff1f", " "),
    # the original also replaces the apostrophe with a space here; the wipe
    # above already took them all, so this is a faithful no-op.
    ("'", " "),
    ("\uff0a", " "),
    ("\u2022", " "),
    ("(, ", "("),
)


def _read_fragments(path: str) -> list[str]:
    """The fragments file as bash's mapfile reads it: lines, no trailing newlines."""
    with open(path, encoding="utf-8", errors="surrogateescape") as handle:
        lines = handle.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _remove_fragments(name: str, fragments_file: str) -> str:
    """Remove each fragment, case-insensitively, where word protection allows it.

    The occurrence is located on lower-cased copies of both sides and stripped
    from the original, which assumes the lowercasing keeps the fragment's length
    - true for the ASCII and accented Latin the names carry; U+0130 folds to a
    letter plus a mark in Python and to one letter in the shell. An occurrence
    is removed only if neither edge sits in the middle of a run of letters
    (including accented ones) or digits - the shared alignAffixToRunBoundary
    rule - otherwise it is stepped over, not mangled ("cat" out of
    "concatenate" would leave a stray word behind).
    """
    for raw in _read_fragments(fragments_file):
        fragment = raw[:-1] if raw.endswith("\r") else raw
        if not fragment or fragment.startswith("#"):
            continue
        lower_frag = fragment.lower()
        frag_len = len(fragment)
        from_ = 0
        while True:
            lower_name = name.lower()
            hit = lower_name.find(lower_frag, from_)
            if hit < 0:
                break
            occ = name[hit: hit + frag_len]
            safe = align_affix_to_run_boundary("prefix", occ, [name[hit:]]) == occ
            if safe:
                safe = align_affix_to_run_boundary("suffix", occ, [name[: hit + frag_len]]) == occ
            if safe:
                name = name[:hit] + name[hit + frag_len:]
                from_ = hit
            else:
                from_ = hit + frag_len
    return name


def _is_digit(ch: str) -> bool:
    """Whether bash's [0-9] matches - ASCII digits only, never Unicode forms."""
    return "0" <= ch <= "9"


_PREFIX_RULES = (
    (0, 8, 8, DATE_PREFIX_PATTERN),
    (0, 5, 5, r"^[0-9][0-9]-[0-9][0-9]$"),
    (0, 4, 4, r"^[0-9][0-9][0-9][0-9]$"),
    (0, 3, 3, r"^[0-9][0-9][0-9]$"),
    (0, 2, 2, r"^[0-9][0-9]$"),
    (1, 2, 4, r"^[0-9][0-9]$"),
    (0, 1, 1, r"^[0-9]$"),
)


def _surround_ok(name: str, off: int, length: int, strip: int) -> bool:
    """Whether every stripped position OUTSIDE the number is a delimiter.

    The offset-1 rule strips characters around the number, so those must be
    separators or brackets - never letters or digits, or the rule would collapse
    "a16z" down to "16". Positions past the string's end are fine.
    """
    for ci in range(strip):
        if off <= ci < off + length:
            continue
        ch = name[ci] if ci < len(name) else ""
        if ch and affix_char_class(ch) != "O":
            return False
    return True


def _range_join_follows(name: str, off: int, length: int, strip: int) -> bool:
    """Whether the rule would peel a number out of the middle of a range.

    A rule that strips a delimiter AFTER the number, with the next retained
    character still a digit, was joining two numbers of one range - taking
    "12" of "(12-15)" would strand the "15)" - so the rule stands aside.
    """
    return strip > off + length and strip < len(name) and _is_digit(name[strip])


def _extend_prefix(prefix: str, name: str) -> tuple[str, str]:
    """Extend the prefix over the whole digit run, and over dash-joined digits.

    A fixed-width rule must not stop in the middle of a longer run of digits,
    nor of a number range: a dash directly between two digit runs joins them,
    so the extension keeps going over "<dash><digits>" while that shape holds;
    a dash not followed by a digit is an ordinary separator and ends the prefix.
    """
    while True:
        while name and _is_digit(name[0]):
            prefix += name[0]
            name = name[1:]
        if len(name) >= 2 and name[0] == "-" and _is_digit(name[1]):
            prefix += "-"
            name = name[1:]
        else:
            return prefix, name


def _split_prefix(name: str) -> tuple[str, str]:
    """The leading date/number prefix, if any, split off from the name.

    The rules are ordered longest number first, first match wins.
    """
    prefix = ""
    for off, length, strip, pat in _PREFIX_RULES:
        candidate = name[off: off + length]
        if re.match(pat, candidate) is None:
            continue
        if not _surround_ok(name, off, length, strip):
            continue
        if _range_join_follows(name, off, length, strip):
            continue
        prefix = candidate
        name = name[strip:]
        if off == 0 and strip == length:
            prefix, name = _extend_prefix(prefix, name)
        break
    return prefix, name


def _settle(name: str) -> str:
    """Collapse whitespace, trim the ends, and strip the trailing separators.

    Dashes, dots, slashes and underscores go in that exact order - a separator
    only becomes trailing once the earlier kind is gone - then collapse and
    trim once more, and peel a leading "- " that nothing else would.
    """
    name = re.sub(r" {2,}", " ", name)
    name = name.strip()
    name = name.rstrip("-").rstrip(".").rstrip("/").rstrip("_")
    name = re.sub(r" {2,}", " ", name)
    name = name.strip()
    if name.startswith("- "):
        name = name[2:]
    return name


def clean_names_individually(raw_name: str, fragments_file: str | None = None) -> tuple[str, str]:
    """Clean one name; the (prefix, cleaned name) pair the callers reassemble.

    ``fragments_file`` is optional: an explicit path that is missing or empty
    is treated exactly like no file at all, so callers may always pass one.
    """
    name = normalize_number_separator(raw_name)
    for old, new in _PUNCT:
        name = name.replace(old, new)
    if fragments_file and os.path.isfile(fragments_file) and os.path.getsize(fragments_file) > 0:
        name = _remove_fragments(name, fragments_file)
    prefix, name = _split_prefix(name)
    return prefix, _settle(name)




def _usable(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def fragments_file_for(explicit: str | None = None) -> tuple[str, bool]:
    """Which fragments file a run uses, and whether it is usable.

    An explicit path someone typed is never quietly dropped: unusable means
    (no file, refused). The default is optional - absent or empty answers
    (no file, fine to go on).
    """
    if explicit:
        if _usable(explicit):
            return explicit, True
        return "", False
    default = _default_fragments()
    if _usable(default):
        return default, True
    return "", True


def _default_fragments() -> str:
    """`data/fragments.txt` beside the checkout, asked for the same way the
    podcast tables and the beets log are - so one variable moves all of them."""
    return os.path.join(commands.script_dir(), "data", "fragments.txt")