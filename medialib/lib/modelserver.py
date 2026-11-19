"""A model server a run starts for itself when it needs one, and stops again.

A local model server holds most of a GPU, so the one a step would rather use
is often not running - another holds the card - and a run should neither make
the user start it beforehand nor leave it holding the card afterwards. A
:class:`Server` is an address and, optionally, the command that starts the
server there:

- answering already, it is used, and left as it was found - it is not this
  run's to stop;
- not answering, it is started - in a process group of its own, its output
  kept in a log beside the run's scratch - where the GPU has the room it needs
  free, and used once it answers;
- started here, it is stopped once nothing has used it for ``idle`` seconds,
  and when the run ends at the latest;
- with no room on the GPU, or no command to start it with, it is not to be had
  now, and the caller goes down its ladder to what it can have.
"""

from __future__ import annotations

import atexit
import json
import os
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable

__all__ = ["Server", "free_vram", "answering", "served_model", "stop_all"]

# How long a server started here has to answer, and how often it is asked.
START_TIMEOUT = 300
_POLL = 2.0
# How long a stopped server has to exit before it is killed.
_STOP_GRACE = 30

# Every server this process started, so the run's end stops them all.
_started: list = []
_started_lock = threading.Lock()


def free_vram() -> int:
    """The first GPU's free memory in MiB, 0 without one to ask."""
    try:
        done = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                               "--format=csv,noheader,nounits"],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
        return int((done.stdout or "0").split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def root(url: str) -> str:
    """The server's address without the API path a user may have given."""
    base = url.strip().rstrip("/")
    for tail in ("/v1/chat/completions", "/v1"):
        if base.endswith(tail):
            base = base[:-len(tail)]
    return base if "://" in base else "http://" + base


def served_model(url: str):
    """The models the server at ``url`` lists at ``/v1/models``: [] when it
    lists none, None when it does not answer at all."""
    try:
        with urllib.request.urlopen(root(url) + "/v1/models",
                                    timeout=10) as reply:
            listing = json.loads(reply.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    try:
        return [str(entry.get("id") or "") for entry in listing.get("data")
                or [] if isinstance(entry, dict)]
    except AttributeError:
        return []


def answering(url: str) -> bool:
    return served_model(url) is not None


class Server:
    """One model server: its address, and how to start it where it is not
    running - see the module."""

    def __init__(self, url: str, command: str = "", need_mib: int = 0,
                 idle: float = 300.0) -> None:
        self.url = url
        self.command = command
        self.need_mib = need_mib
        self.idle = idle
        self._process: subprocess.Popen | None = None
        self._log_path = ""
        self._lock = threading.Lock()
        self._users = 0
        self._timer: threading.Timer | None = None

    @property
    def started_here(self) -> bool:
        return self._process is not None

    def acquire(self, log: Callable[[str], None]) -> bool:
        """Whether the server can be used now - answering, or started here
        and answering - and, when it can, it is held in use until
        :meth:`release`."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not answering(self.url) and not self._start(log):
                return False
            self._users += 1
            return True

    def release(self) -> None:
        """The use :meth:`acquire` began is over: a server started here is
        stopped once nothing has used it for ``idle`` seconds."""
        with self._lock:
            self._users = max(0, self._users - 1)
            if self._process is None or self._users:
                return
            self._timer = threading.Timer(self.idle, self._idle_stop)
            self._timer.daemon = True
            self._timer.start()

    def _start(self, log: Callable[[str], None]) -> bool:
        if self._process is not None and self._process.poll() is None:
            return self._wait(log)
        if not self.command:
            return False
        free = free_vram()
        if free < self.need_mib:
            return False
        handle, self._log_path = tempfile.mkstemp(
            prefix="model-server-", suffix=".log",
            dir=os.environ.get("TMPDIR"))
        log("  Starting the model server for %s (%d MiB free on the GPU): %s"
            % (self.url, free, self.command))
        try:
            self._process = subprocess.Popen(
                shlex.split(self.command), stdin=subprocess.DEVNULL,
                stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True)
        except OSError as error:
            os.close(handle)
            log("  WARNING: the model server could not be started: %s"
                % error)
            return False
        os.close(handle)
        with _started_lock:
            _started.append(self)
        return self._wait(log)

    def _wait(self, log: Callable[[str], None]) -> bool:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                break
            if answering(self.url):
                return True
            time.sleep(_POLL)
        log("  WARNING: the model server for %s did not come up, see %s"
            % (self.url, self._log_path))
        self._stop()
        return False

    def _idle_stop(self) -> None:
        with self._lock:
            if not self._users:
                self._stop()

    def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=_STOP_GRACE)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except OSError:
            pass

    def stop(self) -> None:
        """A server started here stopped now; one found running is left."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._stop()


def stop_all() -> None:
    """Every server this process started, stopped: the run is over."""
    with _started_lock:
        servers = list(_started)
        _started.clear()
    for server in servers:
        server.stop()


atexit.register(stop_all)
