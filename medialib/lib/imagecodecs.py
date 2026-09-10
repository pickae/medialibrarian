"""The shared still-image codec table: one list, read several ways.

"Which format is this file in", "what does this repo call that", "is it a lossy
format at all" are three questions about one list, so the list lives here once
and is answered here in Python and in SQL alike.

This is the IDENTITY of an image format - the name ImageMagick and ffprobe print
for it, the extensions it is written under, and whether it throws information
away. What any of that is WORTH in bytes is a separate reading of the same list
and lives beside it in :mod:`medialib.lib.imagebitrate`, keyed by the family names
here.

The KIND is the load-bearing column: half the formats an image library holds are
lossless, and a lossless file cannot be starved of bytes whatever its size says.
A format that can be written either way - WebP, JPEG XL, AVIF, TIFF - is listed
by what it is USED as in the wild, because nothing in a file's name says which of
the two a particular file is, and the adequacy model would rather be told than
guess.

The table is an ordered sequence, not a mapping: the lookups walk it in order, so
a spelling listed twice answers by its first row both times.
"""

from __future__ import annotations

from medialib.lib.enums import shell_lower

__all__ = [
    "LOSSY",
    "LOSSLESS",
    "PALETTE",
    "VECTOR",
    "UNKNOWN",
    "TABLE",
    "families",
    "family_of",
    "kind_of",
    "extensions_of",
    "aliases_of",
    "raster_extensions",
    "family_sql",
    "kind_sql",
]

# What a format does with the pixels it is given.
#
# LOSSY throws information away to get smaller, which is the only kind an
# adequacy model has anything to say about. LOSSLESS keeps every pixel, so it is
# never starved - it may be enormous, it may be tiny, but no amount of re-encoding
# recovers what it never lost. PALETTE keeps every pixel it has, having already
# been reduced to at most 256 colours before it was written, so it is lossless
# about a picture that was already quantised. VECTOR has no pixels at all and no
# size to be judged against.
LOSSY = "lossy"
LOSSLESS = "lossless"
PALETTE = "palette"
VECTOR = "vector"

# The answer for a format the table does not list, or none at all from a file
# nothing could read. One marker for both lookups, distinct from every real
# family and kind.
UNKNOWN = "unknown"

# One row per family: (family, kind, extensions, aliases).
#
# The FAMILY is the name this repo knows the format by, which is also the first
# extension where the two agree. The EXTENSIONS are every suffix it is written
# under - the list a command filters an input tree on. The ALIASES are the other
# spellings a probe may answer with: ImageMagick's ``%m`` prints a delegate name
# in upper case ("JPEG", "PNG", "MPO"), ffprobe prints a codec name
# ("mjpeg", "png"), and neither agrees with the extension for half the list.
#
# To recognise another format, add its spelling to a row - or add a row if it is
# genuinely a different format. Nothing here is spelled out anywhere else.
TABLE = (
    ("jpeg", LOSSY, ("jpg", "jpeg", "jpe", "jfif"), ("mjpeg", "mpo")),
    ("jpeg2000", LOSSY, ("jp2", "j2k", "jpf", "jpx"),
     ("jpeg2000", "j2c", "jpc")),
    ("webp", LOSSY, ("webp",), ()),
    ("heic", LOSSY, ("heic", "heif", "hif"), ("heix", "mif1")),
    ("avif", LOSSY, ("avif",), ("avifs",)),
    ("jxl", LOSSY, ("jxl",), ("jpegxl",)),
    ("png", LOSSLESS, ("png",), ("apng", "png00", "png24", "png32")),
    ("tiff", LOSSLESS, ("tif", "tiff"), ("tiff64", "ptif")),
    ("bmp", LOSSLESS, ("bmp", "dib"), ("bmp2", "bmp3")),
    ("tga", LOSSLESS, ("tga",), ("targa",)),
    ("pcx", LOSSLESS, ("pcx",), ("dcx",)),
    ("netpbm", LOSSLESS, ("pnm", "ppm", "pgm", "pbm"), ("pam",)),
    ("psd", LOSSLESS, ("psd",), ("psb",)),
    ("gif", PALETTE, ("gif",), ("gif87",)),
    ("ico", PALETTE, ("ico",), ("icon", "cur")),
    ("svg", VECTOR, ("svg", "svgz"), ("msvg", "rsvg")),
)


