"""What a tool said while it ran: kept in RAM, and shown only when it failed.

A run of these commands has workers running ffmpeg, whisper or yt-dlp side by
side, and their output passed through as it comes would be several tools
talking over each other. So nothing a tool prints reaches the terminal while it
runs. It is kept instead - the first lines, which hold how a tool set itself up,
and a rolling window of the last ones, which hold how it ended - and a tool that
succeeded is never heard from. One that failed is replayed as one block, under
the run's lock so no other worker's line lands inside it, and its whole kept
output goes into the run's error log under `logs/error/`
(`medialib/lib/errorlog.py`), so it outlives the scratch - with the files it
was handed, the input they came from, and where in the code it was run from.

A tool that succeeded but said something that tends to mean damage - ffmpeg
decoding a broken frame and carrying on, say - is the one exception to its
silence: one warning, and the lines that raised it kept in the error log.

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
from collections.abc import Callable
from dataclasses import dataclass

from medialib.lib import errorlog, runlog, statusline
from medialib.lib.errorlog import redact

__all__ = ["ToolOutput", "ToolRun", "causes", "ffmpeg_loglevel", "finished",
           "from_log", "pick", "redact", "replay_text", "report_failure",
           "report_notable", "run", "watched"]

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
        # The lines errorlog.NOTABLE picks out, as (key, line), kept whether
        # they fell in the head, the tail or between; and how many there were.
        self.notable: list[tuple[str, str]] = []
        self.notable_count = 0

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

    def drain(self, stream) -> None:
        """Feed it everything ``stream`` (a pipe opened in binary) has until the
        tool closes it, then close the stream."""
        for chunk in iter(lambda: stream.read1(65536), b""):
            self.feed(chunk)
        stream.close()

    def close(self) -> None:
        """The tool has finished: an unended last line is still a line."""
        self._pending += self._decoder.decode(b"", final=True)
        last = _collapse(self._pending)
        self._pending = ""
        if last:
            self._add(last)

    def _add(self, line: str) -> None:
        self.total += 1
        key = errorlog.notable_key(line) if line else ""
        if key:
            self.notable_count += 1
            if len(self.notable) < errorlog.NOTABLE_LIMIT:
                self.notable.append((key, line))
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
    tool: str = ""

    @property
    def ok(self) -> bool:
        return not self.failure

    @property
    def name(self) -> str:
        """What the tool is called in the error line: as the caller named it,
        else the program that was run."""
        if self.tool:
            return self.tool
        return os.path.basename(self.argv[0]) if self.argv else "tool"


def from_log(path: str, failure: str, label: str = "",
             tool: str = "") -> ToolRun:
    """A failed run whose output a tool wrote to a log of its own - read back,
    so it can be replayed like any other before the scratch it sits in goes.
    A log that is missing replays as nothing."""
    output = ToolOutput()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                output.feed(chunk)
    except OSError:
        pass
    output.close()
    return ToolRun([tool or os.path.basename(path)], 1, output,
                   failure=failure, label=label, tool=tool)


def _status_text(returncode: int) -> str:
    if returncode < 0:
        return "was killed by signal %d" % -returncode
    return "exited with status %d" % returncode


def finished(argv, returncode: int, output: ToolOutput, label: str = "",
             tool: str = "", verify: Callable[[], str] | None = None,
             stdout: str = "") -> ToolRun:
    """How a run that has ended went, for a caller that ran the tool itself
    and fed its output to ``output`` - :func:`run` decides it the same way."""
    output.close()
    failure = "" if returncode == 0 else _status_text(returncode)
    if not failure and verify is not None:
        failure = verify() or ""
    return ToolRun([str(word) for word in argv], returncode, output,
                   stdout=stdout, failure=failure, label=label, tool=tool)


def run(argv, label: str = "", *,
        tool: str = "",
        warning: str = "",
        shown_if: Callable[[str], bool] | None = None,
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
    zero exit, returns a reason. ``label`` names the item in the error line,
    and ``tool`` the tool, for one started through a launcher whose own name
    would say nothing.

    ``warning`` is for a failure the caller treats as a skip rather than an
    error: the block opens with that ``WARNING:`` sentence instead, and like
    every warning it is not printed under ``-q``.

    ``report`` False leaves the replay to the caller, who may want to retry
    first: :func:`report_failure` is the same replay, called by hand. A run
    that succeeded but reported damage is warned about either way: there is
    no retry to wait for.
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
                         % (exc.strerror or exc), label=label, tool=tool)
    else:
        assert process.stdout is not None
        if stdout == "capture":
            reader = threading.Thread(target=output.drain,
                                      args=(process.stderr,), daemon=True)
            reader.start()
            captured = process.stdout.read().decode("utf-8", "replace")
            process.stdout.close()
            reader.join()
        else:
            output.drain(process.stdout)
        result = finished(argv, process.wait(), output, label, tool, verify,
                          captured)
    if result.failure:
        if report:
            report_failure(result, lock_file=lock_file, warning=warning,
                           shown_if=shown_if)
    else:
        report_notable(result, lock_file=lock_file)
    return result


# The tools whose exit status 1 is warnings with the work done: mkvtoolnix
# says so of all of its own, and 2 is its one failure.
_WARNINGS_ARE_ONE = ("mkvmerge", "mkvpropedit", "mkvextract")


def watched(argv, *, label: str = "", tool: str = "", ok=None,
            announce: bool = True, skipped: str = "",
            on_missing: int | None = None,
            **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a tool that does work - converts, muxes,
    unpacks, copies - with a failure kept in the error log.

    The same call and the same result as ``subprocess.run``: what the caller
    asked to be given back is given back, and what it sent to the null device
    or left to the terminal is captured instead - a tool's stderr is what says
    why it failed - and handed back as nothing, as before. An exit status
    outside ``ok`` is a failure; left out, it is 0 alone, or 0 and 1 for the
    mkvtoolnix tools, whose 1 is warnings with the file complete.

    ``announce`` replays a failure the way :func:`run` does; False keeps it in
    the log without a word, for a caller that prints its own line about it.
    ``skipped`` is the caller's sentence for a failure it treats as a skip -
    the step falls back or is left out, the item goes on - which makes it a
    warning rather than an error.

    A tool that cannot be started raises, as ``subprocess.run`` does, unless
    ``on_missing`` gives the status to answer with instead.
    """
    argv = [str(word) for word in argv]
    if ok is None:
        program = os.path.basename(argv[0]) if argv else ""
        ok = (0, 1) if program.split(".")[0] in _WARNINGS_ARE_ONE else (0,)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    check = kwargs.pop("check", False)
    asked = kwargs.get("stderr")
    merged = asked == subprocess.STDOUT
    if not merged:
        kwargs["stderr"] = subprocess.PIPE
    output = ToolOutput()
    try:
        done = subprocess.run(argv, **kwargs)
    except OSError as exc:
        output.feed("%s\n" % exc)
        failed = finished(argv, 127, output, label, tool)
        failed.failure = "could not be started: %s" % (exc.strerror or exc)
        _settle(failed, announce, skipped)
        if on_missing is None:
            raise
        empty = "" if kwargs.get("text") else b""
        return subprocess.CompletedProcess(argv, on_missing, empty, empty)
    except subprocess.TimeoutExpired as exc:
        for said in (exc.stderr, exc.output if merged else None):
            if said:
                output.feed(said)
        failed = finished(argv, 124, output, label, tool)
        failed.failure = "did not finish within %s second(s)" % exc.timeout
        _settle(failed, announce, skipped)
        raise
    # getattr: a stand-in for subprocess.run may hand back a status alone.
    said = getattr(done, "stdout" if merged else "stderr", None)
    if said:
        output.feed(said)
    result = finished(argv, done.returncode, output, label, tool)
    if done.returncode in ok:
        result.failure = ""
        if announce:
            report_notable(result)
    elif not safety_stopped():
        _settle(result, announce, skipped)
    if asked != subprocess.PIPE and not merged and hasattr(done, "stderr"):
        done.stderr = None
    if check and done.returncode not in ok:
        raise subprocess.CalledProcessError(
            done.returncode, argv, getattr(done, "stdout", None),
            getattr(done, "stderr", None))
    return done


