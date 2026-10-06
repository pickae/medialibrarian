"""What a tool said while it ran: kept in RAM, and shown only when it failed.

A run of these commands has workers running ffmpeg, whisper or yt-dlp side by
side, and their output passed through as it comes would be several tools
talking over each other. So nothing a tool prints reaches the terminal while it
runs. It is kept instead - the first lines, which hold how a tool set itself up,
and a rolling window of the last ones, which hold how it ended - and a tool that
succeeded is never heard from. One that failed is replayed as one block, under
the run's lock so no other worker's line lands inside it, and its whole kept
output is copied to `logs/tool-failures/` so it outlives the scratch.

How much is replayed follows the run's verbosity:

* quiet - the one error line, and where the output was kept;
* normal - that, and the tool's last lines;
* verbose - that, the command that was run, the tool's first lines and many
  more of its last.

The tool itself is asked to say more under ``-v`` by the caller, which knows its
flags: :func:`pick` and :func:`ffmpeg_loglevel` are the choice made once.
"""

from __future__ import annotations

import codecs
import collections
import io
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from medialib import commands
from medialib.lib import runlog

__all__ = ["ToolOutput", "ToolRun", "ffmpeg_loglevel", "pick", "redact",
           "report_failure", "run"]

# What is kept of one run: its first lines, then the last ones.
HEAD_LINES = 200
TAIL_LINES = 2000

# What is replayed on a failure, by verbosity.
SHOWN_TAIL = {runlog.QUIET: 0, runlog.NORMAL: 40, runlog.VERBOSE: 300}
SHOWN_HEAD_VERBOSE = 50

# The most of one unfinished line held while waiting for its end. A tool that
# writes megabytes without a line break is writing something that is not text.
_PENDING_LIMIT = 65536

# Terminal control sequences: colour, cursor movement, erase-line.
_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# A secret written as name=value or name: value, and a bearer token. What is
# kept and replayed can contain a request URL or a header, and neither belongs
# on a terminal or in a log.
_SECRET = re.compile(
    r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|"
    r"passwd|secret)\s*[=:]\s*)([^\s&\"',;]+)")
_BEARER = re.compile(r"(?i)\b(authorization\s*:\s*\w+\s+)(\S+)")
_FLAG_SECRET = re.compile(
    r"(?i)(--(?:api-?key|token|password|secret)\s+)(\S+)")

# The environment is where the commands are handed their keys and logins, so
# the value of any variable named like one is starred wherever it turns up,
# whatever form it was written in.
_SECRET_NAME = re.compile(r"(?i)key|token|pass|secret")
_SECRET_MIN = 6

FAILURE_DIR = "tool-failures"


def redact(text: str) -> str:
    """``text`` with the value of every key, token and password starred out."""
    for name, value in os.environ.items():
        if len(value) >= _SECRET_MIN and _SECRET_NAME.search(name):
            text = text.replace(value, "***")
    text = _SECRET.sub(r"\1***", text)
    text = _FLAG_SECRET.sub(r"\1***", text)
    return _BEARER.sub(r"\1***", text)


def pick(normal: list, verbose: list) -> list:
    """The tool flags for this run's verbosity: ``verbose`` under ``-v``,
    ``normal`` otherwise. Quiet runs a tool as normal: what it says is kept
    either way, and quiet only decides how much of it is shown."""
    return list(verbose if runlog.verbosity() >= runlog.VERBOSE else normal)


def ffmpeg_loglevel() -> list:
    """ffmpeg's (and ffprobe's) ``-loglevel`` for this run: its own default,
    ``info``, or ``verbose`` under ``-v``."""
    return ["-loglevel", pick(["info"], ["verbose"])[0]]


def _collapse(text: str) -> str:
    """The part of a line a terminal would still be showing.

    A progress counter rewrites its line with carriage returns, and the last
    rewrite is the one that matters; an empty one after it is the counter
    clearing itself, not news.
    """
    if "\r" not in text:
        return text
    segments = [part for part in text.split("\r") if part.strip()]
    return segments[-1] if segments else ""


class ToolOutput:
    """One tool's output, as a terminal would have shown it, within a bound.

    Fed as it arrives, in pieces that need not end at a line break: bytes are
    decoded as UTF-8 with anything else replaced, carriage-return redraws are
    collapsed to their last state, and control sequences are dropped. The first
    ``head`` lines are kept, then a window of the last ``tail``; what fell
    between is counted, so a replay can say how much it is not showing.
    """

    def __init__(self, head: int = HEAD_LINES, tail: int = TAIL_LINES) -> None:
        self._head_limit = head
        self._head: list[str] = []
        self._tail: collections.deque = collections.deque(maxlen=tail)
        self._dropped = 0
        self._pending = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.total = 0

    def feed(self, data: bytes | str) -> None:
        """Take the next piece of output."""
        if isinstance(data, bytes):
            data = self._decoder.decode(data)
        text = self._pending + _CSI.sub("", data)
        *complete, self._pending = text.split("\n")
        for line in complete:
            self._add(_collapse(line))
        if "\r" in self._pending:
            # Only the latest redraw is worth holding, plus whatever has
            # started after it, so a counter that never ends its line does not
            # grow without bound.
            done, _, rest = self._pending.rpartition("\r")
            self._pending = _collapse(done) + "\r" + rest
        if len(self._pending) > _PENDING_LIMIT:
            self._pending = self._pending[-_PENDING_LIMIT:]

    def close(self) -> None:
        """The tool has finished: an unended last line is still a line."""
        self._pending += self._decoder.decode(b"", final=True)
        last = _collapse(self._pending)
        self._pending = ""
        if last:
            self._add(last)

    def _add(self, line: str) -> None:
        self.total += 1
        if len(self._head) < self._head_limit:
            self._head.append(line)
            return
        if len(self._tail) == self._tail.maxlen:
            self._dropped += 1
        self._tail.append(line)

    def _gap(self, count: int) -> str:
        return "[... %d line(s) not kept ...]" % count

    def lines(self) -> list[str]:
        """Everything kept, with a marker where lines were not."""
        gap = [self._gap(self._dropped)] if self._dropped else []
        return self._head + gap + list(self._tail)

    def first(self, count: int) -> list[str]:
        return self.lines()[:count]

    def last(self, count: int) -> list[str]:
        return self.lines()[-count:] if count > 0 else []

    def text(self) -> str:
        return "".join(line + "\n" for line in self.lines())


