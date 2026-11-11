"""The status row's geometry: whether there is a terminal to pin a row on,
and how wide it is.

The row is a terminal affordance: on a tty each refresh is a carriage return plus an
erase-to-end-of-line laid exactly over the last one, truncated short of the terminal
width by ``STATUS_ROW_MARGIN`` (a terminal that wraps on its final column would park
the cursor on a row of its own). Off a terminal there is no in-place row - the same
text becomes an ordinary line, appended far more rarely, so a log gets an occasional
heartbeat rather than one refresh per tick. Drawing it, and keeping it out of the
way of the lines that scroll past, is the run's console's
(`medialib/lib/console.py`).
"""

import os
import subprocess

from medialib.lib import runcontext

__all__ = [
    "STATUS_ROW_MARGIN",
    "state",
    "init_status_line",
    "status_geometry",
    "shorten_path",
]

# The columns at the end of the row that no text may occupy. A terminal that
# wraps on its final column would put the cursor on a row of its own choosing,
# so the row keeps off the last column.
STATUS_ROW_MARGIN = 1


def _terminal_field(name: str) -> property:
    """``name`` on the run's terminal, as a property rather than through
    ``__getattr__``: whatever sets the attribute and later restores it -
    pytest's monkeypatch included - then writes the old value back instead
    of deleting it."""

    def get(self):
        return getattr(runcontext.current().terminal, name)

    def put(self, value) -> None:
        setattr(runcontext.current().terminal, name, value)

    return property(get, put)


class _State:
    """The settled row, kept by the run (`medialib/lib/runcontext.py`) so a
    worker is handed the one its parent settled: ``row`` is empty off a
    terminal and ``"1"`` on one, ``cols`` is the width, and ``interval`` is the
    seconds between refreshes."""

    __slots__ = ()

    row = _terminal_field("row")
    cols = _terminal_field("cols")
    interval = _terminal_field("interval")


state = _State()


def _is_tty() -> bool:
    try:
        return os.isatty(2)
    except OSError:
        return False


def _stty_cols() -> str:
    """The columns field of ``stty size``, or "" when stty cannot answer.

    stty's stdin is ``/dev/null``, which is why a real stty comes up empty and the
    width falls to ``tput``, while a stub on PATH still answers its knob. The
    answer is ``rows cols``; only the columns are wanted.
    """
    try:
        with open(os.devnull, "rb") as devnull:
            ran = subprocess.run(["stty", "size"], stdin=devnull,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        return ""
    if ran.returncode != 0:
        return ""
    size = ran.stdout.decode("utf-8", "replace").rstrip("\n")
    # Everything after the last space ("" when there is none)
    return size.rsplit(" ", 1)[-1]


def _tput_cols() -> str:
    """``tput cols`` - terminfo's idea of the width, or "" when it cannot answer."""
    try:
        ran = subprocess.run(["tput", "cols"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return ran.stdout.decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return ""


def _is_width(value: str) -> bool:
    return value != "" and all(c in "0123456789" for c in value)


def init_status_line() -> int:
    """Settle once how (and whether) the row can be drawn.

    On a terminal the row is redrawn in place every ``2`` seconds; off one it is an
    ordinary line every ``30``. The width is ``stty`` first (it reads the terminal
    stderr is on), ``tput`` second (terminfo), and ``80`` the floor - a figure
    ``stty`` or ``tput`` cannot vouch for is not trusted for a row that must stay on
    one console line. Off a terminal the width is one no line will reach, so the same
    code path never truncates a log line.
    """
    if _is_tty():
        state.row = "1"
        state.interval = 2
        cols = _stty_cols()
        if not _is_width(cols):
            cols = _tput_cols()
        if _is_width(cols) and int(cols) >= 40:
            state.cols = int(cols)
        else:
            state.cols = 80
    else:
        state.row = ""
        state.interval = 30
        state.cols = 1000
    return 0


def status_geometry() -> tuple:
    """What this process settled about the row, ``(row, cols, interval)``:
    what a run's console (`medialib/lib/console.py`) draws against, in this
    process and in every worker it is handed to."""
    return (state.row, state.cols, state.interval)


def shorten_path(max_len: str, text: str) -> str:
    """``text`` cut down to at most ``max_len`` characters by dropping its FRONT.

    The file name in a row is what gives way, which is why the front goes: what
    survives has to be the part that identifies the file, not the folders above it. A
    limit too small to carry the ``...`` marker just cuts; a limit that is not a
    number cuts to nothing rather than erroring.
    """
    digits = max_len if max_len != "" and all(
        c in "0123456789" for c in max_len) else "0"
    limit = int(digits)
    if limit < 4:
        return text[:limit]
    if len(text) > limit:
        return "..." + text[-(limit - 3):]
    return text