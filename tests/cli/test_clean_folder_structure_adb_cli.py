"""`clean-folder-structure-adb` as a process, driven through its local backend.

MTP has no in-place rename, so `clean-folder-structure` cannot `mv` on a gvfs
phone mount. This helper instead snapshots the device tree, mirrors it locally as
files whose CONTENT is their original device-relative path, runs the real cleaner
on that mirror, reads the mapping back out and replays only the renames on the
device. `CFS_DEV_BACKEND=local` points every "device" operation at an ordinary
directory, which is what makes the whole flow testable with no phone attached.

**The invariant the mirror trick creates, and the one worth guarding hardest:**
mirror files hold PATHS as their content, so a regression that copied mirror
content back onto the device instead of renaming would overwrite every file with
a path string and look like a success. Every fixture here carries recognisable
content and is read back afterwards.

Every run names a fragments file holding no fragments, for the reason in
`test_clean_folder_structure_cli.py`: the default resolves to a gitignored path
outside the suite's control.
"""

from __future__ import annotations

import os
import re
import shutil

import pytest

from tests import blackbox

pytestmark = pytest.mark.fs

_MOUNT = "mtp:host=fakephone"


def _plan_sources(log: str) -> list[str]:
    """The RENAME sources a dry run printed, in the order it printed them."""
    return re.findall(r"^  RENAME  (.*)$", log, re.MULTILINE)


@pytest.fixture
def adb(sandbox, tmp_path):
    """A "device" directory and a way to drive the helper against it."""
    fragments = tmp_path / "no-fragments.txt"
    fragments.write_text("# no fragments\n", encoding="utf-8")

    def run(command, *args, expect=0, **environment):
        # The local backend by default, and a case that names the variable wins:
        # one of them is about an unknown backend being refused.
        settings = {"CFS_DEV_BACKEND": "local", **environment}
        done = sandbox.run(command, *args, "-f", fragments,
                           env=dict(os.environ, **settings))
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    def device(*paths, **contents):
        """A device tree. `paths` are empty files, `contents` name their own
        bytes - `contents` keys cannot hold a slash, so `paths` covers the rest.
        """
        root = tmp_path / ("dev%d" % next(counter))
        for path in paths:
            full = root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.touch()
        for name, text in contents.items():
            full = root / name
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text)
        root.mkdir(parents=True, exist_ok=True)
        return root

    counter = iter(range(1, 1000))
    sandbox.run_dev = run
    sandbox.device = device
    return sandbox


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestTheReplay:
    """A default run: the names change on the device and the bytes do not."""

    @pytest.fixture
    def run(self, adb):
        device = adb.device()
        _write(device / "My_Show" / "My_Movie.mp4", "the real bytes")
        log = adb.run_dev("clean-folder-structure-adb", device)
        return device, log

    def test_the_folder_and_the_file_are_renamed_on_the_device(self, run):
        device, _ = run
        assert (device / "My Show").is_dir()
        assert (device / "My Show" / "My Movie.mp4").is_file()
        assert not (device / "My_Show").exists()

    def test_the_file_still_holds_its_own_bytes(self, run):
        """Not its mirror path, which is what a copy-instead-of-rename would
        leave behind while reporting success."""
        device, _ = run
        assert (device / "My Show" / "My Movie.mp4").read_text() \
            == "the real bytes"

    def test_the_run_reports_the_tally_it_applied(self, run):
        _, log = run
        assert ("Done: 1 rename(s) applied on device, 0 skipped, of 1 planned"
                in log)

    def test_the_run_says_how_much_it_snapshotted(self, run):
        _, log = run
        assert re.search(r"^==>   \d+ file\(s\), \d+ dir\(s\)$", log,
                         re.MULTILINE), log


