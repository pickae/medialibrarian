"""The run's console: one per run, shared by every thread and worker of it.

A run prints from many places at once - its own thread, threads beside it,
worker processes - and keeps a status row pinned under what scrolls past. The
:class:`Console` is what all of them print through:

- its LOCK serialises the writers across processes (``flock`` on a file in the
  console's directory) and threads (a lock per directory in each process), so
  a line never lands on the end of another or of the row;
- its COUNTERS are small files beside the lock, bumped under it, so a position
  in the queue is the same number whichever worker prints it;
- its ROW is drawn under the same lock, erased for every line and drawn again
  under it;
- its BLOCKS keep a film's lines together. Work names what it is about with
  :func:`about` - the item, and the lane it runs in when several kinds of work
  run at once. On a terminal a lane's lines about one item are held and
  written as one block when the item is done, and what each lane is doing is
  the row's to say; off a terminal, or under ``-v``, each line is written as
  it comes and names its lane and item in front instead.

A Console is pickled to the workers with the rest of a run's state. What
travels is the directory, the row's geometry and the row's render; a worker
draws the row the parent settled rather than settling one of its own.
"""

import contextlib
import os
import re
import sys
import threading

from medialib.lib import runlog, statusline

__all__ = ["Console", "LaneRow", "about", "active", "current", "hide_active",
           "subject",
           "write"]


# One lock per console directory in this process, with how deep the thread
# holding it is: the file lock is taken by the outermost hold only, because a
# second flock on a fresh descriptor of the same file would wait on the first.
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()

# What the work on each thread is about: a stack of [item, lane, held], where
# held is the list of lines kept back for the block an entry opened, or None.
_ABOUT = threading.local()

# The leader runlog writes in front of a line, which the lane and item go after.
_LEADER = re.compile(r"(==> |\[\d\d:\d\d:\d\d\] )?")


class _DirectoryLock:
    def __init__(self) -> None:
        self.guard = threading.RLock()
        self.depth = 0
        self.fd = -1


def _lock_for(directory: str) -> _DirectoryLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(directory)
        if lock is None:
            lock = _LOCKS[directory] = _DirectoryLock()
        return lock


def active() -> "Console | None":
    """The console this process writes its lines through, or None."""
    return runlog._console


def current() -> "Console":
    """The run's console in this process, made and made active the first time
    it is asked for when nothing has made one yet."""
    console = runlog._console
    return console if console is not None else Console().activate()


def hide_active() -> None:
    """Take down the row of this process's console, if it shows one: what a
    run's cleanup calls on every way out."""
    console = runlog._console
    if console is not None:
        console.hide()


def write(text: str) -> None:
    """``text`` on stderr, through the run's console when it has one."""
    console = runlog._console
    if console is None:
        sys.stderr.write(text)
        sys.stderr.flush()
    else:
        console.write(text)