def keep(argv, returncode: int, said, *, skipped: str = "", label: str = "",
         tool: str = "") -> str:
    """Keep a failure the caller ran the tool for itself and has already
    said in a line of its own: ``said`` is what the tool printed. The error
    log's path, or empty."""
    output = ToolOutput()
    if said:
        output.feed(said)
    result = finished(argv, returncode, output, label, tool)
    if result.ok:
        result.failure = "exited 0, but what it made falls short"
    return _keep(result, skipped)


def _settle(result: ToolRun, announce: bool, skipped: str) -> None:
    """A failed :func:`watched` call, replayed or only kept."""
    if announce:
        report_failure(result, warning=skipped)
    else:
        result.log_path = _keep(result, skipped)


def safety_stopped() -> bool:
    """Whether the run is being stopped: a tool the interrupt killed did not
    fail, and is not worth a record."""
    from medialib.lib import safety

    return safety.abort_requested()


def _kind(result: ToolRun, warning: str = "") -> str:
    """Which errorlog kind a failed run is."""
    if warning:
        return "tool-skipped"
    if result.failure.startswith("could not be started"):
        return "tool-not-started"
    if result.returncode == 0:
        return "tool-bad-output"
    return "tool-failed"


def causes(result: ToolRun) -> list[str]:
    """What the tool's own lines, or the way it ended, say about why it
    failed: each likely cause once, in the order its first line came."""
    said = []
    for key, _line in result.output.notable:
        sentence = errorlog.CAUSES[key].says
        if sentence not in said:
            said.append(sentence)
    by_signal = errorlog.signal_cause(result.returncode)
    if by_signal:
        said.append(by_signal)
    return said


