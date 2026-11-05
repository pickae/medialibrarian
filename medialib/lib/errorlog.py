"""The errors a run keeps: one file per run under `logs/error/`.

The console says little on purpose - several workers share it, and a tool's
whole output there would be several tools talking over each other. What the
console leaves out is kept here instead, for whoever has to work out afterwards
what went wrong: when it happened, the command line the run was started with,
the input file the failing work was about, every file the failing tool was
handed (and which of those sat in the RAM scratch, so the hard-drive file behind
them can still be found), where in the code the tool was called from, the
tool's command and its whole kept output, and for a crash the Python traceback.

What counts as an error is :data:`KINDS`, the one list of them. Every record is
one of those kinds, and every kind is also said on the console - an ``ERROR:``
or ``WARNING:`` line - so the console and the log cannot drift apart: the
console is the short form, the file the long one. :data:`NOTABLE` is the other
list: lines in a tool's output worth noticing even when the tool exited 0, and
the likely cause they name when it did not.

One file per run and not one per error: a run that fails on a hundred files
leaves one file to read, not a hundred. It is named for when the run started,
the command and its pid - ``20261105-140211-convert-audio-4242.log`` - and made
only when the first error arrives, so a clean run leaves nothing behind. The
workers a run fans out to and the commands it starts as children inherit the
file's name through the environment and add their records to the same file,
each record one write under the run's lock.

Plain text with the standard library, and no logging framework: the records are
written by several processes at once, which ``logging``'s file handler does not
coordinate either, and one formatter and one locked append is all there is.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import os
import re
import shlex
import sys
import threading
import time
import traceback
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from medialib import commands

__all__ = ["CAUSES", "ERROR_DIR", "KINDS", "NOTABLE", "Kind", "Notable",
           "crash_kind", "current_file", "install_hooks", "note_origin",
           "notable_lines", "origin_of", "record", "record_crash", "redact", "start_run",
           "subject", "working_on"]

ERROR_DIR = "error"

# Handed down to every worker and child of the run, so all of them write to
# the one file and name the command line the run was started with.
NAME_VARIABLE = "ERROR_LOG_NAME"
RUN_VARIABLE = "ERROR_LOG_RUN"
STARTED_VARIABLE = "ERROR_LOG_STARTED"
# The item and input file a parent was working on when it started this
# process, so a child command's records still name the file on disk.
ITEM_VARIABLE = "ERROR_LOG_ITEM"
INPUT_VARIABLE = "ERROR_LOG_INPUT"

ERROR, WARNING = "ERROR", "WARNING"


@dataclass(frozen=True)
class Kind:
    """One kind of error a run keeps: its key, how serious it is, and what it
    means in a sentence, written into every record of it."""

    key: str
    severity: str
    means: str


# Every kind of error a run keeps, and the only place they are listed.
# docs/output.md lists them too, and a test fails if the two disagree.
KINDS = {kind.key: kind for kind in (
    # What the user asked for and the run refused.
    Kind("usage", ERROR,
         "the command line was refused: an unknown option, a bad value, or a "
         "missing argument"),
    Kind("input-missing", ERROR,
         "the input folder or file the command was pointed at does not exist"),
    Kind("input-unusable", ERROR,
         "the input folder is empty or holds nothing this command works on"),
    Kind("output-inside-input", ERROR,
         "the output folder given sits inside the input folder"),
    Kind("config", ERROR,
         "the settings file could not be read, or is not private while it "
         "holds a secret"),
    Kind("tool-missing", ERROR,
         "a tool the command needs is not installed or not on PATH"),
    # The third-party tools.
    Kind("tool-failed", ERROR,
         "a third-party tool the command ran exited non-zero or was killed"),
    Kind("tool-not-started", ERROR,
         "a third-party tool could not be started at all"),
    Kind("tool-bad-output", ERROR,
         "a third-party tool exited 0 but what it made falls short"),
    Kind("tool-skipped", WARNING,
         "a third-party tool failed and the command skipped that step rather "
         "than the item"),
    Kind("tool-warning", WARNING,
         "a third-party tool succeeded but said something that usually means "
         "a damaged input or output (see NOTABLE)"),
    Kind("output-check", ERROR,
         "an output was made but failed the command's own check of it"),
    # The run itself.
    Kind("worker-died", ERROR,
         "a worker process ended abnormally and its item was not finished"),
    Kind("disk-full", ERROR,
         "a write failed because the disk or the RAM scratch is full"),
    Kind("out-of-memory", ERROR,
         "the machine or the GPU ran out of memory"),
    Kind("permission", ERROR,
         "a file or folder could not be read or written: permission denied or "
         "a read-only filesystem"),
    Kind("crash", ERROR,
         "the command stopped on an unexpected Python exception"),
    Kind("error", ERROR,
         "any other error a command reports on the console"),
)}


@dataclass(frozen=True)
class Notable:
    """A line in a tool's output worth noticing.

    ``says`` is the sentence it is reported with. ``warn`` makes it worth a
    warning even when the tool exited 0 - a tool that reports damage and
    succeeds anyway is the case nobody else would catch.
    """

    key: str
    pattern: re.Pattern
    says: str
    warn: bool


# The lines picked out of a tool's output. Most of what ffmpeg and the rest
# warn about is harmless, and repeating it would bury what is not, so this is
# kept to the lines that tend to mean a broken file or a starved machine.
NOTABLE = (
    Notable("decode-error", re.compile(
        r"Error while decoding stream|error while decoding MB|"
        r"concealing \d+ (?:DC|AC|MV) errors|corrupt decoded frame|"
        r"Invalid NAL unit size|Error splitting the input into NAL units|"
        r"missing picture in access unit|[Pp]acket corrupt|"
        r"Header missing|Error decoding (?:audio|video)|invalid frame size"),
        "the input has damaged frames or packets: the output may have "
        "glitches or gaps", True),
    Notable("invalid-data", re.compile(
        r"Invalid data found when processing input|moov atom not found|"
        r"Truncating packet of size|partial file|"
        r"File ended prematurely|Unexpected end of file|unexpected EOF"),
        "the input is truncated or not what its name says", True),
    # unrar, 7-Zip and unzip on an archive damaged in transit or on disk.
    Notable("archive-damaged", re.compile(
        r"Corrupt header is found|[Aa]rchive header is corrupt|"
        r"Unexpected end of archive|checksum error|CRC [Ff]ailed|bad CRC|"
        r"\bData Error\b|\bHeaders Error\b|Unexpected end of data|"
        r"Can ?not open the file as archive|is not RAR archive|"
        r"invalid compressed data|bad zipfile offset"),
        "the archive is damaged: members past the damage may be missing or "
        "broken", True),
    Notable("empty-output", re.compile(
        r"Output file is empty, nothing was encoded|"
        r"Output file #\d+ does not contain any stream"),
        "the tool wrote an empty output", True),
    Notable("disk-full", re.compile(
        r"No space left on device|Disk quota exceeded|ENOSPC"),
        "the disk or the RAM scratch is full", False),
    Notable("out-of-memory", re.compile(
        r"CUDA out of memory|CUDA_ERROR_OUT_OF_MEMORY|out of memory|"
        r"Cannot allocate memory|std::bad_alloc|MemoryError|"
        r"Killed process \d+"),
        "the machine or the GPU ran out of memory", False),
    Notable("permission", re.compile(
        r"Permission denied|Read-only file system|Operation not permitted"),
        "a file could not be read or written: permission denied", False),
    Notable("blocked", re.compile(
        r"HTTP Error 429|Too Many Requests|Sign in to confirm you.re not a bot|"
        r"HTTP Error 403"),
        "the site refused or rate-limited the request", False),
    Notable("network", re.compile(
        r"Connection (?:refused|reset|timed out)|Temporary failure in name "
        r"resolution|Name or service not known|Network is unreachable|"
        r"Could not resolve host|SSL: CERTIFICATE_VERIFY_FAILED"),
        "the network or the remote host could not be reached", False),
)

# The notable lines that name a cause, keyed for the failure they explain.
CAUSES = {notable.key: notable for notable in NOTABLE}

# The most notable lines one tool run keeps, so a file damaged throughout does
# not fill memory with the same complaint.
NOTABLE_LIMIT = 20

# A signal that ends a tool with no word of its own, said for what it usually is.
_SIGNAL_CAUSES = {9: "killed outright (SIGKILL): usually the kernel's "
                     "out-of-memory killer",
                  -9: "killed outright (SIGKILL): usually the kernel's "
                      "out-of-memory killer",
                  137: "exit 137 is SIGKILL: usually the kernel's "
                       "out-of-memory killer"}

# Secrets, starred out of everything written here and everything a tool's
# replay prints: a command line or a tool's output can carry a request URL or a
# header.
_SECRET = re.compile(
    r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|"
    r"passwd|secret)\s*[=:]\s*)([^\s&\"',;]+)")
_BEARER = re.compile(r"(?i)\b(authorization\s*:\s*\w+\s+)(\S+)")
_FLAG_SECRET = re.compile(
    r"(?i)(--(?:api-?key|token|password|secret)\s+)(\S+)")
# The environment is where the commands are handed their keys and logins, so
# the value of any variable named like one is starred wherever it turns up.
_SECRET_NAME = re.compile(r"(?i)key|token|pass|secret")
_SECRET_MIN = 6


def redact(text: str) -> str:
    """``text`` with the value of every key, token and password starred out."""
    for name, value in os.environ.items():
        if len(value) >= _SECRET_MIN and _SECRET_NAME.search(name):
            text = text.replace(value, "***")
    text = _SECRET.sub(r"\1***", text)
    text = _FLAG_SECRET.sub(r"\1***", text)
    return _BEARER.sub(r"\1***", text)


def notable_lines(lines: Iterable[str]) -> list[tuple[str, str]]:
    """The notable lines among ``lines``, as (key, line), at most
    :data:`NOTABLE_LIMIT` of them."""
    found = []
    for line in lines:
        key = notable_key(line)
        if key:
            found.append((key, line))
            if len(found) >= NOTABLE_LIMIT:
                break
    return found


def notable_key(line: str) -> str:
    """Which :data:`NOTABLE` entry this line is, or ``""``."""
    for notable in NOTABLE:
        if notable.pattern.search(line):
            return notable.key
    return ""


# What the work in hand is about, per thread: the item a worker was handed and
# the input file it stands for. A stack, so a step inside an item can name a
# more precise file for as long as it lasts.
_LOCAL = threading.local()

# Scratch copies and the hard-drive files they were made from, per process.
_ORIGINS: dict[str, str] = {}


def _subjects() -> list:
    stack = getattr(_LOCAL, "subjects", None)
    if stack is None:
        stack = _LOCAL.subjects = []
    return stack


@contextlib.contextmanager
def working_on(path: str, item: str = "") -> Iterator[None]:
    """Name the input file the work inside the block is about, so an error
    recorded meanwhile - by a tool working on a RAM copy of it, say - still
    names the file on disk that was the culprit."""
    stack = _subjects()
    stack.append((item or path, _absolute(path)))
    try:
        yield
    finally:
        stack.pop()


def work_item(work) -> contextlib.AbstractContextManager:
    """The serial path's twin of :func:`run_in_worker`: the item a run works
    on in its own process, named for the errors recorded meanwhile the same
    way a worker's item is."""
    from medialib.lib import workerpool

    label = workerpool.label(work)
    return working_on(label if os.path.exists(label) else "", item=label)