class Console:
    """One run's console. ``directory`` holds its lock and counters; "" is a
    console no other process shares, locked within this one only."""

    def __init__(self, directory: str = "", render=None) -> None:
        self.directory = directory
        # None is this process's own settlement, read as it stands; a worker
        # holds the one its parent had.
        self._geometry: tuple | None = None
        self.render = render
        # Whether a row is pinned: a worker erases it for its lines whether or
        # not it can draw it again.
        self.pinned = False
        self._init_local()

    def _init_local(self) -> None:
        self._local_render = None
        self._refresher: threading.Thread | None = None
        self._stop: threading.Event | None = None

    def __getstate__(self) -> dict:
        return {"directory": self.directory, "_geometry": self.geometry,
                "render": self.render, "pinned": self.pinned}

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._init_local()

    @property
    def geometry(self) -> tuple:
        """``(row, cols, interval)`` the row is drawn against."""
        if self._geometry is None:
            return statusline.status_geometry()
        return self._geometry

    # --- the lock ------------------------------------------------------------

    @property
    def lock_path(self) -> str:
        return os.path.join(self.directory, "lock") if self.directory else ""

    @contextlib.contextmanager
    def locked(self):
        """The console to this thread alone for the block, across every
        process of the run. Re-entrant; the file lock is a no-op where flock
        is not in play."""
        lock = _lock_for(self.directory)
        with lock.guard:
            lock.depth += 1
            try:
                if lock.depth == 1 and self.directory and runlog.have_flock():
                    lock.fd = self._take_file_lock()
                yield
            finally:
                lock.depth -= 1
                if lock.depth == 0 and lock.fd >= 0:
                    os.close(lock.fd)
                    lock.fd = -1

    def _take_file_lock(self) -> int:
        import fcntl

        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return -1
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            pass
        return fd

    # --- the counters --------------------------------------------------------

    def _counter(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def value(self, name: str) -> int:
        """A counter's value; one nobody has bumped, or one caught mid-rewrite,
        reads as 0."""
        if not self.directory:
            return 0
        try:
            with open(self._counter(name)) as handle:
                return int(handle.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def set(self, name: str, value: int) -> None:
        with self.locked(), open(self._counter(name), "w") as handle:
            handle.write("%d\n" % value)

    def count(self, name: str = "position", amount: int = 1) -> int:
        """Add ``amount`` to a counter and return what it now holds."""
        with self.locked():
            value = self.value(name) + amount
            self.set(name, value)
        return value

    # --- lines ---------------------------------------------------------------

    def write(self, text: str, out=None) -> None:
        """``text`` on rows of its own: the row erased first and drawn again
        under it, all under the console's lock."""
        out = sys.stderr if out is None else out
        with self.locked():
            self._erase()
            out.write(text)
            out.flush()
            self._repin()

    def counted(self, line: str, total=None, name: str = "position",
                always: bool = False, after: str = "") -> int:
        """Bump the position and print ``line`` behind its "[n/total] ",
        both under one hold of the lock so the numbers come out in order.
        ``always`` is a line said whatever the verbosity - a failure's - and
        ``after`` is written under it, as it is."""
        with self.locked():
            number = self.count(name)
            text = "%s%s\n%s" % (runlog.counted_prefix(number, total), line,
                                 after)
            self.say(text, always)
        return number

    def say(self, text: str, always: bool = False) -> None:
        """``text`` as :func:`runlog.say` says it, through this console:
        left out under -q unless ``always``."""
        if always:
            self.emit(text)
            return
        if runlog.verbosity() >= runlog.NORMAL:
            self.emit(text)
        runlog.keep(text)

    def emit(self, text: str) -> None:
        """A line of runlog's for the console: held for its item's block, or
        written now with its lane and item in front, or simply written."""
        entry = _lane_entry(getattr(_ABOUT, "stack", None))
        if entry is None:
            self.write(text)
            return
        if self._blocks():
            entry[2].append(text)
            return
        self.write(_prefixed(text, entry))

    def _blocks(self) -> bool:
        return bool(self.geometry[0]) and runlog.verbosity() < runlog.VERBOSE

    # --- the row -------------------------------------------------------------

    def _row(self) -> bool:
        return bool(self.geometry[0]) and runlog.verbosity() >= runlog.NORMAL

    def _erase(self) -> None:
        if self.pinned and self._row():
            sys.stderr.write("\r\033[K")

    def _draw(self, text: str) -> None:
        if runlog.verbosity() < runlog.NORMAL:
            return
        if self.geometry[0]:
            width = self.geometry[1] - statusline.STATUS_ROW_MARGIN
            sys.stderr.write("\r%s\033[K" % text[:width])
        else:
            sys.stderr.write("%s\n" % text)
        sys.stderr.flush()

    def _render(self):
        render = self._local_render or self.render
        return None if render is None else render()

    def _repin(self) -> None:
        if not (self.pinned and self._row()):
            return
        text = self._render()
        if text is not None:
            self._draw(text)

    def tick(self) -> None:
        """One refresh of the row, under the lock."""
        with self.locked():
            text = self._render()
            if text is not None:
                self._draw(text)

    def activate(self) -> "Console":
        """Make this the console this process writes its lines through."""
        runlog._console = self
        return self

    def show(self, render=None, travels: bool = True) -> None:
        """Pin the row ``render`` draws, and keep it fresh on a thread of this
        process. ``travels`` False is a render that cannot be pickled - one
        over this process's memory - and a worker then leaves the row to the
        refresher rather than drawing it."""
        self.hide()
        if render is not None:
            if travels:
                self.render = render
            else:
                self._local_render = render
        self.pinned = True
        self.activate()
        self.tick()
        interval = self.geometry[2]
        stop = threading.Event()

        def refresh() -> None:
            while not stop.wait(interval):
                self.tick()

        self._stop = stop
        self._refresher = threading.Thread(target=refresh, daemon=True,
                                           name="console-row")
        self._refresher.start()

    def hide(self) -> None:
        """Stop the refresher and leave the row behind as its last word,
        lines below it written plainly. Safe when no row is shown."""
        if self._refresher is None:
            return
        if self._stop is not None:
            self._stop.set()
        self._refresher.join()
        self._refresher = None
        self._stop = None
        self.render = None
        self._local_render = None
        self.pinned = False
        if self._row():
            sys.stderr.write("\n")
            sys.stderr.flush()


def _lane_entry(stack):
    """The outermost entry on the stack that has a lane, or None."""
    for entry in stack or ():
        if entry[1]:
            return entry
    return None


def _name(item: str) -> str:
    """An item as a line names it: a film by its file's stem."""
    base = os.path.basename(os.path.normpath(str(item)))
    stem, extension = os.path.splitext(base)
    return stem if extension else base


def _prefixed(text: str, entry) -> str:
    item, lane = entry[0], entry[1]
    match = _LEADER.match(text)
    lead = match.group(0) if match else ""
    named = "[%s] %s: " % (lane, _name(item)) if item else "[%s] " % lane
    return lead + named + text[len(lead):]


def subject() -> tuple:
    """The (item, lane) the work on this thread is about, innermost first;
    ("", "") when nothing has said."""
    stack = getattr(_ABOUT, "stack", None)
    return (stack[-1][0], stack[-1][1]) if stack else ("", "")


@contextlib.contextmanager
def about(item: str, lane: str = ""):
    """The lines logged on this thread inside the block are about ``item``,
    worked on in ``lane``. Under a lane, on a terminal, they are written as
    one block when the outermost lane's block ends - an exception included.

    The entry is taken off the stack it was put on, by identity: a generator
    that holds the block across its yields may be closed on another thread.
    """
    stack = getattr(_ABOUT, "stack", None)
    if stack is None:
        stack = _ABOUT.stack = []
    entry: list = [item, lane,
                   [] if lane and _lane_entry(stack) is None else None]
    stack.append(entry)
    try:
        yield
    finally:
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] is entry:
                del stack[index]
                break
        if entry[2]:
            write("".join(entry[2]))


class LaneRow:
    """The row of a run whose lanes run side by side: each lane's progress,
    ``opus 12/40 · whisper 3/7 · remux 2 · subs 18/40``. A lane shows once it
    has a count, and as ``done/total`` once it knows its total. Bumped from
    any thread of the process that draws it."""

    def __init__(self, lanes: tuple) -> None:
        self._order = tuple(lanes)
        self._done: dict = {}
        self._total: dict = {}
        self._guard = threading.Lock()

    def total(self, lane: str, total: int | None = None, add: int = 0) -> None:
        with self._guard:
            if total is not None:
                self._total[lane] = total
            if add:
                self._total[lane] = self._total.get(lane, 0) + add
            self._done.setdefault(lane, 0)

    def done(self, lane: str, amount: int = 1) -> None:
        with self._guard:
            self._done[lane] = self._done.get(lane, 0) + amount

    def render(self) -> str | None:
        with self._guard:
            parts = []
            for lane in self._order:
                if lane not in self._done:
                    continue
                if lane in self._total:
                    parts.append("%s %d/%d" % (lane, self._done[lane],
                                               self._total[lane]))
                else:
                    parts.append("%s %d" % (lane, self._done[lane]))
        return "  " + " · ".join(parts) if parts else None