class TestThePreview:
    """`-s` prints the renames it would make and touches nothing."""

    @pytest.fixture
    def run(self, adb):
        device = adb.device()
        _write(device / "Another_Show" / "Some_File.mp4", "untouched")
        before = blackbox.tree_of(device)
        log = adb.run_dev("clean-folder-structure-adb", "-s", device)
        return adb, device, before, log

    def test_the_device_is_not_touched(self, run):
        _, device, before, _ = run
        assert blackbox.tree_of(device) == before
        assert (device / "Another_Show" / "Some_File.mp4").read_text() \
            == "untouched"

    def test_it_names_the_rename_and_the_target_it_would_produce(self, run):
        _, _, _, log = run
        assert _plan_sources(log) == ["Another_Show/Some_File.mp4"]
        assert "Another Show/Some File.mp4" in log

    def test_it_reports_a_preview_and_how_to_apply_it(self, run):
        _, _, _, log = run
        assert "Preview: 1 rename(s) planned" in log
        assert "nothing changed" in log

    def test_dropping_the_flag_applies_exactly_what_was_previewed(self, run):
        adb, device, _, _ = run
        adb.run_dev("clean-folder-structure-adb", device)
        assert (device / "Another Show" / "Some File.mp4").is_file()


class TestPruningOnTheDevice:
    """Sub-folders the cleaning emptied out are removed from the device too -
    but never the input root, even when everything under it went."""

    def test_an_empty_subfolder_is_pruned_and_a_populated_one_is_kept(
            self, adb):
        device = adb.device()
        (device / "keep_me" / "deep_empty").mkdir(parents=True)
        (device / "Empty_Folder").mkdir(parents=True)
        _write(device / "keep_me" / "file.mp4", "k")
        adb.run_dev("clean-folder-structure-adb", device)
        assert not (device / "Empty_Folder").exists()
        assert not (device / "keep me" / "deep_empty").exists()
        assert (device / "keep me").is_dir()
        assert (device / "keep me" / "file.mp4").read_text() == "k"
        assert device.is_dir()

    def test_the_root_survives_even_when_everything_under_it_was_pruned(
            self, adb):
        device = adb.device()
        (device / "only_empty").mkdir(parents=True)
        adb.run_dev("clean-folder-structure-adb", device)
        assert device.is_dir()


class TestATreeThatNeedsNothing:
    """A clean tree costs no rename, and the report says so rather than
    reporting a rename that did nothing."""

    def test_it_is_left_alone_and_nothing_is_planned(self, adb):
        device = adb.device()
        _write(device / "Clean Name" / "Clean File.mp4", "c")
        before = blackbox.tree_of(device)
        log = adb.run_dev("clean-folder-structure-adb", device)
        assert blackbox.tree_of(device) == before
        assert "of 0 planned" in log


class TestTheMirrorSandbox:
    """The mirror is a temp directory, and it must never appear inside the
    device tree - where the next run would snapshot it as device content."""

    def test_nothing_of_the_mirror_is_left_in_the_device_tree(self, adb):
        device = adb.device("A_B/c_d.mp4")
        adb.run_dev("clean-folder-structure-adb", device)
        assert [p for p in device.rglob("*")
                if p.name == "root" or "mirror" in p.name] == []


class TestThePlanIsDeterministic:
    """Sorted, and the same on every run.

    The plan is what a user reads to know what applying it will do, so it may
    not depend on the order a mapping happens to iterate in. Sorting does not
    untangle an A -> B, B -> C chain; it makes the outcome the same every time.
    """

    @pytest.fixture
    def plans(self, adb):
        device = adb.device()
        for name in ("delta", "alpha", "charlie", "bravo", "echo_one",
                     "foxtrot", "golf", "hotel"):
            _write(device / "Some_Folder" / ("%s_file.mp4" % name), "x")
        return [_plan_sources(
            adb.run_dev("clean-folder-structure-adb", "-s", device))
            for _ in range(5)]

    def test_the_plan_names_every_file(self, plans):
        assert len(plans[0]) == 8

    def test_the_plan_is_sorted(self, plans):
        assert plans[0] == sorted(plans[0])

    def test_five_dry_runs_print_the_same_plan(self, plans):
        assert plans[1:] == plans[:-1]


