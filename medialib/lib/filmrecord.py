"""One record per film: what the probes said about it, kept while the file
stays the same.

ingest-movies reads a film's tracks in nearly every pass, and on a spinning
disk each read is a seek into the film. A :class:`Film` keeps what a probe
printed, so the next pass to ask gets the same answer without asking the file
again.

An answer is only as good as the file it was read from, so a record is tied
to the file by its stat signature - device, inode, size, and the modification
and change times to the nanosecond. A pass that rewrites the film (a mux, the
improved copy swapped in, a header edited in place) changes the signature,
and the next read finds the record stale and probes afresh. The change time
is what catches a header edit whose time stamp was put back afterwards, and
an inode is what tells a copy swapped in under the same name from the file it
replaced. The passes that write a film also :func:`forget` it outright, so a
file system with coarse time stamps cannot hand back a stale answer either.

What a probe printed is kept as bytes, not as the objects read from it: the
callers change the tracks they are given (an action, an opus beside it), and
each read builds its own.

The records live in this process. A worker is handed its film's record with
the work (:func:`of`, pickled), and :func:`adopt` s it, so a probe made before
the pool started is not made again in the worker.
"""

from __future__ import annotations

import os
import subprocess
import threading

__all__ = ["Film", "Answer", "of", "adopt", "output", "run", "forget",
           "reset"]

# Records past this many are dropped oldest first: a library is read once per
# run, and a run over a very large one should not hold every film's answers.
MAX_RECORDS = 4096


class Answer:
    """What one probe printed: its exit status and its standard output, the
    shape of a finished ``subprocess.run`` as far as a reader needs one."""

    __slots__ = ("returncode", "stdout")

    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout


class Film:
    """A film's path, the signature its answers were read under, and the
    answers by the command that gave them."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.signature: tuple | None = None
        self.answers: dict[tuple, Answer] = {}

    def __getstate__(self) -> dict:
        return {"path": self.path, "signature": self.signature,
                "answers": {argv: (answer.returncode, answer.stdout)
                            for argv, answer in self.answers.items()}}

    def __setstate__(self, state: dict) -> None:
        self.path = state["path"]
        self.signature = state["signature"]
        self.answers = {argv: Answer(*answer)
                        for argv, answer in state["answers"].items()}


_RECORDS: dict[str, Film] = {}
_GUARD = threading.Lock()


def _signature(path: str) -> tuple | None:
    try:
        seen = os.stat(path)
    except OSError:
        return None
    return (seen.st_dev, seen.st_ino, seen.st_size, seen.st_mtime_ns,
            seen.st_ctime_ns)


def of(path: str) -> Film:
    """The film's record as it stands, answers and all - empty when the file
    has changed since they were read, or none were."""
    signature = _signature(path)
    with _GUARD:
        record = _RECORDS.get(path)
        if record is None or signature is None \
                or record.signature != signature:
            return Film(path)
        copy = Film(path)
        copy.signature = record.signature
        copy.answers = dict(record.answers)
        return copy


def adopt(record: Film) -> None:
    """A record handed over from another process, kept here as this
    process's own - still checked against the file on every read."""
    if record.signature is None or not record.answers:
        return
    with _GUARD:
        _keep(record)


def _keep(record: Film) -> None:
    _RECORDS.pop(record.path, None)
    _RECORDS[record.path] = record
    while len(_RECORDS) > MAX_RECORDS:
        del _RECORDS[next(iter(_RECORDS))]


def output(argv: list, path: str) -> Answer:
    """What ``argv``, a probe of the file at ``path``, prints: from the
    film's record while the file is unchanged, else run, and kept when the
    file did not change under it.

    Standard input and standard error are closed off, as a probe's are.
    OSError - the tool is not there - is raised as ``subprocess.run`` raises
    it, and nothing is kept.
    """
    key = tuple(argv)
    before = _signature(path)
    if before is not None:
        with _GUARD:
            record = _RECORDS.get(path)
            if record is not None and record.signature == before:
                answer = record.answers.get(key)
                if answer is not None:
                    return answer
    done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    answer = Answer(done.returncode, done.stdout or b"")
    if before is None or _signature(path) != before:
        return answer
    with _GUARD:
        record = _RECORDS.get(path)
        if record is None or record.signature != before:
            record = Film(path)
            record.signature = before
        record.answers[key] = answer
        _keep(record)
    return answer


def run(argv: list, **_kwargs) -> Answer:
    """:func:`output` in the shape of a runner - ``(argv, **kwargs)`` to a
    result with ``returncode`` and ``stdout`` - for a reader that takes one;
    the file probed is the last argument."""
    return output(argv, argv[-1])


def forget(path: str) -> None:
    """Whatever was read about the file at ``path``, dropped: it has just
    been written, or replaced."""
    with _GUARD:
        _RECORDS.pop(path, None)


def reset() -> None:
    """Every record dropped."""
    with _GUARD:
        _RECORDS.clear()
