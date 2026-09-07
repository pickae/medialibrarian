"""The central media enums, and the pure helpers that read a file's extension.

The shell kept these in one file with three other things:

1. these - the extension lists every script filters on, and the name helpers that
   go with them;
2. abort and signal plumbing for the xargs worker pools (``trapWorkerAbort``,
   ``initAbortFlag``, ``exitIfAborted``), which becomes signal handling and
   ``concurrent.futures`` cancellation rather than a translation;
3. filesystem safety (``safeRename``, ``uniqueSuffixPath``, ``isEmptyFolder``),
   which is real logic and lives in :mod:`medialib.lib.safety`;
4. the run footer and its reporting.

They are ported in that order because only the first is pure, and because it is
what the other modules actually want: eleven of them call ``lowerExtensionOf`` or
``extensionList``, and ``renameCoverToFolder`` cannot be ported without these
lists at all.

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
    "DATE_PREFIX_PATTERN",
]

IMAGE_EXTENSIONS = ("jpg", "jpeg", "webp", "png",)
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
AUDIO_EXTENSIONS = ("m4a", "opus", "m4b", "mp3", "mka", "ogg", "ogx", "flac", "mpga",)
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

    This is the single definition (item 7.4). The renumberer had a second one
    that already answered this way, from bash's ``*.* && != .*``; what changes is
    the filters built from the central lists, which used bash's ``?*.*`` and
    called a hidden audio file an mp3.
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


# The one codepoint in all of Unicode where the shell and Python disagree about
# lower case. bash's ${x,,} lowers character by character through the C library,
# which maps U+0130 (the dotted capital I) to a plain "i"; Python applies the full
# Unicode mapping, which is "i" plus a combining dot above. Compared across all
# 292,463 printable codepoints, that is the only difference between the two - so a
# single substitution is a complete fix here and not a patch over an unknown set.
_SHELL_LOWER_EXCEPTIONS = str.maketrans({"\u0130": "i"})


def shell_lower(text: str) -> str:
    """``text`` lower-cased the way ``${text,,}`` lower-cases it."""
    return text.translate(_SHELL_LOWER_EXCEPTIONS).lower()


def extension_list(extensions: Iterable[str]) -> str:
    """The extensions as a human-readable list - ".mp3 / .flac / .opus".

    What a script prints when it has to say which files it will act on, so it is
    generated from the same list the filter is built from and cannot drift from it.
    """
    return " / ".join(f".{extension}" for extension in extensions)


def _bash_name(python_name: str) -> str:
    """``LOSSLESS_TRACK_CODECS`` -> ``losslessTrackCodecs``, the bash spelling."""
    head, *rest = python_name.lower().split("_")
    return head + "".join(word.capitalize() for word in rest)


# Every list above, under the name bash exported it as. Built by inspection rather
# than written out, so a list added here appears here too and nobody has to
# remember to register it.
LISTS: dict[str, tuple[str, ...]] = {
    _bash_name(name): value
    for name, value in sorted(globals().items())
    if (name.endswith(("_EXTENSIONS", "_CODECS")) and isinstance(value, tuple))
}
