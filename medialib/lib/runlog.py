"""What a run says about itself while it works.

One line to stderr per step, timestamped only when ``LOG_TIMESTAMPS`` is set, and
a "[n/total] " counter that appears only when flock is there to keep it honest.
The eighteen commands say these things the same way, which is what lets one
command read another's output.

How much is said is the run's verbosity, one of three levels settled as
``LOG_VERBOSITY``: quiet (errors only), normal (what a run has always said),
verbose (that plus the reason behind each decision). Settled on the run
(`medialib/lib/runcontext.py`) rather than passed as an argument, because the
workers a run fans out to and the commands it starts as children have to say
the same amount: a worker is handed the run, a child its environment.

Every line said here is also kept in the run's info log under `logs/info/`, at
a verbosity of its own; see "the info log" below.
"""

import os
import shlex
import sys
import threading
import time
import traceback

from medialib import commands
from medialib.lib import errorlog, runcontext
from medialib.lib.runcontext import can_lock, have_flock, settle_flock, take_lock

__all__ = ["log", "say", "result", "detail", "warn", "error", "error_continued",
           "verbosity", "keep",
           "settle_verbosity",
           "QUIET", "NORMAL", "VERBOSE", "counted_prefix", "film_header",
           "cpu_count", "can_lock", "have_flock", "settle_flock", "take_lock",
           "warn_uncounted_progress", "keep", "start_run", "record_crash",
           "install_hooks"]

QUIET, NORMAL, VERBOSE = 0, 1, 2

VERBOSITY_VARIABLE = "LOG_VERBOSITY"

_LEVEL_WORDS = {QUIET: "quiet", NORMAL: "", VERBOSE: "verbose"}


def settle_verbosity(level: int) -> None:
    """Set the run's verbosity, for this process and everything it starts.

    Normal is written as an empty value rather than removed, so a parent's
    "quiet" or "verbose" cannot outlive a child that asked for normal.
    """
    runcontext.settle(VERBOSITY_VARIABLE, _LEVEL_WORDS[level])


def verbosity() -> int:
    """The run's verbosity. Anything but the two words is normal, so a value
    nobody set and a value nobody meant both leave a run saying what it always
    said."""
    value = runcontext.get(VERBOSITY_VARIABLE)
    if value == "quiet":
        return QUIET
    if value == "verbose":
        return VERBOSE
    return NORMAL


def keep(text: str, needs: int = NORMAL) -> None:
    """``text`` into the run's info log, as a console line of level
    ``needs`` - for a line written to the console some other way."""
    _keep(text, needs, None)


def _keep(text: str, needs: int, stream) -> None:
    """``text`` into the run's info log when it
    went to the console and the log keeps lines of level ``needs``. A line
    written into some other stream is a piece of a report, not the console."""
    if stream is None or stream is sys.stderr or stream is sys.stdout:
        _info_keep(text, needs)


def _say_line(message: tuple, stream, needs: int) -> None:
    """One line: on the console when the run's verbosity reaches ``needs``,
    and into the info log when its own level does."""
    text = " ".join(str(part) for part in message)
    if verbosity() >= needs:
        _write_text(text, stream)
    _keep(text, needs, stream)


def _write_text(text: str, stream) -> None:
    """One line on stderr, timestamped only when the run asked for it."""
    if runcontext.get("LOG_TIMESTAMPS"):
        line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), text)
    else:
        line = "==> %s\n" % text
    _emit(line, stream)


def _emit(text: str, stream) -> None:
    out = sys.stderr if stream is None else stream
    console = runcontext.current().console
    if console is not None and out is sys.stderr:
        console.emit(text)
    else:
        out.write(text)


def log(*message: object, stream=None) -> None:
    """``log``: one line on stderr, timestamped only when the run asked for it.

    The arguments are joined with spaces. Silent under ``-q``, so a line that
    has to survive it - an error - goes through :func:`error` instead.
    """
    _say_line(message, stream, NORMAL)


