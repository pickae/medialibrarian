"""The video aspect-ratio buckets: what SHAPE a coded pixel size is.

This is the other half of what "1920x1080" says. ``resolutions`` answers how much
detail is in it; this answers what shape it is. Two independent bucketings of one
raw fact, and neither is derived from the other - a scope film and a vertical
short can both be 1080p.

**Why the normed ratio is the display value.** A bucket has three names and they
are not interchangeable. "239:100" is exact and unreadable; "Scope" is what people
say and is ambiguous (four rows here have been called Scope at some point);
"2.39:1" is the one that can be COMPARED at a glance. The normed values are
written to a fixed two decimals so that sorting them AS TEXT puts them in shape
order, which is what a pivot table does to a dimension's values.

**Where one bucket ends and the next begins.** Nowhere, honestly: a shot is
composed at a ratio and then cropped, letterboxed and padded to an even number of
chroma-subsampled lines, so a 2.39:1 film arrives as 1920x800 (2.400), 1920x804
(2.388) or 2048x858 (2.387) and none of them is 2.39. A table of exact matches
would put almost every film in "other", which is not a bucketing. So a size gets
the NEAREST bucket, and the boundary between neighbours is the GEOMETRIC mean of
their ratios - geometric because an aspect ratio is a proportion, not a length:
1.33 is as far from 1.78 as 1.78 is from 2.39, both being a factor of about 1.34,
and only the geometric midpoint agrees with that.

There is no "other". The ladder is open-ended at both ends, so a 5:1 banner is
Polyvision and a 1:3 phone crop is vertical.
"""

from __future__ import annotations

import math
from typing import NamedTuple

__all__ = [
    "UNKNOWN",
    "TABLE",
    "bucket_of",
    "label_of",
    "bucket_names",
    "spellings",
    "named",
    "ratio_of",
    "normed_of",
    "names_of",
    "usage_of",
    "height_at",
    "boundaries",
    "bucket_sql",
]


class Bucket(NamedTuple):
    """One shape: its key, the exact integer ratio, the display value, and prose."""

    key: str
    numerator: int
    denominator: int
    normed: str
    names: str
    usage: str

    @property
    def value(self) -> float:
        """The exact ratio as a number. The boundaries are computed from THIS, so
        the rounded display value never decides which bucket a file is in."""
        return self.numerator / self.denominator

    @property
    def ratio(self) -> str:
        return f"{self.numerator}:{self.denominator}"