def _subject(result: ToolRun) -> str:
    """The tool as the error line names it: by the item it worked on - the
    caller's label, else the item the run has in hand - and its name."""
    label = result.label or errorlog.subject()[0]
    return "%s: %s" % (label, result.name) if label else result.name


def _keep(result: ToolRun, warning: str = "") -> str:
    """The failure's whole kept output, in the run's error log; its path, or
    empty when it could not be written - a full disk is no reason to lose the
    error line."""
    message = "%s %s" % (_subject(result), result.failure)
    if warning:
        message = "%s (%s)" % (warning, message)
    return errorlog.record(
        _kind(result, warning), message, tool_argv=result.argv,
        tool_status=result.failure, tool_output=result.output.lines(),
        tool_lines=result.output.total, causes=causes(result))


def report_notable(result: ToolRun, stream=None, lock_file: str = "") -> str:
    """Warn about a run that succeeded but said something that tends to mean
    damage, and keep the lines that did; the error log's path, or empty when
    there was nothing to say.

    Kept at every verbosity, printed unless the run is quiet, like any other
    warning.
    """
    if not result.ok:
        return ""
    worrying = [(key, line) for key, line in result.output.notable
                if errorlog.CAUSES[key].warn]
    if not worrying:
        return ""
    says = errorlog.CAUSES[worrying[0][0]].says
    lines = [line for _, line in worrying]
    path = errorlog.record(
        "tool-warning", "%s finished, but %s" % (_subject(result), says),
        tool_argv=result.argv, tool_status="exited 0",
        tool_output=lines + ["[... the last lines it printed ...]"]
        + result.output.lines()[-SHOWN_TAIL[runlog.NORMAL]:],
        causes=[errorlog.CAUSES[key].says
                for key in dict.fromkeys(key for key, _ in worrying)])
    if runlog.verbosity() < runlog.NORMAL:
        return path
    out = io.StringIO()
    runlog.warn("%s finished, but %s:" % (_subject(result), says), stream=out)
    out.write(redact("    | %s\n" % lines[0]))
    more = max(result.output.notable_count, len(worrying)) - 1
    if more:
        out.write("    ... and %d more line(s) like it or worse\n" % more)
    if path:
        out.write("    kept in %s\n" % path)
    target = sys.stderr if stream is None else stream
    if lock_file:
        with open(lock_file, "a") as handle, runlog.take_lock(handle):
            _write_block(target, out.getvalue())
    else:
        _write_block(target, out.getvalue())
    return path


