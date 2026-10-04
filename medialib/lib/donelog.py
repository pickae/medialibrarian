"""A record of the films a heavy phase has finished, kept across runs.

One film per line - its key (:func:`plexnames.id_key`), a tab, and the path
it was finished under, which is there for whoever reads the file and is never
read back - so a film is known by its id wherever it has since been moved or
however its title has since been spelled. A film with no id tag is never
recorded, and so never skipped. Deleting a line, or the file, has the film or
every film done again.
"""

from __future__ import annotations

import os

from medialib.lib import plexnames


class DoneLog:
    """The films recorded in ``path``, and the means to record one more.

    Each film is appended as soon as it is finished rather than at the end of
    the run, so a run that is stopped keeps every film it got through.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._keys: set = set()
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    key = line.rstrip("\n").split("\t", 1)[0].strip()
                    if key and not key.startswith("#"):
                        self._keys.add(key)
        except FileNotFoundError:
            pass

    def __len__(self) -> int:
        return len(self._keys)

    def has(self, movie: str) -> bool:
        key = plexnames.id_key(movie)
        return bool(key) and key in self._keys

    def add(self, movie: str) -> bool:
        """Record ``movie`` as done, and say whether it could be: a film with
        no id tag cannot."""
        key = plexnames.id_key(movie)
        if not key:
            return False
        if key not in self._keys:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write("%s\t%s\n" % (key, movie))
            self._keys.add(key)
        return True