class TestTheFlagsSurviveTheMirror:
    """A flag has to reach the cleaner running on the mirror, and its result has
    to come back out through the mapping."""

    def test_numbering_comes_back_padded_and_in_natural_order(self, adb):
        device = adb.device()
        for number in range(1, 12):
            _write(device / "album" / ("%d.mp3" % number), str(number))
        adb.run_dev("clean-folder-structure-adb", "-n", device)
        assert (device / "album" / "01.mp3").is_file()
        assert (device / "album" / "11.mp3").is_file()
        assert not (device / "album" / "1.mp3").exists()
        assert (device / "album" / "02.mp3").read_text() == "2"
        assert (device / "album" / "10.mp3").read_text() == "10"

    def test_year_sorting_creates_the_year_folder_on_the_device(self, adb):
        """The only flag whose replay has to CREATE a directory rather than
        rename inside one that is already there."""
        device = adb.device()
        _write(device / "docs" / "20230101 alpha.txt", "a")
        _write(device / "docs" / "20240202 beta.txt", "b")
        _write(device / "docs" / "notes.txt", "n")
        adb.run_dev("clean-folder-structure-adb", "-y", device)
        assert (device / "docs" / "2023").is_dir()
        assert (device / "docs" / "2023" / "20230101 alpha.txt").read_text() \
            == "a"
        assert (device / "docs" / "2024" / "20240202 beta.txt").is_file()
        assert (device / "docs" / "notes.txt").is_file()

    def test_year_sorting_a_second_time_nests_nothing(self, adb):
        """The mapping is recomputed from scratch each run, so an already-filed
        year must not be filed again."""
        device = adb.device()
        _write(device / "docs" / "20230101 alpha.txt", "a")
        adb.run_dev("clean-folder-structure-adb", "-y", device)
        before = blackbox.tree_of(device)
        adb.run_dev("clean-folder-structure-adb", "-y", device)
        assert blackbox.tree_of(device) == before
        assert not (device / "docs" / "2023" / "2023").exists()

    def test_date_fixing_comes_through_with_the_content(self, adb):
        device = adb.device()
        _write(device / "docs" / "2021.03.05 Report.pdf", "r")
        adb.run_dev("clean-folder-structure-adb", "-d", device)
        assert (device / "docs" / "20210305 Report.pdf").read_text() == "r"


class TestTheRefusals:
    """Each happens before anything is snapshotted or touched.

    The "target already exists, skipping" branch is deliberately not among them:
    the mirror is a faithful snapshot, so the cleaner resolves every collision it
    can see inside the mirror, and the replay's own existence check fires only on
    genuine device incoherence - which no local backend can stage.
    """

    def test_a_device_path_with_nothing_under_it_is_refused(self, adb):
        device = adb.device()
        log = adb.run_dev("clean-folder-structure-adb", device, expect=1)
        assert "nothing found under" in log
        assert device.is_dir()

    def test_a_path_that_is_not_a_directory_is_refused_by_the_backend(
            self, adb):
        device = adb.device()
        log = adb.run_dev("clean-folder-structure-adb", device / "nope",
                          expect=1)
        assert "local backend: not a directory" in log

    def test_more_than_one_path_is_refused(self, adb):
        device = adb.device()
        log = adb.run_dev("clean-folder-structure-adb", device, device,
                          expect=1)
        assert "expected exactly one path" in log

    def test_an_unknown_backend_is_refused_rather_than_falling_back_to_adb(
            self, adb):
        device = adb.device()
        log = adb.run_dev("clean-folder-structure-adb", device, expect=1,
                          CFS_DEV_BACKEND="bogus")
        assert "unknown CFS_DEV_BACKEND: bogus" in log


@pytest.fixture
def mount(adb, tmp_path):
    """A staged gvfs MTP mount, and the gvfs root it sits under.

    A staged mount is an ordinary directory and renames happily, which is a
    different route through `clean-folder-structure` entirely - so the cases
    about the adb route set `CFS_MTP_FORCE_ADB=1` to stand in for a mount that
    refuses, and the cases about the in-place route leave it unset.
    """
    if not shutil.which("tree"):
        pytest.fail("the host has no `tree`: the -s route writes its artifacts "
                    "with it")
    root = tmp_path / "gvfs" / "run" / "user" / "1000" / "gvfs"

    def at(*parts):
        folder = root.joinpath(_MOUNT, *parts)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    adb.gvfs = root
    adb.at = at
    return adb


