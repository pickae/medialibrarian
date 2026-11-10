"""The run: what one run of a command has settled about itself, in one object.

A run settles things as it starts - how much it says, whether it can lock,
where it records an interrupt, its scratch base, its console - and every part
of it reads them back: its own threads, the worker processes it fans out to,
and the commands it starts as children. They are kept here, in one
:class:`Run` per process:

- a WORKER is handed the parent's run, pickled, and adopts it in one place
  (:func:`adopt`, called by `medialib/lib/workerpool.py` as the worker
  starts). It reads nothing of this from the environment: a worker started
  from a forkserver has the forkserver's environment, which is the parent's as
  it was when the forkserver started, not as it is now;
- a CHILD COMMAND is a fresh run, and the environment is how it is handed the
  parent's: every value settled here is also written there, and a value this
  run did not settle is read from there - which is the parent's hand-down.

Nothing here imports the rest of the package, so every module can import it.
"""

import contextlib
import os
import shutil
import threading
import typing

__all__ = ["Run", "current", "adopt", "snapshot", "reset", "settle", "get",
           "forget", "can_lock", "settle_flock", "have_flock", "take_lock",
           "abort_flag", "abort_requested", "request_abort", "label",
           "working_on", "subject", "child_environment",
           "ITEM_VARIABLE", "INPUT_VARIABLE"]

# The values a parent command hands its run down by, which a worker is handed
# whether or not this process settled them itself: read here from the
# environment once, when the run is pickled, rather than by the worker.
HANDED_DOWN = (
    "LOG_VERBOSITY", "LOG_TIMESTAMPS", "HAVE_FLOCK", "UNCOUNTED_PROGRESS_WARNED",
    "ABORT_FLAG", "ABORT_FLAG_OWNER", "SAFETY_LOG", "LENGTH_LOG",
    "ERROR_LOG_NAME", "ERROR_LOG_RUN", "ERROR_LOG_STARTED", "infoLogLevel",
    "SKIP_TOOL_PREFLIGHT",
)

# The tmpfs every Linux host has, where a run's scratch goes first.
SHM = "/dev/shm"

# The item and input file a child command's errors are about, handed down.
ITEM_VARIABLE = "ERROR_LOG_ITEM"
INPUT_VARIABLE = "ERROR_LOG_INPUT"


class Terminal:
    """What the run settled about the terminal its status row is drawn on
    (`medialib/lib/statusline.py`): ``row`` is empty off a terminal and "1" on
    one, ``cols`` is the width, ``interval`` the seconds between refreshes."""

    def __init__(self) -> None:
        self.row = ""
        self.cols = 1000
        self.interval = 30


class Run:
    """One run's state in this process.

    ``values`` are the settled values by their environment names; ``console``
    is the run's console (`medialib/lib/console.py`); ``ram_base`` and
    ``scratch_bases`` are the scratch the run works in
    (`medialib/lib/ramscratch.py`); ``disk_turns`` is where the disks' turns
    are shared (`medialib/lib/diskturn.py`); ``terminal`` is the status
    row's geometry; ``failures`` are the abnormal worker exits this process
    reaped (`medialib/lib/workerpool.py`).
    """

    def __init__(self) -> None:
        self.values: dict = {}
        self.console: typing.Any = None
        self.ram_base: str | None = None
        self.scratch_bases: list = []
        self.disk_turns = ""
        self.terminal = Terminal()
        self.failures: list = []

    def __getstate__(self) -> dict:
        # What a worker is handed: everything but the failures, which are this
        # process's to report.
        state = dict(self.__dict__)
        state["failures"] = []
        values = dict(self.values)
        for name in HANDED_DOWN:
            if name not in values and name in os.environ:
                values[name] = os.environ[name]
        state["values"] = values
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)


_RUN = Run()


def current() -> Run:
    """This process's run."""
    return _RUN


def snapshot() -> Run:
    """The run as a worker is handed it."""
    return _RUN


def adopt(run: Run) -> None:
    """Make a run handed from a parent this process's own: the one place a
    worker takes on its parent's state. Its values are written into the
    environment too, as the hand-down to whatever the worker starts."""
    global _RUN
    _RUN = run
    for name, value in run.values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def reset() -> None:
    """A fresh run, for a process that runs more than one."""
    global _RUN
    _RUN = Run()


def settle(name: str, value: str) -> str:
    """Settle ``name`` for this run, and hand it down to its children."""
    _RUN.values[name] = value
    os.environ[name] = value
    return value


def forget(name: str) -> None:
    """Settle ``name`` as unset, for this run and its children."""
    _RUN.values[name] = None
    os.environ.pop(name, None)


