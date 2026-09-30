"""Asking before a report under `logs/` replaces the one an earlier run left.

The reports a command writes there are worklists: the films TMDb could not name,
the renames a dry run would make, the numbers missing from a run. Somebody may be
half-way through one, and a rerun over the same folder would replace it without a
word. So a run that is about to write one that is already there asks first, once
for all of them, before it has done any work - a question at the end of an
hour's run would be asked of an empty room.

The answer is read from standard input, and only "y" or "yes" overwrites. No
answer at all - a closed or empty stdin, which is what a cron run has - is a no:
the earlier reports are kept, and the run stops before doing anything. Piping
the answer in (`echo y | ...`) is how an unattended run says yes.

The logs that APPEND rather than overwrite - the ytdlp download archives,
beets' import log - lose nothing to a rerun and are never asked about.
"""

import os
import sys
from collections.abc import Iterable

__all__ = ["confirm_overwrite"]


def confirm_overwrite(paths: Iterable[str]) -> bool:
    """Whether the run may write ``paths``: True when none of them is there yet,
    or when the user said yes to replacing the ones that are.

    The question and the refusal go to stderr, so a command's stdout stays its
    own report.
    """
    existing = [path for path in dict.fromkeys(paths) if os.path.exists(path)]
    if not existing:
        return True
    sys.stderr.write("An earlier run already wrote:\n")
    for path in existing:
        sys.stderr.write("  %s\n" % path)
    sys.stderr.write("Overwrite %s? [y/N] "
                     % ("it" if len(existing) == 1 else "them"))
    sys.stderr.flush()
    try:
        answer = sys.stdin.readline()
    except (OSError, ValueError):
        answer = ""
    # A terminal echoes what was typed; a piped answer is shown, so the line
    # after the question does not run on from it.
    if answer and not _interactive():
        sys.stderr.write(answer.strip() + "\n")
    if answer.strip().lower() in ("y", "yes"):
        return True
    if not answer:
        sys.stderr.write("\nNo answer on standard input, so nothing was "
                         "overwritten. Pipe \"y\" in to\noverwrite from a run "
                         "nobody is watching.\n")
    else:
        sys.stderr.write("Nothing was overwritten.\n")
    return False


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (OSError, ValueError):
        return False