@dataclass
class ToolRun:
    """How one tool run went.

    ``failure`` is empty when it succeeded, and otherwise says how it did not.
    ``stdout`` is the tool's standard output when the caller asked to have it.
    ``log_path`` is where a failure's output was kept, empty when nothing was.
    """

    argv: list
    returncode: int
    output: ToolOutput
    stdout: str = ""
    failure: str = ""
    log_path: str = ""
    label: str = ""

    @property
    def ok(self) -> bool:
        return not self.failure


def _status_text(returncode: int) -> str:
    if returncode < 0:
        return "was killed by signal %d" % -returncode
    return "exited with status %d" % returncode


def _pump(stream, output: ToolOutput) -> None:
    for chunk in iter(lambda: stream.read1(65536), b""):
        output.feed(chunk)
    stream.close()


def run(argv, label: str = "", *,
        verify: Callable[[], str] | None = None,
        stdout: str = "merge",
        cwd: str | None = None,
        env: dict | None = None,
        report: bool = True,
        lock_file: str = "") -> ToolRun:
    """Run one tool with its output kept, and replay it if the tool failed.

    ``stdout`` is ``"merge"`` - the tool's standard output is chatter like its
    standard error, and kept with it - or ``"capture"``, for a tool whose
    standard output is the answer: it is handed back in ``ToolRun.stdout``.

    A tool fails when it exits non-zero, cannot be started, or - for a tool
    whose exit status is not to be trusted - when ``verify``, asked after a
    zero exit, returns a reason. ``label`` names the item in the error line.

    ``report`` False leaves the replay to the caller, who may want to retry
    first: :func:`report_failure` is the same replay, called by hand.
    ``lock_file`` is the run's shared lock, so the block is not interleaved with
    another worker's lines.
    """
    argv = [str(word) for word in argv]
    output = ToolOutput()
    captured = ""
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if stdout == "capture" else subprocess.STDOUT)
    except OSError as exc:
        output.feed("%s\n" % exc)
        output.close()
        result = ToolRun(argv, 127, output, failure="could not be started: %s"
                         % (exc.strerror or exc), label=label)
    else:
        assert process.stdout is not None
        if stdout == "capture":
            reader = threading.Thread(target=_pump,
                                      args=(process.stderr, output),
                                      daemon=True)
            reader.start()
            captured = process.stdout.read().decode("utf-8", "replace")
            process.stdout.close()
            reader.join()
        else:
            _pump(process.stdout, output)
        returncode = process.wait()
        output.close()
        failure = "" if returncode == 0 else _status_text(returncode)
        if not failure and verify is not None:
            failure = verify() or ""
        result = ToolRun(argv, returncode, output, stdout=captured,
                         failure=failure, label=label)
    if result.failure and report:
        report_failure(result, lock_file=lock_file)
    return result


def _keep(result: ToolRun) -> str:
    """The failure's whole kept output, in `logs/`; its path, or empty when it
    could not be written - a full disk is no reason to lose the error line."""
    tool = os.path.basename(result.argv[0]) if result.argv else "tool"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", result.label or tool).strip("-")
    name = "%s-%d-%s-%s.log" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid(),
                                tool, slug[:60] or "run")
    try:
        path = commands.logs_file(commands.script_dir(),
                                  os.path.join(FAILURE_DIR, name))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(redact("command: %s\n" % shlex.join(result.argv)))
            handle.write("%s\n\n" % result.failure)
            handle.write(redact(result.output.text()))
    except OSError:
        return ""
    return path


def _block(result: ToolRun, level: int) -> str:
    """The replay, as one piece of text."""
    out = io.StringIO()
    tool = os.path.basename(result.argv[0]) if result.argv else "the tool"
    subject = "%s: %s" % (result.label, tool) if result.label else tool
    runlog.error(subject, result.failure, stream=out)
    output = result.output
    if level >= runlog.VERBOSE:
        out.write("    command: %s\n" % redact(shlex.join(result.argv)))
        head = output.first(SHOWN_HEAD_VERBOSE)
        tail = output.last(SHOWN_TAIL[level])
        if len(output.lines()) > len(head) + len(tail):
            shown = head + ["[...]"] + tail
        else:
            shown = output.lines()
    else:
        shown = output.last(SHOWN_TAIL[level])
    for line in shown:
        out.write(redact("    | %s\n" % line))
    if result.log_path:
        out.write("    all %d line(s) it printed: %s\n"
                  % (output.total, result.log_path)
                  if shown else "    its output: %s\n" % result.log_path)
    return out.getvalue()


def report_failure(result: ToolRun, stream=None, lock_file: str = "") -> None:
    """Replay a failed run: keep its output in `logs/`, then print the block."""
    if not result.log_path:
        result.log_path = _keep(result)
    text = _block(result, runlog.verbosity())
    out = sys.stderr if stream is None else stream
    if lock_file:
        with open(lock_file, "a") as handle, runlog.take_lock(handle):
            out.write(text)
            out.flush()
    else:
        out.write(text)
        out.flush()