def subject() -> tuple[str, str]:
    """The (item, input file) the work in hand is about, innermost first; both
    empty when nothing has said."""
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


ORIGINS_FILE = ".errorlog-origins"


def note_origin(copy: str, source: str) -> None:
    """``copy`` (a file or folder in the scratch) was made from ``source``,
    so a failure on the copy can name the original.

    Kept in this process and in a small file in the run's scratch base, which
    is the one place the workers share: an archive unpacked by the run and
    then worked on by a worker still names the archive.
    """
    if not (copy and source):
        return
    copy, source = _absolute(copy), _absolute(source)
    _ORIGINS[copy] = source
    shared = _origins_file()
    if shared and "\t" not in copy + source and "\n" not in copy + source:
        try:
            with open(shared, "a", encoding="utf-8") as handle:
                handle.write("%s\t%s\n" % (copy, source))
        except OSError:
            pass


def _origins_file() -> str:
    try:
        from medialib.lib import ramscratch
        base = ramscratch._STATE.ram_base
    except Exception:                       # pragma: no cover - import guard
        return ""
    return os.path.join(base, ORIGINS_FILE) if base else ""


def _shared_origins() -> dict[str, str]:
    shared = _origins_file()
    found: dict[str, str] = {}
    if not shared:
        return found
    try:
        with open(shared, encoding="utf-8") as handle:
            for line in handle:
                copy, _, source = line.rstrip("\n").partition("\t")
                if copy and source:
                    found[copy] = source
    except OSError:
        pass
    return found


