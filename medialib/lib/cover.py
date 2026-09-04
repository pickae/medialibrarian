"""Choosing the cover art in a folder, and calling it ``folder.<ext>``.

One folder in, at most one rename out.

The selection is three rules stacked, and each only shows itself when the one
above it ties:

  1. the highest-priority cover word any candidate matches wins **outright** -
     a folder holding both ``front.jpg`` and ``cover.jpg`` never considers the
     cover, however much bigger it is;
  2. within that group, the largest file;
  3. and where those tie too, the first in natural order.

The third is not decoration. Two equally sized candidates are the normal case for
a folder whose art was downloaded twice, and without a defined order the answer
would be whatever the filesystem listed first.
"""

import os

from medialib.lib.enums import LISTS, lower_extension_of, shell_lower
from medialib.lib.safety import SkipLog, safe_rename
from medialib.lib.versionsort import version_key

__all__ = ["COVER_WORDS", "choose_cover", "rename_cover_to_folder"]

# Highest priority first.
COVER_WORDS = ("folder", "front", "cover")

# What counts as an image here is the union of the two central lists, so a cover
# in a format only one of them knows (an svg, a tiff) is still a cover.
_IMAGE_EXTENSIONS = frozenset(
    shell_lower(extension)
    for name in ("imageExtensions", "coverImageExtensions")
    for extension in LISTS[name]
)


def _candidates(directory: str) -> list[str]:
    """The immediate image files, in natural order.

    Natural order of the FULL path, because that is what the shell sorts, and the
    difference is load-bearing: a version sort treats a leading dot specially
    only at the start of the line, so ".cover.jpg" sorts before its siblings on
    its own and among them once a directory is in front of it. A hidden file
    reaches this sort - ".cover.jpg" has an extension, even though ".cover" does
    not - so sorting the bare names would pick a different winner.
    """
    try:
        names = [
            entry.name
            for entry in os.scandir(directory)
            if entry.is_file(follow_symlinks=False)
        ]
    except OSError:
        return []
    prefix = f"{directory}/" if directory else ""
    paths = [
        prefix + name
        for name in names
        if lower_extension_of(name) in _IMAGE_EXTENSIONS
    ]
    return sorted(paths, key=version_key)


def _basename(path: str) -> str:
    """``basename(1)``: the last component, with any trailing slashes ignored."""
    stripped = path.rstrip("/")
    if not stripped:
        return "/" if path else ""
    return stripped.rpartition("/")[2]


def _size(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def choose_cover(directory: str) -> str | None:
    """The path of the cover to promote, or None when the folder has no candidate."""
    paths = _candidates(directory)
    if not paths:
        return None

    for word in COVER_WORDS:
        best: str | None = None
        best_size = -1  # not 0: a real file can be empty, and that is a candidate
        for path in paths:
            base = path.rpartition("/")[2]
            stem = shell_lower(base.rpartition(".")[0])
            if word not in stem:
                continue
            size = _size(path)
            if size > best_size:
                best_size = size
                best = path
        if best is not None:
            return best
    return None


def rename_cover_to_folder(directory: str, log: SkipLog | None = None) -> str | None:
    """Promote the folder's best cover image to ``folder.<ext>``.

    Returns the message the shell logs when a rename happened, and None when
    nothing did - which covers a folder with no candidate, a winner that is
    already called ``folder.<ext>``, and a rename refused because a different
    ``folder.<ext>`` is in the way. The last of those is recorded in ``log``;
    the other two are not events at all.
    """
    if not os.path.isdir(directory):
        return None
    best = choose_cover(directory)
    if best is None:
        return None

    base = best.rpartition("/")[2]
    extension = lower_extension_of(base)
    if not safe_rename(best, f"{directory}/folder.{extension}", log):
        return None
    folder = _basename(directory)
    return f'  "{folder}": cover art "{base}" -> "folder.{extension}"'
