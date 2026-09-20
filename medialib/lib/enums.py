"""The central media enums, and the pure helpers that read a file's extension.

Every list is one maintained source of truth. Adding a format means adding it
here, and nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "LISTS",
    "extension_of",
    "lower_extension_of",
    "extension_list",
    "BRACKET_OPEN",
    "BRACKET_CLOSE",
    "PART_WORDS",
    "DUPLICATE_MARKER_PATTERNS",
    "DATE_PREFIX_PATTERN",
]

# Every still-image suffix a command here can be pointed at. What each of these
# formats IS - its family, and whether it throws pixels away - is
# :mod:`medialib.lib.imagecodecs`, which groups the same suffixes by format. The
# two cannot be one list without a circular import, so they are one list and one
# test: ``tests/lib/test_imagecodecs.py`` fails if a suffix is in either and not
# the other.
IMAGE_EXTENSIONS = (
    "jpg", "jpeg", "jpe", "jfif", "jp2", "j2k", "jpf", "jpx", "webp", "heic",
    "heif", "hif", "avif", "jxl", "png", "tif", "tiff", "bmp", "dib", "tga",
    "pcx", "pnm", "ppm", "pgm", "pbm", "psd", "gif", "ico",
)
# What an image can be converted TO, as against the IMAGE_EXTENSIONS above,
# which is what one can be converted FROM. In order of preference, so the first
# is the one a command writes when it is not told otherwise.
#
# Each of these names its codec AND the extension that codec is written under -
# unlike the audio and video codec lists, where a codec goes into a container of
# a different name - so a reader of this list can use it as either.
IMAGE_CODECS = ("avif", "webp", "jxl",)
COVER_IMAGE_EXTENSIONS = ("jpeg", "jpg", "png", "svg", "tiff", "tif", "bmp",)
COMIC_EXTENSIONS = ("cbr", "cbz", "cb7",)
COMIC_PDF_EXTENSIONS = ("pdf",)
# aac is raw ADTS rather than a container, which is why it sits beside the two
# MP4 spellings of the same codec. It belongs on the list because concat-audio
# joins a folder of it, and this is what every command answers "is there any
# audio here at all?" from.
AUDIO_EXTENSIONS = ("m4a", "opus", "m4b", "aac", "mp3", "mka", "ogg", "ogx",
                    "flac", "mpga",)
# What audio can be converted TO, as against the AUDIO_EXTENSIONS above, which is
# what one can be converted FROM. In order of preference, so the first is the one
# a command writes when it is not told otherwise.
#
# These are CODEC names and nothing else. IMAGE_CODECS above doubles as its own
# extension list because an image codec is written under its own name; an audio
# codec is not - opus goes into .opus, xhe-aac into .m4a - so the container each
# one is written in is the separate mapping below, and a reader wanting a file
# name has to go through it.
AUDIO_CODECS = ("opus", "xheaac",)
# The container each of AUDIO_CODECS is written in, which is also the output
# extension. Kept beside the list rather than in the command that writes it,
# because "opus" and ".opus" being the same word is a coincidence of that one
# codec and not a rule anything may derive: xhe-aac in a .xheaac file is not a
# file any player would open.
AUDIO_CODEC_EXTENSIONS = {
    "opus": "opus",
    "xheaac": "m4a",
}
ALWAYS_TRANSCODE_EXTENSIONS = ("m4a", "m4b", "mka",)
# The largest plausible kbps a track of each AUDIO_EXTENSIONS format encodes at.
#
# A file's size divided by its format's ceiling is the shortest that file can
# possibly be: a real track is at or above its plausible rate, never below it, so
# this division is a LOWER bound on duration. convert-audio's dynamic queue uses
# exactly that bound - a file whose minimum possible duration is still past the
# split threshold is a definite chunking candidate, and one whose is not is
# preloaded. It is deliberately a ceiling, not a census: a file that reads as
# plausibly short is encoded whole at worst - the chunking it would have got is
# an optimization the run gives up, not a decision it gets wrong.
AUDIO_MAX_KBPS = {
    "m4a": 256,
    "m4b": 256,
    "aac": 256,
    "mp3": 320,
    "mpga": 320,
    "opus": 120,
    "flac": 500,
    "ogg": 256,
    "ogx": 256,
    "mka": 256,
}
# An extension the table above has no entry for: the smallest of the table's
# rates. The division a rate feeds is the file's SHORTEST possible duration, so
# the smallest rate is the longest that figure can be, and an unknown format is
# routed to probing rather than assumed short.
AUDIO_MAX_KBPS_DEFAULT = 120
VIDEO_EXTENSIONS = (
    "mp4", "mkv", "avi", "mov", "webm", "m4v", "flv", "mpg", "mpeg", "wmv", "ts",
)
SOURCE_VIDEO_EXTENSIONS = (
    "avi", "mp4", "flv", "flv2", "m4v", "m3u8", "mov", "webm", "mpg", "vob",
)
BOOK_INPUT_EXTENSIONS = ("pdf", "epub", "mobi", "chm", "azw3", "lit", "txt",)
BOOK_CONVERT_EXTENSIONS = ("mobi", "chm", "azw3", "lit", "txt",)
BOOK_IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "svg",)
NARRATABLE_BOOK_EXTENSIONS = (
    "epub", "mobi", "azw3", "fb2", "pdf", "txt", "rtf", "odt", "doc", "docx", "html",
    "htmlz", "chm", "lit", "prc", "pdb", "lrf", "pml", "snb", "rb", "tcr",
)
ARCHIVE_EXTENSIONS = (
    "zip", "rar", "7z", "tar", "tar.gz", "tgz", "tar.bz2", "tbz2", "tbz", "tar.xz", "txz",
    "tar.zst", "tzst",
)
LOSSLESS_AUDIO_EXTENSIONS = ("flac", "ape", "wav", "wv",)
LOSSLESS_CODECS = ("flac", "ape", "alac", "pcm_s16le", "pcm_s16be", "wavpack",)
LOSSLESS_TRACK_CODECS = ("dts", "flac", "truehd", "wav", "ape", "pcm",)


# --- the bracket set ----------------------------------------------------------
# The single source of truth for which characters count as brackets across the
# name-cleaning library. Matched pairs BY INDEX: the i-th opener closes with the
# i-th closer. Each consumer derives whatever shape it needs - a membership set, a
# direction-specific character class - and keeps that locally.
BRACKET_OPEN = "([{<"
BRACKET_CLOSE = ")]}>"

# --- the parts Plex stacks ---------------------------------------------------
# The keywords Plex's scanner reads as "this file is one piece of a film that was
# split": the keyword, an optional dot, and a number, as one trailing word.
# Matched case-insensitively wherever it is used, the way the scanner matches.
# https://support.plex.tv/articles/naming-and-organizing-your-movie-media-files/
PART_WORDS = ("cd", "dvd", "part", "pt", "disk", "disc")

# The ones of those that are also the name of a SOURCE. Written with no number
# after it, "DVD" is not a part that lost its number - it is what the release
# was made from, and it stands beside the "BluRay" and the "VHS" that were
# always read that way because they are not part keywords at all.
#
# Only "dvd". The rest of the list is pieces rather than formats: "part" and
# "pt" say nothing else, and a bare "disc" or "disk" is the piece word with its
# number gone. "cd" is the one that could be argued either way, and it is not
# in here on purpose - a film on a Compact Disc is a rarity, while "cd1"/"cd2"
# is how a generation of films was split in two, so a bare "cd" is far likelier
# to be a lost number than a source.
SOURCE_PART_WORDS = ("dvd",)

# --- the markers a copy gets --------------------------------------------------
# What a file manager appends when a second file of one name lands in a folder.
# None of them says anything about the film: they are the two desktops' way of
# not overwriting, and a name carrying one is the same name as its sibling.
#
# Each is anchored at the END of a stem and matched case-insensitively. The
# bracketed number is the widest of them and the reason the list is a list: it
# is also how a person writes a second version, so only a folder where nothing
# else tells the copies apart may act on it.
DUPLICATE_MARKER_PATTERNS = (
    # Windows Explorer: "Film - Copy", "Film - Copy (2)".
    r"\s*-\s*copy(?:\s*\([0-9]+\))?",
    # GNOME Files and the other freedesktop managers.
    r"\s*\((?:another |[0-9]+(?:st|nd|rd|th) )?copy\)",
    # macOS Finder: "Film copy", "Film copy 2".
    r"\s+copy(?:\s+[0-9]+)?",
    # Every manager's last resort, and a person's first: a bare number in
    # brackets. Never a YEAR, though - a film folder ends in one by convention,
    # and a rule that read "Rivertown (1942)" as the 1942nd copy of Rivertown
    # would take the year off every name in the library.
    r"\s*\((?![12][0-9]{3}\))[0-9]+\)",
)

# --- the date prefix ----------------------------------------------------------
# What counts as a DATE at the front of a name: an eight-digit YYYYMMDD whose year
# starts with 1 or 2. Such a prefix is split off by the individual cleaner and is
# deliberately NOT cleared as a uniform prefix - a date timestamps the item and
# stays informative even when the whole group shares it, unlike a serial number.
#
# Anchored at both ends: it is matched against a candidate prefix on its own, never
# searched inside a longer string.
DATE_PREFIX_PATTERN = r"^[1-2][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$"


def extension_of(path: str) -> str:
    """A file's extension as it is written, or "" when it has none.

    **A name that begins with a dot has no extension**, `.hidden.mp3` included.
    A dot-leading name is not content these commands work on - it is something a
    tool left behind - so calling it an mp3 would put it in front of the encoder,
    the census and the cover search, which is the one thing it must not be in
    front of. `.hidden` answers the same, for the plainer reason that its only
    dot begins the name.

    This is the single definition.
    """
    base = path.rpartition("/")[2]
    if base.startswith(".") or "." not in base:
        return ""
    return base.rpartition(".")[2]


def lower_extension_of(path: str) -> str:
    """:func:`extension_of`, lower-cased.

    The comparison everything else does is against a lowered extension, so lowering
    is part of reading it rather than a step every caller repeats.
    """
    return shell_lower(extension_of(path))


# The one codepoint in all of Unicode where this lower-casing disagrees with
# Python's. The C library, applied character by character, maps U+0130 (the
# dotted capital I) to a plain "i"; Python's full Unicode mapping is "i" plus a
# combining dot above. Compared across all 292,463 printable codepoints, that is
# the only difference between the two - so a single substitution is a complete
# fix here and not a patch over an unknown set.
_SHELL_LOWER_EXCEPTIONS = str.maketrans({"\u0130": "i"})


def shell_lower(text: str) -> str:
    """``text`` lower-cased with C-library semantics: U+0130 comes out a plain ``i``."""
    return text.translate(_SHELL_LOWER_EXCEPTIONS).lower()


def extension_list(extensions: Iterable[str]) -> str:
    """The extensions as a human-readable list - ".mp3 / .flac / .opus".

    What a script prints when it has to say which files it will act on, so it is
    generated from the same list the filter is built from and cannot drift from it.
    """
    return " / ".join(f".{extension}" for extension in extensions)


def _bash_name(python_name: str) -> str:
    """``LOSSLESS_TRACK_CODECS`` -> ``losslessTrackCodecs``."""
    head, *rest = python_name.lower().split("_")
    return head + "".join(word.capitalize() for word in rest)


# Every list above, keyed by its camelCase name. Built by inspection rather
# than written out, so a list added here appears here too and nobody has to
# remember to register it.
LISTS: dict[str, tuple[str, ...]] = {
    _bash_name(name): value
    for name, value in sorted(globals().items())
    if (name.endswith(("_EXTENSIONS", "_CODECS")) and isinstance(value, tuple))
}