# Narrowest first. Two of the integer ratios are approximations that the industry
# itself only ever gives as a decimal (IMAX 15/70 and Cinerama); the pair is the
# nearest small one and is marked ~ in the names. To offer another bucket, add a
# row in ratio order - the boundaries, the SQL and the listings all come from here.
TABLE: tuple[Bucket, ...] = (
    Bucket("vertical", 9, 16, "0.56", "Vertical, portrait",
           "TikTok, Reels, vertical microdramas (ReelShort, DramaBox), Quibi Turnstyle"),
    Bucket("square", 1, 1, "1.00", "Square",
           "The Instagram era; Mommy (Dolan, 2014)"),
    Bucket("movietone", 6, 5, "1.19", "Movietone ratio, sound aperture",
           "1929-32 early talkies; The Lighthouse (2019)"),
    Bucket("fiveFour", 5, 4, "1.25", "5:4",
           "Some silents; old 1280x1024 monitors"),
    Bucket("fullscreen", 4, 3, "1.33", "Fullscreen, SDTV, Academy (loosely)",
           "Silent film, PAL/NTSC TV, DVD, most pre-1953 cinema"),
    Bucket("academy", 11, 8, "1.37", "Academy ratio (properly)",
           "The 1932-53 studio standard; Ida, First Reformed, parts of The Grand Budapest Hotel"),
    Bucket("imaxFilm", 10, 7, "1.43", "IMAX (15/70 film), ~10:7",
           "True IMAX sequences: The Dark Knight, Oppenheimer, Dune Part Two"),
    Bucket("vistaVision", 3, 2, "1.50", "VistaVision native, 35mm stills",
           "Wings of Desire (partly), photographic work"),
    Bucket("broadcastCompromise", 14, 9, "1.56", "14:9 compromise",
           "The broadcast fudge for 4:3 and 16:9 dual transmission"),
    Bucket("computerWidescreen", 16, 10, "1.60", "WSXGA, golden-ish",
           "Laptop panels, not film"),
    Bucket("europeanWidescreen", 5, 3, "1.66", "European widescreen, Super 16",
           "Continental and UK films of the 60s-80s, older Disney animation"),
    Bucket("widescreen", 16, 9, "1.78", "Widescreen, HDTV, HD/UHD",
           "The universal delivery container today"),
    Bucket("flat", 37, 20, "1.85", "Flat, Academy Flat",
           "The default US theatrical non-scope release"),
    Bucket("imaxDigital", 19, 10, "1.90", "IMAX Digital, IMAX 1.90",
           "Digital IMAX; the DCI full container is 1.896 (4096x2160)"),
    Bucket("univisium", 2, 1, "2.00", "Univisium (Storaro)",
           "The Netflix house favourite: Stranger Things, Jessica Jones, Nightcrawler"),
    Bucket("toddAo", 11, 5, "2.20", "Todd-AO, 70mm",
           "Lawrence of Arabia, 2001, West Side Story (65mm prints)"),
    Bucket("cinemaScope", 47, 20, "2.35", "CinemaScope, Scope, Panavision, Techniscope",
           "Anamorphic prints 1958-1970, and the label everyone still uses"),
    Bucket("scope", 239, 100, "2.39", "Scope, Panavision, 2.40",
           "The current anamorphic standard (DCI Scope is 2048x858)"),
    Bucket("originalCinemaScope", 51, 20, "2.55", "Original CinemaScope",
           "1953-57, The Robe; four magnetic tracks ate less of the frame"),
    Bucket("cinerama", 44, 17, "2.59", "Cinerama, ~44:17",
           "Three synchronised 35mm projectors: This Is Cinerama, How the West Was Won"),
    Bucket("ultraPanavision", 69, 25, "2.76", "Ultra Panavision 70, MGM Camera 65",
           "Ben-Hur (1959), Its a Mad Mad Mad Mad World, revived for The Hateful Eight (2015)"),
    Bucket("polyvision", 4, 1, "4.00", "Polyvision",
           "The triptych finale of Napoleon (Gance, 1927) - three projectors side by side"),
)

# A size that could not be read, or is not a size. Kept distinct from every real
# bucket so nothing is silently called square for being unreadable, and the same
# word the resolution ladder uses, because it means the same thing.
UNKNOWN = "unknown"


def _row(key: str) -> Bucket | None:
    for bucket in TABLE:
        if bucket.key == key:
            return bucket
    return None


def _dimension(value: object) -> int:
    """A dimension, or 0 for anything that is not a run of digits."""
    text = "" if value is None else str(value)
    return int(text) if text.isascii() and text.isdigit() else 0


def boundaries() -> list[tuple[str, float]]:
    """(display value, upper bound) for every bucket but the widest.

    The bound is the geometric mean of this bucket's ratio and the next one's: a
    size below it is still in this bucket. The widest needs none, which is what
    makes the ladder open-ended at the top.
    """
    return [
        (f"{low.normed}:1", math.sqrt(low.value * high.value))
        for low, high in zip(TABLE, TABLE[1:], strict=False)
    ]


def bucket_of(width: object, height: object) -> str:
    """The bucket a coded pixel size falls in.

    The CODED size, which is what a probe reports: a file with non-square pixels -
    a 720x576 PAL broadcast meant to be shown at 4:3, an anamorphic DVD - is
    bucketed by the shape it is STORED at, because the display aspect ratio is a
    separate flag most of a library does not carry. Everything mastered this
    century has square pixels; the handful where they differ are the price of not
    guessing.

    Both dimensions are needed for a ratio, so - unlike a resolution tier - there
    is no falling back on the axis that was readable.
    """
    w, h = _dimension(width), _dimension(height)
    if w == 0 or h == 0:
        return UNKNOWN
    ratio = w / h
    for low, high in zip(TABLE, TABLE[1:], strict=False):
        if ratio < math.sqrt(low.value * high.value):
            return low.key
    return TABLE[-1].key


