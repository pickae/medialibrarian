"""The "is this image worth re-encoding" model.

A picture that was never given enough bytes cannot be improved by encoding it
again, only made worse, while one that has plenty can be re-encoded far smaller
at the same quality. Telling the two apart is one anchor scaled along three axes
- the format, the pixel count, and what is IN the picture - and the reading of a
measured file size against the result, which :mod:`medialib.lib.adequacy` names.

Which format a name means, and whether it is lossy at all, are answered by
:mod:`medialib.lib.imagecodecs`. The model itself is pure arithmetic over its tables
and needs no tool; only measuring a file reaches for ImageMagick.

**The anchor and where it comes from.** A colour photographic JPEG at one
megapixel needs about 1 bit per pixel to be adequate. The JPEG FAQ puts
"without visible loss" at 10:1 to 20:1 over 24-bit colour, which is 1.2 to 2.4
bpp; the HTTP Archive's 2024 Web Almanac measures the median JPEG actually served
on the web at 2.0 bpp, WebP at 1.3 and AVIF at 1.4. So 1 bpp is a JPEG that is
good rather than lavish, and :data:`adequacy.GENEROUS_FACTOR` times it - 2 bpp -
lands exactly on the web's own median, which is the file that can be halved and
still look the same. That is the reading this model exists to make.

**Why the axes are these three.** A format's efficiency is the largest term and
the best measured one. The pixel count matters slightly sub-linearly, because a
codec with big adaptive blocks finds more to predict in a larger frame. And what
is in the picture is worth more than either: a page of flat inked artwork costs a
fraction of a photograph of the same size, and judging one against the other's
requirement is how a converter comes to skip a perfectly good comic scan.
"""

from __future__ import annotations

import os
import subprocess

from medialib.lib import adequacy, imagecodecs
from medialib.lib.formatting import awk_number

__all__ = [
    "adequate_bpp_1mp",
    "adequate_image_pixels",
    "codec_tuning_table",
    "COLOUR",
    "GREY",
    "colour_tuning_table",
    "PHOTO",
    "ART",
    "LINE",
    "detail_tuning_table",
    "UNMEASURED_DETAIL",
    "ART_COLOUR_CEILING",
    "LINE_COLOUR_CEILING",
    "image_codec_tuning",
    "image_colour_factor",
    "image_detail_factor",
    "adequate_image_bytes",
    "image_verdict",
    "image_stats",
    "stats_from_identify",
    "image_detail",
    "detail_from_identify",
    "image_adequacy",
]

# What a colour photographic JPEG needs to be adequate, in BITS PER PIXEL, and
# the pixel count that figure belongs to. Everything else is this figure scaled,
# so it is the one anchor the rest of the model multiplies.
adequate_bpp_1mp = 1.0
adequate_image_pixels = 1000000

# Per-family tuning, keyed on the family names of ``imagecodecs``. Each row is
# (family, factor, exponent): what the family needs at the one-megapixel anchor
# relative to JPEG, and how its requirement scales with the pixel count (1 would
# be linearly, and every row is at or under it).
#
# The FACTORS are matched-quality efficiency, from the published comparisons:
# WebP is 25-35% smaller than JPEG, AVIF 40-50% smaller than JPEG and 15-30%
# smaller than WebP, HEIC within a few percent of AVIF, and JPEG XL 10-15%
# better than AVIF again in the quality range the web actually uses.
#
# The LOSSLESS and PALETTE rows are a different measurement, because a format
# that keeps every pixel has no quality to be matched at: their factors are what
# such a file typically COSTS - the Web Almanac's median PNG is 3.8 bpp and its
# median still GIF 3.5 - halved, so that a file of the ordinary size for its
# format reads as generous, which is what it is. Being lossless they can never
# read as starved whatever the arithmetic says; :func:`image_verdict` is where
# that is enforced.
#
# The EXPONENTS descend with the size of a format's prediction and transform
# units, which is the whole of what a bigger picture gives a codec to work with:
# JPEG's blocks are 8x8 and fixed, so it gains almost nothing; AV1 and JPEG XL
# reach 128x128 and 256x256. The uncompressed formats are exactly linear.
codec_tuning_table = (
    ("jpeg", "1.00", "0.95"),
    ("jpeg2000", "0.80", "0.92"),
    ("webp", "0.70", "0.92"),
    ("heic", "0.55", "0.89"),
    ("avif", "0.52", "0.88"),
    ("jxl", "0.45", "0.88"),
    ("png", "1.90", "0.97"),
    ("gif", "1.75", "0.98"),
    ("ico", "1.75", "0.98"),
    ("tiff", "3.00", "1.00"),
    ("psd", "4.00", "1.00"),
    ("bmp", "4.00", "1.00"),
    ("tga", "4.00", "1.00"),
    ("pcx", "4.00", "1.00"),
    ("netpbm", "4.00", "1.00"),
)

