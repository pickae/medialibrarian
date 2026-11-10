"""What a run said on the console, kept: one file per run under `logs/info/`.

The error log keeps only what went wrong; this keeps the run's account of
itself - the progress lines, the warnings, the closing figures, and under
``verbose`` the reason behind each decision - for reading after the console has
scrolled away or the terminal was closed.

How much is kept is the ``infoLogLevel`` setting, chosen apart from the
console's ``-q`` and ``-v``: ``off``, ``quiet`` (errors and results only),
``normal`` (what a run says by default) or ``verbose`` (that plus ``-v``'s
lines). So a run can say little on the console and still keep everything.

The file takes the error log's name - ``20261105-140211-convert-audio-4242.log``
- so the two files of one run sit side by side under the same name, and like
it is made only when the first line arrives. The workers a run fans out to and
the commands it starts write into the same file, each line one write under the
run's lock, stamped to the millisecond with the process that said it.

Only what goes through :mod:`medialib.lib.runlog` is kept: a tool's replayed
output and a crash's traceback are in the error log instead.
"""

from __future__ import annotations

import os
import shlex

from medialib import commands
from medialib.lib import errorlog, runcontext

__all__ = ["INFO_DIR", "LEVELS", "level", "current_file", "keep"]

INFO_DIR = "info"

LEVEL_VARIABLE = "infoLogLevel"
DEFAULT_LEVEL = "normal"

# The words the setting takes, as the runlog levels they keep; -1 keeps nothing.
LEVELS = {"off": -1, "quiet": 0, "normal": 1, "verbose": 2}

# This process's open file, kept open for the run: (pid, path, handle). The
# pid is there because a forked worker inherits it and must open its own.
_OPEN: list = []


def level() -> int:
    """The verbosity the info log keeps, as a runlog level; -1 for none. A
    word nobody meant is the default, so a typo still keeps the run's lines."""
    word = runcontext.get(LEVEL_VARIABLE).strip().lower()
    return LEVELS.get(word, LEVELS[DEFAULT_LEVEL])


def current_file() -> str:
    """Where this run's lines go: the error log's name, under `logs/info/`."""
    name = os.path.basename(errorlog.current_file())
    return os.path.join(commands.script_dir(), "logs", INFO_DIR, name)


def keep(text: str, needs: int) -> None:
    """Keep ``text`` - one or more console lines - if the info log's level is
    at least ``needs``. Never raises: the file is a copy of the console, and
    losing it is no reason to lose the run."""
    if level() < needs:
        return
    try:
        _write(text)
    except Exception:                       # never let the log cost the run
        pass


def _write(text: str) -> None:
    lines = [line.rstrip("\r") for line in text.strip("\n").split("\n")]
    if not any(line.strip() for line in lines):
        return
    prefix = "%s [%d] " % (errorlog._stamp(), os.getpid())
    body = "".join(prefix + line + "\n" for line in lines)
    handle = _handle()
    with runcontext.take_lock(handle):
        handle.write(errorlog.redact(body))
        handle.flush()


def _handle():
    path = current_file()
    if _OPEN and _OPEN[0][0] == os.getpid() and _OPEN[0][1] == path:
        return _OPEN[0][2]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = not os.path.exists(path)
    handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - kept open for the run
    _OPEN[:] = [(os.getpid(), path, handle)]
    if fresh:
        with runcontext.take_lock(handle):
            if handle.tell() == 0:
                handle.write(errorlog.redact(_header()))
                handle.flush()
    return handle


def _header() -> str:
    word = next(name for name, value in LEVELS.items() if value == level())
    run = os.environ.get(errorlog.RUN_VARIABLE, "") or shlex.join(
        [commands.current_program()])
    return ("run:      %s\nstarted:  %s    cwd: %s\nkeeps:    %s\n%s\n"
            % (run, os.environ.get(errorlog.STARTED_VARIABLE, ""),
               errorlog._cwd(), word, "=" * 78))