def say(text: str, stream=None) -> None:
    """``text`` written as it is - stderr unless ``stream`` says otherwise -
    unless the run is quiet. For a command's own progress lines and closing
    figures, which -q leaves out; never for a result, which no verbosity
    changes and which belongs on stdout, nor for an error, which every
    verbosity prints."""
    if verbosity() >= NORMAL:
        _emit(text, stream)
    _keep(text, NORMAL, stream)


def result(*message: object, stream=None) -> None:
    """A :func:`log` line that is the run's result rather than its progress -
    a dry run's account of what it would change - printed at every
    verbosity."""
    _say_line(message, stream, QUIET)


def detail(*message: object, stream=None) -> None:
    """A line only ``-v`` prints: why an item was handled the way it was.

    One whole line per call, like :func:`log`, which is what lets workers print
    these as they go without their lines mixing.
    """
    _say_line(message, stream, VERBOSE)


def warn(*message: object, stream=None) -> None:
    """A ``WARNING:`` line: printed unless the run is quiet."""
    _say_line(("WARNING:",) + message, stream, NORMAL)


def error(*message: object, stream=None, kind: str = "error",
          files=(), persist: bool = True) -> None:
    """An ``ERROR:`` line, printed at every verbosity - ``-q`` included.

    Every one that reaches the console is also kept in the run's error log
    (`medialib/lib/errorlog.py`) as ``kind``, with ``files`` named in it, so
    the two never disagree about what went wrong. A line written into some
    other stream is a piece of a larger report, kept by whoever writes that.
    """
    _say_line(("ERROR:",) + message, stream, QUIET)
    if persist and (stream is None or stream is sys.stderr):
        errorlog.record(kind, " ".join(str(part) for part in message),
                        files=files)


def error_continued(*message: object, stream=None) -> None:
    """A line under an :func:`error` that goes on explaining it, printed
    wherever the error is."""
    _say_line(message, stream, QUIET)


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
    if runcontext.get("UNCOUNTED_PROGRESS_WARNED"):
        return
    runcontext.settle("UNCOUNTED_PROGRESS_WARNED", "1")
    log("WARNING: flock not installed (it ships in util-linux) - progress lines "
        "will be printed one per item")
    log('         without their "[n/total]" position, and the run\'s closing '
        "counts may read low. The")
    log("         work itself is unaffected; nothing is skipped and "
        "nothing is at risk.")


# --- the info log -----------------------------------------------------------------
#
# What a run said on the console, kept: one file per run under `logs/info/`.
#
# The error log keeps only what went wrong; this keeps the run's account of
# itself - the progress lines, the warnings, the closing figures, and under
# ``verbose`` the reason behind each decision - for reading after the console
# has scrolled away or the terminal was closed.
#
# How much is kept is the ``infoLogLevel`` setting, chosen apart from the
# console's ``-q`` and ``-v``: ``off``, ``quiet`` (errors and results only),
# ``normal`` (what a run says by default) or ``verbose`` (that plus ``-v``'s
# lines). So a run can say little on the console and still keep everything.
#
# The file takes the error log's name - ``20261105-140211-convert-audio-4242.log``
# - so the two files of one run sit side by side under the same name, and like
# it is made only when the first line arrives. The workers a run fans out to
# and the commands it starts write into the same file, each line one write
# under the run's lock, stamped to the millisecond with the process that said
# it.
#
# Only what goes through this module is kept: a tool's replayed output and a
# crash's traceback are in the error log instead.

INFO_DIR = "info"

INFO_LEVEL_VARIABLE = "infoLogLevel"
_INFO_DEFAULT = "normal"

# The words the setting takes, as the levels they keep; -1 keeps nothing.
INFO_LEVELS = {"off": -1, "quiet": QUIET, "normal": NORMAL, "verbose": VERBOSE}

# This process's open file, kept open for the run: (pid, path, handle). The
# pid is there because a forked worker inherits it and must open its own.
_INFO_OPEN: list = []


def info_level() -> int:
    """The verbosity the info log keeps, as a level; -1 for none. A word
    nobody meant is the default, so a typo still keeps the run's lines."""
    word = runcontext.get(INFO_LEVEL_VARIABLE).strip().lower()
    return INFO_LEVELS.get(word, INFO_LEVELS[_INFO_DEFAULT])