# --- the colour axis ----------------------------------------------------------
# Whether the picture has colour in it at all, which is the one thing about its
# CONTENT a header alone can say: a probe reads it out of the JPEG's component
# count or the PNG's colour type without decoding a pixel.
#
# The JPEG FAQ measures a grayscale file at 10-25% smaller than a colour one of
# the same visual quality - the chroma planes are subsampled and cheap to begin
# with, so dropping them saves less than the three-to-one in the raw data
# suggests. 0.80 is the middle of that.
COLOUR = "colour"
GREY = "grey"

colour_tuning_table = {
    COLOUR: "1.00",
    GREY: "0.80",
}

# --- the detail axis ----------------------------------------------------------
# What KIND of picture it is, which no header states and only a decode can
# answer - which is why it is measured separately and why every caller that
# cannot afford the decode leaves it unmeasured.
#
# PHOTO is a photograph or a painted scan: continuous tone everywhere, the
# content the anchor was set on. ART is flat artwork - inked comic pages, cel
# animation, diagrams, screenshots, anything built from large areas of one
# colour, which the published guidance puts at roughly half a photograph's
# quality setting for the same appearance. LINE is a two-tone scan, which is
# what the bilevel formats were invented for and costs a fraction again.
PHOTO = "photo"
ART = "art"
LINE = "line"

detail_tuning_table = {
    PHOTO: "1.00",
    ART: "0.45",
    LINE: "0.15",
}

# What an UNMEASURED detail axis is read as: an axis nobody measured must never
# be the thing that makes a file read starved. Reading a photograph as flat
# artwork understates what it needs and can only make the verdict kinder,
# whereas the other way round would have a converter skip pictures on the
# strength of a measurement it never took.
#
# :data:`LINE` is not used for this even though it is kinder still: a two-tone
# picture is nearly always in a lossless or palette format, where the verdict is
# floored at adequate anyway, so reading every unmeasured picture as line art
# would flatten the axis for no file it actually protects.
UNMEASURED_DETAIL = ART

# Where the unique-colour count puts a decoded picture on the detail axis. Flat
# artwork is built from a bounded set of colours - a few hundred inks and their
# anti-aliasing - while a photograph runs into the tens of thousands within a
# single frame; the gap between the two is wide enough that the boundary is not a
# close call. Counted over the WHOLE picture, which is why this needs the decode.
ART_COLOUR_CEILING = 4096
LINE_COLOUR_CEILING = 16

# What ImageMagick's ``%[type]`` answers for a picture already reduced to a
# palette. Matched on the prefix, because it has a "...Alpha" spelling too and
# the transparency says nothing about the content.
_PALETTE_TYPES = ("palette",)


def image_codec_tuning(codec: str) -> str:
    """``"<factor> <exponent> <kind> <family>"`` for an image format.

    The family and the kind come from ``imagecodecs``; the table above says what
    they are worth. A format that list does not name - or none at all, from a
    file nothing could probe - is tuned as JPEG and named as the unknown it is:
    JPEG is the ladder's reference point, so an unidentified file is judged
    neither generously nor harshly.
    """
    family = imagecodecs.family_of(codec)
    kind = imagecodecs.kind_of(codec)
    row = next((r for r in codec_tuning_table if r[0] == family), None)
    if row is None:
        return f"1.00 0.95 {kind} {family}"
    return f"{row[1]} {row[2]} {kind} {family}"


