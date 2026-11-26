"""Every entry at or below a directory, in find's own order.

Its own module because more than one caller asks this question, and one
definition of a rule beats several that agree today.
"""

import contextlib
import os
from collections.abc import Iterable, Iterator

__all__ = ["entries_below", "holding_out", "is_held_out"]

# The folders a run has said no walk of it is to go into - ingest-movies'
# shows, which are ingest-series' to ingest - as normalised absolute paths.
_HELD_OUT: frozenset = frozenset()


def _normal(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


@contextlib.contextmanager
def holding_out(paths: Iterable[str]):
    """For as long as the block runs, every walk that asks
    :func:`is_held_out` - :func:`entries_below` among them - passes over
    ``paths`` and everything below them."""
    global _HELD_OUT
    before = _HELD_OUT
    _HELD_OUT = before | frozenset(_normal(path) for path in paths)
    try:
        yield
    finally:
        _HELD_OUT = before


def is_held_out(path: str) -> bool:
    """Whether ``path`` is, or is below, a folder a run is holding out."""
    if not _HELD_OUT:
        return False
    normal = _normal(path)
    return any(normal == held or normal.startswith(held + os.sep)
               for held in _HELD_OUT)


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
        if _HELD_OUT and is_held_out(entry.path):
            continue
        yield entry
        if entry.is_dir(follow_symlinks=False):
            pending.append(listing(entry.path))
