"""The live status row.

The converting wrappers keep one line pinned at the bottom of the console while
their per-file progress reports scroll past above it. A caller supplies the row's
CONTENT as a render function that yields one line; everything about where that line
goes and how often it is refreshed lives here.

The row is a terminal affordance: on a tty each refresh is a carriage return plus an
erase-to-end-of-line laid exactly over the last one, truncated short of the terminal
width by ``STATUS_ROW_MARGIN`` (a terminal that wraps on its final column would park
the cursor on a row of its own). Off a terminal there is no in-place row - the same
text becomes an ordinary line, appended far more rarely, so a log gets an occasional
heartbeat rather than one refresh per tick.
"""

import os
import subprocess
import sys
import threading

from medialib.lib import runlog

__all__ = [
    "STATUS_ROW_MARGIN",
    "state",
    "init_status_line",
    "draw_status",
    "clear_status",
    "end_status",
    "repin_status",
    "status_tick",
    "status_monitor",
    "start_status_monitor",
    "stop_status_monitor",
    "shorten_path",
]

# The columns at the end of the row that no text may occupy: bash's
# ``export statusRowMargin=1``. A terminal that wraps on its final column would put
# the cursor on a row of its own choosing, so the row keeps off the last column.
STATUS_ROW_MARGIN = 1


class _State:
    """The settled row, the way bash holds it in shell globals.

    ``row`` is ``STATUS_ROW`` (empty off a terminal, ``"1"`` on one), ``cols`` is
    ``STATUS_COLS``, ``interval`` is ``statusInterval`` seconds between refreshes,
    and ``mon_pid`` is ``STATUS_MON_PID`` - the background refresher's handle.
    """

    def __init__(self):
        self.row = ""
        self.cols = 1000
        self.interval = 30
        self.mon_pid = None
        self._mon_stop = None


state = _State()


def _is_tty() -> bool:
    try:
        return os.isatty(2)
    except OSError:
        return False


def _stty_cols() -> str:
    """The columns field of ``stty size``, or "" when stty cannot answer.

    bash asks ``stty size 2>/dev/null <&2``; in that redirection order stty's stdin
    is ``/dev/null`` (the ``2>/dev/null`` lands first), so this reads ``/dev/null``
    the same way - which is why a real stty comes up empty and the width falls to
    ``tput``, while a stub on PATH still answers its knob. The answer is
    ``rows cols``; only the columns are wanted.
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
    # bash's ${size##* } : everything after the last space ("" when there is none)
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


def draw_status(text: str) -> int:
    """Draw or redraw the row.

    On a terminal: back to the start of the console row, print, then erase whatever a
    previous, longer refresh left to the right of it - the text truncated short of
    the terminal width by ``STATUS_ROW_MARGIN``. Off one: one ordinary line.
    """
    if state.row:
        width = state.cols - STATUS_ROW_MARGIN
        sys.stderr.write("\r%s\033[K" % text[:width])
    else:
        sys.stderr.write("%s\n" % text)
    sys.stderr.flush()
    return 0


def clear_status() -> int:
    """Erase the row, leaving the cursor at the start of an empty console row.

    A line that is about to scroll past then lands on a clean row rather than half
    way along the row's text. Off a terminal the heartbeat lines are ordinary output
    that nothing has to make way for, so this is a no-op.
    """
    if state.row:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
    return 0


def end_status() -> int:
    """Leave the row where it is and move on to a fresh console row.

    Whatever prints next starts on its own line instead of overwriting the last thing
    the row said. Off a terminal there is no row to end.
    """
    if state.row:
        sys.stderr.write("\n")
        sys.stderr.flush()
    return 0


def repin_status(render) -> int:
    """Put the row back, right after a scrolling line was printed over it.

    ``render`` yields the row's text the way the bash render function prints it.
    Deliberately a no-op off a terminal: there the row is an occasional heartbeat
    rather than a fixed row, and re-printing it after every line would drown the log.
    """
    if not state.row:
        return 0
    text = render()
    if text is None:
        return 0
    return draw_status(text)


def _take_lock(lock_file: str):
    """bash's ``takeLock`` on the row's console lock: serialise against the parallel
    workers' own printing. A no-op where flock is not in play.
    """
    if not runlog.have_flock():
        return
    import fcntl
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    finally:
        os.close(fd)


def status_tick(lock_file: str, render) -> int:
    """One refresh: render the row and draw it.

    ``lock_file`` is the file whose lock serialises the console against the workers
    that print the scrolling lines; pass "" when nothing else prints concurrently.
    """
    text = render()
    if text is None:
        return 0
    if lock_file:
        _take_lock(lock_file)
        draw_status(text)
    else:
        draw_status(text)
    return 0


def status_monitor(lock_file: str, render, stop_event: threading.Event):
    """bash's ``statusMonitor``: refresh every ``state.interval`` seconds until
    ``stop_event`` is set.

    The first refresh comes after an interval, not immediately - the caller has
    already drawn the row once. Stopping is a signal in the ordinary course of things
    (the work the row reports on finished), not an interrupt, so it ends quietly.
    """
    while not stop_event.wait(state.interval):
        status_tick(lock_file, render)


def start_status_monitor(lock_file: str, render) -> int:
    """Start the background refresher.

    The row is drawn once synchronously first, so it is there from the moment the work
    starts rather than after the first interval (and so stopping always has a row to
    finish off). The refresher's handle is left in ``state.mon_pid`` for the caller to
    stop it.
    """
    stop_status_monitor()
    status_tick(lock_file, render)
    stop_event = threading.Event()
    thread = threading.Thread(target=status_monitor,
                              args=(lock_file, render, stop_event),
                              daemon=True)
    thread.start()
    state.mon_pid = thread
    state._mon_stop = stop_event
    return 0


def stop_status_monitor() -> int:
    """Stop the refresher and finish the row off with a newline.

    Safe to call when none is running, and always returns 0 so a cleanup that runs
    twice never trips.
    """
    if state.mon_pid is None:
        return 0
    state._mon_stop.set()
    state.mon_pid.join()
    state.mon_pid = None
    state._mon_stop = None
    end_status()
    return 0


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