def image_colour_factor(colour: str) -> str:
    """A colour reading as its factor, three decimals.

    Anything that is not one of :data:`colour_tuning_table`'s keys - including
    the empty string a probe that could not say answers - counts as colour, the
    dearer of the two, because a picture wrongly read as grey would be judged
    against four fifths of what it needs.
    """
    value = colour_tuning_table.get(colour or COLOUR,
                                    colour_tuning_table[COLOUR])
    return f"{awk_number(value):.3f}"


def image_detail_factor(detail: str) -> str:
    """A detail reading as its factor, three decimals.

    An unmeasured axis reads as :data:`UNMEASURED_DETAIL`, and so does a word
    the table does not know: both are "nobody told this model what is in the
    picture", and both take the reading that cannot manufacture a starved
    verdict.
    """
    value = detail_tuning_table.get(detail or UNMEASURED_DETAIL)
    if value is None:
        value = detail_tuning_table[UNMEASURED_DETAIL]
    return f"{awk_number(value):.3f}"


def adequate_image_bytes(codec: str, width: object, height: object,
                         colour: str = "", detail: str = "") -> str:
    """What an image of that description needs to be adequate, in BYTES.

    Bytes rather than bits per pixel because that is what a file HAS: a caller
    holding a directory entry can compare the two without doing arithmetic of its
    own, and the pixel count has already been folded in once here rather than
    again at every call.

    Prints nothing when the pixel size is unknown, since every axis is a multiple
    of it and a guessed size would be a guessed verdict.
    """
    factor, exponent, _kind, _family = image_codec_tuning(codec).split()
    w = awk_number(width)
    h = awk_number(height)
    if w <= 0 or h <= 0:
        return ""
    pixels = w * h

    anchor = awk_number(adequate_bpp_1mp)
    reference = awk_number(adequate_image_pixels)
    f = awk_number(factor)
    p = awk_number(exponent)
    colour_factor = awk_number(image_colour_factor(colour))
    detail_factor = awk_number(image_detail_factor(detail))
    if reference <= 0:
        return ""

    # The bits-per-pixel requirement at THIS size: the anchor's, scaled by how
    # far the picture is from the reference megapixel and by how much the format
    # gains from that distance. An exponent of 1 leaves it flat, which is what
    # the uncompressed formats get.
    bpp = anchor * f * colour_factor * detail_factor * (pixels / reference) ** (p - 1)
    return "%.0f" % (bpp * pixels / 8)


def image_verdict(size_bytes: object, adequate: object,
                  codec: str = "") -> str:
    """What a file size IS for a requirement - the shared verdict, with the one
    thing that is peculiar to images applied on top.

    That one thing is losslessness. A PNG, a TIFF, a BMP and a still GIF hold
    every pixel they were given, so "it was not given enough bytes" is not a
    thing that can be true of them however small they are; a 400-byte lossless
    icon is a small picture, not a degraded one. Their verdict is therefore
    floored at adequate, which leaves the reading that still means something -
    whether there is a conversion's worth of bytes to save - saying exactly what
    it says for everything else.

    A VECTOR drawing has no verdict at all: its size is what its shapes cost and
    has nothing to do with the pixels something rasterises it into, so neither
    half of the reading means anything. A format nothing could identify, on the
    other hand, is judged as the lossy one it probably is - the alternative is to
    declare every unreadable file safe to re-encode.
    """
    kind = imagecodecs.kind_of(codec)
    if kind == imagecodecs.VECTOR:
        return adequacy.UNKNOWN
    name = adequacy.verdict(size_bytes, adequate)
    if kind in (imagecodecs.LOSSLESS, imagecodecs.PALETTE):
        return adequacy.at_least_adequate(name)
    return name


# --- measuring a file -----------------------------------------------------------

def _identify(arguments: list) -> str:
    """One ``identify`` call, or "" for anything that goes wrong with it."""
    from medialib.lib import imagemagick
    try:
        done = subprocess.run(imagemagick.identify_argv(arguments),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL)
    except OSError:
        return ""
    if done.returncode != 0:
        return ""
    # A multi-frame file - an animated GIF, a TIFF of scanned pages, a JPEG with
    # a thumbnail attached - prints one line per frame. The first is the picture.
    return done.stdout.decode("utf-8", "replace").strip().split("\n")[0].strip()


