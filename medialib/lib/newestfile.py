"""``find <dir> -type f -iname '*.<ext>' -printf '%T@\\t%p\\n' | sort -rn |
head -n1 | cut -f2-`` - the newest file of a kind at or below a directory.

Its own module because more than one caller asks this question, and one
definition of a rule beats two that agree today.
"""

import os

__all__ = ["newest_file"]


def newest_file(directory, extension):
    """The newest file at or below <directory> whose name ends in .<extension>,
    case-insensitively, by mtime - or None when there is none.

    The walk is RECURSIVE because find's is: a producer that names its own output
    may put it in a directory of its own, and a listing of the top level only
    would call that output missing. An mtime tie goes to the lexicographically
    larger path, which is what the shell's reversed last-resort comparison does.
    """
    best = None
    best_mtime = -1.0
    suffix = "." + extension.lower()
    for base, _dirnames, filenames in os.walk(directory):
        for name in filenames:
            if not name.lower().endswith(suffix):
                continue
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if (mtime > best_mtime
                    or (mtime == best_mtime and best is not None
                        and path > best)):
                best_mtime = mtime
                best = path
    return best