def _absolute(path: str) -> str:
    try:
        return os.path.abspath(path) if path else ""
    except (TypeError, ValueError):
        return str(path)


def origin_of(path: str) -> str:
    """The hard-drive file a scratch path was made from, or ``""``."""
    return _origin_of(path)


def _origin_of(path: str) -> str:
    """The hard-drive file a scratch path was made from, by it or by any
    folder it sits in; empty when nothing said."""
    origins = {**_shared_origins(), **_ORIGINS}
    probe = _absolute(path)
    while probe:
        if probe in origins:
            return origins[probe]
        parent = os.path.dirname(probe)
        if parent == probe:
            return ""
        probe = parent
    return ""


def _scratch_roots() -> list[str]:
    """Where this run's scratch can be: its base and spill parents, the
    configured base and the tmpfs every Linux host has."""
    roots = []
    try:
        from medialib.lib import ramscratch
        roots += [ramscratch._STATE.ram_base or ""] + list(ramscratch._STATE.bases)
        roots.append(ramscratch.SHM)
    except Exception:                       # pragma: no cover - import guard
        pass
    roots.append(os.environ.get("ramScratchBase", ""))
    return [_absolute(root) for root in roots if root]


def _in_scratch(path: str, roots: list[str]) -> bool:
    absolute = _absolute(path)
    return any(absolute == root or absolute.startswith(root.rstrip(os.sep) + os.sep)
               for root in roots)


