"""Every entry at or below a directory, in find's own order.

Its own module because more than one caller asks this question, and one
definition of a rule beats several that agree today.
"""

import os
from collections.abc import Iterator

__all__ = ["entries_below"]


def entries_below(top: str) -> Iterator[os.DirEntry]:
    """Every entry under ``top`` - files, folders, links and the rest - the way
    ``find <top> -mindepth 1`` lists them.

    The order is the filesystem's: each directory's entries in readdir order,
    a subdirectory descended into at once, where it stands rather than after
    its siblings. A link is yielded but never followed. A folder that cannot be
    listed - lost+found on an ext4 drive is root's alone - is yielded and
    passed over: it holds nothing the caller could act on, and one unreadable
    corner must not end the walk of a whole drive.

    Each directory is listed in full before its entries are yielded, so a
    caller may remove an entry it was just handed. Iterative rather than
    recursive, because the depth of a tree - an unpacked archive's above all -
    is not something this can assume anything about.
    """
    def listing(path: str) -> Iterator[os.DirEntry]:
        try:
            return iter(list(os.scandir(path)))
        except OSError:
            return iter(())

    pending = [listing(top)]
    while pending:
        entry = next(pending[-1], None)
        if entry is None:
            pending.pop()
            continue
        yield entry
        if entry.is_dir(follow_symlinks=False):
            pending.append(listing(entry.path))
