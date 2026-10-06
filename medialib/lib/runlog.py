"""What a run says about itself while it works.

One line to stderr per step, timestamped only when ``LOG_TIMESTAMPS`` is set, and
a "[n/total] " counter that appears only when flock is there to keep it honest.
The eighteen commands say these things the same way, which is what lets one
command read another's output.

How much is said is the run's verbosity, one of three levels read from
``LOG_VERBOSITY``: quiet (errors only), normal (what a run has always said),
verbose (that plus the reason behind each decision). An environment variable
rather than an argument because the workers a run fans out to and the commands
it starts as children have to say the same amount, and the environment is the
one thing all of them inherit.
"""

import contextlib
import os
import shutil
import sys
import time

__all__ = ["log", "detail", "warn", "error", "verbosity", "settle_verbosity",
           "QUIET", "NORMAL", "VERBOSE", "counted_prefix", "film_header",
           "cpu_count", "can_lock", "have_flock", "settle_flock", "take_lock",
           "warn_uncounted_progress"]

QUIET, NORMAL, VERBOSE = 0, 1, 2

VERBOSITY_VARIABLE = "LOG_VERBOSITY"
_LEVEL_WORDS = {QUIET: "quiet", NORMAL: "", VERBOSE: "verbose"}


def can_lock() -> bool:
    """Whether this host can serialise the shared counters at all.

    Two ways, and either will do. ``flock(1)`` on PATH stays the first one
    asked, so a Linux box that has had the tool
    taken off it still reads as it always did. Failing that, the C library's
    own ``flock`` - which is what :func:`take_lock` actually calls - and that
    is the rung macOS lands on: it has never shipped the util-linux command
    line tool, but the system call has been there all along, so refusing to
    count on a Mac would have declined a facility it was already using.

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
    """``HAVE_FLOCK``, probed now and left in the environment for the children.

    A run settles it at startup so the workers it fans out to inherit one answer
    rather than each probing for their own; a run whose halves disagreed about it
    would print two different kinds of progress line. Correctness does not depend
    on the call - :func:`have_flock` settles it on first read either way - so a
    command that takes no lock and starts no worker need not make it.
    """
    have = "1" if can_lock() else ""
    os.environ["HAVE_FLOCK"] = have
    return have


def have_flock() -> bool:
    """Whether this run locks. The only reader of ``HAVE_FLOCK``.

    An answer already in the environment stands, including an empty one: that is
    a parent's decision handed down, or a test asking for the countless path.
    Nothing there at all is nobody having asked yet, and the probe answers it.

    One reader, because a direct read cannot tell an unsettled variable from a
    settled "no" - and answering "no" on a host with flock costs the run its
    progress positions and makes its closing counts read low.
    """
    value = os.environ.get("HAVE_FLOCK")
    return bool(settle_flock() if value is None else value)


@contextlib.contextmanager
def take_lock(handle):
    """``takeLock``: hold the lock on an open file for the block, or hold nothing.

    Gated on HAVE_FLOCK rather than simply calling fcntl unconditionally, and
    the gate is what keeps the two halves of a run agreeing: a process that
    locked while its siblings printed the countless progress line would count
    correctly and say something else. The variable is the whole run's one
    answer, so a parent that settled "no" is obeyed here even on a host that
    could lock. Never raises, so the block's body runs either way.
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


def settle_verbosity(level: int) -> None:
    """Set the run's verbosity, for this process and everything it starts.

    Normal is written as an empty value rather than removed, so a parent's
    "quiet" or "verbose" cannot outlive a child that asked for normal.
    """
    os.environ[VERBOSITY_VARIABLE] = _LEVEL_WORDS[level]


def verbosity() -> int:
    """The run's verbosity. Anything but the two words is normal, so a value
    nobody set and a value nobody meant both leave a run saying what it always
    said."""
    value = os.environ.get(VERBOSITY_VARIABLE, "")
    if value == "quiet":
        return QUIET
    if value == "verbose":
        return VERBOSE
    return NORMAL