def _block(result: ToolRun, level: int, warning: str = "",
           shown_if: Callable[[str], bool] | None = None) -> str:
    """The replay, as one piece of text."""
    if warning and level < runlog.NORMAL:
        return ""
    out = io.StringIO()
    if warning:
        runlog.warn("%s (%s %s)" % (warning, result.name, result.failure),
                    stream=out)
    else:
        subject = ("%s: %s" % (result.label, result.name) if result.label
                   else result.name)
        runlog.error(subject, result.failure, stream=out)
    out.write(_body(result, level, shown_if))
    return out.getvalue()


def _body(result: ToolRun, level: int,
          shown_if: Callable[[str], bool] | None = None) -> str:
    """The replay's lines under its opening one: the tool's own, and where the
    rest of them were kept."""
    out = io.StringIO()
    output = result.output
    lines = output.lines()
    if shown_if is not None:
        lines = [line for line in lines if shown_if(line)]
    tail_count = SHOWN_TAIL[level]
    if level >= runlog.VERBOSE:
        out.write("    command: %s\n" % redact(shlex.join(result.argv)))
        if len(lines) > SHOWN_HEAD_VERBOSE + tail_count:
            shown = (lines[:SHOWN_HEAD_VERBOSE] + ["[...]"]
                     + lines[-tail_count:])
        else:
            shown = lines
    else:
        shown = lines[-tail_count:] if tail_count else []
    for line in shown:
        out.write(redact("    | %s\n" % line))
    for cause in causes(result):
        out.write("    likely cause: %s\n" % cause)
    if result.log_path:
        out.write("    all %d line(s) it printed: %s\n"
                  % (output.total, result.log_path)
                  if shown else "    its output: %s\n" % result.log_path)
    return out.getvalue()


def replay_text(result: ToolRun,
                shown_if: Callable[[str], bool] | None = None) -> str:
    """A failed run's replay without its opening line, for a caller that
    prints one of its own - its output kept in `logs/` all the same. Empty
    under -q, where a caller's warning is not printed either."""
    if not result.log_path:
        result.log_path = _keep(result)
    level = runlog.verbosity()
    return _body(result, level, shown_if) if level >= runlog.NORMAL else ""


def report_failure(result: ToolRun, stream=None, lock_file: str = "",
                   warning: str = "",
                   shown_if: Callable[[str], bool] | None = None) -> None:
    """Replay a failed run: keep its output in `logs/`, then print the block -
    opened by ``warning`` when the caller treats the failure as a skip.

    ``shown_if`` thins what is printed, for a tool with lines that never say
    why it failed; the log in `logs/` keeps every line regardless.
    """
    if not result.log_path:
        result.log_path = _keep(result, warning)
    text = _block(result, runlog.verbosity(), warning, shown_if)
    if not text:
        return
    out = sys.stderr if stream is None else stream
    if lock_file:
        with open(lock_file, "a") as handle, runlog.take_lock(handle):
            _write_block(out, text)
    else:
        _write_block(out, text)


def _write_block(out, text: str) -> None:
    """The block on a console row of its own: a status row pinned where it is
    about to go is erased first, and redraws itself under it."""
    if out is sys.stderr:
        statusline.clear_status()
    out.write(text)
    out.flush()