class TestTheDelegationToThisHelper:
    """`clean-folder-structure` hands a phone folder to this helper when the
    mount cannot rename, and its flags have to survive the handover. Driven with
    the local backend, so the delegated run goes through the whole flow - which
    also proves the two commands still fit together."""

    def test_an_mtp_path_is_delegated_and_cleaned(self, mount):
        folder = mount.at("Internal storage", "Music")
        _write(folder / "My_Album" / "My_Track.mp3", "audio bytes")
        log = mount.run_dev("clean-folder-structure", folder,
                            CFS_MTP_FORCE_ADB="1")
        assert "Snapshotting device tree" in log
        assert (folder / "My Album" / "My Track.mp3").read_text() \
            == "audio bytes"
        assert not (folder / "My_Album").exists()

    def test_the_preview_never_hands_the_phone_folder_over(self, mount):
        """`-s` renames nothing on the device whichever route would apply, so it
        simulates in its own sandbox and leaves the folder as it found it - bar
        the two tree files it is documented to write there."""
        folder = mount.at("Card", "Pics")
        _write(folder / "Some_Folder" / "Some_Pic.jpg", "p")
        log = mount.run_dev("clean-folder-structure", "-s", folder,
                            CFS_MTP_FORCE_ADB="1")
        assert "Snapshotting device tree" not in log
        assert (folder / "Some_Folder" / "Some_Pic.jpg").is_file()
        assert not (folder / "Some Folder").exists()
        assert (folder / "before.tree").is_file()
        assert (folder / "after.tree").is_file()

    def test_numbering_survives_the_delegation(self, mount):
        folder = mount.at("Card", "Album")
        for number in (1, 2, 3):
            _write(folder / "tracks" / ("%d.mp3" % number), str(number))
        mount.run_dev("clean-folder-structure", "-n", folder,
                      CFS_MTP_FORCE_ADB="1")
        assert (folder / "tracks" / "1.mp3").is_file()
        assert (folder / "tracks" / "3.mp3").read_text() == "3"

    def test_the_uri_spelling_becomes_the_gvfs_path_and_takes_the_same_route(
            self, mount):
        """The `mtp://host/...` form a file manager copies. It has to resolve to
        the mount and then delegate, rather than reaching a test no URI passes.
        """
        folder = mount.at("SD card", "Podcasts")
        _write(folder / "The_Show" / "Ep_01.mp3", "episode bytes")
        log = mount.run_dev("clean-folder-structure",
                            "mtp://fakephone/SD%20card/Podcasts",
                            CFS_MTP_FORCE_ADB="1",
                            CFS_GVFS_ROOT=str(mount.gvfs))
        assert "Snapshotting device tree" in log
        assert (folder / "The Show" / "Ep 01.mp3").read_text() \
            == "episode bytes"

    def test_a_uri_that_resolves_to_nothing_is_refused_by_name(self, mount):
        mount.at("SD card", "Podcasts")
        log = mount.run_dev("clean-folder-structure",
                            "mtp://fakephone/SD%20card/Nope", expect=1,
                            CFS_MTP_FORCE_ADB="1",
                            CFS_GVFS_ROOT=str(mount.gvfs))
        assert "not a folder on the mounted phone" in log


class TestTheRouteThatSkipsThisHelper:
    """A mount that CAN rename is cleaned in place: nothing is snapshotted, no
    adb is wanted, and a phone with USB debugging switched off is still served.

    `ADB` points at nothing, so a run that reached the helper anyway would fail
    loudly rather than quietly passing on a host that has adb and a phone.
    """

    def test_a_renameable_mount_is_cleaned_without_adb(self, mount):
        folder = mount.at("SD card", "Podcasts")
        _write(folder / "The_Show" / "Ep_01.mp3", "episode bytes")
        log = mount.run_dev("clean-folder-structure", folder,
                            ADB="/nonexistent")
        assert "Snapshotting device tree" not in log
        assert "renames in place" in log
        assert (folder / "The Show" / "Ep 01.mp3").read_text() \
            == "episode bytes"
        assert not (folder / "The_Show").exists()

    def test_the_rename_probe_leaves_nothing_behind(self, mount):
        """It has to actually try a rename to know the mount allows one, which
        means it creates something - and must take it away again."""
        folder = mount.at("SD card", "Probe")
        _write(folder / "The_Show" / "Ep_01.mp3", "x")
        mount.run_dev("clean-folder-structure", folder, ADB="/nonexistent")
        assert [p for p in folder.rglob(".cfsRenameProbe*")] == []

    def test_the_uri_spelling_takes_that_route_too(self, mount):
        folder = mount.at("SD card", "More")
        _write(folder / "Another_Show" / "Ep_02.mp3", "x")
        mount.run_dev("clean-folder-structure",
                      "mtp://fakephone/SD%20card/More",
                      ADB="/nonexistent", CFS_GVFS_ROOT=str(mount.gvfs))
        assert (folder / "Another Show" / "Ep 02.mp3").is_file()