def start_run(argv: list | None = None) -> None:
    """Settle this run's error log, for this process and everything it
    starts, and install the hooks that keep a crash.

    Settled before any worker or child exists, because they learn it from the
    environment they start with. A child command finds it settled already and
    keeps writing to its parent's file - one run, one file - naming its own
    command line in each record beside the run's.
    """
    _settle_name(argv)
    install_hooks()


def current_file() -> str:
    """Where this run's records go. A process nobody started a run in - a
    library call, a test - gets a name of its own the first time it asks."""
    _settle_name()
    name = os.environ[NAME_VARIABLE]
    return os.path.join(commands.script_dir(), "logs", ERROR_DIR, name)


def _settle_name(argv: list | None = None) -> None:
    """The run's file name, start time and command line, settled in the
    environment unless a parent settled them already."""
    if os.environ.get(NAME_VARIABLE):
        return
    program = commands.current_program()
    if argv is None:
        argv = sys.argv[1:]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", program).strip("-") or "run"
    os.environ[NAME_VARIABLE] = "%s-%s-%d.log" % (
        time.strftime("%Y%m%d-%H%M%S"), safe, os.getpid())
    os.environ[STARTED_VARIABLE] = _stamp()
    os.environ[RUN_VARIABLE] = shlex.join([program] + [str(a) for a in argv])


