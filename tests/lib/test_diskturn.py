"""The white box for medialib/lib/diskturn.py: which disks spin, and that a
turn on one that does is held by one job at a time."""

import os
import threading
import time

import pytest

from medialib.lib import diskturn

pytestmark = pytest.mark.fs


def _sysfs(tmp_path, device_dir: str, rotational=None, slaves=()):
    """A fake /sys/dev/block entry for the device the tmp folder is on."""
    device = os.stat(str(tmp_path)).st_dev
    root = tmp_path / "sys"
    block = root / "devices" / device_dir
    block.mkdir(parents=True)
    if rotational is not None:
        (block / "queue").mkdir()
        (block / "queue" / "rotational").write_text(rotational + "\n")
    if slaves:
        (block / "slaves").mkdir()
        for name, spins in slaves:
            below = root / "devices" / name
            (below / "queue").mkdir(parents=True)
            (below / "queue" / "rotational").write_text(spins + "\n")
            (block / "slaves" / name).symlink_to(below)
    links = root / "dev"
    links.mkdir()
    (links / ("%d:%d" % (os.major(device), os.minor(device)))).symlink_to(block)
    return str(links)


class TestRotational:
    @pytest.fixture(autouse=True)
    def _a_block_device(self, monkeypatch):
        # The tmp folder may be on tmpfs, an anonymous device; the fake sysfs
        # stands in for a real disk either way.
        monkeypatch.setattr(diskturn.os, "major", lambda _device: 8)

    def test_a_disk_says_whether_it_spins(self, tmp_path):
        assert diskturn.rotational(str(tmp_path), _sysfs(tmp_path, "sda", "1"))

    def test_a_disk_that_does_not_spin(self, tmp_path):
        assert not diskturn.rotational(str(tmp_path),
                                       _sysfs(tmp_path, "nvme0n1", "0"))

    def test_a_partition_asks_its_disk(self, tmp_path):
        sysfs = _sysfs(tmp_path, "sda/sda1")
        (tmp_path / "sys" / "devices" / "sda" / "queue").mkdir()
        (tmp_path / "sys" / "devices" / "sda" / "queue" / "rotational") \
            .write_text("0\n")
        assert not diskturn.rotational(str(tmp_path), sysfs)

    def test_an_encrypted_disk_spins_when_the_disk_under_it_does(self,
                                                                 tmp_path):
        """A device-mapper device reports itself; the disk under it is the
        one that answers."""
        sysfs = _sysfs(tmp_path, "dm-3", "0", slaves=[("sdb", "1")])
        assert diskturn.rotational(str(tmp_path), sysfs)

    def test_one_that_cannot_be_told_is_taken_to_spin(self, tmp_path):
        assert diskturn.rotational(str(tmp_path), str(tmp_path / "nowhere"))

    def test_an_anonymous_device_is_taken_to_spin(self, tmp_path,
                                                  monkeypatch):
        monkeypatch.setattr(diskturn.os, "major", lambda _device: 0)
        assert diskturn.rotational(str(tmp_path), _sysfs(tmp_path, "x", "0"))


class TestTurn:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(diskturn, "_SPINS", {})
        monkeypatch.setattr(diskturn, "_LOCKS", {})
        monkeypatch.setitem(diskturn._STATE, "dir", "")

    def _overlapped(self, path: str) -> bool:
        """Whether two jobs on ``path`` were ever inside a turn at once."""
        inside = []
        most = []
        guard = threading.Lock()

        def job():
            with diskturn.turn(path):
                with guard:
                    inside.append(1)
                    most.append(len(inside))
                time.sleep(0.05)
                with guard:
                    inside.pop()

        threads = [threading.Thread(target=job) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return max(most) > 1

    def test_a_spinning_disk_takes_one_job_at_a_time(self, tmp_path,
                                                     monkeypatch):
        monkeypatch.setattr(diskturn, "rotational", lambda _path: True)
        assert not self._overlapped(str(tmp_path))

    def test_through_the_shared_lock_files_too(self, tmp_path, monkeypatch):
        monkeypatch.setattr(diskturn, "rotational", lambda _path: True)
        turns = diskturn.init(str(tmp_path / "turns"))
        assert not self._overlapped(str(tmp_path))
        assert os.listdir(turns)

    def test_a_disk_that_does_not_spin_is_shared(self, tmp_path, monkeypatch):
        monkeypatch.setattr(diskturn, "rotational", lambda _path: False)
        assert self._overlapped(str(tmp_path))

    def test_a_job_holding_the_turn_may_ask_for_it_again(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setattr(diskturn, "rotational", lambda _path: True)
        diskturn.init(str(tmp_path / "turns"))
        done = threading.Event()

        def nested():
            with diskturn.turn(str(tmp_path)), \
                    diskturn.turn(str(tmp_path / "missing" / "file.mkv")):
                done.set()

        thread = threading.Thread(target=nested, daemon=True)
        thread.start()
        thread.join(5)
        assert done.is_set()

    def test_a_path_not_there_yet_is_on_its_folder_s_disk(self, tmp_path):
        assert diskturn.device_of(str(tmp_path / "a" / "b.mkv")) \
            == os.stat(str(tmp_path)).st_dev
