"""Which kind of host this is, for the handful of decisions that differ by it.

Three answers, because three is how many the rest of the package can act on:
``"linux"``, ``"macos"`` and ``"windows"``, with anything else (a BSD, an
illumos) reading as ``"linux"`` - not because it is Linux, but because every
decision keyed on this asks "does the POSIX shape hold here", and on those it
does.

What actually differs on macOS, and where it is acted on:

  * the package manager a refusal names - Homebrew, not apt
    (:mod:`medialib.lib.tooldeps`);
  * where a hand-installed or Homebrew build of ffmpeg lands - ``/opt/homebrew``
    on Apple Silicon, ``/usr/local`` on Intel (:mod:`medialib.lib.ffmpegselect`);
  * where large regenerable per-user data belongs - ``~/Library/Caches``, not
    ``~/.cache`` (:mod:`medialib.lib.ramscratch`);
  * there is no ``flock(1)`` and no ``/dev/shm``, and the first of those is a
    capability the C library still has (:mod:`medialib.lib.runlog`);
  * the hardware video path is VideoToolbox rather than VAAPI or NVENC
    (``medialib/cli/convert_video_run.py``).

Read through a function rather than a module-level constant, so a test can say
what the host is for one case without the import order deciding it.
"""

from __future__ import annotations

import sys

__all__ = ["host_kind", "is_macos", "is_windows", "is_posix"]


def host_kind(platform: str | None = None) -> str:
    """``"linux"``, ``"macos"`` or ``"windows"`` for ``sys.platform``.

    ``platform`` is the value to read instead, which is how a case asks what
    the answer would be somewhere else.
    """
    name = sys.platform if platform is None else platform
    if name == "darwin":
        return "macos"
    if name.startswith("win") or name == "cygwin":
        return "windows"
    return "linux"


def is_macos(platform: str | None = None) -> bool:
    return host_kind(platform) == "macos"


def is_windows(platform: str | None = None) -> bool:
    return host_kind(platform) == "windows"


def is_posix(platform: str | None = None) -> bool:
    """Linux and macOS both: a host with the POSIX facilities the ports of the
    shell functions assume - signals, file locking, an execute bit."""
    return host_kind(platform) != "windows"
