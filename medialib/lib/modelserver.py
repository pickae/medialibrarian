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
- started here and no longer answering though its process is still there, it
  is stopped and started again;
- started here, it is stopped once nothing has used it for ``idle`` seconds,
  and when the run ends at the latest;
- with no room on the GPU, or no command to start it with, it is not to be had
  now, and the caller goes down its ladder to what it can have.

A run that fans out to worker processes holds its servers in the run's own
process instead (:func:`held_for_run`): a worker lives for one item, and a
server it started would be stopped when it ends, under every other worker still
asking it. The run starts what is not answering, keeps it for the whole run,
starts it again when it goes down, and stops at the end only what it started.
A worker of such a run never starts or stops a server itself: it uses one that
answers, and waits for the run to bring back one that does not.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator

from medialib.lib import runcontext

__all__ = ["Server", "free_vram", "answering", "served_model", "stop_all",
           "held_for_run", "OWNER_VARIABLE"]

# The process that holds the run's servers (:func:`held_for_run`), by its pid,
# handed to the workers and the commands it starts.
OWNER_VARIABLE = "MODEL_SERVER_OWNER"

# How long a server started here has to answer, and how often it is asked.
START_TIMEOUT = 300
_POLL = 2.0
# How long a stopped server has to exit before it is killed.
_STOP_GRACE = 30
# How often the run holding its servers looks whether they still answer, and
# how many looks in a row a server whose process is still there may miss
# before it is started again - a server swapping models is slow to answer.
WATCH_SECONDS = 10.0
_WATCH_MISSES = 6

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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _owner() -> int:
    """The live process, other than this one, that holds the run's servers;
    0 where there is none."""
    try:
        pid = int(runcontext.get(OWNER_VARIABLE) or 0)
    except ValueError:
        return 0
    return pid if pid > 0 and pid != os.getpid() and _alive(pid) else 0


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
        self._answered = False
        self._misses = 0

    @property
    def started_here(self) -> bool:
        return self._process is not None

    def acquire(self, log: Callable[[str], None],
                until: float | None = None) -> bool:
        """Whether the server can be used now - answering, or started here
        and answering - and, when it can, it is held in use until
        :meth:`release`. In a worker of a run that holds its servers, only
        whether it answers, waiting for the run to bring it back - until
        ``until``, a time.monotonic() reading, where that comes sooner."""
        owner = _owner()
        if owner:
            return self._await(owner, until)
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not answering(self.url):
                if self._process is not None and self._process.poll() is None \
                        and self._answered:
                    log("  The model server for %s stopped answering; "
                        "restarting it" % self.url)
                    self._stop()
                if not self._start(log):
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

    def _await(self, owner: int, until: float | None) -> bool:
        deadline = time.monotonic() + START_TIMEOUT
        if until is not None:
            deadline = min(deadline, until)
        while True:
            if answering(self.url):
                return True
            if time.monotonic() >= deadline or not _alive(owner):
                return False
            time.sleep(_POLL)

    def keep_up(self, log: Callable[[str], None]) -> None:
        """Started again where it has stopped answering: at once when the
        process started here is gone, after :data:`_WATCH_MISSES` looks in a
        row otherwise. A server found running that went away is started here
        as one would be that was never running."""
        with self._lock:
            if answering(self.url):
                self._misses = 0
                return
            self._misses += 1
            gone = self._process is not None and self._process.poll() is not None
            if not gone and self._misses < _WATCH_MISSES:
                return
            self._misses = 0
            if self._process is not None:
                log("  The model server for %s stopped answering; restarting it"
                    % self.url)
                self._stop()
            self._start(log)

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
                self._answered = True
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
        self._answered = False
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


@contextlib.contextmanager
def held_for_run(servers: Iterable[Server], log: Callable[[str], None],
                 watch: float = WATCH_SECONDS) -> Iterator[list[Server]]:
    """``servers`` held by this process for as long as the block runs, for
    the worker processes it starts - see the module. Each is started where it
    is not answering and there is room for it, watched every ``watch``
    seconds and started again when it goes down, and stopped at the end when
    it was started here; one found running is left running. Yields the ones
    that answer as the block starts.

    Nothing is held where another live process already holds the run's
    servers: that one's workers, and so this process's, use what it holds."""
    if _owner():
        yield []
        return
    unique = list({id(server): server for server in servers}.values())
    answered = [server for server in unique if server.acquire(log)]
    had = runcontext.get(OWNER_VARIABLE, None)
    runcontext.settle(OWNER_VARIABLE, str(os.getpid()))
    done = threading.Event()

    def look() -> None:
        while not done.wait(watch):
            for server in unique:
                server.keep_up(log)

    watcher = threading.Thread(target=look, name="model-server-watch",
                               daemon=True)
    watcher.start()
    try:
        yield answered
    finally:
        done.set()
        watcher.join()
        if had is None:
            runcontext.forget(OWNER_VARIABLE)
        else:
            runcontext.settle(OWNER_VARIABLE, had)
        for server in unique:
            with server._lock:
                server._users = 0
            server.stop()


atexit.register(stop_all)