def _stamp() -> str:
    """Now, to the millisecond, with the offset from UTC: a log read on
    another machine or after a clock change still places it."""
    now = datetime.datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + "%03d %s" % (
        now.microsecond // 1000, now.strftime("%z"))


def crash_kind(exc: BaseException) -> str:
    """The kind an exception is recorded as: what it says about the machine
    when it says something, a crash otherwise."""
    if isinstance(exc, MemoryError):
        return "out-of-memory"
    if isinstance(exc, OSError):
        if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
            return "disk-full"
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return "permission"
    return "crash"


def _caller_stack() -> list[str]:
    """Where in the commands this record was asked for from, outermost first:
    the frames inside the package, without the logging's own."""
    package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mine = {os.path.abspath(__file__)}
    try:
        from medialib.lib import runlog, toolcapture
        mine |= {os.path.abspath(runlog.__file__),
                 os.path.abspath(toolcapture.__file__)}
    except Exception:                       # pragma: no cover - import guard
        pass
    frames = [frame for frame in traceback.extract_stack()
              if os.path.abspath(frame.filename).startswith(package)
              and os.path.abspath(frame.filename) not in mine]
    return traceback.format_list(frames)


def _file_lines(files: Iterable[str], tool_argv: list) -> list[str]:
    """The files the error involved, each with what is known about it."""
    roots = _scratch_roots()
    seen: list[str] = []
    for path in list(files) + _paths_in(tool_argv):
        absolute = _absolute(str(path))
        if absolute and absolute not in seen:
            seen.append(absolute)
    lines = []
    for path in seen[:40]:
        notes = []
        scratch = _in_scratch(path, roots)
        if scratch:
            notes.append("RAM scratch")
        # Named wherever it is known, the scratch or not: a page unpacked to
        # a disk spill is as far from its comic as one in RAM.
        origin = _origin_of(path)
        if origin and origin != path:
            notes.append("made from %s" % origin)
        elif scratch and subject()[1]:
            notes.append("made while working on the input above")
        notes.append("exists" if os.path.lexists(path) else "not there")
        lines.append("  %s  [%s]" % (path, ", ".join(notes)))
    if len(seen) > 40:
        lines.append("  [... %d more ...]" % (len(seen) - 40))
    return lines


def _paths_in(argv: list) -> list[str]:
    """The words of a tool's command line that name files: an absolute path,
    or one that exists relative to here, also when written ``key=path`` or
    ``file:path``."""
    found = []
    for word in argv[1:]:
        word = str(word)
        if len(word) > 4096 or "\n" in word:
            continue
        for candidate in (word, word.partition("=")[2],
                          word[5:] if word.startswith("file:") else ""):
            if not candidate or candidate.startswith("-"):
                continue
            if os.path.isabs(candidate) or (
                    os.sep in candidate and os.path.lexists(candidate)):
                found.append(candidate)
                break
    return found


def _process_line() -> str:
    """Which process this is, as a reader of the log needs it."""
    import multiprocessing

    current = multiprocessing.current_process()
    if current.name != "MainProcess":
        return "pid %d, a worker (%s) of pid %d" % (
            os.getpid(), current.name[:200], os.getppid())
    return "pid %d: %s" % (os.getpid(), shlex.join(
        [commands.current_program()] + [str(a) for a in sys.argv[1:]]))


def record(kind: str, message: str, *, files: Iterable[str] = (),
           tool_argv: list | None = None, tool_status: str = "",
           tool_output: Iterable[str] = (), tool_lines: int | None = None,
           causes: Iterable[str] = (), exc: BaseException | None = None,
           stack: bool = True) -> str:
    """Add one record to this run's error log, and return the file's path -
    empty when it could not be written: a full disk is no reason to lose the
    console's error line, which the caller prints either way.

    ``kind`` is a key of :data:`KINDS`; one that is not is kept as ``error``
    with the key it was given written beside it, rather than lost.
    """
    try:
        return _write(_render(kind, message, files, tool_argv or [],
                              tool_status, list(tool_output), tool_lines,
                              list(causes), exc, stack))
    except Exception:                       # never let the log cost the run
        return ""


def _render(kind, message, files, tool_argv, tool_status, tool_output,
            tool_lines, causes, exc, stack) -> str:
    known = KINDS.get(kind)
    heading = kind if known else "error (%s)" % kind
    known = known or KINDS["error"]
    item, source = subject()
    out = ["=" * 78,
           "%s  %s  %s" % (_stamp(), known.severity, heading),
           "what:     %s" % message,
           "means:    %s" % known.means]
    for cause in causes:
        out.append("cause:    %s" % cause)
    out.append("run:      %s" % os.environ.get(RUN_VARIABLE, ""))
    out.append("started:  %s    cwd: %s" % (os.environ.get(STARTED_VARIABLE, ""),
                                           _cwd()))
    out.append("process:  %s" % _process_line())
    if item:
        out.append("item:     %s" % item)
    if source and source != _absolute(item):
        out.append("input:    %s" % source)
    elif source:
        out.append("input:    %s  [%s]" % (
            source, "exists" if os.path.lexists(source) else "not there"))
    # A child command's own item sits inside what its parent was working on:
    # a page in RAM is one of the parent's comic, which is what to look at.
    handed = os.environ.get(INPUT_VARIABLE, "")
    if handed and handed != source:
        out.append("for:      %s  [what the command that started this one "
                   "was working on]" % handed)
    file_lines = _file_lines(files, tool_argv)
    if file_lines:
        out.append("files:")
        out += file_lines
    if tool_argv:
        out.append("tool:     %s" % shlex.join([str(a) for a in tool_argv]))
    if tool_status:
        out.append("status:   %s" % tool_status)
    if stack:
        frames = _caller_stack()
        if frames:
            out.append("called from (most recent call last):")
            out += [line.rstrip("\n") for line in frames]
    if exc is not None:
        out.append("traceback (most recent call last):")
        out += [line.rstrip("\n") for line in
                traceback.format_exception(type(exc), exc, exc.__traceback__)]
    if tool_output:
        total = tool_lines if tool_lines is not None else len(tool_output)
        out.append("tool output (%d line(s)):" % total)
        out += ["  | %s" % line for line in tool_output]
    return redact("\n".join(out) + "\n\n")


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return "(gone)"


def _write(text: str) -> str:
    """One record onto the end of the run's file, in one write, under the run's
    lock so a worker writing beside it cannot land inside it."""
    from medialib.lib import runlog

    path = current_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle, runlog.take_lock(handle):
        handle.write(text)
        handle.flush()
    return path


def record_crash(exc: BaseException, where: str = "") -> str:
    """Keep an exception nobody caught, and say it on the console: one line,
    the traceback under -v, and the whole of it always when it could not be
    kept - a traceback is never lost to a full disk."""
    from medialib.lib import runlog

    kind = crash_kind(exc)
    summary = "%s: %s" % (type(exc).__name__, exc) if str(exc) else type(exc).__name__
    if where:
        summary = "%s: %s" % (where, summary)
    path = record(kind, summary, files=_exception_files(exc), exc=exc,
                  stack=False)
    runlog.error(summary, persist=False)
    if path and runlog.verbosity() < runlog.VERBOSE:
        runlog.error_continued("       the traceback is kept in %s" % path)
    else:
        sys.stderr.write("".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)))
        if path:
            runlog.error_continued("       and kept in %s" % path)
    sys.stderr.flush()
    return path


def _exception_files(exc: BaseException) -> list[str]:
    files = []
    for name in ("filename", "filename2"):
        value = getattr(exc, name, None)
        if isinstance(value, str | bytes | os.PathLike) and value:
            files.append(os.fsdecode(value))
    return files


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


def run_in_worker(target, args: tuple, label: str) -> None:
    """A worker's body: its item named for every record made inside it, and an
    exception it did not catch kept before the worker ends with status 1 -
    which the parent then reports as the worker it lost."""
    install_hooks()
    with working_on(label if os.path.exists(label) else "", item=label):
        try:
            target(*args)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            record_crash(exc, where=label)
            raise SystemExit(1) from exc


def signal_cause(returncode: int) -> str:
    """What an exit status says about why a tool ended, when it says
    anything."""
    return _SIGNAL_CAUSES.get(returncode, "")
