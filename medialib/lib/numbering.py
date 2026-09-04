"""Renaming a folder's commonest filetype to 01, 02, 03 ...

Only the plurality filetype is touched, and only when it has at least two members:
a lone file of a type is not a series, and a stray ``cover.jpg`` among the audio
must keep its name. Extensions are counted case-insensitively, so ``.MP3`` and
``.mp3`` are one filetype, but each file keeps its own extension exactly as it was
- the numbering renames the stem, not the type. A tie between two filetypes goes
to the one whose first file comes first.

Already-numbered folders are detected and left completely alone, so a second run
does nothing and no file is touched needlessly.

The order is ``sort -V`` over the paths ``find`` printed, which is why this needs
``versionsort`` - and why it needs the directory, not just the names. See
``plan_numbering``.
"""

import os
from collections.abc import Sequence
from typing import NamedTuple

from medialib.lib import enums
from medialib.lib.runlog import log
from medialib.lib.versionsort import version_key

__all__ = ["NO_EXTENSION", "Numbering", "plan_numbering"]

# The key a file with no extension is counted under. A real extension cannot
# contain a slash, so this can never collide with one.
NO_EXTENSION = "/no-ext/"


class Numbering(NamedTuple):
    """What the operation decided: which filetype, how many of it, what to rename."""

    extension: str
    # Not "count": a NamedTuple field of that name would shadow tuple.count.
    total: int
    renames: list[tuple[str, str]]


def _key(name: str) -> str:
    """The extension a file is tallied under."""
    return enums.lower_extension_of(name) or NO_EXTENSION


def plan_numbering(directory: str, names: Sequence[str]) -> Numbering:
    """Decide the renames for one folder, without performing any of them.

    ``directory`` is joined to each name **for ordering only**, and it changes the
    answer. bash sorts the full paths that ``find`` prints, and version sort puts a
    name beginning with a dot before everything else - which a path beginning with
    a slash never triggers. So a folder holding ".cover" and "a10" orders them one
    way by basename and the other way by path, and the path is what ships. Passing
    the directory is how that stays true instead of being a bug found twice.

    The renames are empty when there is nothing to do: an empty folder, a plurality
    group of one, or a folder already numbered exactly this way. That last is what
    makes the operation idempotent, and it is decided before any file moves rather
    than discovered halfway through.
    """
    prefix = f"{directory}/" if directory else ""
    ordered = sorted(names, key=lambda name: version_key(prefix + name))
    if not ordered:
        return Numbering(NO_EXTENSION, 0, [])

    counts: dict[str, int] = {}
    for name in ordered:
        counts[_key(name)] = counts.get(_key(name), 0) + 1

    # Walk the files, not the tally: a tie is settled by first appearance. bash
    # used to walk an associative array, which is hash order and reproducible in
    # no other language; the rule was made explicit on both sides instead.
    plurality, count = NO_EXTENSION, 0
    for name in ordered:
        if counts[_key(name)] > count:
            plurality, count = _key(name), counts[_key(name)]

    group = [name for name in ordered if _key(name) == plurality]
    if len(group) < 2:
        return Numbering(plurality, count, [])

    width = len(str(len(group)))
    renames = []
    for number, name in enumerate(group, 1):
        extension = enums.extension_of(name)
        target = f"{number:0{width}d}.{extension}" if extension else f"{number:0{width}d}"
        renames.append((name, target))

    if all(source == target for source, target in renames):
        return Numbering(plurality, count, [])
    return Numbering(plurality, count, renames)


def number_files_in_folder(directory: str, files) -> None:
    """``numberFilesInFolder``: the plurality filetype of one folder, numbered.

    ``files`` are the folder's own files, in the order `sort -V` puts them - the
    caller lists them, because what counts as a sibling is the caller's rule.
    """
    names = [os.path.basename(path) for path in files]
    plan = plan_numbering(directory, names)
    if not plan.renames:
        return

    label = "<none>" if plan.extension == NO_EXTENSION else plan.extension
    log('  "%s": numbering %d .%s file(s)'
        % (os.path.basename(directory), plan.total, label))

    # Two phases through unique temporary names, so an overlapping source and
    # target - 10.mp3 becoming 02.mp3 while 2.mp3 is still there - cannot
    # clobber each other.
    stage = os.path.join(directory, ".cfs_renumber_%d_" % os.getpid())
    staged = []
    for index, (source, target) in enumerate(plan.renames):
        temporary = "%s%d" % (stage, index)
        try:
            os.replace(os.path.join(directory, source), temporary)
        except OSError:
            continue
        staged.append((temporary, target))
    for temporary, target in staged:
        try:
            os.replace(temporary, os.path.join(directory, target))
        except OSError:
            pass
