"""Tests for medialib.lib.mtp - a phone folder reached over MTP.

What is pinned here:

  * the percent decode, including a null byte, a spelling that is not a whole
    character, and the backslash pairings against the escapes;
  * the mount-root fallbacks;
  * the rename probe's failure branches, whose probe name carries the process
    id.
"""

import os

import pytest

from medialib.lib import mtp

pytestmark = pytest.mark.fs


class TestPercentDecode:
    def test_plain(self):
        assert mtp.percent_decode("SD card/Podcasts") == "SD card/Podcasts"

    def test_escapes_case_insensitive(self):
        assert mtp.percent_decode("Pixel%207") == "Pixel 7"
        assert mtp.percent_decode("%C3%9Cn%c3%af") == "Ünï"
        assert mtp.percent_decode("%C3%AF") == "ï"

    def test_a_multibyte_character_is_one_character(self):
        # the bash decodes in bytes, so the two escapes of one character come
        # back as that character, not as two
        assert mtp.percent_decode("n%C3%BC") == "nü"

    def test_a_backslash_stays(self):
        assert mtp.percent_decode("back\\slash") == "back\\slash"
        assert mtp.percent_decode("\\") == "\\"
        assert mtp.percent_decode("trail\\") == "trail\\"

    def test_backslashes_against_escapes(self):
        # the shapes that decide how the bash's doubling of backslashes pairs
        # up against the escapes its rewrite inserts: an odd run of
        # backslashes in front of an escape decodes the escape, and a name
        # that already holds the spelling of an escape keeps it
        assert mtp.percent_decode("back\\%5Cslash") == "back\\\\slash"
        assert mtp.percent_decode("back\\\\%5Cslash") == "back\\\\\\slash"
        assert mtp.percent_decode("%41%5C") == "A\\"
        assert mtp.percent_decode("a\\x41b") == "a\\x41b"
        assert mtp.percent_decode("\\%41%41") == "\\AA"

    def test_a_stray_percent_stays(self):
        assert mtp.percent_decode("50%off") == "50%off"
        assert mtp.percent_decode("50%4") == "50%4"
        assert mtp.percent_decode("%") == "%"
        assert mtp.percent_decode("%g4") == "%g4"

    def test_an_incomplete_escape_at_the_end(self):
        assert mtp.percent_decode("ab%4") == "ab%4"
        assert mtp.percent_decode("ab%41") == "abA"

    def test_a_null_byte_does_not_survive(self):
        # the bash's answer is a command substitution, and one drops nulls
        assert mtp.percent_decode("%00") == ""
        assert mtp.percent_decode("a%00b") == "ab"

    def test_a_spelling_that_is_not_a_whole_character(self):
        # the byte the escape names, read back in the filesystem's encoding:
        # an incomplete UTF-8 sequence comes back the way the encoding says,
        # and bash, holding the raw byte, answers the same way when the two
        # are compared
        assert mtp.percent_decode("%C3") == "\udcc3"


