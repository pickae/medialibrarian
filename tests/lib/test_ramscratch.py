"""The white box for medialib/lib/ramscratch.py.

The create probe that decides a base directory usable, the init chain that
settles the run directory (and the re-entry that keeps it one per run), the
filesystem the mount table names, the free-space share the size check reads
(the tmpfs cap, the disk pass-through), the disk spill base, the two size
decisions, and the per-shell cleanup list with the EXIT trap that releases it.

The module works the filesystem directly, so the cases work a real directory and
what is asserted is what is on disk afterwards. Two things a test cannot have on
the host it runs on are stood in - the RAM filesystem (``SHM``) and the mount
table (``MOUNTS``), both named in the module for that reason - and one is faked,
``_disk_usage``, because a filesystem of a chosen size is the whole input to the
share calculation.
"""

import os
import stat
import sys

import pytest

from medialib.lib import ramscratch

pytestmark = pytest.mark.fs

# 100 MiB total, 50 MiB free: under the default 1 GiB headroom, so the spill
# logic sees it.
SMALL = (104857600, 52428800)
# 10 GiB total, 5 GiB free: over the headroom, so the spill logic does not.
BIG = (10737418240, 5368709120)

_POSIX_MODES = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the mode bits a read-only tree is granted back are POSIX's")

# A mount table names its points in POSIX paths, and the one every table has is
# "/" - above nothing on a host whose paths start with a drive letter. So a case
# whose answer comes from the ROOT line has no answer on Windows; the ones whose
# answer comes from a real directory hold there and are not gated.
_POSIX_ROOT_MOUNT = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fallback mount point `/` is not above a Windows path")


