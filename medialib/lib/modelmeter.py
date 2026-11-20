"""How long a model was at work and what it wrote: the account generate-chapters
and the picture-subtitle OCR give of their model.

A :class:`Tally` counts the requests and the tokens their replies report. A
:class:`Spans` holds the stretches of time the model was at work, and its
:attr:`Spans.seconds` joins the ones that overlap, so time two of them shared
is counted once: requests sent side by side, files chaptered at once, two
films' subtitles read together all take the wall-clock they took, not its sum,
and a pause between two stretches - the model waiting on whisper, say - is not
counted at all. :func:`rate_line` says one of them in a line.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

__all__ = ["Tally", "Spans", "rate_line"]


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
