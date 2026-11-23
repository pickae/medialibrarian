"""Tests for medialib.lib.hostmem."""

from medialib.lib import hostmem, hostos


class TestParseProcMeminfo:
    def test_prefers_memavailable_over_bare_memfree(self, tmp_path,
                                                    monkeypatch):
        """Linux page cache counts as available, not used."""
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            "MemTotal:       65536000 kB\n"
            "MemFree:         1024000 kB\n"
            "MemAvailable:   48000000 kB\n"
            "Buffers:         2048000 kB\n"
            "Cached:         40000000 kB\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(hostos, "is_windows", lambda: False)
        monkeypatch.setattr(hostos, "is_macos", lambda: False)

        assert hostmem.available_ram_bytes(str(meminfo)) == 48000000 * 1024
        assert hostmem.total_ram_bytes(str(meminfo)) == 65536000 * 1024

    def test_falls_back_to_reclaimable_sum_without_memavailable(
            self, tmp_path, monkeypatch):
        """When MemAvailable is absent, Buffers + Cached + SReclaimable are
        added to MemFree rather than treating cache as used RAM."""
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            "MemTotal:       32000000 kB\n"
            "MemFree:         1000000 kB\n"
            "Buffers:         2000000 kB\n"
            "Cached:         15000000 kB\n"
            "SReclaimable:    3000000 kB\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(hostos, "is_windows", lambda: False)
        monkeypatch.setattr(hostos, "is_macos", lambda: False)

        expected = (1000000 + 2000000 + 15000000 + 3000000) * 1024
        assert hostmem.available_ram_bytes(str(meminfo)) == expected

    def test_missing_file_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hostos, "is_windows", lambda: False)
        monkeypatch.setattr(hostos, "is_macos", lambda: False)

        missing = str(tmp_path / "no-such-meminfo")
        assert hostmem.available_ram_bytes(missing) == 0
        assert hostmem.total_ram_bytes(missing) == 0


class TestMacosAvailable:
    def test_counts_reclaimable_pages_from_vm_stat(self, monkeypatch):
        sample = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                             10000.\n"
            "Pages active:                          500000.\n"
            "Pages inactive:                        200000.\n"
            "Pages speculative:                      50000.\n"
            "Pages purgeable:                        40000.\n"
            "Pages wired down:                      100000.\n"
        )

        class _Done:
            stdout = sample

        monkeypatch.setattr(hostos, "is_windows", lambda: False)
        monkeypatch.setattr(hostos, "is_macos", lambda: True)
        monkeypatch.setattr(hostmem.subprocess, "run", lambda *a, **k: _Done())

        assert hostmem.available_ram_bytes() == (
            (10000 + 200000 + 50000 + 40000) * 16384
        )
