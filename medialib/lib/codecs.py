"""The shared video-codec table: one list, read nine ways.

"Which codec is this file in", "is that the codec my encoder produces" and "how
old a way of compressing is it" are three questions about one list of codecs,
so the list lives here once and is answered here in Python and in SQL alike -
the adequacy model reads it per file, the census groups a whole library by it.

This is the IDENTITY of a codec - what ffprobe calls it, which family that name
belongs to, and which generation of compression that family is. What any of that
is WORTH in bitrate is a separate reading of the same list and lives beside it in
:mod:`medialib.lib.videobitrate`, keyed by the family names here: a codec's name does
not change when the adequacy model is retuned, and the tuning does not change
when ffmpeg learns a new spelling of an old codec.

The table is an ordered sequence, not a mapping: the lookups walk it in order
and the SQL emitter writes one WHEN per row in that order, so a spelling listed
twice would answer by its first row both the same way - and "oldest generation
first" is data the census prints, not something to re-derive.
"""

from __future__ import annotations

from medialib.lib.enums import shell_lower

__all__ = [
    "MPEG2_ERA",
    "MPEG4_ERA",
    "MODERN_ERA",
    "UNKNOWN",
    "TABLE",
    "families",
    "family_of",
    "era_of",
    "aliases_of",
    "family_sql",
    "era_sql",
    "encoder_codec",
]

# The three generations a family can belong to. An ERA here is a generation of
# COMPRESSION, not a release date: it says what kind of tools the codec has to
# work with, which is why the intra-only masters sit with MPEG-2 (a format that
# codes every frame on its own compresses like the era before inter prediction
# got good, whatever year it was published in).
MPEG2_ERA = "mpeg2Era"
MPEG4_ERA = "mpeg4Era"
MODERN_ERA = "modern"

# The answer for a codec the table does not list, or none at all from a file
# that could not be probed. One marker for both lookups, distinct from any real
# family, so nothing unreadable is silently counted as something known.
UNKNOWN = "unknown"

# One row per family, oldest generation first. Each row is (family, era,
# aliases): the family is the name this repo knows the codec by - what a lookup
# answers with and what the adequacy model keys its tuning on - and the aliases
# are the ffprobe spellings that mean it, matched case-insensitively because
# they arrive from ffprobe, from mediainfo and from people, and none of them
# agree on h265/HEVC/x265.
#
# The families are grouped by what they can DO rather than by who published them:
# "vc1" holds the WMV generations that are really the same coder, and "intra"
# holds the intra-only and lossless mastering formats, which have far more in
# common with each other than any of them has with a predictive codec.
#
# To recognise another codec, add its spelling to a row - or add a row if it is
# genuinely a new family. Nothing here is spelled out anywhere else.
TABLE = (
    ("mpeg2", MPEG2_ERA, ("mpeg2video", "mpeg1video")),
    ("intra", MPEG2_ERA, ("prores", "dnxhd", "mjpeg", "ffv1", "huffyuv", "rawvideo",
                          "dvvideo")),
    ("mpeg4", MPEG4_ERA, ("mpeg4", "msmpeg4v1", "msmpeg4v2", "msmpeg4v3", "h263",
                          "flv1", "rv40", "theora")),
    ("vc1", MODERN_ERA, ("vc1", "wmv1", "wmv2", "wmv3")),
    ("vp8", MODERN_ERA, ("vp8",)),
    ("h264", MODERN_ERA, ("h264", "avc", "avc1", "x264")),
    ("vp9", MODERN_ERA, ("vp9",)),
    ("hevc", MODERN_ERA, ("hevc", "h265", "x265")),
    ("av1", MODERN_ERA, ("av1",)),
    ("vvc", MODERN_ERA, ("vvc", "h266")),
)


def _row(name: str) -> tuple[str, str, tuple[str, ...]] | None:
    """The table row a name resolves to, or None.

    Walks the table in order, matched case-insensitively against the family
    names and their aliases alike - a caller that already holds a family name
    gets the same answer as one holding whatever ffprobe printed. An empty name
    resolves to nothing, the same as one the table does not list: that is each
    lookup's cue to answer :data:`UNKNOWN`.
    """
    wanted = shell_lower(name)
    if not wanted:
        return None
    for family, era, aliases in TABLE:
        if wanted == shell_lower(family) or any(wanted == shell_lower(alias) for alias in aliases):
            return family, era, aliases
    return None


