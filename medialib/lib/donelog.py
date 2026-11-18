"""A record of the films a heavy phase has finished, kept across runs.

One film per line - its key (:func:`plexnames.id_key`), a tab, the path it
was finished under (there for whoever reads the file, never read back), and
the scope it was finished for: the comma list of codes the run was told to
do, in the language table's order - so a film is known by its id wherever it
has since been moved and however its title has since been spelled, and for
what an earlier run got through. A line written before the scope was
recorded has none, and is read as ``default_scope``; a film is done for a
run only when its recorded scope holds every code the run wants, so a code
added to the list later is fetched for the film once, and never again. A
film with no id tag is never recorded, and so never skipped. Deleting a
line, or the file, has the film or every film done again.
"""

from __future__ import annotations

import os

from medialib.lib import languages, plexnames


def _ordered(codes: set | tuple) -> tuple:
    """``codes`` in the language table's order, a code the table does not
    know after every one it does."""
    order = {row.code2: index for index, row in enumerate(languages.LANGUAGES)}
    return tuple(sorted(set(codes),
                        key=lambda code: order.get(code, len(order))))


class DoneLog:
    """The films recorded in ``path``, and the means to record one more.

    Each film is appended as soon as it is finished rather than at the end of
    the run, so a run that is stopped keeps every film it got through. A film
    is recorded for the codes its run was told to do, and a later run is told
    off for a film only when that film's recorded scope holds every code the
    later run wants.
    """

    def __init__(self, path: str, default_scope: tuple = ()) -> None:
        self.path = path
        self._default = frozenset(default_scope)
        self._scope: dict = {}
        self._lines: list = []
        self._line: dict = {}
        try:
            with open(path, encoding="utf-8") as handle:
                self._lines = handle.readlines()
        except FileNotFoundError:
            pass
        for index, line in enumerate(self._lines):
            fields = line.rstrip("\n").split("\t")
            key = fields[0].strip()
            if not key or key.startswith("#"):
                continue
            codes = fields[2].strip() if len(fields) > 2 else ""
            self._scope[key] = (frozenset(codes.split(","))
                                if codes else self._default)
            self._line[key] = index

    def __len__(self) -> int:
        return len(self._scope)

    def has(self, movie: str, wanted: tuple = ()) -> bool:
        """Is ``movie`` recorded as done for every code in ``wanted`` - the
        codes a run wants, ``()`` for one that wants none in particular?"""
        key = plexnames.id_key(movie)
        if not key or key not in self._scope:
            return False
        return set(wanted) <= self._scope[key]

    def add(self, movie: str, codes: tuple = ()) -> bool:
        """Record ``movie`` as done for ``codes``, and say whether it could
        be: a film with no id tag cannot. A film already recorded is
        rewritten only when this run got through more than its line says -
        its line then carries the wider scope, in place."""
        key = plexnames.id_key(movie)
        if not key:
            return False
        scope = self._scope.get(key, frozenset()) | set(codes)
        if key in self._line:
            if set(codes) <= self._scope[key]:
                return True
            self._lines[self._line[key]] = (
                "%s\t%s\t%s\n" % (key, movie, ",".join(_ordered(scope))))
            self._scope[key] = scope
            self._write()
            return True
        if codes:
            line = "%s\t%s\t%s\n" % (key, movie, ",".join(_ordered(codes)))
        else:
            line = "%s\t%s\n" % (key, movie)
        self._lines.append(line)
        self._line[key] = len(self._lines) - 1
        self._scope[key] = scope
        self._write()
        return True

    def _write(self) -> None:
        """The lines back to the file, one film per line as read."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.writelines(self._lines)
