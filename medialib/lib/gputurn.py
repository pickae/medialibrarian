"""Whose turn it is on the GPU, for work that would not both fit in its memory.

Two kinds of work in one run may want the one card at once - the commentary
transcription's whisper runs and a language model served from this machine -
and a card that holds one does not always hold both. A run that starts the
second anyway is an out-of-memory failure, and whisper-ctranslate2 reports
that with a zero exit status.

A job takes a CLAIM for as long as it runs: its kind, and the MiB it needs
(None for "the whole card"). Claims of the same kind never wait for each
other - the plan that sized them already fitted them together. A claim of
another kind is let in beside the ones already running when the card has the
room for it, and otherwise waits for them to finish. Claims are files under
one directory that every process of the run shares, read and written under an
flock, so a transcription in a worker process and a request on a thread of
the main one see each other.

Free memory is what nvidia-smi says, which does not yet show a job that has
only just started loading, so a claim younger than :data:`SETTLE_SECONDS` is
counted as holding its whole need on top of what is free. A claimant waits for
the ones that began waiting before it, so a kind that keeps arriving cannot
keep the other out for ever. Without nvidia-smi there is no card to share and
every claim is granted at once.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import threading
import time
from collections.abc import Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: the thread lock alone
    fcntl = None  # type: ignore[assignment]

__all__ = ["Gate", "GateAborted", "free_vram_mib"]

# How long a granted claim is assumed not to be in nvidia-smi's figure yet:
# loading a model's weights takes seconds, and its peak is what the claim needs.
SETTLE_SECONDS = 30

# How often a waiting claim looks again.
POLL_SECONDS = 2


_COUNTER = itertools.count()
_THREADS = threading.Lock()


class GateAborted(Exception):
    """The run was told to stop while a claim was waiting for its turn."""


def free_vram_mib() -> int | None:
    """The first GPU's free memory in MiB, or None when there is no card or
    nvidia-smi cannot be asked."""
    from medialib.lib import whisper

    lines = whisper._nvidia_smi(["--query-gpu=memory.free",
                                 "--format=csv,noheader,nounits"]).splitlines()
    try:
        return int(lines[0].strip())
    except (IndexError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class Gate:
    """The claims of one run, kept in ``directory``."""

    def __init__(self, directory: str, free: Callable = free_vram_mib,
                 clock: Callable = time.time, sleep: Callable = time.sleep,
                 alive: Callable = _alive) -> None:
        self._directory = directory
        self._free = free
        self._clock = clock
        self._sleep = sleep
        self._alive = alive
        os.makedirs(directory, exist_ok=True)

    @contextlib.contextmanager
    def claim(self, kind: str, need_mib: int | None,
              abort: Callable[[], bool] = lambda: False,
              say: Callable[[str], None] = lambda line: None):
        """Hold a claim of ``kind`` needing ``need_mib`` MiB (None: the whole
        card) while the block runs, after waiting for its turn. Raises
        :class:`GateAborted` if ``abort`` turns true while it waits."""
        name = "%d.%d.%d" % (os.getpid(), threading.get_ident(),
                             next(_COUNTER))
        arrived = self._clock()
        said = False
        try:
            while not self._try(name, kind, need_mib, arrived):
                if abort():
                    raise GateAborted(kind)
                if not said:
                    said = True
                    say("Waiting for the GPU (%s needs %s)" % (
                        kind, "all of it" if need_mib is None
                        else "%d MiB" % need_mib))
                self._sleep(POLL_SECONDS)
            yield
        finally:
            with self._locked(), contextlib.suppress(OSError):
                os.remove(os.path.join(self._directory, name))

    @contextlib.contextmanager
    def _locked(self):
        with _THREADS, open(os.path.join(self._directory, ".lock"),
                            "a+") as handle:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _entries(self) -> dict:
        found = {}
        for name in os.listdir(self._directory):
            if name.startswith("."):
                continue
            path = os.path.join(self._directory, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    entry = json.load(handle)
            except (OSError, ValueError):
                continue
            if not self._alive(int(name.split(".", 1)[0])):
                with contextlib.suppress(OSError):
                    os.remove(path)
                continue
            found[name] = entry
        return found

    def _try(self, name: str, kind: str, need, arrived: float) -> bool:
        with self._locked():
            now = self._clock()
            others = {key: entry for key, entry in self._entries().items()
                      if key != name and entry["kind"] != kind}
            active = [entry for entry in others.values()
                      if entry["state"] == "active"]
            ahead = [entry for entry in others.values()
                     if entry["state"] == "waiting"
                     and entry["arrived"] < arrived]
            granted = not active and not ahead
            if not granted:
                free = self._free()
                if free is None:
                    # No card to share.
                    granted = True
                elif not ahead and need is not None and all(
                        entry["need"] is not None for entry in active):
                    reserved = sum(entry["need"] for entry in active
                                   if now - entry["since"] < SETTLE_SECONDS)
                    granted = free - reserved >= need
            self._write(name, {"kind": kind, "need": need, "arrived": arrived,
                               "state": "active" if granted else "waiting",
                               "since": now})
            return granted

    def _write(self, name: str, entry: dict) -> None:
        path = os.path.join(self._directory, name)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(entry, handle)
        os.replace(temporary, path)