def label_of(width: object, height: object) -> str:
    """The display value a size gets - "1.78:1", or "unknown". What a report holds."""
    key = bucket_of(width, height)
    # Every key bucket_of can answer with has a row, so normed_of has one.
    return UNKNOWN if key == UNKNOWN else (normed_of(key) or UNKNOWN)


def bucket_names() -> list[str]:
    """The bucket keys, narrowest first."""
    return [bucket.key for bucket in TABLE]


def normed_of(key: str) -> str | None:
    row = _row(key)
    return None if row is None else f"{row.normed}:1"


def ratio_of(key: str) -> str | None:
    row = _row(key)
    return None if row is None else row.ratio


def names_of(key: str) -> str | None:
    row = _row(key)
    return None if row is None else row.names


def usage_of(key: str) -> str | None:
    row = _row(key)
    return None if row is None else row.usage


def spellings() -> str:
    """Every bucket with its display value and the names it answers to.

    Generated from the table, so a name that is accepted is a name that is offered.
    """
    return ", ".join(
        f"{b.key} {b.normed}:1 ({b.ratio}, {b.names})" for b in TABLE
    )


def named(wanted: str) -> str | None:
    """The canonical bucket a name means - a key, an integer ratio ("16:9"), a
    display value ("1.78:1" or "1.78") or a marketing name ("Techniscope").

    Case is ignored and a leading "~" dropped, for the same reason the resolution
    ladder accepts "4k": nobody agrees on the spelling of a marketing name.

    The canonical spellings are matched in a FIRST pass and the marketing names
    only in a SECOND, because the marketing names are not unique - four rows have
    been called "Scope", and one of them is also the bucket whose key is "scope".
    Without two passes that key would resolve to a different row than the one it
    names, which would make the enum lie about itself. An ambiguous marketing name
    still goes somewhere: to the first row claiming it, the narrower one.
    """
    text = wanted.lower().strip()
    if not text:
        return None
    text = text.removeprefix("~")
    # A display value may be given with or without its ":1", but an integer ratio
    # may itself END in ":1" - 1:1, 2:1 and 4:1 are three rows here - so the
    # trimmed form is a SECOND thing to try, never a replacement.
    bare = text[:-2] if text.endswith(":1") else text

    for bucket in TABLE:
        if text in (bucket.key.lower(), bucket.ratio.lower(), bucket.normed):
            return bucket.key
        if bare == bucket.normed:
            return bucket.key
    for bucket in TABLE:
        for name in bucket.names.split(","):
            name = name.strip().lower().removeprefix("~")
            if name and text == name:
                return bucket.key
    return None


def height_at(key_or_name: str, width: object) -> int | None:
    """The height that bucket has at ``width`` pixels - "how tall is a 2.39:1 frame
    in a 1920 wide master" (804).

    Computed from the integer ratio, so it is the same number the bucketing is
    drawn around, and rounded rather than truncated so it is the nearest line and
    not always the one below.
    """
    key = named(key_or_name)
    if key is None:
        return None
    row = _row(key)
    w = _dimension(width)
    if row is None or w <= 0:
        return None
    return int(w * row.denominator / row.numerator + 0.5)


def bucket_sql(width_expression: str, height_expression: str) -> str:
    """The buckets as a SQL CASE over two columns, answering with the display value.

    Narrowest first, each boundary the geometric midpoint to the next, so the
    widest needs no upper bound. A row missing either dimension is unknown - a
    ratio needs both - and a zero or negative one is unknown too rather than a
    division by zero.
    """
    lines = [
        "CASE",
        f"            WHEN {width_expression} IS NULL OR {height_expression} IS NULL "
        f"OR {width_expression} <= 0 OR {height_expression} <= 0 THEN '{UNKNOWN}'",
    ]
    for label, boundary in boundaries():
        lines.append(
            f"            WHEN CAST({width_expression} AS DOUBLE) / {height_expression} "
            f"< {boundary:.6f} THEN '{label}'"
        )
    lines.append(f"            ELSE '{normed_of(TABLE[-1].key)}'")
    lines.append("        END")
    return "\n".join(lines)
