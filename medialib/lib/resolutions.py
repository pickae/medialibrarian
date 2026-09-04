"""The video resolution ladder: what a coded pixel size is called.

"How much of this library is 2160p", "how many chunks does a frame this size need"
and "scale everything down to at most 1080p" are three questions about one ladder,
so the ladder is one table and everything else reads it - including the SQL, which
is generated rather than written, so a report and a lookup cannot drift apart.

This is the SIZE a pixel count says. Its SHAPE is the other, independent reading of
the same number and lives in ``aspectratios``: a scope film and a vertical short
can both be 1080p, so neither table is derived from the other.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "SUB_TIER",
    "UNKNOWN_TIER",
    "TABLE",
    "tier_of",
    "tier_names",
    "spellings",
    "named",
    "ceiling",
    "capped",
    "tier_sql",
    "width_sql",
]


class Tier(NamedTuple):
    """One rung: the name, the 16:9 size at which it is reached, and its aliases."""

    name: str
    width: int
    height: int
    aliases: tuple[str, ...]


# Smallest first. To offer another tier, add a row: nothing here is spelled out
# anywhere else. The marketing names and the line counts are the same ladder read
# two ways, so both are accepted rather than one being declared correct.
TABLE: tuple[Tier, ...] = (
    Tier("720p", 1280, 720, ("HDready",)),
    Tier("1080p", 1920, 1080, ("fullHD", "FHD")),
    Tier("1440p", 2560, 1440, ("2K", "quadHD", "QHD")),
    Tier("2160p", 3840, 2160, ("4K", "UltraHD", "UHD", "ultraHD4K")),
    Tier("4320p", 7680, 4320, ("8K", "UltraHD8K")),
)

# Everything below the first row. A floor rather than a row because it is
# open-ended downwards and has no geometry of its own: a 720x576 broadcast and a
# 320x240 web video are both SD, so there is no size to scale one down TO.
SUB_TIER = "SD"

# A size that could not be read at all. Kept distinct from SD so nothing is
# silently counted as small merely for being unreadable.
UNKNOWN_TIER = "unknown"


def _dimension(value: object) -> int | None:
    """A dimension, or None when it is not a non-negative integer.

    bash tests ``^[0-9]+$`` against the argument as text, so "1920" counts and
    "1920.0", "-1" and "" do not. A dimension that fails counts as ABSENT rather
    than as an error, which is what lets a half-probed size still be classified by
    whichever axis was readable.
    """
    text = "" if value is None else str(value)
    return int(text) if text.isascii() and text.isdigit() else None


def tier_of(width: object, height: object) -> str:
    """The tier a coded size falls in.

    A size is IN a tier when it reaches that tier's width OR its height, and in
    the highest such tier. Either dimension counts because a frame that is not
    16:9 only fills one axis: an old 4:3 "4K" camera is ~2880x2160 and a
    cinemascope frame is ~3840x1600, and both do a 2160p tier's worth of work.
    """
    w, h = _dimension(width), _dimension(height)
    if w is None and h is None:
        return UNKNOWN_TIER
    result = SUB_TIER
    for tier in TABLE:
        if (w or 0) >= tier.width or (h or 0) >= tier.height:
            result = tier.name
    return result


def tier_names() -> list[str]:
    """The selectable tiers, smallest first.

    ``SUB_TIER`` is not among them: the open-ended floor can be an ANSWER but not
    a selection.
    """
    return [tier.name for tier in TABLE]


def spellings() -> str:
    """Every tier with the aliases it also answers to, for a caller's help text.

    Generated from the table, so a name that is accepted is a name that is offered.
    """
    return ", ".join(
        f"{tier.name} ({', '.join(tier.aliases)})" if tier.aliases else tier.name
        for tier in TABLE
    )


def named(wanted: str) -> str | None:
    """The canonical tier a name or alias means, or None if the table has no such
    name - which is how a caller validating a user's choice catches a typo.

    Matching ignores case, since nobody agrees on whether it is 4K or 4k.
    ``SUB_TIER`` resolves to itself: it is a real answer to "what tier is this
    file", just not a size anything can be scaled to.
    """
    lowered = wanted.lower()
    if lowered == SUB_TIER.lower():
        return SUB_TIER
    for tier in TABLE:
        if lowered == tier.name.lower():
            return tier.name
        if any(lowered == alias.lower() for alias in tier.aliases):
            return tier.name
    return None


def ceiling(tier_name: str) -> tuple[int, int] | None:
    """The largest 16:9 frame still inside the tier, by any of its spellings.

    None for a name that is not a tier WITH A SIZE - a typo, or the open-ended
    ``SUB_TIER`` floor - so a bad selection is caught at the call rather than
    silently scaling to nothing.
    """
    canonical = named(tier_name)
    if canonical is None:
        return None
    for tier in TABLE:
        if tier.name == canonical:
            return tier.width, tier.height
    return None


def capped(width: object, height: object, tier_name: str) -> tuple[object, object]:
    """The size scaled down just enough to fit the tier, keeping the aspect ratio.

    Only ever DOWN. A source already inside the ceiling comes back untouched,
    which is what makes this a cap rather than a target: asking for "at most
    1080p" must never blow a 720p source up, since the pixels do not exist and the
    file would only get bigger. An empty tier, a tier with no size of its own and
    an unreadable size all pass through unchanged, so a caller can hand its cap
    over unconditionally.

    Both results are rounded to even numbers, because the 10-bit 4:2:0 pixel
    formats these encoders use subsample chroma by two and reject an odd side.

    A size that is not scaled comes back exactly as it was given, spelling and all.
    """
    w, h = _dimension(width), _dimension(height)
    if not tier_name or w is None or h is None or w == 0 or h == 0:
        return width, height
    limits = ceiling(tier_name)
    if limits is None:
        return width, height

    limit_w, limit_h = limits
    factor = 1.0
    if w > limit_w:
        factor = limit_w / w
    if h > limit_h and limit_h / h < factor:
        factor = limit_h / h
    if factor >= 1:
        # The caller's own text, not the parsed number. bash hands the size to awk,
        # which prints an unused -v assignment exactly as it was given, so a size
        # that is not scaled comes back spelled as it arrived - "0720" stays
        # "0720". Unchanged means unchanged.
        return width, height
    return max(int(w * factor / 2 + 0.5) * 2, 2), max(int(h * factor / 2 + 0.5) * 2, 2)


# --- the same ladder, as SQL --------------------------------------------------
# A report classifies its rows in the database rather than here, so the ladder has
# to exist a second time as an expression - generated from the table above so it is
# the same ladder and not a copy. Written highest tier first, which is what turns
# "reaches this floor" into "reaches this one but not the next" with no upper bound
# written anywhere.


def tier_sql(width_expression: str, height_expression: str) -> str:
    """The ladder as a SQL CASE over two columns.

    A row with neither dimension is unknown; a missing single dimension is
    COALESCEd to 0 so the other still classifies it, exactly as ``tier_of`` does.
    """
    lines = [
        "CASE",
        f"            WHEN {width_expression} IS NULL AND {height_expression} IS NULL "
        f"THEN '{UNKNOWN_TIER}'",
    ]
    for tier in reversed(TABLE):
        lines.append(
            f"            WHEN COALESCE({width_expression}, 0) >= {tier.width} "
            f"OR COALESCE({height_expression}, 0) >= {tier.height} THEN '{tier.name}'"
        )
    lines.append(f"            ELSE '{SUB_TIER}'")
    lines.append("        END")
    return "\n".join(lines)


def width_sql(width_expression: str) -> str:
    """The same tiers over a width alone.

    That is what an image measured page by page has to be classified by: a comic
    page is portrait, so its height only says whether a page is a double spread
    and the width is its real resolution. The floors are the table's, so a "1080p"
    scan and a "1080p" film mean the same number of pixels across.
    """
    lines = [
        "CASE",
        f"            WHEN {width_expression} IS NULL THEN '{UNKNOWN_TIER}'",
    ]
    for tier in reversed(TABLE):
        lines.append(f"            WHEN {width_expression} >= {tier.width} THEN '{tier.name}'")
    lines.append(f"            ELSE '{SUB_TIER}'")
    lines.append("        END")
    return "\n".join(lines)
