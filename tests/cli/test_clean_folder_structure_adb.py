"""The white box for medialib/cli/clean_folder_structure_adb.py: what is said
to the phone's shell, how adb's own listings are read, and what the replay does
when the device does not match the snapshot it planned from.

test_clean_folder_structure_adb_cli.py runs the command as a process through
its local backend; the parts here are the ones that backend cannot reach.
"""

from __future__ import annotations

import subprocess

import pytest

from medialib.cli import clean_folder_structure_adb as cfsadb


@pytest.mark.stubbed
class TestAQuotedNameReachesTheShellIntact:
    """Every device command is a string a shell parses, so a name the quoting
    gets wrong is a rename of the wrong file on the phone - or of none."""

    @pytest.mark.parametrize("name", [
        "plain.mp4",
        "it's here.mp4",
        "'leading and trailing'",
        "two  spaces and a\ttab.mp3",
        "$HOME and ${PATH}.jpg",
        "`date` and $(date).jpg",
        "line one\nline two.txt",
        "back\\slash \"double\".txt",
        "-starts-with-a-dash",
        "",
    ])
    def test_the_shell_prints_back_exactly_the_name(self, name):
        done = subprocess.run(
            ["bash", "-c", "printf %s " + cfsadb.shell_quote(name)],
            stdout=subprocess.PIPE, check=True)
        assert done.stdout.decode() == name


@pytest.mark.pure
class TestCountingTheAttachedDevices:
    """`adb devices` lists every phone it can see, but only a `device` one is
    usable - an unauthorized phone is attached and still cannot be driven."""

    @pytest.mark.parametrize("listing,expected", [
        ("List of devices attached\n\n", 0),
        ("List of devices attached\nR58M12AB34C\tdevice\n\n", 1),
        ("List of devices attached\nR58M12AB34C\tunauthorized\n\n", 0),
        ("List of devices attached\nR58M12AB34C\tdevice\n"
         "emulator-5554\toffline\n\n", 1),
        ("List of devices attached\nR58M12AB34C\tdevice\n"
         "192.168.1.20:5555\tdevice\n\n", 2),
        ("List of devices attached\r\nR58M12AB34C\tdevice\r\n\r\n", 1),
        ("", 0),
    ])
    def test_only_the_ready_ones_count(self, listing, expected):
        assert cfsadb.count_attached(listing) == expected


_FUSE = "/run/user/1000/gvfs/mtp:host=ACME_Phone_R58M12AB34C"

# What `ls -1 /storage` prints on a phone with an SD card in it, through a
# shell that ends its lines in CRLF.
_WITH_CARD = "emulated\r\nself\r\n1A2B-3C4D\r\n"


@pytest.mark.pure
class TestTheDevicePathsAFusePathIsTriedAt:
    """The gvfs path names a storage LABEL ("SD card"), which the phone's own
    filesystem knows nothing about - so the part after it is tried under each
    root the phone could be holding it in."""

    def test_the_primary_roots_come_first_and_then_the_listed_volumes(self):
        assert cfsadb.probe_paths(_FUSE + "/SD card/DCIM/Camera",
                                  _WITH_CARD) == [
            "/sdcard/DCIM/Camera",
            "/storage/emulated/0/DCIM/Camera",
            "/storage/self/primary/DCIM/Camera",
            "/storage/1A2B-3C4D/DCIM/Camera",
        ]

    def test_self_and_emulated_are_not_tried_as_volumes_of_their_own(self):
        probes = cfsadb.probe_paths(_FUSE + "/Internal storage/Music",
                                    _WITH_CARD)
        assert "/storage/self/Music" not in probes
        assert "/storage/emulated/Music" not in probes

    @pytest.mark.parametrize("path", [
        _FUSE + "/Internal storage",
        _FUSE + "/Internal storage/",
        _FUSE,
    ])
    def test_a_path_at_the_storage_itself_is_the_roots_themselves(self, path):
        assert cfsadb.probe_paths(path, _WITH_CARD) == [
            "/sdcard", "/storage/emulated/0", "/storage/self/primary",
            "/storage/1A2B-3C4D"]

    def test_a_phone_listing_no_volumes_is_tried_at_the_roots_alone(self):
        assert cfsadb.probe_paths(_FUSE + "/Internal storage/Podcasts",
                                  "") == [
            "/sdcard/Podcasts", "/storage/emulated/0/Podcasts",
            "/storage/self/primary/Podcasts"]


@pytest.mark.fs
class TestATargetThatAlreadyExistsOnTheDevice:
    """The mirror is planned from a snapshot, and the device can disagree with
    it by the time the replay reaches a name - a FAT-formatted card, where a
    case-only rename finds its target "already there", is the everyday way.
    Nothing may be overwritten, and the footer has to say what fell short."""

    @pytest.fixture
    def replayed(self, tmp_path, capsys):
        device = tmp_path / "device"
        device.mkdir()
        (device / "Holiday_Pic.jpg").write_text("the snapshot's file")
        (device / "Holiday Pic.jpg").write_text("a file already there")
        (device / "Other_Pic.jpg").write_text("other")
        plan = cfsadb.Plan(dry_run=False)
        cfsadb.replay(cfsadb.Device("local"), str(device),
                      {"Holiday_Pic.jpg": "Holiday Pic.jpg",
                       "Other_Pic.jpg": "Other Pic.jpg"}, plan)
        plan.footer()
        return device, capsys.readouterr().err

    def test_both_files_keep_their_own_bytes(self, replayed):
        device, _ = replayed
        assert (device / "Holiday_Pic.jpg").read_text() \
            == "the snapshot's file"
        assert (device / "Holiday Pic.jpg").read_text() \
            == "a file already there"

    def test_the_rest_of_the_plan_still_goes_through(self, replayed):
        device, _ = replayed
        assert (device / "Other Pic.jpg").read_text() == "other"
        assert not (device / "Other_Pic.jpg").exists()

    def test_it_names_the_target_it_would_not_overwrite(self, replayed):
        _, log = replayed
        assert "target already exists, skipping (no overwrite): " \
            "Holiday Pic.jpg" in log

    def test_the_footer_counts_the_skip(self, replayed):
        _, log = replayed
        assert "Done: 1 rename(s) applied on device, 1 skipped, of 2 " \
            "planned\n" in log