def get(name: str, default=""):
    """``name`` as this run settled it, or as its parent handed it down when
    it did not; ``default`` when neither did."""
    values = _RUN.values
    if name in values:
        value = values[name]
        return default if value is None else value
    return os.environ.get(name, default)


# --- the lock ---------------------------------------------------------------------

def can_lock() -> bool:
    """Whether this host can serialise the shared counters at all.

    Two ways, and either will do. ``flock(1)`` on PATH stays the first one
    asked, so a Linux box that has had the tool taken off it still reads as it
    always did. Failing that, the C library's own ``flock`` - which is what
    :func:`take_lock` actually calls - and that is the rung macOS lands on: it
    has never shipped the util-linux command line tool, but the system call has
    been there all along.

    Windows reaches neither and answers no, which is the honest answer there:
    ``fcntl`` is a POSIX module and is not built into that interpreter.
    """
    if shutil.which("flock"):
        return True
    try:
        import fcntl  # noqa: F401 - probed, not called
    except ImportError:
        return False
    return hasattr(fcntl, "flock")


def settle_flock() -> str:
    """``HAVE_FLOCK``, probed now and settled for the run.

    A run settles it at startup so the workers it fans out to are handed one
    answer rather than each probing for their own; a run whose halves
    disagreed about it would print two different kinds of progress line.
    Correctness does not depend on the call - :func:`have_flock` settles it on
    first read either way.
    """
    return settle("HAVE_FLOCK", "1" if can_lock() else "")


def have_flock() -> bool:
    """Whether this run locks.

    An answer already settled or handed down stands, including an empty one:
    that is a parent's decision, or a test asking for the countless path.
    Nothing at all is nobody having asked yet, and the probe answers it - a
    direct read could not tell an unsettled value from a settled "no", and
    answering "no" on a host with flock costs the run its progress positions.
    """
    value = get("HAVE_FLOCK", None)
    return bool(settle_flock() if value is None else value)


@contextlib.contextmanager
def take_lock(handle):
    """Hold the lock on an open file for the block, or hold nothing.

    Gated on :func:`have_flock`, which is what keeps the halves of a run
    agreeing: a process that locked while its siblings printed the countless
    progress line would count correctly and say something else. Never raises,
    so the block's body runs either way.
    """
    locked = False
    if have_flock():
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError:
            locked = False
    try:
        yield
    finally:
        if locked:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


# --- the interrupt ----------------------------------------------------------------
# A FILE rather than a signal handler's variable: a worker is a separate
# process, and a variable one of them sets is a variable nobody else sees.

def abort_flag() -> str:
    """Where this run records an interrupt, or "" before it has settled one."""
    return get("ABORT_FLAG", "")


def abort_requested() -> bool:
    """Has this run been asked to stop?"""
    flag = abort_flag()
    return bool(flag) and os.path.exists(flag)


def request_abort() -> None:
    """Record an interrupt for the whole run.

    Never raises: it runs inside a signal handler, where a failure would take
    down the run it exists to end tidily.
    """
    flag = abort_flag()
    if not flag:
        return
    try:
        with open(flag, "ab"):
            pass
    except OSError:
        pass


# --- what the work in hand is about -------------------------------------------------

def label(item) -> str:
    """The name a piece of work goes by - a worker's, and the item its errors
    are about. A pair or a record is named by its first field: the commands
    hand their pools tuples of (input, output) and objects of their own, and
    the first field is the one that names the work in every case."""
    if isinstance(item, tuple | list) and item:
        item = item[0]
    text = item if isinstance(item, str) else str(item)
    return text[:200]


_SUBJECTS = threading.local()


def _subjects() -> list:
    stack = getattr(_SUBJECTS, "stack", None)
    if stack is None:
        stack = _SUBJECTS.stack = []
    return stack


@contextlib.contextmanager
def working_on(path: str, item: str = ""):
    """Name the input file the work inside the block is about, so an error
    recorded meanwhile - by a tool working on a RAM copy of it, say - still
    names the file on disk that was the culprit."""
    stack = _subjects()
    try:
        source = os.path.abspath(path) if path else ""
    except (TypeError, ValueError):
        source = str(path)
    stack.append((item or path, source))
    try:
        yield
    finally:
        stack.pop()


def subject() -> tuple:
    """The (item, input file) the work in hand is about, innermost first; what
    a parent command handed down when nothing in this one has said."""
    stack = _subjects()
    if stack:
        return stack[-1]
    return (os.environ.get(ITEM_VARIABLE, ""), os.environ.get(INPUT_VARIABLE, ""))


def child_environment(environment: dict) -> dict:
    """``environment`` for a child command, with the item and input file in
    hand handed down to it."""
    item, source = subject()
    if item:
        environment[ITEM_VARIABLE] = item
    if source:
        environment[INPUT_VARIABLE] = source
    return environment