def _write(message: tuple, stream) -> None:
    """One line on stderr, timestamped only when the run asked for it."""
    text = " ".join(str(part) for part in message)
    if os.environ.get("LOG_TIMESTAMPS", ""):
        line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), text)
    else:
        line = "==> %s\n" % text
    (sys.stderr if stream is None else stream).write(line)


def log(*message: object, stream=None) -> None:
    """``log``: one line on stderr, timestamped only when the run asked for it.

    The arguments are joined with spaces. Silent under ``-q``, so a line that
    has to survive it - an error - goes through :func:`error` instead.
    """
    if verbosity() >= NORMAL:
        _write(message, stream)


def detail(*message: object, stream=None) -> None:
    """A line only ``-v`` prints: why an item was handled the way it was.

    One whole line per call, like :func:`log`, which is what lets workers print
    these as they go without their lines mixing.
    """
    if verbosity() >= VERBOSE:
        _write(message, stream)


def warn(*message: object, stream=None) -> None:
    """A ``WARNING:`` line: printed unless the run is quiet."""
    if verbosity() >= NORMAL:
        _write(("WARNING:",) + message, stream)


def error(*message: object, stream=None) -> None:
    """An ``ERROR:`` line, printed at every verbosity - ``-q`` included."""
    _write(("ERROR:",) + message, stream)


def counted_prefix(count: int, total: int | None) -> str:
    """``countedPrefix``: the "[n/total] " a progress line carries, or nothing.

    Nothing without flock: the count is shared between workers through a lock
    file, and a host that cannot take the lock has no honest number to print -
    so it prints none rather than one per worker that each start at 1.

    A total of None is a run that has not finished counting its own queue - the
    dynamic queue does not know how many jobs it will hand out until the last of
    them is planned - and such a run prints the position without a denominator
    rather than one it would have to promise.
    """
    if not have_flock():
        return ""
    if total is None:
        return "[%d] " % count
    return "[%d/%d] " % (count, total)


def film_header(index: int, total: int, root: str, film: str) -> str:
    """The line each film of a walk over a folder opens with: where it stands
    in the folder, and the film spelled from it - so a film nothing is done to
    is still named, and the lines below it need not name it again.

    No flock is asked for, unlike :func:`counted_prefix`: one walk counts its
    own films in one process, and the number is honest without a lock.
    """
    return "[%d/%d] %s" % (index, total, os.path.relpath(film, root))


def cpu_count() -> int:
    """``cpuCount``: the logical cores, and never less than 1 - a host whose
    count cannot be read runs serially rather than not at all."""
    try:
        count = os.cpu_count()
    except NotImplementedError:      # pragma: no cover - os.cpu_count is total
        count = None
    return count if count and count > 0 else 1


def python_bin() -> str:
    """Which interpreter a helper is run with: ``PYTHON_BIN`` from the
    environment overrides everything, and otherwise it is the one running now.

    PATH stays authoritative, which is what keeps a test's stub interpreter in
    charge of the helpers a run shells out to.
    """
    return os.environ.get("PYTHON_BIN") or sys.executable


def jobs_per_core(divisor: int = 1) -> int:
    """``jobsPerCore``: how many jobs to run at once when one job wants
    ``divisor`` cores. Floored at 1, so a machine smaller than the divisor still
    runs serially rather than not at all."""
    count = cpu_count() // max(1, divisor)
    return count if count >= 1 else 1


def warn_uncounted_progress() -> None:
    """``warnUncountedProgress``: say once, at startup, what a missing flock costs.

    Once per RUN and not once per caller: several of these scripts reach this
    twice over, and a child has nothing to add to something the run has said.
    """
    if have_flock():
        return
    if os.environ.get("SKIP_TOOL_PREFLIGHT", ""):
        return
    if os.environ.get("UNCOUNTED_PROGRESS_WARNED", ""):
        return
    os.environ["UNCOUNTED_PROGRESS_WARNED"] = "1"
    log("WARNING: flock not installed (it ships in util-linux) - progress lines "
        "will be printed one per item")
    log('         without their "[n/total]" position, and the run\'s closing '
        "counts may read low. The")
    log("         work itself is unaffected; nothing is skipped and "
        "nothing is at risk.")
