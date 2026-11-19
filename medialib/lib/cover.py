"""Choosing the cover art in a folder, and calling it by the name it is
given - ``folder.<ext>`` by default, another name or none at the caller's
choice.

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
from typing import NamedTuple

from medialib.lib.enums import LISTS, lower_extension_of, shell_lower
from medialib.lib.safety import SkipLog, safe_rename
from medialib.lib.versionsort import version_key

__all__ = ["COVER_WORDS", "CoverChoice", "choose_cover",
           "cover_choice", "rename_cover_to_folder"]

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

    Natural order of the FULL path, and the difference is load-bearing: a version
    sort treats a leading dot specially only at the start of the line, so
    ".cover.jpg" sorts before its siblings on its own and among them once a
    directory is in front of it. A hidden file reaches this sort - ".cover.jpg"
    has an extension, even though ".cover" does not - so sorting the bare names
    would pick a different winner.
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


class CoverChoice(NamedTuple):
    """What :func:`choose_cover` decided, and the figure it was taken on.

    ``word`` is the cover word the winner carries - the highest-priority one
    any candidate matched - and ``candidates`` how many carry it, which is
    what it was largest among. Both are empty when the folder has none.
    """

    path: str | None
    word: str
    candidates: int


def _stem(path: str) -> str:
    """The filename without its extension, the cover words are matched in."""
    base = path.rpartition("/")[2]
    return base.rpartition(".")[0]


def cover_choice(directory: str) -> CoverChoice:
    """The cover to promote, the word it won on, and how many candidates
    carry that word - the selection with the figure it was taken on.
    """
    paths = _candidates(directory)
    for word in COVER_WORDS:
        candidates = [path for path in paths if word in shell_lower(_stem(path))]
        if not candidates:
            continue
        best: str | None = None
        best_size = -1  # not 0: a real file can be empty, and that is a candidate
        for path in candidates:
            size = _size(path)
            if size > best_size:
                best_size = size
                best = path
        return CoverChoice(best, word, len(candidates))
    return CoverChoice(None, "", 0)


def choose_cover(directory: str) -> str | None:
    """The path of the cover to promote, or None when the folder has no candidate."""
    return cover_choice(directory).path


def rename_cover_to_folder(directory: str, log: SkipLog | None = None,
                           name: str = "folder") -> str | None:
    """Promote the folder's best cover image to ``<name>.<ext>``.

    ``name`` is empty for no rename at all: the selection is the caller's
    business, and a folder that keeps its cover under its own name is not an
    event to log.

    Returns the message logged when a rename happened, and None when nothing did
    - which covers a folder with no candidate, a winner that is already called
    ``<name>.<ext>``, and a rename refused because a different ``<name>.<ext>``
    is in the way. The last of those is recorded in ``log``; the other two are
    not events at all.
    """
    if not os.path.isdir(directory) or not name:
        return None
    best = choose_cover(directory)
    if best is None:
        return None

    base = best.rpartition("/")[2]
    extension = lower_extension_of(base)
    if not safe_rename(best, f"{directory}/{name}.{extension}", log):
        return None
    folder = _basename(directory)
    return f'  "{folder}": cover art "{base}" -> "{name}.{extension}"'
