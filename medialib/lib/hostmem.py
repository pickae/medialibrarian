"""Host physical memory probes, shared across the package.

On Linux, ``/proc/meminfo``'s ``MemFree`` counts only untouched pages and treats
the kernel's optional page cache as "used", even though the kernel reclaims
that cache immediately when an application asks for memory. Every RAM check in
the repository goes through here so each one sees truly available memory (free
plus reclaimable cache) rather than bare ``MemFree``.
"""

from __future__ import annotations

import ctypes
import re
import subprocess

from medialib.lib import hostos

__all__ = ["available_ram_bytes", "total_ram_bytes"]

_VM_STAT_PAGE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_LINE = re.compile(r"^([^:]+):\s+(\d+)\.")


def _parse_proc_meminfo(text: str) -> dict[str, int]:
    """Parse ``/proc/meminfo`` values into bytes keyed by field name."""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        unit = parts[1].lower() if len(parts) > 1 else ""
        scale = 1024 if unit == "kb" else 1
        fields[name] = value * scale
    return fields


def _linux_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    try:
        with open(path, encoding="utf-8") as handle:
            return _parse_proc_meminfo(handle.read())
    except OSError:
        return {}


def _linux_available(fields: dict[str, int]) -> int:
    # MemAvailable is the kernel's own estimate of memory available to new
    # processes without swapping (free + reclaimable page cache + SReclaimable,
    # minus low watermarks). Fall back to summing those fields only when
    # MemAvailable is absent.
    if "MemAvailable" in fields:
        return fields["MemAvailable"]
    reclaimable = (fields.get("MemFree", 0)
                   + fields.get("Buffers", 0)
                   + fields.get("Cached", 0)
                   + fields.get("SReclaimable", 0))
    return reclaimable


def _macos_available() -> int:
    """Free + inactive + speculative + purgeable pages from ``vm_stat``."""
    try:
        out = subprocess.run(["vm_stat"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    match = _VM_STAT_PAGE.search(out)
    if not match:
        return 0
    page_size = int(match.group(1))
    reclaimable_keys = {
        "Pages free",
        "Pages inactive",
        "Pages speculative",
        "Pages purgeable",
    }
    pages = 0
    for line in out.splitlines():
        found = _VM_STAT_LINE.match(line.strip())
        if found and found.group(1) in reclaimable_keys:
            pages += int(found.group(2))
    return pages * page_size


def _macos_total() -> int:
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True,
                             timeout=5).stdout.strip()
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _windows_memory() -> tuple[int, int]:
    """``(available_bytes, total_bytes)`` via ``GlobalMemoryStatusEx``."""
    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    kernel32 = getattr(ctypes, "windll", None)
    if kernel32 is None:
        return 0, 0
    try:
        if not kernel32.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0, 0
    except OSError:
        return 0, 0
    return int(status.ullAvailPhys), int(status.ullTotalPhys)


def available_ram_bytes(meminfo_path: str = "/proc/meminfo") -> int:
    """Truly available physical RAM in bytes (including reclaimable OS cache),
    or 0 when it cannot be determined."""
    if hostos.is_windows():
        return _windows_memory()[0]
    if hostos.is_macos():
        return _macos_available()
    return _linux_available(_linux_meminfo(meminfo_path))


def total_ram_bytes(meminfo_path: str = "/proc/meminfo") -> int:
    """Total installed physical RAM in bytes, or 0 when it cannot be read."""
    if hostos.is_windows():
        return _windows_memory()[1]
    if hostos.is_macos():
        return _macos_total()
    return _linux_meminfo(meminfo_path).get("MemTotal", 0)
