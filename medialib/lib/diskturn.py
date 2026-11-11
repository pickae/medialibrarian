"""Whose turn it is on a disk: one disk-bound job at a time per spinning disk.

A run that does several kinds of work at once - transcoding on the CPU,
transcribing on the GPU, remuxing, downloading - gains from the overlap only
while the kinds use different parts of the machine. Two jobs that each read a
whole film as fast as the disk can give it do not overlap usefully on a
spinning disk: the head seeks between the two and each takes longer than both
would one after the other. So the jobs bound by the disk - a remux, a stream
copied or transcoded out of a film, a film read through for its subtitle
timings - take a TURN on the disk the film is on, and a turn is held by one job
at a time.

Only a disk that spins is taken turns on. One that does not - an SSD, NVMe -
reads several films at once about as fast as it reads one, and is left to be
shared the way it always was. A disk that cannot be told - a network or FUSE
file system, a btrfs subvolume - is taken to spin: the cautious answer is the
one that never makes a disk do two things at once.

A disk is what ``st_dev`` names, read off the path or the nearest folder above
it that exists. Two file systems on one physical disk are two turns here; that
is the price of a probe that needs no root and no parsing of the mount table,
and it is the uncommon layout for a film library.

The turns are shared between the threads of the run and its worker processes
through a lock file per disk, in the folder :func:`init` names and a worker
:func:`adopt` s. Without one - a command that never set it up - a turn is still
taken between the threads of this process, and a worker takes none.
"""

from __future__ import annotations

import contextlib
import os
import threading

from medialib.lib import runcontext

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: the thread lock alone
    fcntl = None  # type: ignore[assignment]

__all__ = ["init", "adopt", "lock_dir", "turn", "rotational", "device_of"]

_LOCKS: dict = {}
_SPINS: dict = {}
_GUARD = threading.Lock()
# How deep this thread is in a turn of each disk, so a job that holds one and
# reaches code that asks for it again is not left waiting on itself.
_DEPTH = threading.local()


def init(directory: str) -> str:
    """Share the turns through lock files in ``directory``, made here; the
    folder, for the workers to :func:`adopt`, or "" when it cannot be made."""
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        directory = ""
    runcontext.current().disk_turns = directory
    return directory


def adopt(directory: str) -> None:
    """A worker's half of :func:`init`: the folder the run's turns are in."""
    runcontext.current().disk_turns = directory or ""


def lock_dir() -> str:
    return runcontext.current().disk_turns


def device_of(path: str) -> int | None:
    """The ``st_dev`` of ``path``, or of the nearest folder above it that
    exists; None when not even the root can be read."""
    probe = os.path.abspath(path) if path else os.getcwd()
    while True:
        try:
            return os.stat(probe).st_dev
        except OSError:
            parent = os.path.dirname(probe)
            if parent == probe:
                return None
            probe = parent


def rotational(path: str, sysfs: str = "/sys/dev/block") -> bool:
    """Whether the disk ``path`` is on spins - True when it cannot be told.

    Read from the block device's ``queue/rotational``. A partition has none of
    its own and is answered by the disk it is a partition of, the folder above
    it in sysfs; a device-mapper device - an encrypted disk, a logical volume -
    spins when any device under it does, whatever it says of itself."""
    device = device_of(path)
    if device is None or not hasattr(os, "major"):
        # No device, or no sysfs to read it in: Windows.
        return True
    major, minor = os.major(device), os.minor(device)
    if major == 0:
        # An anonymous device: tmpfs, a network or FUSE file system, a btrfs
        # subvolume. No block device to ask.
        return True
    return _spins(os.path.realpath(os.path.join(sysfs, "%d:%d"
                                                % (major, minor))))


def _spins(block: str, depth: int = 0) -> bool:
    try:
        below = [entry for entry in os.listdir(os.path.join(block, "slaves"))
                 if entry]
    except OSError:
        below = []
    if below and depth < 8:
        return any(_spins(os.path.realpath(os.path.join(block, "slaves",
                                                        entry)), depth + 1)
                   for entry in below)
    for candidate in (block, os.path.dirname(block)):
        try:
            with open(os.path.join(candidate, "queue", "rotational"),
                      encoding="ascii") as handle:
                return handle.read().strip() != "0"
        except OSError:
            continue
    return True


def _spinning(device, path: str) -> bool:
    with _GUARD:
        if device in _SPINS:
            return _SPINS[device]
    spins = rotational(path)
    with _GUARD:
        _SPINS[device] = spins
    return spins


def _lock_for(device) -> threading.Lock:
    with _GUARD:
        lock = _LOCKS.get(device)
        if lock is None:
            lock = _LOCKS[device] = threading.Lock()
        return lock


@contextlib.contextmanager
def turn(path: str):
    """Hold the turn on the disk ``path`` is on for as long as the block runs
    - at once on a disk that does not spin, which is not taken turns on."""
    device = device_of(path)
    if device is None or not _spinning(device, path):
        yield
        return
    depths = getattr(_DEPTH, "by_device", None)
    if depths is None:
        depths = _DEPTH.by_device = {}
    if depths.get(device):
        depths[device] += 1
        try:
            yield
        finally:
            depths[device] -= 1
        return
    with _lock_for(device):
        handle = _open_lock_file(device)
        try:
            if handle is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            depths[device] = 1
            try:
                yield
            finally:
                depths[device] = 0
        finally:
            if handle is not None:
                handle.close()


def _open_lock_file(device):
    """The lock file of ``device`` in the shared folder, open; None without a
    folder, or with one that has gone - the thread lock alone then."""
    directory = runcontext.current().disk_turns
    if not directory or fcntl is None:
        return None
    try:
        return open(os.path.join(directory, "disk-%d.lock" % device), "a")
    except OSError:
        return None
