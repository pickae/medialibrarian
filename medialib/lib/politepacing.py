"""The one way this library spaces out the requests it sends a site.

Every source asked over the network - an API with a stated rate, a volunteer's
server with none - is asked through a :class:`Pacer` of its own: each request
waits until the gap since the last one has run out, and the gap is a few
seconds with some jitter either side, drawn afresh for every request, so a run
over a library reads like somebody clicking through the site rather than a
metronome. An API that publishes its rate is given no jitter: spacing each call
evenly at the limit IS the rate.

A pacer is shared by everything that asks its source, the threads of one run
included: the wait is taken under a lock, so two threads asking the same site
queue for it rather than both going out at once.
"""

from __future__ import annotations

import random
import threading
import time
import weakref
from collections.abc import Callable

__all__ = ["Pacer", "reset_all"]

# Every pacer made, so a test can forget them all at once.
_ALL: weakref.WeakSet = weakref.WeakSet()


class Pacer:
    """Requests to one source, never closer together than ``seconds``, plus or
    minus up to ``jitter``.

    ``now`` and ``sleep`` are the clock and ``draw`` the dice, so a test waits
    for nothing and knows the gap it will be given. Left out, they are
    :mod:`time`'s and :mod:`random`'s, looked up at each request rather than
    when the pacer is made.
    """

    def __init__(self, seconds: float, jitter: float = 0.0, *,
                 now: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 draw: Callable[[float, float], float] | None = None
                 ) -> None:
        self.seconds = seconds
        self.jitter = min(jitter, seconds)
        self.now = now
        self.sleep = sleep
        self.draw = draw
        self._last: float | None = None
        self._lock = threading.Lock()
        _ALL.add(self)

    def wait(self) -> None:
        """Hold the next request back until its gap has run out, and count it
        as sent. The first request of a run goes straight out."""
        with self._lock:
            now = self.now or time.monotonic
            if self._last is not None:
                gap = self.seconds
                if self.jitter:
                    gap += (self.draw or random.uniform)(-self.jitter,
                                                         self.jitter)
                waiting = self._last + gap - now()
                if waiting > 0:
                    (self.sleep or time.sleep)(waiting)
            self._last = now()

    def reset(self) -> None:
        """Forget when the last request went out, so the next one goes
        immediately."""
        with self._lock:
            self._last = None


def reset_all() -> None:
    """Forget every pacer's last request. For a test that would otherwise pay
    the gap a previous test left behind."""
    for pacer in list(_ALL):
        pacer.reset()
