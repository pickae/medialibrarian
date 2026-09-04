"""The census field primitives: the pieces every report row is built out of.

Nothing here decides what a file IS - that is the dispatcher's job - and
nothing here probes anything except the one call that asks the filesystem for a
size.

The coercions all say the same thing in different ways: a value that is not a
number becomes EMPTY, never zero. A bitrate of 0 and a bitrate nobody stated are
different facts about a file, and a spreadsheet that reads the second as the first
gets a wrong answer rather than a gap.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from fractions import Fraction

from medialib.lib.enums import shell_lower

DEFAULT_SEPARATOR = ","
TAB = "\t"

_WHOLE = re.compile(r"[0-9]+")
_DECIMAL = re.compile(r"[0-9]+(?:[.][0-9]+)?")
_RATIONAL = re.compile(r"([0-9]+)/([0-9]+)")

# What each report's row holds. Two columns are common to all four - the full path
# and the raw size in bytes - and everything after them is that type's own, because
# a duration means nothing for a comic and a page count means nothing for a film.
# The units are in the NAMES so a spreadsheet can sum a column without parsing it.
COLUMNS = {
    "audio": ("path", "sizeBytes", "durationSeconds", "bitrateBitsPerSecond",
              "channels", "codec", "chapters"),
    "video": ("path", "sizeBytes", "durationSeconds", "videoBitrateBitsPerSecond",
              "bitrateAdequacy", "resolution", "frameRateFps", "videoCodec",
              "container", "dynamicRange", "audioTracks", "firstAudioChannels",
              "firstAudioCodec", "firstAudioBitrateBitsPerSecond",
              "subtitleTracks", "chapters"),
    "books": ("path", "sizeBytes", "pages", "words", "characters"),
    "comics": ("path", "sizeBytes", "pages", "firstPageResolution", "container",
               "imageCodec"),
}



# --- what "%.3f" means in the shell ---------------------------------------------
# bash's printf reads its argument with strtold and formats it with long double, so
# the value it rounds is the nearest 80-bit float - 64 bits of mantissa - and not
# the nearest double Python would parse the same text into. On a value sitting on
# the boundary the two disagree, and in BOTH directions, so neither rounding the
# exact decimal nor rounding through a Python float reproduces the shell.
#
# So the 80-bit value is computed exactly, with fractions, and rounded from there.
# Exact rather than approximated because an approximation is the whole problem: the
# question is only ever asked about values where the last bit decides.

_LONG_DOUBLE_MANTISSA = 64
_HALF = Fraction(1, 2)


def _round_half_even(value: Fraction) -> int:
    whole = value.numerator // value.denominator
    remainder = value - whole
    if remainder > _HALF or (remainder == _HALF and whole % 2):
        whole += 1
    return whole


def _as_long_double(text: str) -> Fraction:
    """The exact value of the 80-bit float ``strtold`` gives this decimal string."""
    value = Fraction(text)
    if value == 0:
        return Fraction(0)
    # The exponent that leaves the mantissa in [2^63, 2^64), found from the bit
    # lengths and then corrected - the estimate can be one out either way.
    exponent = (value.numerator.bit_length() - value.denominator.bit_length()
                - _LONG_DOUBLE_MANTISSA)
    while value / Fraction(2) ** exponent >= 2 ** _LONG_DOUBLE_MANTISSA:
        exponent += 1
    while value / Fraction(2) ** exponent < 2 ** (_LONG_DOUBLE_MANTISSA - 1):
        exponent -= 1
    mantissa = _round_half_even(value / Fraction(2) ** exponent)
    return Fraction(mantissa) * Fraction(2) ** exponent


def three_decimals(text: str) -> str:
    """``printf '%.3f'`` of that decimal string, as the shell would print it.

    The rule is shared, not a census detail: anything the shell formats with
    ``%.3f`` - the census durations and the chapter probe lengths alike -
    rounds the value the way long double does, so the rounding lives here once.
    """
    thousandths = _round_half_even(_as_long_double(text) * 1000)
    return f"{thousandths // 1000}.{thousandths % 1000:03d}"


def printf_f0(text: str) -> str:
    """``printf '%.0f'`` of that decimal string, as the shell would print it.

    The same 80-bit parse as ``three_decimals`` - bash's printf reads its
    argument with ``strtold`` and formats with long double - rounded to zero
    places instead of three. A string the parse cannot read raises, the way the
    shell's ``printf`` errors and its caller's ``||`` default settles.
    """
    return str(_round_half_even(_as_long_double(text)))


def extension_in(extension: str, candidates: Iterable[str]) -> bool:
    """Is this suffix one of those, compared lower-case on both sides?

    Every suffix in the census is matched case-insensitively, so a ``.MP3`` and a
    ``.Mp3`` are one thing. Lower-cased the way the shell does it rather than the
    way Python does - the two disagree about exactly one codepoint, and a census
    that met it would file the same extension under two names.
    """
    folded = shell_lower(extension)
    return any(folded == shell_lower(candidate) for candidate in candidates)


def columns(report: str, separator: str = DEFAULT_SEPARATOR) -> str | None:
    """That report's header row, or None for a type there is no report for.

    The separator is a parameter because the header has to be built with the one
    the FILE uses when a reader is checking whether a first line is this header.
    """
    names = COLUMNS.get(report)
    if names is None:
        return None
    return join(names, separator)


class Joined(str):
    """A joined row that also remembers whether joining had to alter a field.

    A plain string with one attribute rather than a tuple, because every caller
    wants the line and only the run's ending wants the flag - the shell says the
    same thing with a global that the caller reads once at the end.
    """

    sanitised: bool = False


def join(fields: Iterable[str], separator: str = DEFAULT_SEPARATOR) -> Joined:
    """The fields as one separated line.

    The two formats are escaped differently because they ARE different formats:

    * comma - RFC 4180. A field holding a quote, the separator or a line break is
      wrapped in quotes and its own quotes doubled. Lossless, which matters when
      the first column is a path.
    * tab - no quoting at all, because TSV has no agreed quoting convention and a
      reader that meets a quote keeps it as data. The three characters that would
      break the row become spaces instead. That is lossy, so the row says so.
    """
    out = []
    sanitised = False
    for field in fields:
        if separator == TAB:
            replaced = field.replace("\t", " ").replace("\n", " ").replace("\r", " ")
            if replaced != field:
                sanitised = True
            field = replaced
        elif ('"' in field or separator in field
                or "\n" in field or "\r" in field):
            field = '"' + field.replace('"', '""') + '"'
        out.append(field)
    # Joined on the count of fields, not on whether the row is still empty: an
    # EMPTY first field is indistinguishable from "nothing added yet", and treating
    # the two alike drops the separator that field is entitled to.
    line = Joined(separator.join(out))
    line.sanitised = sanitised
    return line


def to_int(value: str) -> str:
    """A whole number kept as it stands, anything else emptied."""
    return value if _WHOLE.fullmatch(value) else ""


def to_chapters(value: str) -> str:
    """The same, except that a file declaring NO chapters counts as one.

    A file without chapter marks is not a file with nothing in it - it is one
    unbroken chapter, which is what a player shows. Recorded as 0 the column is
    unusable in exactly the place a census is for: a thousand unmarked audiobooks
    would total zero parts, and a mean chapter length would divide by it.

    A count that is not a number at all still empties: "one chapter" is an
    assertion about a file that was read, and unknown is not.
    """
    if not _WHOLE.fullmatch(value):
        return ""
    # The shell tests 10#$value and leaves the TEXT alone, so "007" stays "007"
    # while "000" becomes 1. Reading it as decimal is what stops a leading zero
    # being taken for octal, not a decision to normalise the column.
    return value if int(value, 10) > 0 else "1"


def to_seconds(value: str) -> str:
    """A duration as seconds with millisecond precision, anything else emptied.

    ffprobe reports six decimals of which three are noise; three is what every
    player and every chapter format uses.
    """
    if not _DECIMAL.fullmatch(value):
        return ""
    return three_decimals(value)


def to_frame_rate(value: str) -> str:
    """A frame rate as frames per second with three decimals, else emptied.

    ffprobe states a rate as an exact RATIONAL - "24000/1001" - because that is
    what the containers store and what the NTSC rates genuinely are: 23.976 is a
    rounding of 24000/1001, not the other way round. A column meant to be compared
    against a threshold cannot hold a fraction, so the division happens here.

    Done in integers, the way the shell does it: the numerator is scaled by a
    thousand and divided with rounding, which is exact for every rate a container
    can state. A rate of 0/0 - what ffprobe prints for a stream whose rate it could
    not work out - is not a number and empties like every other unknown.
    """
    rational = _RATIONAL.fullmatch(value)
    if rational:
        numerator = int(rational.group(1))
        denominator = int(rational.group(2))
        if denominator == 0 or numerator == 0:
            return ""
        # Integer division of the denominator too, which is what rounds the last
        # digit: an odd denominator rounds down where a half would round up.
        scaled = (numerator * 1000 + denominator // 2) // denominator
        return f"{scaled // 1000}.{scaled % 1000:03d}"
    if _DECIMAL.fullmatch(value):
        rounded = three_decimals(value)
        # Only the decimal spelling is checked for zero; the rational one has
        # already refused a zero numerator above.
        return "" if rounded == "0.000" else rounded
    return ""


def file_size(path: str) -> str:
    """The raw size in bytes, or "" for a file that cannot be stat'd.

    Raw rather than human-readable on purpose: this column is meant to be summed.
    """
    try:
        return str(os.stat(path).st_size)
    except OSError:
        return ""
