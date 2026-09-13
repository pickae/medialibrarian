"""Did the conversion keep the whole recording?

A transcode is one of the few operations whose failure looks exactly like its
success. The output file is there, it plays, it carries the cover and the
chapter marks, and it is simply not as long as what went in. Nothing downstream
notices: the next run's up-to-date check sees a short output and encodes it
again, which is a retry rather than a report, so a truncated book can sit in a
library for as long as nobody plays it to the end.

Two things in this pipeline truncate without failing, and both exit 0. A PIPE
between two processes ends wherever the reader stops reading, and a WAVE states
its length in 32 bits - so a book whose PCM is larger than 4 GiB comes out at
4 GiB and reports success. And a chunked encode that loses a chunk re-joins the
chunks it still has.

So every command here that writes a converted file asks the one question that
catches both without knowing either: is the output as long as the input. A file
that is not is named, removed - it is this run's own output, and the whole point
is that a truncated book must not survive the run that made it - and recorded,
so the run ends non-zero instead of printing "Done."

The record is a FILE named in the environment, because the checks run inside
worker PROCESSES: a count raised in one of them is invisible to the run that
started it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from medialib.lib import formatting, runlog

__all__ = [
    "TOLERANCE_SECONDS",
    "TOLERANCE_FRACTION",
    "LOG_VARIABLE",
    "SKIP_VARIABLE",
    "checking",
    "media_duration",
    "total_duration",
    "allowed_drift",
    "length_matches",
    "init_log",
    "record",
    "failures",
    "report",
    "verify",
]

# How far an output may be from its input before it is a failed conversion,
# whichever of the two is larger.
#
# The second absorbs what the comparison is made of rather than what the encoder
# did: a container states a duration its own samples only approximately add up
# to, and an encoder pads the last frame out. The fraction is for a source whose
# stated duration is an ESTIMATE - a VBR mp3 with no header frame is timed by
# dividing its size by its first frame's bitrate, and that is wrong by a
# percentage, not by a second. Neither shape of slop is what this looks for: a
# real truncation loses a third of a book, or five sixths of it.
TOLERANCE_SECONDS = 1.0
TOLERANCE_FRACTION = 0.01

# The one way to switch the measuring off, for a caller whose tools do not make
# media: the test suite's stubbed tier, where every "encode" is an empty file a
# stand-in touched, so every output is short of every input and the answer would
# be no every time. The same escape hatch the tool preflight has, and set the
# same way - never by this library, only by something that knows its tools are
# not real. A run with real tools does not have it, which is the point.
SKIP_VARIABLE = "SKIP_LENGTH_CHECK"

# Where the workers record what came out short. Read by the run that started
# them, once they have all finished.
LOG_VARIABLE = "LENGTH_LOG"

# Inside one record. A path may hold anything else.
_UNIT = "\t"


def checking() -> bool:
    """Whether this run measures what it produced."""
    return not os.environ.get(SKIP_VARIABLE, "")


def media_duration(path: str) -> float:
    """How long a media file is, in seconds, or 0 when nothing can say.

    The container's own figure, which is the same question every other duration
    in this library is asked - so the two sides of the comparison are measured
    the same way and a container convention cannot look like a lost hour.
    """
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return 0.0
    return formatting.awk_number(
        done.stdout.decode("utf-8", "surrogateescape").strip())


def total_duration(paths) -> float:
    """How long a list of files is together, for an output made by JOINING them.

    A file nothing can measure adds nothing, so one unreadable track among many
    understates the total rather than voiding it - the comparison then errs
    towards accepting the join, which is the right way for a check to be wrong.
    """
    return sum(media_duration(path) for path in paths)


def allowed_drift(source_seconds: object) -> float:
    """The slack for a source of this length."""
    seconds = abs(formatting.awk_number(source_seconds))
    return max(TOLERANCE_SECONDS, seconds * TOLERANCE_FRACTION)


def length_matches(source_seconds: object, output_seconds: object) -> bool:
    """Whether an output of this length is the whole of an input of that one.

    Both directions, not just the short one. Short is the failure that prompted
    this - an encoder that stopped early - but an output LONGER than its source
    by more than the padding of one frame is a re-join that took a chunk twice,
    which is the same class of silent wrongness seen from the other side.

    A source of no stated length is not a comparison at all: it answers true,
    because "this file says nothing about itself" is not evidence against the
    output. A caller with an EMPTY output has already failed for a better reason
    than this one.
    """
    source = formatting.awk_number(source_seconds)
    output = formatting.awk_number(output_seconds)
    if source <= 0:
        return True
    return abs(output - source) <= allowed_drift(source)


def init_log(path: str = "") -> str:
    """Settle where this run records the outputs that came out the wrong length,
    and empty it.

    Made by ``mkstemp`` rather than named and then opened, and a named path that
    is a symlink is refused: exclusive creation is the only way the file that
    gets truncated is certainly not somebody else's.

    A log a PARENT already opened is kept and appended to, so a wrapper that
    runs two conversions reports both runs' findings once at the end.
    """
    if path and not os.path.islink(path):
        os.environ[LOG_VARIABLE] = path
        open(path, "w").close()
        return path
    if path:
        sys.stderr.write("\nWARNING: the length log path is a symlink and was "
                         "not used:\n  %s\n" % path)
    else:
        inherited = os.environ.get(LOG_VARIABLE, "")
        if inherited and os.path.isfile(inherited) and not os.path.islink(
                inherited):
            return inherited
    directory = os.environ.get("TMPDIR", "/tmp")
    descriptor, path = tempfile.mkstemp(prefix="lengthMismatch.",
                                        dir=directory)
    os.close(descriptor)
    os.environ[LOG_VARIABLE] = path
    return path


def record(name: str, source_seconds: object, output_seconds: object) -> None:
    """Note one output that is not as long as its input.

    Appended, one open per record, because the writers are separate processes:
    a handle held open across a fork would interleave two workers' bytes in one
    line. A run that never called :func:`init_log` records nothing rather than
    creating a file nobody will read.
    """
    log_path = os.environ.get(LOG_VARIABLE, "")
    if not log_path:
        return
    line = _UNIT.join([
        str(name).replace(_UNIT, " ").replace("\n", " "),
        "%.3f" % formatting.awk_number(source_seconds),
        "%.3f" % formatting.awk_number(output_seconds),
    ])
    try:
        with open(log_path, "a", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def failures() -> list[tuple[str, float, float]]:
    """Everything recorded so far, as (name, source seconds, output seconds)."""
    log_path = os.environ.get(LOG_VARIABLE, "")
    if not log_path:
        return []
    try:
        with open(log_path, encoding="utf-8", errors="surrogateescape") as fh:
            # The newline terminates a record rather than separating two, so the
            # empty piece after the last one is not a record.
            lines = fh.read().split("\n")[:-1]
    except OSError:
        return []
    found = []
    for line in lines:
        fields = line.split(_UNIT)
        if len(fields) != 3:
            continue
        found.append((fields[0], formatting.awk_number(fields[1]),
                      formatting.awk_number(fields[2])))
    return found


def report(stream=None) -> int:
    """The closing recap, and how many files are in it.

    Silent when there is nothing to say: a run where every output matched its
    input says so by ending successfully, not by printing that it did.
    """
    found = failures()
    if not found:
        return 0
    out = sys.stderr if stream is None else stream
    out.write("%d file(s) did not convert to their full length:\n"
              % len(found))
    for name, source, output in found:
        out.write("  %s (input %s, output %s)\n"
                  % (name, formatting.fmt_hms("%.3f" % source),
                     formatting.fmt_hms("%.3f" % output)))
    return len(found)


def verify(name: str, source: str, output: str, source_seconds: object = None,
           remove: bool = True, log=runlog.log) -> bool:
    """One finished output measured against what it was made from.

    <source_seconds> is the source's length where the caller already probed it -
    which most of them have, since the same figure decides whether the file
    needed converting at all - and the source is probed here otherwise.

    A missing or empty output is a failure without a probe: there is nothing to
    measure, and ffprobe would answer 0 for both "no file" and "a file of no
    length", which are the same verdict anyway.

    <remove> is for the caller whose output is not its own to delete - a file
    that was already there, or one staged somewhere the caller will clean up
    itself.
    """
    if not checking():
        return True
    if source_seconds is None:
        source_seconds = media_duration(source)
    expected = formatting.awk_number(source_seconds)

    try:
        present = os.path.getsize(output) > 0
    except OSError:
        present = False
    produced = media_duration(output) if present else 0.0

    if present and length_matches(expected, produced):
        return True

    log("ERROR: the output is not as long as the input (input %s, output %s): "
        "%s" % (formatting.fmt_hms("%.3f" % expected),
                formatting.fmt_hms("%.3f" % produced), name))
    if remove:
        try:
            os.remove(output)
        except OSError:
            pass
    record(name, expected, produced)
    return False
