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

Resolved per call, by a PATH lookup and no subprocess. A cached answer would
have to be reset by every test that stands a stub on PATH, which is most of
them, and a ``which`` is nothing beside the image conversion it prefixes.
"""

from __future__ import annotations

import shutil

__all__ = ["convert_argv", "identify_argv", "CONVERT_SPEC", "IDENTIFY_SPEC"]

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
