"""How long a model was at work and what it wrote: the account every use of a
model gives of it - generate-chapters, the picture-subtitle OCR, the cover
search.

A :class:`Tally` counts the requests and the tokens their replies report. A
:class:`Spans` holds the stretches of time the model was at work, and its
:attr:`Spans.seconds` joins the ones that overlap, so time two of them shared
is counted once: requests sent side by side, files chaptered at once, two
films' subtitles read together all take the wall-clock they took, not its sum,
and a pause between two stretches - the model waiting on whisper, say - is not
counted at all. :func:`rate_line` says one of them in a line, and
:func:`record` and :func:`gathered` carry them across a run's worker
processes.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

__all__ = ["Tally", "Spans", "rate_line", "record", "gathered"]


@dataclass
class Tally:
    """What the requests counted into it took: how many answered, the seconds
    each was under way summed, and the tokens the model wrote (``tokens``)
    and read (``read``), from those whose server reported them (``counted``
    of them, over ``counted_seconds``). A harness reports none. Safe to add
    to from many threads."""

    requests: int = 0
    seconds: float = 0.0
    tokens: int = 0
    counted: int = 0
    counted_seconds: float = 0.0
    read: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False,
                                  repr=False, compare=False)

    def add(self, seconds: float, tokens: int | None,
            read: int | None = None) -> None:
        with self._lock:
            self.requests += 1
            self.seconds += seconds
            if tokens is not None:
                self.tokens += tokens
                self.read += read or 0
                self.counted += 1
                self.counted_seconds += seconds

    def merge(self, other: Tally) -> None:
        with other._lock:
            taken = (other.requests, other.seconds, other.tokens,
                     other.counted, other.counted_seconds, other.read)
        with self._lock:
            self.requests += taken[0]
            self.seconds += taken[1]
            self.tokens += taken[2]
            self.counted += taken[3]
            self.counted_seconds += taken[4]
            self.read += taken[5]


class Spans:
    """The stretches of time something was at work, as (start, end) on the
    monotonic clock. Safe to add to from many threads."""

    def __init__(self) -> None:
        self._spans: list[tuple[float, float]] = []
        self._lock = threading.Lock()

    def add(self, start: float, end: float) -> None:
        with self._lock:
            self._spans.append((start, end))

    @contextlib.contextmanager
    def timing(self) -> Iterator[None]:
        """The block timed as one stretch."""
        started = time.monotonic()
        try:
            yield
        finally:
            self.add(started, time.monotonic())

    def merge(self, other: Spans) -> None:
        with other._lock:
            taken = list(other._spans)
        with self._lock:
            self._spans += taken

    def all(self) -> list[tuple[float, float]]:
        with self._lock:
            return list(self._spans)

    @property
    def seconds(self) -> float:
        """The wall-clock: the stretches joined where they overlap."""
        return joined(self.all())


def joined(spans) -> float:
    """The time the (start, end) stretches cover, each moment once."""
    total, end = 0.0, None
    for start, stop in sorted(spans):
        if end is None or start >= end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total


def record(path: str, spans: Spans, tally: Tally) -> None:
    """``spans`` and ``tally`` added to the record at ``path``, for a run whose
    model is asked from several worker processes: :func:`gathered` reads them
    back added up. One line a call, appended whole. Never raises - the record
    is an account, and losing it is no reason to lose the work."""
    with tally._lock:
        counted = {"requests": tally.requests, "seconds": tally.seconds,
                   "tokens": tally.tokens, "counted": tally.counted,
                   "counted_seconds": tally.counted_seconds, "read": tally.read}
    line = json.dumps({"spans": spans.all(), "tally": counted}) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def gathered(path: str) -> tuple[Spans, Tally]:
    """Everything :func:`record` wrote to ``path``, added up; nothing where
    there is no record. The stretches are on the monotonic clock, which every
    process of one machine shares, so overlapping ones still join."""
    spans, tally = Spans(), Tally()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return spans, tally
    for line in lines:
        # Read whole before any of it is added: a line torn in the middle is
        # skipped, not half counted.
        try:
            entry = json.loads(line)
            stretches = [(float(start), float(end)) for start, end in entry["spans"]]
            counted = entry["tally"]
            figures = (int(counted["requests"]), float(counted["seconds"]),
                       int(counted["tokens"]), int(counted["counted"]),
                       float(counted["counted_seconds"]), int(counted["read"]))
        except (ValueError, KeyError, TypeError):
            continue
        for start, end in stretches:
            spans.add(start, end)
        tally.requests += figures[0]
        tally.seconds += figures[1]
        tally.tokens += figures[2]
        tally.counted += figures[3]
        tally.counted_seconds += figures[4]
        tally.read += figures[5]
    return spans, tally


def rate_line(name: str, wall: float, tally: Tally) -> str:
    """One line: the requests, the wall-clock, the tokens written with tokens
    a second over that wall-clock - and, where several requests ran, over the
    time each was under way, which is the model's own speed when they ran side
    by side - and the tokens read, where the server said."""
    text = "%-9s %3d request(s) in %7.1fs" % (name + ":", tally.requests, wall)
    if not tally.counted:
        return text + (", tokens not reported" if tally.requests else "")
    text += ", %d token(s) written, %.1f tok/s" % (
        tally.tokens, tally.tokens / wall if wall > 0 else 0.0)
    if tally.counted > 1 and tally.counted_seconds > 0:
        text += " (%.1f tok/s a request)" % (tally.tokens / tally.counted_seconds)
    if tally.read:
        text += ", %d read, %d in all" % (tally.read, tally.read + tally.tokens)
    if tally.counted < tally.requests:
        text += ", %d request(s) uncounted" % (tally.requests - tally.counted)
    return text