def family_of(name: str) -> str:
    """The family a codec belongs to, or :data:`UNKNOWN` for one the table does
    not list.

    What a caller keys its own per-codec behaviour on, so that a new spelling of
    a known codec is a one-word change here rather than a change everywhere the
    codec is acted on. Never a guess: the unknown stays named as the unknown it
    is, and the caller decides what it is worth.
    """
    row = _row(name)
    return row[0] if row is not None else UNKNOWN


def era_of(name: str) -> str:
    """The generation the codec compresses like, or :data:`UNKNOWN` when it is
    not listed - never a guess, because "assume it is modern" and "assume it is
    ancient" are both wrong answers for a caller that has to judge a file it
    could not identify.
    """
    row = _row(name)
    return row[1] if row is not None else UNKNOWN


def aliases_of(name: str) -> str | None:
    """The ffprobe spellings that mean ``name``, space separated - for a caller
    listing what it accepts, or building a match of its own.

    None when the name is not a family, the way the shell function returns 1
    with no answer: a name that is not a family has none to give, and the
    absence of an empty line is part of the contract.
    """
    row = _row(name)
    return " ".join(row[2]) if row is not None else None


def families() -> list[str]:
    """The family names, oldest generation first, one per row."""
    return [family for family, _era, _aliases in TABLE]


def _case_sql(expression: str, column: str) -> str:
    """The shared builder behind the two SQL emitters.

    One WHEN per row in table order - first match wins in SQL exactly as it does
    in the bash scan - with the values lower-cased and the family itself deduped
    out of its alias list, and the answer whatever column the row holds for it.
    The expression is pasted in verbatim, and the NULL/empty arm comes first so
    a file that probed to nothing lands in ``unknown`` rather than in the ELSE,
    matched lower-cased and trimmed the way the bash lookups match, so a report
    spelling "H265" and one spelling "hevc" land in the same bucket.
    """
    if column not in ("family", "era"):
        raise ValueError(f"column must be 'family' or 'era', not {column!r}")
    lines = [
        "CASE",
        f"            WHEN {expression} IS NULL OR trim({expression}) = '' THEN '{UNKNOWN}'",
    ]
    for family, era, aliases in TABLE:
        lowered_family = shell_lower(family)
        values = [lowered_family]
        for alias in aliases:
            lowered = shell_lower(alias)
            if lowered != lowered_family:
                values.append(lowered)
        answer = family if column == "family" else era
        in_list = ", ".join(f"'{value}'" for value in values)
        lines.append(
            f"            WHEN lower(trim({expression})) IN ({in_list}) THEN '{answer}'"
        )
    lines.append(f"            ELSE '{UNKNOWN}'")
    lines.append("        END")
    return "\n".join(lines)


def family_sql(expression: str) -> str:
    """The family lookup as a SQL CASE expression.

    So a report that recorded raw codec names can be grouped by family without
    the grouping being spelled out a second time in SQL. Same table, same
    answers, same case-insensitive matching as :func:`family_of` - which is the
    point of it being generated from the table rather than written out.
    """
    return _case_sql(expression, "family")


def era_sql(expression: str) -> str:
    """The generation lookup as a SQL CASE expression - the same for the
    generation, which is the coarsest reading of the column: three buckets over
    any spelling a file can carry.
    """
    return _case_sql(expression, "era")


def encoder_codec(ffmpeg_encoder: str) -> str:
    """The codec family an ffmpeg ENCODER produces, or "" for one this repo does
    not use.

    Judged by the codec that comes OUT, not by the library that wrote it (both
    AV1 encoders make AV1). Matched on substrings, in order, because every
    ffmpeg encoder for a codec carries that codec's name or number somewhere in
    it - and the order is part of the answer: a name that mentions two codecs is
    the first rule's, and the matching is case-sensitive, as the shell's case
    is, so a name handed over in upper case is an encoder this repo does not
    use.
    """
    if "av1" in ffmpeg_encoder:
        return "av1"
    if "hevc" in ffmpeg_encoder or "265" in ffmpeg_encoder:
        return "hevc"
    if "264" in ffmpeg_encoder:
        return "h264"
    return ""