def _row(name: str) -> tuple[str, str, tuple[str, ...], tuple[str, ...]] | None:
    """The table row a name resolves to, or None.

    Walks the table in order, matched case-insensitively against the family
    names, the extensions and the aliases alike - so a caller holding an
    extension, a caller holding ImageMagick's ``%m`` and a caller holding
    ffprobe's codec name all get the same answer. A leading dot is taken off
    first, since the two ways of writing a suffix are the same suffix. An empty
    name resolves to nothing, which is each lookup's cue to answer
    :data:`UNKNOWN`.
    """
    wanted = shell_lower(name).lstrip(".")
    if not wanted:
        return None
    for family, kind, extensions, aliases in TABLE:
        if wanted == shell_lower(family):
            return family, kind, extensions, aliases
        if any(wanted == shell_lower(other)
               for other in extensions + aliases):
            return family, kind, extensions, aliases
    return None


def family_of(name: str) -> str:
    """The family a format belongs to, or :data:`UNKNOWN` for one not listed.

    What a caller keys its own per-format behaviour on, so a new spelling of a
    known format is a one-word change here rather than a change everywhere the
    format is acted on. Never a guess: the unknown stays named as the unknown it
    is, and the caller decides what it is worth.
    """
    row = _row(name)
    return row[0] if row is not None else UNKNOWN


def kind_of(name: str) -> str:
    """Whether the format throws pixels away - one of :data:`LOSSY`,
    :data:`LOSSLESS`, :data:`PALETTE`, :data:`VECTOR`, or :data:`UNKNOWN`.

    The one question the adequacy model cannot answer without: a lossless source
    holds every pixel it was given, so "is this file big enough to be any good"
    has no meaning for it, whereas for a lossy one it is the whole question.
    """
    row = _row(name)
    return row[1] if row is not None else UNKNOWN


def extensions_of(name: str) -> tuple[str, ...]:
    """Every suffix that family is written under, in the order the table lists
    them - the first is the one to write a new file with. Empty for a name the
    table does not know.
    """
    row = _row(name)
    return row[2] if row is not None else ()


def aliases_of(name: str) -> str | None:
    """The probe spellings that mean ``name``, space separated - for a caller
    listing what it accepts, or building a match of its own.

    None, and not an empty line, for a name that is not a family.
    """
    row = _row(name)
    return " ".join(row[3]) if row is not None else None


def families() -> list[str]:
    """The family names, in table order - the lossy formats first."""
    return [family for family, _kind, _extensions, _aliases in TABLE]


def raster_extensions() -> tuple[str, ...]:
    """Every suffix in the table that names a format with PIXELS in it, in table
    order and de-duplicated.

    What an input filter wants: :data:`VECTOR` is left out because an SVG has no
    resolution and no file size worth judging, and rasterising one is a decision a
    command makes on its own rather than a conversion it falls into.
    """
    out: list[str] = []
    for _family, kind, extensions, _aliases in TABLE:
        if kind == VECTOR:
            continue
        for extension in extensions:
            if extension not in out:
                out.append(extension)
    return tuple(out)


def _case_sql(expression: str, column: str) -> str:
    """The shared builder behind the two SQL emitters.

    One WHEN per row in table order - first match wins in SQL exactly as it does
    in :func:`_row` - with the values lower-cased and the family deduped out of
    its own spellings. The NULL/empty arm comes first, so a file that probed to
    nothing lands in ``unknown`` rather than in the ELSE.
    """
    if column not in ("family", "kind"):
        raise ValueError(f"column must be 'family' or 'kind', not {column!r}")
    lines = [
        "CASE",
        f"            WHEN {expression} IS NULL OR trim({expression}) = '' "
        f"THEN '{UNKNOWN}'",
    ]
    for family, kind, extensions, aliases in TABLE:
        lowered_family = shell_lower(family)
        values = [lowered_family]
        for other in extensions + aliases:
            lowered = shell_lower(other)
            if lowered not in values:
                values.append(lowered)
        answer = family if column == "family" else kind
        in_list = ", ".join(f"'{value}'" for value in values)
        lines.append(
            f"            WHEN lower(trim({expression})) IN ({in_list}) "
            f"THEN '{answer}'")
    lines.append(f"            ELSE '{UNKNOWN}'")
    lines.append("        END")
    return "\n".join(lines)


def family_sql(expression: str) -> str:
    """The family lookup as a SQL CASE expression, so a report that recorded raw
    format names can be grouped by family without the grouping being spelled out
    a second time.
    """
    return _case_sql(expression, "family")


def kind_sql(expression: str) -> str:
    """The same for the kind - the coarsest reading of the column, and the one
    that separates the files a library can still save bytes on from the ones it
    cannot.
    """
    return _case_sql(expression, "kind")
