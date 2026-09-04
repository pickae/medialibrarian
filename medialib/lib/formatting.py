"""Rendering numbers the way a person reads them.

Four formatters - a duration, a clock, a ratio and a byte count - and each one
encodes a judgement about how much precision a number deserves.
"""

from __future__ import annotations

import math
import re

__all__ = ["awk_number", "awk_looks_numeric", "awk_gt", "fmt_hms",
           "fmt_clock", "fmt_ratio", "fmt_bytes"]

# How awk reads a string as a number: optional space, optional sign, digits with an
# optional fraction and exponent, and everything from the first character that does
# not fit is ignored - so "12abc" is 12 and "abc" is 0. Notably NOT hexadecimal
# ("0x10" is 0) and notably not octal either: "0720" is 720 here, while the same
# text inside bash's (( )) is 464. Two coercions, one shell, and the difference
# has been a bug here before.
_AWK_NUMBER = re.compile(r"[ \t\n]*[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")


def awk_number(value: object) -> float:
    """``value`` as awk's ``x + 0`` would read it, and always a finite number.

    The finiteness is a deliberate departure. Text like "1e400" overflows a double,
    and awk carries the infinity into the formatters, where gawk renders a duration
    as the genuinely remarkable "+inf:-nan:-nan". Carrying it here would mean a
    formatter that raises - ``int(inf)`` is not a number - and **a formatter that
    crashes is a worse failure than one that prints nonsense**: it takes down a run
    over a log line. So an overflow reads as zero, which is what this function
    already does with the words "inf" and "nan", and the rule stays statable: this
    coercion produces finite numbers.
    """
    match = _AWK_NUMBER.match("" if value is None else str(value))
    if match is None:
        return 0.0
    try:
        number = float(match.group(0))
    except ValueError:
        return 0.0
    return number if math.isfinite(number) else 0.0


# What awk reads NUMERICALLY when it has a choice: a value that came in from a -v
# assignment, an input field or getline is a "strnum", and awk compares it as a
# number only when the WHOLE of it is a number - surrounding blanks allowed, and
# nothing else. This is a stricter pattern than _AWK_NUMBER above, deliberately:
# that one says what "x + 0" makes of a string, this one says whether "x > y" is
# arithmetic at all.
_AWK_STRNUM = re.compile(
    r"^[ \t\n]*[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?[ \t\n]*$")


def awk_looks_numeric(value: object) -> bool:
    """True when awk would read this value as a number rather than as text."""
    return _AWK_STRNUM.match("" if value is None else str(value)) is not None


def _awk_string(value: object) -> str:
    """A value as awk spells it in a string comparison: text as itself, and a
    number through CONVFMT ("%.6g") - except an integral one, which awk writes as
    an integer whatever CONVFMT says."""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        if float(value).is_integer():
            return "%d" % int(value)
        return "%.6g" % value
    return "" if value is None else str(value)


def awk_gt(left: object, right: object) -> bool:
    """``left > right`` as awk compares them - a ``str`` side standing for a
    strnum (a -v assignment or an input field) and an ``int``/``float`` side for a
    numeric constant in the program text.

    awk compares numerically only when BOTH sides read as numbers; otherwise it
    compares them as STRINGS, byte by byte. That is not a detail worth glossing:
    a duration ffprobe could not read comes back as "N/A", and ``"N/A" > "60"``
    holds because "N" sorts after "6" - so the shell treats a file it could not
    read as an over-long one, where a port that coerced both sides to numbers
    would treat it as a file of no length at all and take a completely different
    branch. The string comparison is byte order, which is what the callers run
    under (their seam pins LC_ALL=C) and what Python's own ``>`` on str gives for
    the ASCII these values are.
    """
    numeric = ((isinstance(left, (int, float)) or awk_looks_numeric(left))
               and (isinstance(right, (int, float))
                    or awk_looks_numeric(right)))
    if numeric:
        return awk_number(left) > awk_number(right)
    return _awk_string(left) > _awk_string(right)


def _seconds(value: object, elide_hours: bool) -> str:
    total = int(awk_number(value) + 0.5)
    if total < 0:
        total = 0
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if elide_hours and hours == 0:
        return f"{minutes}:{seconds:02d}"
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def fmt_hms(value: object) -> str:
    """A duration as h:mm:ss, hours always shown."""
    return _seconds(value, False)


def fmt_clock(value: object) -> str:
    """A duration as m:ss, growing an hours field only when there is one."""
    return _seconds(value, True)


def fmt_ratio(value: object) -> str:
    """A real-time speed-up, at a precision that suits its size.

    Two decimals below 10, one above. The same figure is reported for audio and
    video, whose scales are three orders of magnitude apart - an Opus run lands
    around 60x, a software AV1 encode around 0.3x - and no single precision serves
    both: "60.00x" is noise and "0.3x" is coarse enough to hide a real improvement.
    """
    number = awk_number(value)
    return f"{number:.2f}" if number < 10 else f"{number:.1f}"


def fmt_bytes(value: object) -> str:
    """A byte count at a size someone can read - "1.42 GB", not 1524908032.

    DECIMAL units (1 GB = 1,000,000,000 bytes): what the census reports already
    say, what the storage is sold as, and therefore the only answer that does not
    make two parts of this repo disagree about how big the same library is.
    """
    number = awk_number(value)
    if number < 1000:
        return f"{int(number)} B"
    if number < 1_000_000:
        return f"{number / 1000:.1f} kB"
    if number < 1_000_000_000:
        return f"{number / 1_000_000:.1f} MB"
    return f"{number / 1_000_000_000:.2f} GB"
