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

import os
import re

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
    "magick_coder",
    "coder_prefixed",
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


# --- the coder ImageMagick is told to read with ---------------------------------
# Left to itself, ImageMagick picks the decoder from a file's CONTENT, so a page
# called 001.jpg that is really SVG or MVG is handed to a vector or drawing
# coder - which can read other files, and fetch URLs, on the page's say-so. An
# input operand spelled "jpeg:001.jpg" is read by that coder or not at all.
#
# The coder is the one the file's first bytes name, when they are one of the
# raster formats below: a PNG saved as .jpg is common in scanned books, and a
# webp sidecar is copied under a .jpg name, and both are still pictures. Only
# when the bytes say nothing listed here does the extension decide - and a
# file whose extension promises a raster format it does not hold then fails
# to read rather than being read as something else.

# ImageMagick's coder for each family, where it is not the family name. The
# VECTOR family is absent on purpose: an SVG is never forced on anything.
_CODER_NAMES = {"jpeg2000": "jp2", "netpbm": "pnm"}

# (offset, bytes, coder) for the formats a file's opening bytes name outright.
_SIGNATURES = (
    (0, b"\xff\xd8\xff", "jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"GIF87a", "gif"),
    (0, b"GIF89a", "gif"),
    (8, b"WEBP", "webp"),
    (0, b"II*\x00", "tiff"),
    (0, b"MM\x00*", "tiff"),
    (0, b"II+\x00", "tiff"),
    (0, b"MM\x00+", "tiff"),
    (0, b"8BPS", "psd"),
    (0, b"\xff\x0a", "jxl"),
    (0, b"\x00\x00\x00\x0cJXL \r\n\x87\n", "jxl"),
    (0, b"\x00\x00\x00\x0cjP  \r\n\x87\n", "jp2"),
    (0, b"\xffO\xffQ", "j2k"),
    (0, b"BM", "bmp"),
)

# The ISO media brands at offset 8 of an ftyp box, for the two families that
# live in that container.
_FTYP_BRANDS = {b"avif": "avif", b"avis": "avif",
                b"heic": "heic", b"heix": "heic", b"heim": "heic",
                b"heis": "heic", b"hevc": "heic", b"mif1": "heic",
                b"msf1": "heic"}

# A frame or size selector ImageMagick reads off the end of an operand.
_SELECTOR = re.compile(r"\[[^\[\]]*\]$")

# Every coder an operand may already have been given, here or by a caller
# naming its own scratch format, so that a second pass leaves it as it is.
_GIVEN_CODERS = frozenset(
    [_CODER_NAMES.get(family, family) for family, kind, _e, _a in TABLE
     if kind != VECTOR]
    + [coder for _offset, _signature, coder in _SIGNATURES] + ["miff"])


def _coder_by_content(path: str) -> str:
    """The coder the file's opening bytes name, or "" when they name none of
    the listed formats or the file cannot be read."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
    except OSError:
        return ""
    if head[4:8] == b"ftyp":
        # The major brand and then the compatible ones, four bytes each after
        # the minor version: an AVIF often leads with the generic "mif1" and
        # names itself only further along.
        brands = [head[8:12]] + [head[at:at + 4]
                                 for at in range(16, min(len(head),
                                                         int.from_bytes(
                                                             head[0:4])), 4)]
        coders = [_FTYP_BRANDS.get(brand, "") for brand in brands]
        return "avif" if "avif" in coders else next(
            (coder for coder in coders if coder), "")
    for offset, signature, coder in _SIGNATURES:
        if head[offset:offset + len(signature)] == signature:
            if coder == "webp" and not head.startswith(b"RIFF"):
                continue
            return coder
    return ""


def magick_coder(path: str) -> str:
    """The ImageMagick coder to read <path> with: the one its bytes name, else
    the one its extension implies - "" for an extension this table has no
    raster family for, which leaves ImageMagick to decide as it always has."""
    coder = _coder_by_content(path)
    if coder:
        return coder
    extension = os.path.basename(path).rpartition(".")[2]
    if not extension or extension == os.path.basename(path):
        return ""
    row = _row(extension)
    if row is None or row[1] == VECTOR:
        return ""
    return _CODER_NAMES.get(row[0], row[0])


def coder_prefixed(operand: str) -> str:
    """An INPUT operand of an ImageMagick call with its coder named in front -
    ``"jpeg:page.jpg"``, ``"gif:anim.gif[0]"`` - so that what reads it is the
    raster decoder its name or bytes promised.

    A trailing ``[...]`` selector is kept where ImageMagick looks for it, at
    the end, unless the whole operand is itself an existing file. ``-`` (the
    standard input), an empty operand, one that already names a coder and one
    whose coder cannot be told are returned unchanged. A path is otherwise
    taken as it is, "./" and all: the prefix ends at the first colon, so
    nothing after it is read as a coder, and a name starting with "-" is no
    longer taken for an option. Never for an output: there the extension
    picks the encoder, which is the caller's choice and not the file's.
    """
    if not operand or operand == "-":
        return operand
    path = operand
    if not os.path.exists(operand):
        if shell_lower(operand.partition(":")[0]) in _GIVEN_CODERS:
            return operand
        selector = _SELECTOR.search(operand)
        if selector:
            path = operand[:selector.start()]
    coder = magick_coder(path)
    return "%s:%s" % (coder, operand) if coder else operand


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
