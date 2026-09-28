"""A slow source asked ahead of need, on a thread of its own.

A site asked at a polite pace takes seconds a question, and a library asks it
hundreds. A run that knows early which questions it will have - long before it
can act on the answers - hands them to a :class:`Research`, whose thread asks
them one after another while the run gets on with everything else. When the
run reaches the phase that wants the answers it asks the same questions again,
and each one it was ahead on is answered from memory, at once.

Answers are kept by the QUESTION and never by a file: the run keeps renaming
and remuxing its films while the thread works, and a film that has moved is
still the same question. One the thread never got to, or got a different
question for, is simply asked then - the research is ahead of the run, never
the only way to an answer. For the same reason a walk that trips over a file
the run moved from under it is only a walk cut short, and nothing the run is
told about.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from medialib.lib import safety

__all__ = ["Research"]


class Research:
    """``ask`` answered ahead of need, and remembered.

    A question is whatever ``ask`` is called with, and must be hashable.
    ``keep`` says whether an answer is worth remembering: one that says the
    source did not answer is worth asking again later.
    """

    def __init__(self, name: str, ask: Callable,
                 keep: Callable = lambda _answer: True) -> None:
        self.name = name
        self._ask = ask
        self._keep = keep
        self._answers: dict = {}
        self._work: list = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def ahead(self, work: Callable[[], None]) -> None:
        """Run ``work`` on the thread, after whatever it was handed before.
        ``work`` asks its questions through this object - finding out what to
        ask, probing the films, is on the thread too, and not paid for by the
        run either - and looks at :func:`safety.abort_requested` between
        them."""
        with self._lock:
            self._work.append(work)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="research-" + self.name,
                    daemon=True)
                self._thread.start()

    def __call__(self, *question):
        """The answer to ``question``: remembered, or asked now."""
        with self._lock:
            if question in self._answers:
                return self._answers[question]
        answer = self._ask(*question)
        if self._keep(answer):
            with self._lock:
                self._answers[question] = answer
        return answer

    def finish(self, log: Callable[[str], None]) -> None:
        """Wait for the thread to have done everything it was handed - which
        a run that started it early has usually long since done."""
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            log("Waiting for the %s research to finish" % self.name)
            thread.join()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._work or safety.abort_requested():
                    self._work.clear()
                    self._thread = None
                    return
                work = self._work.pop(0)
            try:
                work()
            except Exception:  # noqa: BLE001 - see the module docstring
                pass
