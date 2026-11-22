"""File and folder names a phone accepts as they are.

Android's shared storage takes only names that would be valid on FAT - its
media provider refuses the rest, and so does an MTP copy onto it - and iOS's
Files app shows a colon as a slash. What is mapped is what either refuses:
the FAT-reserved characters, control characters, a leading dot (a hidden
file on both) and a trailing dot or space, and a name longer than the 255
bytes both filesystems stop at. Everything else - accents, non-Latin
scripts, curly quotes - is left alone, because both handle it.

The replacements are chosen to read the same rather than to be reversible:
"Act 1: Scene 2" becomes "Act 1 - Scene 2", and a double quote a single one.
"""

import os
import re

# 255 is a byte limit on both ext4/f2fs and APFS, and a character limit on
# exFAT that UTF-8 bytes never undercut, so bytes are what is counted.
MAX_NAME_BYTES = 255

_COLON_SPACED = re.compile(r"\s*:\s+")
_PIPE_SPACED = re.compile(r"\s*\|\s*")
_SPACES = re.compile(r" {2,}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_SIMPLE = str.maketrans({
    ":": "-",
    "\\": "-",
    "/": "-",
    '"': "'",
    "<": "(",
    ">": ")",
    "?": "",
    "*": "",
})


def _redecoded(name: str) -> str:
    """A name read off the disk with bytes that are not UTF-8 - Python keeps
    them as lone surrogates - read again as the Windows-1252 they usually are,
    so "Caf\\udce9" becomes "Café" rather than a name no phone can store."""
    raw = name.encode("utf-8", "surrogateescape")
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def _truncated(stem: str, extension: str, limit: int) -> str:
    """``stem`` cut at a character boundary so that it and ``extension``
    together fit in ``limit`` bytes."""
    room = limit - len(extension.encode("utf-8"))
    encoded = stem.encode("utf-8")
    if len(encoded) <= room:
        return stem
    return encoded[:max(room, 1)].decode("utf-8", "ignore").rstrip(" .")


def mobile_name(name: str, reserve: int = 0) -> str:
    """One path component as a phone accepts it. ``reserve`` is the bytes an
    extension added later will take, kept free under the length limit."""
    text = _redecoded(name)
    text = _COLON_SPACED.sub(" - ", text)
    text = _PIPE_SPACED.sub(" - ", text)
    text = _CONTROL.sub("", text.translate(_SIMPLE))
    text = _SPACES.sub(" ", text).strip(". ") or "_"
    limit = MAX_NAME_BYTES - reserve
    if len(text.encode("utf-8")) <= limit:
        return text
    stem, extension = os.path.splitext(text)
    # Only a short, space-free tail is an extension worth keeping whole: in
    # "Disc no. 2 (Live Set)" the part after the dot is not one.
    if len(extension) > 6 or " " in extension:
        stem, extension = text, ""
    return _truncated(stem, extension, limit) + extension


def mobile_path(relative: str) -> str:
    """A relative path with every component a phone accepts. Split on this
    system's separators only - "/" as well as "\\" on Windows - so that a
    backslash, which is a character in a Linux name, is mapped rather than
    taken for a folder."""
    if not relative:
        return relative
    separators = re.escape(os.sep + (os.altsep or ""))
    return os.path.join(*(mobile_name(part) if part not in (".", "..") else part
                          for part in re.split("[%s]" % separators, relative)
                          if part))