def info_file() -> str:
    """Where this run's lines go: the error log's name, under `logs/info/`."""
    name = os.path.basename(errorlog.current_file())
    return os.path.join(commands.script_dir(), "logs", INFO_DIR, name)


def _info_keep(text: str, needs: int) -> None:
    """Keep ``text`` - one or more console lines - if the info log's level is
    at least ``needs``. Never raises: the file is a copy of the console, and
    losing it is no reason to lose the run."""
    if info_level() < needs:
        return
    try:
        _info_write(text)
    except Exception:                       # never let the log cost the run
        pass


def _info_write(text: str) -> None:
    lines = [line.rstrip("\r") for line in text.strip("\n").split("\n")]
    if not any(line.strip() for line in lines):
        return
    prefix = "%s [%d] " % (errorlog._stamp(), os.getpid())
    body = "".join(prefix + line + "\n" for line in lines)
    handle = _info_handle()
    with runcontext.take_lock(handle):
        handle.write(errorlog.redact(body))
        handle.flush()


def _info_handle():
    path = info_file()
    if (_INFO_OPEN and _INFO_OPEN[0][0] == os.getpid()
            and _INFO_OPEN[0][1] == path):
        return _INFO_OPEN[0][2]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = not os.path.exists(path)
    handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - kept open for the run
    _INFO_OPEN[:] = [(os.getpid(), path, handle)]
    if fresh:
        with runcontext.take_lock(handle):
            if handle.tell() == 0:
                handle.write(errorlog.redact(_info_header()))
                handle.flush()
    return handle


def _info_header() -> str:
    word = next(name for name, value in INFO_LEVELS.items()
                if value == info_level())
    run = os.environ.get(errorlog.RUN_VARIABLE, "") or shlex.join(
        [commands.current_program()])
    return ("run:      %s\nstarted:  %s    cwd: %s\nkeeps:    %s\n%s\n"
            % (run, os.environ.get(errorlog.STARTED_VARIABLE, ""),
               errorlog._cwd(), word, "=" * 78))


# --- a crash ----------------------------------------------------------------------

def start_run(argv: list | None = None) -> None:
    """Settle this run's error log (`medialib/lib/errorlog.py`), for this
    process and everything it starts, and install the hooks that keep a
    crash."""
    errorlog.settle_run(argv)
    install_hooks()


def record_crash(exc: BaseException, where: str = "") -> str:
    """Keep an exception nobody caught, and say it on the console: one line,
    the traceback under -v, and the whole of it always when it could not be
    kept - a traceback is never lost to a full disk."""
    kind = errorlog.crash_kind(exc)
    summary = "%s: %s" % (type(exc).__name__, exc) if str(exc) else type(exc).__name__
    if where:
        summary = "%s: %s" % (where, summary)
    path = errorlog.record(kind, summary, files=errorlog.exception_files(exc),
                           exc=exc, stack=False)
    error(summary, persist=False)
    if path and verbosity() < VERBOSE:
        error_continued("       the traceback is kept in %s" % path)
    else:
        sys.stderr.write("".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)))
        if path:
            error_continued("       and kept in %s" % path)
    sys.stderr.flush()
    return path


_HOOKED = False


def install_hooks() -> None:
    """Keep what nobody caught - in the main thread and in any other - before
    Python prints it. An interrupt is not an error and is left alone."""
    global _HOOKED
    if _HOOKED:
        return
    _HOOKED = True
    previous = sys.excepthook

    def hook(kind, value, tb):
        if issubclass(kind, KeyboardInterrupt | SystemExit) or value is None:
            previous(kind, value, tb)
            return
        if value.__traceback__ is None:
            value = value.with_traceback(tb)
        record_crash(value)

    def thread_hook(args):
        if args.exc_type is SystemExit or args.exc_value is None:
            return
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        name = args.thread.name if args.thread is not None else "a thread"
        record_crash(args.exc_value, where="in %s" % name)

    sys.excepthook = hook
    threading.excepthook = thread_hook
