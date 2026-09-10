"""How this host spells an ImageMagick call.

ImageMagick 6 installs one binary per operation - ``convert``, ``identify`` -
and version 7 replaced all of them with a single ``magick`` that takes the
operation as its first word: ``magick identify ...``, and ``magick ...`` on its
own for what used to be ``convert``. Version 7 still installs the old names as
compatibility wrappers on most builds, which is why every call site here could
say ``convert`` for years and be right on both.

That is the part that is ending. The v7 wrappers print a deprecation notice and
are documented as going away, and Homebrew - which is how a Mac gets
ImageMagick at all - ships v7 only. So the name is resolved rather than
written: the old one while it is there, ``magick`` when it is not.

The other half of "does this host take the call" is not the NAME but the BUILD.
ImageMagick compiles each image format against a separate library, and a build
without one still installs, still answers ``-version``, and still passes a PATH
check - it simply cannot write that format. Asking the binary what it can write
is therefore a different question from asking whether it is there, and
:func:`format_modes` is how it is asked.

Resolved per call, by a PATH lookup and no subprocess. A cached answer would
have to be reset by every test that stands a stub on PATH, which is most of
them, and a ``which`` is nothing beside the image conversion it prefixes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

__all__ = ["convert_argv", "identify_argv", "format_modes", "require_format",
           "CONVERT_SPEC", "IDENTIFY_SPEC", "DELEGATES"]

# What the preflight asks for: either spelling satisfies it. The v6 name comes
# first in both, so a refusal names the one the install hints are written for.
CONVERT_SPEC = "convert|magick"
IDENTIFY_SPEC = "identify|magick"


def convert_argv(arguments) -> list[str]:
    """One ``convert`` call, spelled the way this host takes it."""
    return _argv("convert", arguments)


def identify_argv(arguments) -> list[str]:
    """One ``identify`` call, spelled the way this host takes it.

    ``magick identify`` and not a bare ``magick``: the operation is a word of
    its own in v7, and dropping it would run the conversion instead.
    """
    return _argv("identify", arguments)


# What a build has to have been compiled against to handle each of these. A
# refusal names the delegate rather than the program, because "install
# ImageMagick" is no help at all to somebody who plainly has it.
DELEGATES = {
    "avif": "libheif",
    "webp": "libwebp",
    "jxl": "libjxl",
}

# One row of ``-list format``: the name, an optional ``*``, the delegate MODULE
# that serves it, then the three characters of the mode - read, write,
# multi-image - as in ``rw+``, ``-w+`` or ``---``. The header row survives the
# name group and is turned away by the mode, which "Mode" is not.
#
# The module column is what a whole family shares - AVIF, HEIC, HEIF and AVCI
# are all served by HEIC - so it is matched and dropped rather than captured,
# and it is optional so that a listing without it still parses.
_FORMAT_ROW = re.compile(
    r"^\s*([A-Za-z0-9._+-]+)\*?(?:\s+[A-Za-z0-9._+-]+)?\s+([r-][w-][+-])\s")


def format_modes() -> dict[str, str]:
    """Every format this host's ImageMagick knows, lower-cased, to its mode.

    ``{"avif": "rw+", "heic": "r--", ...}`` - from ``-list format``, which is
    the only thing that answers what a BUILD can do rather than what the
    program is called.

    **Empty means the question could not be answered**, not that nothing can be
    written: no ImageMagick on PATH, a build whose listing this cannot parse, a
    stub standing in for one during a test. Callers treat that as "cannot tell"
    and let the run proceed, because refusing over an unreadable answer would
    break more hosts than the delegate it was looking for.
    """
    try:
        done = subprocess.run(convert_argv(["-list", "format"]),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return {}
    if done.returncode != 0:
        return {}
    modes = {}
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        row = _FORMAT_ROW.match(line)
        if row:
            modes[row.group(1).lower()] = row.group(2)
    return modes


def require_format(what: str, image_format: str, *, writing: bool = True,
                   skip_preflight: bool = False, file=None) -> int:
    """Refuse a run whose format this ImageMagick was not built to handle.

    Returns 1 having written the refusal, or 0 silently. The failure this
    prevents is the quiet kind: the conversions are attempted one image at a
    time and every one of them fails, so a run that could not have worked at
    all still walks the whole tree first and reports a tree of nothing.

    Only a CONCLUSIVE answer refuses - the format is listed, and the direction
    asked for is not among its modes. A listing that could not be read says
    nothing either way and is allowed through, on the same reasoning as
    :func:`format_modes`.
    """
    if file is None:
        file = sys.stderr
    if skip_preflight:
        return 0
    modes = format_modes()
    if not modes:
        return 0
    mode = modes.get(image_format.lower())
    letter = "w" if writing else "r"
    if mode is not None and letter in mode:
        return 0
    verb = "write" if writing else "read"
    delegate = DELEGATES.get(image_format.lower())
    needs = ("built with %s" % delegate) if delegate else \
        ("that can %s it" % verb)
    file.write(
        "\nCannot run {what}: this machine's ImageMagick cannot {verb} "
        "{upper}.\n\n"
        "  ImageMagick is installed, but this build has no {upper} {verb} "
        "support,\n"
        "  so every conversion would fail one image at a time. "
        "`{listing}` lists\n"
        "  what it can.\n\n"
        "Install an ImageMagick {needs} and run again. Nothing was changed.\n"
        .format(what=what, verb=verb, upper=image_format.upper(),
                needs=needs,
                listing=" ".join(convert_argv(["-list", "format"]))))
    return 1


def _argv(operation: str, arguments) -> list[str]:
    if shutil.which(operation):
        return [operation] + list(arguments)
    if shutil.which("magick"):
        # `magick` alone IS convert; every other operation names itself.
        prefix = ["magick"] if operation == "convert" else ["magick", operation]
        return prefix + list(arguments)
    # Neither on PATH. The old name is what the preflight named and what the
    # install hint tells the user to get, so it is what the failure should say.
    return [operation] + list(arguments)