def stats_from_identify(answer: str) -> str:
    """``"<format> <width> <height> <colour>"`` out of one ``identify`` line,
    each field empty when it could not be read.

    Modelled apart from the call, so the reading can be tested without a picture
    on disk.

    The colour reading comes from ``%[colorspace]``, which for a ping is what the
    file's own header says: a JPEG's component count, a PNG's colour type. A
    colourspace with "gray" in it is a picture with no colour in it; everything
    else, including one that could not be read, is colour.
    """
    fields = (answer or "").split("|")
    if len(fields) < 4:
        return ""
    image_format, width, height, colorspace = (f.strip() for f in fields[:4])
    if not (width.isdigit() and height.isdigit()):
        return ""
    colour = GREY if "gray" in colorspace.lower() else COLOUR
    return " ".join([image_format.lower(), width, height, colour])


def image_stats(path: str) -> str:
    """``"<format> <width> <height> <colour>"`` for a file, or "" for one that
    could not be read.

    The CHEAP probe, and the only one a converter can afford per image:
    ``-ping`` stops at the header, so this is a read of a few hundred bytes and
    no decoding at all. Everything it answers - the format, the pixel size, and
    whether there is colour in it - is stated in that header by every format in
    the table. What is IN the picture is not, which is what
    :func:`image_detail` is for and why it is separate.
    """
    return stats_from_identify(_identify(
        ["-ping", "-format", "%m|%w|%h|%[colorspace]", path]))


def detail_from_identify(answer: str) -> str:
    """A decoded picture's ``"<type>|<unique colours>"`` as a detail word, or ""
    when it says nothing.

    Two readings, because neither alone is enough. The TYPE catches the pictures
    that were stored as what they are - a bilevel scan, an indexed palette - and
    the COLOUR COUNT catches the ones that were not, which is most of them: a
    comic page saved as a JPEG is a full-colour picture as far as its header is
    concerned, and only counting what is actually in it says otherwise.
    """
    fields = (answer or "").split("|")
    if len(fields) < 2:
        return ""
    type_word, colours = fields[0].strip().lower(), fields[1].strip()
    if type_word.startswith("bilevel"):
        return LINE
    count = int(colours) if colours.isdigit() else 0
    if count <= 0:
        # Nothing counted the colours, so only the type is left to go on - and a
        # palette is flat artwork by construction.
        return ART if type_word.startswith(_PALETTE_TYPES) else ""
    if count <= LINE_COLOUR_CEILING:
        return LINE
    if count <= ART_COLOUR_CEILING or type_word.startswith(_PALETTE_TYPES):
        return ART
    return PHOTO


def image_detail(path: str) -> str:
    """What is in the picture - :data:`PHOTO`, :data:`ART`, :data:`LINE`, or ""
    when it could not be read.

    The EXPENSIVE probe, and the reason every caller of it is opt-in: counting
    a picture's unique colours means decoding all of it, which on a library of
    scanned pages is an order of magnitude more work than reading their headers.
    A caller that cannot afford it leaves the axis unmeasured and is judged by
    :data:`UNMEASURED_DETAIL`, which is the reading that cannot cost a file a
    conversion it deserved.
    """
    return detail_from_identify(
        _identify(["-format", "%[type]|%k", path]))


def image_adequacy(path: str, detail: str = "",
                   size_bytes: object = None) -> str:
    """The verdict on one file on disk - the whole model, end to end.

    ``detail`` is the caller's own reading of the detail axis: the word
    :func:`image_detail` answered where the caller could afford that probe, and
    nothing where it could not. ``size_bytes`` likewise saves a stat for a caller
    that already has one.
    """
    stats = image_stats(path).split()
    if len(stats) < 4:
        return adequacy.UNKNOWN
    image_format, width, height, colour = stats[:4]
    if size_bytes is None:
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            return adequacy.UNKNOWN
    adequate = adequate_image_bytes(image_format, width, height, colour, detail)
    if not adequate:
        return adequacy.UNKNOWN
    return image_verdict(size_bytes, adequate, image_format)
