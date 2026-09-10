"""The still-image census row.

One ``identify -ping`` per file, which reads the header and stops: the format, the
pixel size and whether there is colour in it, without decoding a picture. That is
what makes a census of a library holding forty thousand comic pages and cover
scans finish in the time a census of its films takes.

The suffix is a CLAIM and not a fact, and the same probe checks it: a file whose
header no image reader can make sense of gets a skip reason rather than a row,
however confidently it is named .jpg.

The one column that is not a reading is the adequacy - what that file's size IS
for the format and size it is - and it is the reason this module knows about
:mod:`medialib.lib.imagebitrate` at all. It is also the only expensive thing here,
because the reading it wants is what is IN the picture, and that costs a decode
per file; so it is asked for and paid for only under the census's own -a.
"""

import os

from medialib.lib import adequacy, census, imagebitrate, imagecodecs

__all__ = ["census_image_row", "census_image_adequacy"]


def census_image_adequacy(path, image_format, width, height, colour,
                          size_bytes):
    """What that file's size IS for the picture in it - "starved", "adequate",
    "generous", or "unknown" when it cannot be judged.

    The one judgement in this report rather than a reading, made against the same
    model, tables and boundaries ``convert-images`` decides a single file with -
    so a library counted here as starved is the set of files that command refuses
    to re-encode.

    Unlike that command, this one CAN afford the deep probe: it is already
    walking the library once, a caller who asked for -a asked for exactly this,
    and the difference between a photograph and a page of flat artwork is most of
    the answer. A picture the decode cannot read leaves the axis unmeasured,
    which is the model's most generous reading and never calls a file starved
    that a content-aware run would not.
    """
    if not width or not height:
        return adequacy.UNKNOWN
    detail = imagebitrate.image_detail(path)
    adequate = imagebitrate.adequate_image_bytes(image_format, width, height,
                                                 colour, detail)
    if not adequate:
        return adequacy.UNKNOWN
    return imagebitrate.image_verdict(size_bytes, adequate, image_format)


def census_image_row(path, separator=None):
    """The images report's row for <path> - path, size, adequacy, resolution,
    codec - or ``(None, reason)``.

    The codec is the FORMAT the header says, folded to the family name this repo
    knows it by, not the suffix: the suffix is the claim, and a .jpg holding a
    PNG is a thing a library contains. The resolution is the coded pixel size,
    written as WIDTHxHEIGHT the way the video report writes one.
    """
    if separator is None:
        separator = os.environ.get("CENSUS_SEP", census.DEFAULT_SEPARATOR)
    stats = imagebitrate.image_stats(path).split()
    if len(stats) < 4:
        return None, "no image reader could make sense of it (not an image, " \
                     "or truncated)"
    image_format, width, height, colour = stats[:4]

    size_bytes = census.file_size(path)
    verdict = ""
    if os.environ.get("CENSUS_ADEQUACY", ""):
        verdict = census_image_adequacy(path, image_format, width, height,
                                        colour, size_bytes)

    row = census.join([path, size_bytes, verdict,
                       "%sx%s" % (width, height),
                       imagecodecs.family_of(image_format)], separator)
    return row, None