class TestMountRoot:
    def test_the_override_wins(self, monkeypatch):
        monkeypatch.setenv("CFS_GVFS_ROOT", "/staged/gvfs")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert mtp.mtp_mount_root() == "/staged/gvfs"

    def test_an_empty_override_falls_through(self, monkeypatch):
        monkeypatch.setenv("CFS_GVFS_ROOT", "")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert mtp.mtp_mount_root() == "/run/user/1000/gvfs"

    def test_the_runtime_dir(self, monkeypatch):
        monkeypatch.delenv("CFS_GVFS_ROOT", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert mtp.mtp_mount_root() == "/run/user/1000/gvfs"

    def test_the_uid_default(self, monkeypatch):
        monkeypatch.delenv("CFS_GVFS_ROOT", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 1234)
        assert mtp.mtp_mount_root() == "/run/user/1234/gvfs"

    def test_a_host_with_no_uid_still_answers_a_path(self, monkeypatch):
        """Windows, where ``os.getuid`` does not exist. There is no gvfs there
        to find either, so what matters is that the question is ANSWERED: an
        AttributeError out of here would take down a command that was about to
        fall back to adb perfectly well."""
        monkeypatch.delenv("CFS_GVFS_ROOT", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.delattr(os, "getuid", raising=False)
        assert mtp.mtp_mount_root() == "/run/user/0/gvfs"


class TestMountCanRename:
    def test_a_folder_it_can_do_it_in(self, tmp_path):
        folder = tmp_path / "mount"
        folder.mkdir()
        assert mtp.mtp_mount_can_rename(str(folder)) is True
        # the probe is gone again: asking beats assuming, and the asking leaves
        # no trace
        assert list(folder.iterdir()) == []

    def test_the_force_flag_answers_no_without_asking(self, tmp_path, monkeypatch):
        folder = tmp_path / "mount"
        folder.mkdir()
        monkeypatch.setenv("CFS_MTP_FORCE_ADB", "1")
        assert mtp.mtp_mount_can_rename(str(folder)) is False
        assert list(folder.iterdir()) == []

    def test_only_the_value_one_forces(self, tmp_path, monkeypatch):
        folder = tmp_path / "mount"
        folder.mkdir()
        monkeypatch.setenv("CFS_MTP_FORCE_ADB", "0")
        assert mtp.mtp_mount_can_rename(str(folder)) is True

    def test_a_file_is_not_a_mount(self, tmp_path):
        file = tmp_path / "notes.txt"
        file.write_text("")
        assert mtp.mtp_mount_can_rename(str(file)) is False

    def test_a_missing_path_is_not_a_mount(self, tmp_path):
        assert mtp.mtp_mount_can_rename(str(tmp_path / "ghost")) is False

    def test_a_probe_that_cannot_be_renamed(self, tmp_path):
        # the rename the probe does lands on a folder that already holds
        # something, so it must come back no - and the probe it created on the
        # way must be cleaned up again
        folder = tmp_path / "mount"
        folder.mkdir()
        blocked = folder / (".cfsRenameProbe.{}.renamed".format(os.getpid()))
        blocked.mkdir()
        (blocked / "held.txt").write_text("")
        assert mtp.mtp_mount_can_rename(str(folder)) is False
        assert [p.name for p in folder.iterdir()] == [blocked.name]


class TestResolveMtpUri:
    def _root(self, tmp_path, mounts):
        for name, subs in mounts:
            mount = tmp_path / name
            mount.mkdir()
            for sub in subs:
                (mount / sub).mkdir(parents=True)
        return tmp_path

    def _resolve(self, monkeypatch, tmp_path, uri):
        monkeypatch.setenv("CFS_GVFS_ROOT", str(tmp_path))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        return mtp.resolve_mtp_uri(uri)

    def test_the_mount_itself(self, monkeypatch, tmp_path):
        self._root(tmp_path, [("mtp:host=Pixel 7", [])])
        path, error = self._resolve(monkeypatch, tmp_path, "mtp://Pixel 7")
        assert error == ""
        assert path == str(tmp_path / "mtp:host=Pixel 7")

    def test_a_subfolder_by_escape(self, monkeypatch, tmp_path):
        self._root(tmp_path, [("mtp:host=Pixel 7", ["SD card/Podcasts"])])
        path, error = self._resolve(
            monkeypatch, tmp_path, "mtp://Pixel%207/SD%20card%2FPodcasts"
        )
        assert error == ""
        assert path == str(tmp_path / "mtp:host=Pixel 7/SD card/Podcasts")

    def test_the_mount_name_is_decoded_before_the_match(self, monkeypatch, tmp_path):
        # the on-disk name holds the escape literally, the uri spells the
        # character: both decode to the same host, and that is the match
        self._root(tmp_path, [("mtp:host=tricky %41 name", [])])
        path, error = self._resolve(monkeypatch, tmp_path, "mtp://tricky A name")
        assert error == ""
        assert path == str(tmp_path / "mtp:host=tricky %41 name")

    def test_two_mounts_that_decode_to_one_host(self, monkeypatch, tmp_path):
        # both decode to AB; the match is the one the walk reaches last, which
        # is the byte-sort order the glob the bash uses produces
        self._root(
            tmp_path, [("mtp:host=A%42", ["x"]), ("mtp:host=AB", ["x"])]
        )
        path, error = self._resolve(monkeypatch, tmp_path, "mtp://AB/x")
        assert error == ""
        assert path == str(tmp_path / "mtp:host=AB/x")

    def test_a_file_with_a_mount_shaped_name_is_not_a_mount(self, monkeypatch, tmp_path):
        # with one real mount up, a uri for the file's name still resolves -
        # to the real mount, by the fallback: had the file counted as a mount,
        # it would have matched instead, and the "not a folder" answer come
        # back
        self._root(tmp_path, [("mtp:host=real", [])])
        (tmp_path / "mtp:host=ghost").write_text("")
        path, error = self._resolve(monkeypatch, tmp_path, "mtp://ghost")
        assert error == ""
        assert path == str(tmp_path / "mtp:host=real")

        # and with two mounts up the same uri gets the "none of the phones"
        # answer, naming the host as decoded
        (tmp_path / "mtp:host=other").mkdir()
        path, error = self._resolve(monkeypatch, tmp_path, "mtp://ghost")
        assert path == ""
        assert error == (
            'none of the phones mounted under {} is "ghost".'.format(tmp_path)
        )

    def test_a_symlinked_mount_counts(self, monkeypatch, tmp_path):
        (tmp_path / "phones/phone 1/Podcasts").mkdir(parents=True)
        os.symlink("phones/phone 1", str(tmp_path / "mtp:host=Pixel 7"))
        path, error = self._resolve(
            monkeypatch, tmp_path, "mtp://Pixel%207/Podcasts"
        )
        assert error == ""
        assert path == str(tmp_path / "mtp:host=Pixel 7/Podcasts")

    def test_the_single_mount_fallback(self, monkeypatch, tmp_path):
        self._root(tmp_path, [("mtp:host=Pixel 7", [])])
        path, error = self._resolve(monkeypatch, tmp_path, "mtp://Nokia Lumia 9")
        assert error == ""
        assert path == str(tmp_path / "mtp:host=Pixel 7")

    def test_no_mount_at_all(self, monkeypatch, tmp_path):
        self._root(tmp_path, [])
        path, error = self._resolve(monkeypatch, tmp_path, "mtp://Pixel 7")
        assert path == ""
        assert error == (
            "no phone is mounted under {}. Open it once in the file manager"
            " - that is what creates the mount - then retry.".format(tmp_path)
        )

    def test_the_root_that_does_not_exist(self, monkeypatch, tmp_path):
        missing = tmp_path / "elsewhere"
        monkeypatch.setenv("CFS_GVFS_ROOT", str(missing))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        path, error = mtp.resolve_mtp_uri("mtp://Pixel 7")
        assert path == ""
        assert error.startswith("no phone is mounted under {}".format(missing))

    def test_a_rel_that_is_a_file(self, monkeypatch, tmp_path):
        self._root(tmp_path, [("mtp:host=Pixel 7", [])])
        (tmp_path / "mtp:host=Pixel 7/notes.txt").write_text("")
        path, error = self._resolve(monkeypatch, tmp_path, "mtp://Pixel 7/notes.txt")
        assert path == ""
        assert error == "not a folder on the mounted phone: {}".format(
            tmp_path / "mtp:host=Pixel 7/notes.txt"
        )

    def test_a_rel_that_is_missing(self, monkeypatch, tmp_path):
        self._root(tmp_path, [("mtp:host=Pixel 7", [])])
        path, error = self._resolve(monkeypatch, tmp_path, "mtp://Pixel 7/Ghost")
        assert path == ""
        assert error == "not a folder on the mounted phone: {}".format(
            tmp_path / "mtp:host=Pixel 7/Ghost"
        )

    def test_a_double_slash_in_the_rel(self, monkeypatch, tmp_path):
        # the first slash is the split between host and rel; the second
        # belongs to the path, and the answer says so
        self._root(tmp_path, [("mtp:host=Pixel 7", ["Podcasts"])])
        path, error = self._resolve(
            monkeypatch, tmp_path, "mtp://Pixel 7//Podcasts"
        )
        assert error == ""
        assert path == str(tmp_path / "mtp:host=Pixel 7") + "//Podcasts"

    def test_the_empty_host(self, monkeypatch, tmp_path):
        self._root(tmp_path, [("mtp:host=", ["Podcasts"])])
        path, error = self._resolve(monkeypatch, tmp_path, "mtp:///Podcasts")
        assert error == ""
        assert path == str(tmp_path / "mtp:host=/Podcasts")

    def test_a_pasted_gvfs_path_is_not_a_uri(self, monkeypatch, tmp_path):
        # the prefix is stripped only when the uri begins with it: a gvfs path
        # pasted instead of a uri names no host at all
        self._root(tmp_path, [("mtp:host=Pixel 7", ["Podcasts"])])
        path, error = self._resolve(
            monkeypatch,
            tmp_path,
            "/run/user/1000/gvfs/mtp:host=Pixel 7/Podcasts",
        )
        # one mount is up, so the fallback takes it - and the rel the path
        # left behind is no folder under it
        assert path == ""
        assert error == "not a folder on the mounted phone: {}".format(
            tmp_path / "mtp:host=Pixel 7/run/user/1000/gvfs/mtp:host=Pixel 7/Podcasts"
        )

    def test_the_runtime_dir_root(self, monkeypatch, tmp_path):
        root = tmp_path / "runtime" / "gvfs"
        (root / "mtp:host=Pixel 7").mkdir(parents=True)
        monkeypatch.delenv("CFS_GVFS_ROOT", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
        path, error = mtp.resolve_mtp_uri("mtp://Pixel 7")
        assert error == ""
        assert path == str(root / "mtp:host=Pixel 7")