class _Usage:
    def __init__(self, total, free):
        self.total = total
        self.free = free
        self.used = total - free


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    # A name of its own, so a case about WHERE the run directory goes is not also
    # a case about what names it when nobody said. That default has two cases of
    # its own below.
    ramscratch.reset_state(script="a-command")
    # Neither of the two roots the module would otherwise reach is the host's
    # own: /dev/shm is shared with every process on the machine, and the
    # platform temporary directory is where a leftover run directory would
    # outlive the suite.
    monkeypatch.setattr(ramscratch, "SHM", str(tmp_path / "shm"))
    monkeypatch.setattr(ramscratch.tempfile, "tempdir", str(tmp_path / "plat"))
    os.makedirs(tmp_path / "plat")
    monkeypatch.delenv("ramScratchBase", raising=False)
    monkeypatch.delenv("ramScratchDiskBase", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    yield tmp_path


def _shm(tmp_path):
    """The stood-in RAM filesystem, made usable."""
    os.makedirs(tmp_path / "shm")
    return str(tmp_path / "shm")


def _base(tmp_path):
    base = tmp_path / "ram"
    os.makedirs(base)
    ramscratch._STATE.ram_base = str(base)
    return str(base)


def _mounts(tmp_path, monkeypatch, lines):
    """A mount table of this test's own, in the format /proc/self/mounts has.

    The mount points go in RESOLVED, the way the real table holds them: the
    lookup resolves what it is asked about, so a table naming a path through a
    symlink would match nothing.
    """
    table = tmp_path / "mounts"
    table.write_text("".join(lines), encoding="utf-8")
    monkeypatch.setattr(ramscratch, "MOUNTS", str(table))
    return str(table)


def _as_tmpfs(tmp_path, monkeypatch, directory, total_free=SMALL):
    """Have the module see ``directory`` as a tmpfs of a chosen size."""
    _mounts(tmp_path, monkeypatch,
            ["rootfs / ext4 rw 0 0\n",
             "shm %s tmpfs rw 0 0\n" % os.path.realpath(directory)])
    monkeypatch.setattr(ramscratch, "_disk_usage",
                        lambda _d: _Usage(*total_free))


class TestRamBaseUsable:
    def test_a_directory_that_takes_a_probe(self, tmp_path):
        assert ramscratch.ram_base_usable(str(tmp_path)) is True

    def test_and_the_probe_is_removed_again(self, tmp_path):
        before = sorted(p.name for p in tmp_path.iterdir())
        ramscratch.ram_base_usable(str(tmp_path))
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_a_directory_that_is_not_there(self, tmp_path):
        assert ramscratch.ram_base_usable(str(tmp_path / "absent")) is False

    def test_a_file_is_not_a_base(self, tmp_path):
        target = tmp_path / "file"
        target.write_text("x")
        assert ramscratch.ram_base_usable(str(target)) is False

    @_POSIX_MODES
    def test_a_directory_that_refuses_the_probe(self, tmp_path):
        """The reason the probe CREATES something: -d and -w can both say yes
        about a directory that then refuses every mkdir."""
        closed = tmp_path / "closed"
        closed.mkdir(mode=0o500)
        try:
            assert ramscratch.ram_base_usable(str(closed)) is False
        finally:
            closed.chmod(0o700)


class TestInitRamBase:
    def test_happy_path_makes_and_registers_the_run_dir(self, tmp_path):
        shm = _shm(tmp_path)
        state = ramscratch._STATE
        ramscratch.init_ram_base("")
        assert os.path.dirname(state.ram_base) == shm
        assert os.path.basename(state.ram_base).startswith("a-command.run.")
        assert os.path.isdir(state.ram_base)
        assert state.cleanup == [state.ram_base]
        assert state.trap == "runExitCleanup"

    def test_reentry_keeps_the_existing_base(self, tmp_path):
        base = _base(tmp_path)
        _shm(tmp_path)
        ramscratch.init_ram_base("")
        assert ramscratch._STATE.ram_base == base
        assert ramscratch._STATE.trap is None

    @_POSIX_MODES
    def test_unusable_existing_base_is_replaced(self, tmp_path):
        shm = _shm(tmp_path)
        closed = tmp_path / "closed"
        closed.mkdir(mode=0o500)
        ramscratch._STATE.ram_base = str(closed)
        try:
            ramscratch.init_ram_base("")
        finally:
            closed.chmod(0o700)
        assert os.path.dirname(ramscratch._STATE.ram_base) == shm

    def test_override_wins_over_everything(self, tmp_path, monkeypatch):
        _shm(tmp_path)
        env_base = tmp_path / "envbase"
        env_base.mkdir()
        monkeypatch.setenv("ramScratchBase", str(env_base))
        override = tmp_path / "override"
        override.mkdir()
        ramscratch.init_ram_base(str(override))
        assert os.path.dirname(ramscratch._STATE.ram_base) == str(override)

    def test_env_base_after_a_refused_override(self, tmp_path, monkeypatch):
        _shm(tmp_path)
        env_base = tmp_path / "envbase"
        env_base.mkdir()
        monkeypatch.setenv("ramScratchBase", str(env_base))
        ramscratch.init_ram_base(str(tmp_path / "absent"))
        assert os.path.dirname(ramscratch._STATE.ram_base) == str(env_base)

    def test_the_tmpfs_after_a_refused_env_base(self, tmp_path, monkeypatch):
        shm = _shm(tmp_path)
        monkeypatch.setenv("ramScratchBase", str(tmp_path / "absent"))
        ramscratch.init_ram_base("")
        assert os.path.dirname(ramscratch._STATE.ram_base) == shm

    def test_falls_to_the_platform_temporary_directory(self, tmp_path):
        """No host has a user-space RAM disk to fall back to, so this is the
        last resort everywhere - and on Windows it is the only one there ever
        is."""
        ramscratch.init_ram_base("")
        assert os.path.dirname(ramscratch._STATE.ram_base) == \
            str(tmp_path / "plat")

    def test_tmpdir_when_the_tmpfs_cannot_be_had(self, tmp_path, monkeypatch):
        named = tmp_path / "tmpdir"
        named.mkdir()
        monkeypatch.setenv("TMPDIR", str(named))
        ramscratch.init_ram_base("")
        assert os.path.dirname(ramscratch._STATE.ram_base) == str(named)

    def test_run_dir_failure_falls_back_to_the_root(self, tmp_path, monkeypatch):
        """The root probed usable a moment ago, so this is a race or a full
        tmpfs: a run that starts beats a run that refuses to."""
        shm = _shm(tmp_path)
        calls = []

        def refuse(parent, prefix):
            calls.append(prefix)
            if prefix.startswith("a-command.run."):
                return ""
            return real(parent, prefix)

        real = ramscratch._mkdtemp
        monkeypatch.setattr(ramscratch, "_mkdtemp", refuse)
        ramscratch.init_ram_base("")
        assert ramscratch._STATE.ram_base == shm
        assert ramscratch._STATE.cleanup == []
        assert ramscratch._STATE.trap is None

    def test_a_callers_trap_is_not_replaced(self, tmp_path):
        _shm(tmp_path)
        ramscratch._STATE.trap = "echo caller"
        ramscratch.init_ram_base("")
        assert ramscratch._STATE.trap == "echo caller"

    def test_script_name_names_the_run_dir(self, tmp_path):
        _shm(tmp_path)
        ramscratch.reset_state(script="convert-video")
        ramscratch.init_ram_base("")
        assert os.path.basename(ramscratch._STATE.ram_base).startswith(
            "convert-video.run.")

    def test_nobody_said_so_the_command_names_it(self, tmp_path, monkeypatch):
        """No production caller names the run directory, so this default is what
        the RAM filesystem actually holds during a run - and what tells a reader
        which of the eighteen commands is using the memory."""
        _shm(tmp_path)
        monkeypatch.setenv("CLI_PROGRAM", "ingest-music")
        ramscratch.reset_state()
        ramscratch.init_ram_base("")
        assert os.path.basename(ramscratch._STATE.ram_base).startswith(
            "ingest-music.run.")

    def test_with_nothing_handed_down_it_is_argv0(self, tmp_path, monkeypatch):
        """${0##*/}, which is what the shell named it after."""
        _shm(tmp_path)
        monkeypatch.delenv("CLI_PROGRAM", raising=False)
        monkeypatch.setattr(sys, "argv", ["/somewhere/convert-audio", "-h"])
        ramscratch.reset_state()
        ramscratch.init_ram_base("")
        assert os.path.basename(ramscratch._STATE.ram_base).startswith(
            "convert-audio.run.")


class TestFilesystemType:
    """What ``stat -f -c %T`` used to answer, read out of the mount table
    instead: statfs has the number, and Python exposes no way to ask for it."""

    def test_the_longest_matching_mount_point_wins(self, tmp_path, monkeypatch):
        deep = tmp_path / "a" / "b"
        os.makedirs(deep)
        _mounts(tmp_path, monkeypatch, [
            "rootfs / ext4 rw 0 0\n",
            "shm %s tmpfs rw 0 0\n" % os.path.realpath(tmp_path / "a"),
            "disk %s xfs rw 0 0\n" % os.path.realpath(tmp_path),
        ])
        assert ramscratch.filesystem_type(str(deep)) == "tmpfs"

    @_POSIX_ROOT_MOUNT
    def test_a_directory_under_nothing_but_the_root(self, tmp_path, monkeypatch):
        _mounts(tmp_path, monkeypatch, ["rootfs / ext4 rw 0 0\n"])
        assert ramscratch.filesystem_type(str(tmp_path)) == "ext4"

    def test_a_mount_point_with_a_space_in_it(self, tmp_path, monkeypatch):
        spaced = tmp_path / "my mount"
        spaced.mkdir()
        _mounts(tmp_path, monkeypatch, [
            "rootfs / ext4 rw 0 0\n",
            "shm %s tmpfs rw 0 0\n"
            % os.path.realpath(spaced).replace(" ", "\\040"),
        ])
        assert ramscratch.filesystem_type(str(spaced)) == "tmpfs"

    @_POSIX_ROOT_MOUNT
    def test_a_sibling_whose_name_only_starts_the_same(self, tmp_path,
                                                       monkeypatch):
        os.makedirs(tmp_path / "mountpoint")
        os.makedirs(tmp_path / "mountpointer")
        _mounts(tmp_path, monkeypatch, [
            "rootfs / ext4 rw 0 0\n",
            "shm %s tmpfs rw 0 0\n" % os.path.realpath(tmp_path / "mountpoint"),
        ])
        assert ramscratch.filesystem_type(
            str(tmp_path / "mountpointer")) == "ext4"

    @_POSIX_ROOT_MOUNT
    def test_a_short_line_is_skipped(self, tmp_path, monkeypatch):
        _mounts(tmp_path, monkeypatch, ["broken\n", "rootfs / ext4 rw 0 0\n"])
        assert ramscratch.filesystem_type(str(tmp_path)) == "ext4"

    def test_no_mount_table_at_all_is_no_answer(self, tmp_path, monkeypatch):
        """Which is the Windows case, and a container built without /proc: a
        host with no mount table has no tmpfs to find either."""
        monkeypatch.setattr(ramscratch, "MOUNTS", str(tmp_path / "absent"))
        assert ramscratch.filesystem_type(str(tmp_path)) == ""

    def test_the_host_s_own_table_names_the_ram_filesystem(self):
        """The one case that reads the real /proc, so a change in its format
        would be caught rather than papered over by the fixtures above."""
        if not os.path.exists("/proc/self/mounts") or not os.path.isdir("/dev/shm"):
            pytest.skip("no mount table or no RAM filesystem on this host")
        assert ramscratch.filesystem_type("/dev/shm") in ("tmpfs", "ramfs")


class TestRamDirFreeBytes:
    def test_missing_directory_is_unworkable(self, tmp_path):
        assert ramscratch.ram_dir_free_bytes(str(tmp_path / "absent")) is None

    def test_a_filesystem_that_cannot_be_measured(self, tmp_path, monkeypatch):
        def refuse(_directory):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(ramscratch, "_disk_usage", refuse)
        assert ramscratch.ram_dir_free_bytes(str(tmp_path)) is None

    def test_tmpfs_is_capped_by_the_share(self, tmp_path, monkeypatch):
        # 10 GiB total, the default half: total/100 first (the shell's
        # $((total / 100 * pct))), so the cap is 5368709100, a hair under
        # the free space itself.
        monkeypatch.delenv("ramScratchMaxPercent", raising=False)
        _as_tmpfs(tmp_path, monkeypatch, str(tmp_path), BIG)
        assert ramscratch.ram_dir_free_bytes(str(tmp_path)) == 5368709100

    def test_tmpfs_cap_clamped_by_the_free_space(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ramScratchMaxPercent", "90")
        # 10 GiB * 90% = 9.6 GiB > the 5 GiB free, so the free space stands
        _as_tmpfs(tmp_path, monkeypatch, str(tmp_path), BIG)
        assert ramscratch.ram_dir_free_bytes(str(tmp_path)) == 5368709120

    def test_tmpfs_cap_under_the_free_space(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ramScratchMaxPercent", "10")
        # 10 GiB * 10%, total/100 first: 1073741820 < the free space
        _as_tmpfs(tmp_path, monkeypatch, str(tmp_path), BIG)
        assert ramscratch.ram_dir_free_bytes(str(tmp_path)) == 1073741820

    def test_ramfs_is_a_tmpfs_to_the_share(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ramScratchMaxPercent", raising=False)
        _mounts(tmp_path, monkeypatch, ["shm %s ramfs rw 0 0\n" % os.path.realpath(tmp_path)])
        monkeypatch.setattr(ramscratch, "_disk_usage", lambda _d: _Usage(*BIG))
        assert ramscratch.ram_dir_free_bytes(str(tmp_path)) == 5368709100

    def test_disk_base_passes_the_free_space_through(self, tmp_path, monkeypatch):
        _mounts(tmp_path, monkeypatch, ["disk %s ext4 rw 0 0\n" % os.path.realpath(tmp_path)])
        monkeypatch.setattr(ramscratch, "_disk_usage", lambda _d: _Usage(*BIG))
        assert ramscratch.ram_dir_free_bytes(str(tmp_path)) == 5368709120

    def test_a_filesystem_the_table_cannot_name_is_a_disk(self, tmp_path,
                                                          monkeypatch):
        # the shell's `|| fsType=""`: no answer takes the pass-through rather
        # than the share
        monkeypatch.setattr(ramscratch, "MOUNTS", str(tmp_path / "absent"))
        monkeypatch.setattr(ramscratch, "_disk_usage", lambda _d: _Usage(*BIG))
        assert ramscratch.ram_dir_free_bytes(str(tmp_path)) == 5368709120

    @pytest.mark.parametrize("raw,expected", [
        ("0", 5368709100),       # out of range: the default half
        ("101", 5368709100),
        ("1", 107374182),        # 10 GiB * 1%, total/100 first
        ("100", 5368709120),     # the whole thing, clamped by the free space
        ("junk", 5368709100),
    ])
    def test_percent_boundaries(self, tmp_path, monkeypatch, raw, expected):
        monkeypatch.setenv("ramScratchMaxPercent", raw)
        _as_tmpfs(tmp_path, monkeypatch, str(tmp_path), BIG)
        assert ramscratch.ram_dir_free_bytes(str(tmp_path)) == expected

    def test_the_host_s_own_ram_filesystem_answers_something(self):
        """The measurement end to end, against whatever the host really has."""
        if not os.path.isdir("/dev/shm"):
            pytest.skip("no RAM filesystem on this host")
        assert ramscratch.ram_dir_free_bytes("/dev/shm") >= 0


class TestRamDiskBase:
    def test_env_disk_base_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ramScratchDiskBase", str(tmp_path / "disk"))
        assert ramscratch.ram_disk_base() == str(tmp_path / "disk")
        assert os.path.isdir(tmp_path / "disk")

    def test_xdg_cache_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        assert ramscratch.ram_disk_base() == \
            os.path.join(str(tmp_path / "cache"), "ramScratchOverflow")

    def test_the_cache_home_when_there_is_no_xdg(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        assert ramscratch.ram_disk_base() == os.path.join(
            str(tmp_path / "home"), ".cache", "ramScratchOverflow")

    def test_a_home_the_environment_does_not_name(self, monkeypatch, tmp_path):
        """A default Windows shell sets no HOME at all, and reading it directly
        left the base empty and had the spill refused. expanduser has the
        platform's own answer."""
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)
        base = ramscratch.ram_disk_base()
        assert base is None or base.startswith(os.path.expanduser("~"))

    def test_the_windows_cache_home_is_localappdata(self, monkeypatch, tmp_path):
        local = tmp_path / "AppData" / "Local"
        os.makedirs(local)
        monkeypatch.setattr(ramscratch.os, "name", "nt")
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        assert ramscratch._user_cache_home() == str(local)

    def test_and_the_posix_one_is_the_xdg_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ramscratch.os, "name", "posix")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        # expanduser reads USERPROFILE first on Windows, and this case is about
        # the branch rather than about the platform running it.
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        assert ramscratch._user_cache_home() == os.path.join(
            str(tmp_path / "home"), ".cache")

    @_POSIX_MODES
    def test_a_base_that_cannot_be_made(self, monkeypatch, tmp_path):
        closed = tmp_path / "closed"
        closed.mkdir(mode=0o500)
        monkeypatch.setenv("ramScratchDiskBase", str(closed / "overflow"))
        try:
            assert ramscratch.ram_disk_base() is None
        finally:
            closed.chmod(0o700)

    def test_a_base_that_is_a_file(self, monkeypatch, tmp_path):
        target = tmp_path / "file"
        target.write_text("x")
        monkeypatch.setenv("ramScratchDiskBase", str(target))
        assert ramscratch.ram_disk_base() is None


class TestRamScratchDir:
    def test_happy_path(self, tmp_path, monkeypatch):
        base = _base(tmp_path)
        _as_tmpfs(tmp_path, monkeypatch, base, BIG)
        name, status = ramscratch.ram_scratch_dir("pages")
        assert status == 0
        assert os.path.dirname(name) == base
        assert os.path.basename(name).startswith("pages.")
        assert os.path.isdir(name)

    def test_spill_to_disk_says_so_in_the_log(self, monkeypatch, tmp_path):
        base = _base(tmp_path)
        monkeypatch.setenv("ramScratchDiskBase", str(tmp_path / "disk"))
        _as_tmpfs(tmp_path, monkeypatch, base, SMALL)
        logs = []
        name, status = ramscratch.ram_scratch_dir("pages", log=logs.append)
        assert status == 0
        assert os.path.dirname(name) == str(tmp_path / "disk")
        assert os.path.basename(name).startswith(".pagesScratch.")
        assert logs == ['  RAM scratch is full, putting "pages" on disk '
                        "instead: %s" % name]

    def test_the_spill_threshold_is_inclusive(self, monkeypatch, tmp_path):
        # free is exactly the headroom: the shell's <= consults the disk base
        base = _base(tmp_path)
        monkeypatch.setenv("ramScratchMinFreeBytes", "52428800")
        monkeypatch.setenv("ramScratchDiskBase", str(tmp_path / "disk"))
        _as_tmpfs(tmp_path, monkeypatch, base, SMALL)
        name, _ = ramscratch.ram_scratch_dir("pages")
        assert os.path.dirname(name) == str(tmp_path / "disk")

    def test_the_headroom_one_byte_below_free_stays_in_ram(self, monkeypatch,
                                                           tmp_path):
        base = _base(tmp_path)
        # free is one byte OVER the headroom: no spill is even consulted
        monkeypatch.setenv("ramScratchMinFreeBytes", "52428799")
        monkeypatch.setenv("ramScratchDiskBase", str(tmp_path / "disk"))
        _as_tmpfs(tmp_path, monkeypatch, base, SMALL)
        name, _ = ramscratch.ram_scratch_dir("pages")
        assert os.path.dirname(name) == base
        assert not os.path.exists(tmp_path / "disk")

    def test_a_free_space_the_check_cannot_read_stays_in_ram(self, tmp_path,
                                                             monkeypatch):
        base = _base(tmp_path)

        def refuse(_directory):
            raise OSError(5, "I/O error")

        monkeypatch.setenv("ramScratchDiskBase", str(tmp_path / "disk"))
        monkeypatch.setattr(ramscratch, "_disk_usage", refuse)
        name, _ = ramscratch.ram_scratch_dir("pages")
        assert os.path.dirname(name) == base

    def test_a_spill_the_disk_refuses_falls_back_to_ram(self, tmp_path,
                                                        monkeypatch):
        base = _base(tmp_path)
        _as_tmpfs(tmp_path, monkeypatch, base, SMALL)
        # a disk base that is a file: it can be neither made nor probed
        target = tmp_path / "file"
        target.write_text("x")
        monkeypatch.setenv("ramScratchDiskBase", str(target))
        logs = []
        name, _ = ramscratch.ram_scratch_dir("pages", log=logs.append)
        assert os.path.dirname(name) == base
        assert logs == []

    def test_a_ram_base_that_refuses_the_directory(self, tmp_path, monkeypatch):
        base = _base(tmp_path)
        _as_tmpfs(tmp_path, monkeypatch, base, BIG)
        monkeypatch.setattr(ramscratch, "_mkdtemp", lambda _p, _x: "")
        assert ramscratch.ram_scratch_dir("pages") == ("", 1)

    def test_no_base_yet_initialises_first(self, tmp_path):
        shm = _shm(tmp_path)
        name, _ = ramscratch.ram_scratch_dir("pages")
        assert os.path.dirname(ramscratch._STATE.ram_base) == shm
        assert os.path.dirname(name) == ramscratch._STATE.ram_base


class TestRamScratchFile:
    def test_happy_path(self, tmp_path):
        base = _base(tmp_path)
        name, status = ramscratch.ram_scratch_file("queue")
        assert status == 0
        assert os.path.dirname(name) == base
        assert os.path.basename(name).startswith("queue.")
        assert os.path.isfile(name)
        assert os.path.getsize(name) == 0

    def test_a_base_that_refuses_the_file(self, tmp_path):
        ramscratch._STATE.ram_base = str(tmp_path / "absent")
        assert ramscratch.ram_scratch_file("queue") == ("", 1)


class TestRamScratchDirFor:
    def test_what_fits_stays_in_ram(self, tmp_path, monkeypatch):
        base = _base(tmp_path)
        _as_tmpfs(tmp_path, monkeypatch, base, (10737418240, 209715200))
        name, on_disk, status = ramscratch.ram_scratch_dir_for(
            "104857600", "remux")
        assert (on_disk, status) == (0, 0)
        assert os.path.dirname(name) == base

    def test_one_byte_more_goes_to_disk(self, monkeypatch, tmp_path):
        base = _base(tmp_path)
        monkeypatch.setenv("ramScratchDiskBase", str(tmp_path / "disk"))
        _as_tmpfs(tmp_path, monkeypatch, base, (10737418240, 104857600))
        name, on_disk, status = ramscratch.ram_scratch_dir_for(
            "104857601", "remux")
        assert (on_disk, status) == (1, 0)
        assert os.path.dirname(name) == str(tmp_path / "disk")
        assert os.path.basename(name).startswith(".remuxScratch.")

    def test_a_named_disk_parent_is_not_asked_to_be_made(self, tmp_path,
                                                         monkeypatch):
        base = _base(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setenv("ramScratchDiskBase", str(tmp_path / "cachebase"))
        _as_tmpfs(tmp_path, monkeypatch, base, (10737418240, 104857600))
        name, on_disk, _ = ramscratch.ram_scratch_dir_for(
            "99999999999", "remux", str(dest))
        assert on_disk == 1
        assert os.path.dirname(name) == str(dest)
        assert not os.path.exists(tmp_path / "cachebase")

    def test_a_disk_parent_the_spill_refuses_falls_back_to_ram(self, tmp_path,
                                                               monkeypatch):
        base = _base(tmp_path)
        _as_tmpfs(tmp_path, monkeypatch, base, (10737418240, 104857600))
        name, on_disk, status = ramscratch.ram_scratch_dir_for(
            "99999999999", "remux", str(tmp_path / "absent"))
        assert (on_disk, status) == (0, 0)
        assert os.path.dirname(name) == base

    def test_no_disk_parent_asks_the_cache_base(self, monkeypatch, tmp_path):
        base = _base(tmp_path)
        monkeypatch.setenv("ramScratchDiskBase", str(tmp_path / "disk"))
        _as_tmpfs(tmp_path, monkeypatch, base, (10737418240, 104857600))
        name, on_disk, _ = ramscratch.ram_scratch_dir_for(
            "99999999999", "remux")
        assert on_disk == 1
        assert os.path.dirname(name) == str(tmp_path / "disk")

    def test_a_byte_count_the_check_cannot_read_stays_in_ram(self, tmp_path,
                                                             monkeypatch):
        base = _base(tmp_path)
        monkeypatch.setenv("ramScratchDiskBase", str(tmp_path / "disk"))
        _as_tmpfs(tmp_path, monkeypatch, base, (10737418240, 1))
        name, on_disk, _ = ramscratch.ram_scratch_dir_for("many", "remux")
        assert on_disk == 0
        assert os.path.dirname(name) == base

    def test_a_free_space_the_check_cannot_read_stays_in_ram(self, tmp_path,
                                                             monkeypatch):
        base = _base(tmp_path)

        def refuse(_directory):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(ramscratch, "_disk_usage", refuse)
        name, on_disk, _ = ramscratch.ram_scratch_dir_for(
            "99999999999", "remux")
        assert on_disk == 0
        assert os.path.dirname(name) == base

    def test_a_refused_ram_mktemp_refuses_the_file(self, tmp_path, monkeypatch):
        _base(tmp_path)
        _as_tmpfs(tmp_path, monkeypatch, str(tmp_path), BIG)
        monkeypatch.setattr(ramscratch, "_mkdtemp", lambda _p, _x: "")
        assert ramscratch.ram_scratch_dir_for("1", "remux") == ("", 0, 1)


class TestCleanupList:
    def _base(self, state, tmp_path):
        """What every real run has settled before it registers anything: the
        parent it took its scratch under. The cleanup will not act outside it."""
        state.bases = [str(tmp_path)]

    def _materialise(self, tmp_path, names):
        paths = []
        for name in names:
            path = tmp_path / name
            path.mkdir()
            (path / "content").write_text("x")
            paths.append(str(path))
        return paths

    def test_add_appends(self):
        state = ramscratch._STATE
        ramscratch.add_exit_cleanup(["/a", "/b"], state=state)
        ramscratch.add_exit_cleanup(["/c"], state=state)
        assert state.cleanup == ["/a", "/b", "/c"]

    def test_run_releases_everything_that_exists(self, tmp_path):
        state = ramscratch._STATE
        self._base(state, tmp_path)
        paths = self._materialise(tmp_path, ["one", "two"])
        state.cleanup = paths + [str(tmp_path / "absent"), ""]
        assert ramscratch.run_exit_cleanup(state=state) == 0
        assert state.cleanup == []
        assert not any(os.path.exists(p) for p in paths)

    def test_a_registered_file_is_released_too(self, tmp_path):
        state = ramscratch._STATE
        self._base(state, tmp_path)
        target = tmp_path / "queue"
        target.write_text("x")
        state.cleanup = [str(target)]
        ramscratch.run_exit_cleanup(state=state)
        assert not target.exists()

    def test_run_on_an_empty_list_touches_nothing(self, tmp_path):
        before = sorted(p.name for p in tmp_path.iterdir())
        assert ramscratch.run_exit_cleanup() == 0
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    @_POSIX_MODES
    def test_a_read_only_tree_is_granted_back_before_it_goes(self, tmp_path):
        """An archive can extract read-only directories, and a directory needs
        write and execute before its own entries can be removed."""
        state = ramscratch._STATE
        self._base(state, tmp_path)
        root = tmp_path / "extracted"
        (root / "inner").mkdir(parents=True)
        (root / "inner" / "page.jpg").write_text("x")
        (root / "inner" / "page.jpg").chmod(0o400)
        (root / "inner").chmod(0o500)
        root.chmod(0o500)
        state.cleanup = [str(root)]
        assert ramscratch.run_exit_cleanup(state=state) == 0
        assert not root.exists()

    @_POSIX_MODES
    def test_an_executable_keeps_its_execute_bit_and_a_plain_file_gets_none(
            self, tmp_path):
        """u+X, not u+x: the execute bit only where something already had one.
        Asserted on what the grant leaves behind rather than on the removal,
        which cannot tell the two apart."""
        script = tmp_path / "tool"
        script.write_text("x")
        script.chmod(0o050)
        plain = tmp_path / "page.jpg"
        plain.write_text("x")
        plain.chmod(0o040)
        ramscratch._grant_tree_access(str(tmp_path))
        assert stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR
        assert not stat.S_IMODE(plain.stat().st_mode) & stat.S_IXUSR

    @_POSIX_MODES
    def test_a_symlink_out_of_the_tree_is_not_followed(self, tmp_path):
        """The mode of a link is not a thing POSIX has, and following one would
        change a file the run never owned."""
        outside = tmp_path / "outside"
        outside.write_text("x")
        outside.chmod(0o400)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "link").symlink_to(outside)
        state = ramscratch._STATE
        self._base(state, tmp_path)
        state.cleanup = [str(scratch)]
        ramscratch.run_exit_cleanup(state=state)
        assert not scratch.exists()
        assert outside.exists()
        assert stat.S_IMODE(outside.stat().st_mode) == 0o400

    def test_release_hands_back_just_the_targets(self, tmp_path):
        state = ramscratch._STATE
        self._base(state, tmp_path)
        paths = self._materialise(tmp_path, ["one", "two", "three"])
        state.cleanup = paths
        assert ramscratch.release_exit_cleanup([paths[1]], state=state) == 0
        assert state.cleanup == [paths[0], paths[2]]
        assert not os.path.exists(paths[1])
        assert os.path.isdir(paths[0]) and os.path.isdir(paths[2])

    def test_release_drops_every_occurrence_of_a_target(self, tmp_path):
        # Under this test's own directory rather than a short absolute name:
        # release REMOVES what exists, and "/a" is drive-relative on Windows -
        # `D:\a`, which on a CI runner is the whole work tree.
        state = ramscratch._STATE
        one, two = str(tmp_path / "one"), str(tmp_path / "two")
        state.cleanup = [one, two, one]
        ramscratch.release_exit_cleanup([one], state=state)
        assert state.cleanup == [two]

    def test_release_of_a_path_that_is_gone_says_nothing(self, tmp_path):
        state = ramscratch._STATE
        state.cleanup = [str(tmp_path / "a"), str(tmp_path / "b")]
        ramscratch.release_exit_cleanup([str(tmp_path / "b")], state=state)
        assert state.cleanup == [str(tmp_path / "a")]

    def test_release_then_run_share_one_list(self, tmp_path):
        state = ramscratch._STATE
        self._base(state, tmp_path)
        paths = self._materialise(tmp_path, ["a", "b", "c"])
        state.cleanup = paths
        ramscratch.release_exit_cleanup([paths[0]], state=state)
        ramscratch.add_exit_cleanup([str(tmp_path / "late")], state=state)
        ramscratch.run_exit_cleanup(state=state)
        assert state.cleanup == []
        assert not any(os.path.exists(p) for p in paths)


class TestTheCleanupRegistryRefusesTheDangerousShapes:
    """The failure mode these guard against is not a wrong answer, it is
    `chmod -R u+rwX /` followed by `rm -rf /` - which is what registering one
    path as if it were a list of them actually did."""

    def test_a_bare_string_is_refused_rather_than_iterated(self):
        state = ramscratch._State()
        with pytest.raises(TypeError) as raised:
            ramscratch.add_exit_cleanup("/dev/shm/scratch.XXXX", state=state)
        assert "one character at a time" in str(raised.value)
        assert state.cleanup == []

    def test_a_list_is_still_the_way_to_register(self):
        state = ramscratch._State()
        ramscratch.add_exit_cleanup(["/a", "/b"], state=state)
        assert state.cleanup == ["/a", "/b"]

    @pytest.mark.parametrize("dangerous", ["/", "//", "/.", "", "   ",
                                           "/tmp/..", None, os.sep])
    def test_the_root_is_never_acted_on(self, dangerous, monkeypatch):
        """The root reaches the loop only through a bug, and the grant that runs
        first does not refuse it the way a removal does."""
        released = []
        monkeypatch.setattr(ramscratch, "_release", released.append)
        state = ramscratch._State()
        state.cleanup = [dangerous]
        ramscratch.run_exit_cleanup(state=state)
        assert released == []

    def test_the_home_directory_is_never_acted_on(self, monkeypatch):
        released = []
        monkeypatch.setattr(ramscratch, "_release", released.append)
        state = ramscratch._State()
        state.cleanup = [os.path.expanduser("~"),
                         os.path.expanduser("~") + os.sep]
        ramscratch.run_exit_cleanup(state=state)
        assert released == []

    def test_an_ordinary_scratch_is_still_released(self, tmp_path, monkeypatch):
        released = []
        monkeypatch.setattr(ramscratch, "_release", released.append)
        state = ramscratch._State()
        state.bases = [str(tmp_path)]
        state.cleanup = [str(tmp_path)]
        ramscratch.run_exit_cleanup(state=state)
        assert released == [str(tmp_path)]

    def test_a_path_outside_every_base_is_refused(self, tmp_path, monkeypatch):
        """The rule the root and the home directory are special cases of: the
        cleanup may only remove what this run made."""
        released = []
        monkeypatch.setattr(ramscratch, "_release", released.append)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        state = ramscratch._State()
        state.bases = [str(tmp_path / "scratch")]
        state.cleanup = [str(elsewhere)]
        ramscratch.run_exit_cleanup(state=state)
        assert released == []

    def test_a_run_that_settled_no_base_releases_nothing(self, tmp_path,
                                                         monkeypatch):
        """It has made no scratch, so nothing registered can be its to remove."""
        released = []
        monkeypatch.setattr(ramscratch, "_release", released.append)
        state = ramscratch._State()
        state.cleanup = [str(tmp_path)]
        ramscratch.run_exit_cleanup(state=state)
        assert released == []

    def test_the_base_a_run_settles_is_the_one_it_may_clean(self, tmp_path):
        """init_ram_base records the run directory it made, and the scratch
        under it is released with it."""
        ramscratch.reset_state("probe")
        state = ramscratch._STATE
        ramscratch.init_ram_base(str(tmp_path), state=state)
        scratch, status = ramscratch.ram_scratch_dir("work", state=state,
                                                     log=lambda _m: None)
        assert status == 0
        ramscratch.add_exit_cleanup([scratch], state=state)
        assert ramscratch.run_exit_cleanup(state=state) == 0
        assert not os.path.exists(scratch)
