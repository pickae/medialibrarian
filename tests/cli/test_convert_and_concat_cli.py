"""`convert-and-concat`: an archive standing in for the folder it is named after,
and the book-shape probe that decides whether a run is worth starting.

The heavy media tools are the usual stubs, but the ARCHIVE tools are real - `zip`
and `tar` pack empty files here - so what is asserted is the real unpacking, the
real depth rules and the real collision rules rather than a stand-in for them.

The probe exists because one output file of this command is a CONCATENATION, and a
concatenation is only possible for audio that is all one type: mp3 and flac in one
folder can never be joined. So before a run spends itself converting a tree it
looks for at least one book at the depth this run makes one output - a subfolder
normally, a subfolder of a subfolder under `-s`. A tree with no book there is
refused up front, because converting it would only produce tracks that can then
never be concatenated.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLS = ("zip", "unzip", "tar", "jq", "rsync")


def _book(folder, extension, count=2):
    """A folder of empty "tracks", all one type - which is what makes it a book."""
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(1, count + 1):
        (folder / ("0%d - part.%s" % (number, extension))).touch()
    return folder


def _pack_with_folder(archive, folder):
    """Pack the way packing a folder does: the folder itself as the archive's
    single top-level entry, which is the shape a real download arrives in."""
    subprocess.run(["zip", "-qr", str(archive), folder.name],
                   cwd=str(folder.parent), check=True)
    return archive


def _pack_flat(archive, folder):
    """The other shape: the tracks at the archive's own top level."""
    subprocess.run(["tar", "-czf", str(archive), "-C", str(folder), "."],
                   check=True)
    return archive


def _names(directory):
    return sorted(p.name for p in directory.iterdir() if p.is_file())


@pytest.fixture
def wrapper(sandbox, tmp_path, private_workspace):
    """The command, real archive tools, and the scratch base to read back.

    `private_workspace` is the directory `conftest` points every scratch knob at,
    so "did the run hand its unpacking scratch back" is just "is that directory
    empty" - with no list of scratch names to keep in step with the code.
    """
    for tool in _TOOLS:
        if shutil.which(tool) is None:
            pytest.fail("the host has no %s: the unpacking here is real, not "
                        "stubbed" % tool)
    sandbox.with_media_stubs()
    packs = tmp_path / "pack"
    packs.mkdir()

    def run(*args, expect=0):
        done = sandbox.run("convert-and-concat", *args, timeout=600)
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.packs = packs
    sandbox.scratch = private_workspace
    sandbox.wrapper = run
    return sandbox


def _assert_no_leak(wrapper):
    assert list(wrapper.scratch.iterdir()) == [], \
        "scratch left in the RAM base"


class TestAnArchiveIsOneOfTheInputsOwnSubfolders:
    """The input holds, side by side: a plain folder, an archive packed with its
    own top folder, one packed flat, one shadowed by a folder of its name, and two
    claiming one name."""

    @pytest.fixture
    def run(self, wrapper, tmp_path):
        source = tmp_path / "in"
        outputs = tmp_path / "out"
        packs = wrapper.packs

        _book(source / "Great Expectations", "mp3")
        _pack_with_folder(source / "Bleak House.zip",
                          _book(packs / "Bleak House", "mp3"))
        _pack_flat(source / "Moby Dick.tar.gz", _book(packs / "flat", "mp3"))
        # Shadowed: the folder holds ONE track and the archive two, so which
        # reached the output is visible in what was converted - and the folder's
        # own track is the one that must be there.
        _book(source / "Hard Times", "mp3", count=1)
        _pack_with_folder(source / "Hard Times.zip",
                          _book(packs / "Hard Times", "mp3", count=2))
        _pack_with_folder(source / "Little Dorrit.zip",
                          _book(packs / "Little Dorrit", "mp3"))
        _pack_flat(source / "Little Dorrit.tar.gz", packs / "Little Dorrit")
        (source / "notes.txt").touch()

        log = wrapper.wrapper(source, outputs)
        return wrapper, source, outputs, log

    def test_one_output_file_per_book(self, run):
        """Five books, five files - nothing extra for the shadowed archive or for
        the second of the two claiming one name."""
        _, _, outputs, _ = run
        assert _names(outputs) == ["Bleak House.mp3", "Great Expectations.mp3",
                                   "Hard Times.mp3", "Little Dorrit.mp3",
                                   "Moby Dick.mp3"]

    def test_a_folder_beside_an_archive_of_its_name_wins(self, run):
        """Reported rather than silently preferred, the archive left where it is,
        and its two tracks never reach the transcode."""
        _, source, _, log = run
        assert '"Hard Times" is already a folder here' in log, log
        assert (source / "Hard Times.zip").is_file()
        assert log.count("Converting: Hard Times/") == 1, log

    def test_two_archives_claiming_one_name_are_never_mixed(self, run):
        """The second is reported and skipped, so one folder never ends up
        holding half of each."""
        _, _, _, log = run
        assert 'already stands in for "Little Dorrit"' in log, log
        assert log.count("Converting: Little Dorrit/") == 2, log

    def test_the_input_keeps_its_archives(self, run):
        """Nothing here consumes them."""
        _, source, _, _ = run
        assert (source / "Bleak House.zip").is_file()

    def test_the_unpacking_scratch_is_handed_back(self, run):
        wrapper, _, _, _ = run
        _assert_no_leak(wrapper)


class TestTheArchivesThatCountUnderTheGroupedMode:
    """`-s` makes one output per book under a group folder, so the archives that
    stand in for a book lie one level deeper."""

    @pytest.fixture
    def run(self, wrapper, tmp_path):
        source = tmp_path / "sin"
        outputs = tmp_path / "sout"
        packs = wrapper.packs

        _book(source / "Dickens" / "Great Expectations", "mp3")
        _pack_with_folder(source / "Dickens" / "Bleak House 2.zip",
                          _book(packs / "Bleak House 2", "mp3"))
        # A group folder that exists ONLY as an archive's parent.
        (source / "Melville").mkdir(parents=True)
        _pack_with_folder(source / "Melville" / "Moby Dick 2.zip",
                          _book(packs / "Moby Dick 2", "mp3"))
        # One level too high to mean anything under -s.
        _pack_with_folder(source / "Ignored.zip", packs / "Moby Dick 2")
        # A group with nothing to concatenate: passed over, not fatal.
        (source / "Covers").mkdir(parents=True)
        (source / "Covers" / "cover.jpg").touch()

        log = wrapper.wrapper("-s", source, outputs)
        return wrapper, outputs, log

    def test_one_output_folder_per_group(self, run):
        _, outputs, _ = run
        assert sorted(p.name for p in outputs.iterdir() if p.is_dir()) \
            == ["Dickens", "Melville"]

    def test_a_groups_books_include_the_one_that_was_an_archive(self, run):
        _, outputs, _ = run
        assert _names(outputs / "Dickens") == ["Bleak House 2.mp3",
                                                "Great Expectations.mp3"]

    def test_a_group_can_be_made_of_an_archive_alone(self, run):
        _, outputs, _ = run
        assert _names(outputs / "Melville") == ["Moby Dick 2.mp3"]

    def test_an_archive_directly_in_the_input_is_ignored_and_said_to_be(
            self, run):
        _, outputs, log = run
        assert not (outputs / "Ignored.mp3").exists()
        assert "Ignoring the archives lying directly in the input" in log, log

    def test_a_group_with_no_book_is_passed_over_rather_than_fatal(self, run):
        _, _, log = run
        assert 'Passing over "Covers"' in log, log

    def test_the_unpacking_scratch_is_handed_back(self, run):
        wrapper, _, _ = run
        _assert_no_leak(wrapper)


class TestOnlyConcatenating:
    """`-c` transcodes nothing at all, so an unpacked archive's content IS the
    finished audio - and the input tree is left exactly as it was."""

    @pytest.fixture
    def run(self, wrapper, tmp_path):
        source = tmp_path / "cin"
        outputs = tmp_path / "cout"
        _book(source / "Great Expectations", "opus")
        _pack_with_folder(source / "Bleak House 3.zip",
                          _book(wrapper.packs / "Bleak House 3", "opus"))
        before = blackbox.tree_of(source)
        log = wrapper.wrapper("-c", source, outputs)
        return wrapper, source, outputs, before, log

    def test_it_transcodes_nothing(self, run):
        _, _, _, _, log = run
        assert "Converting:" not in log, log

    def test_it_concatenates_both_the_folder_and_the_archive(self, run):
        _, _, outputs, _, _ = run
        assert _names(outputs) == ["Bleak House 3.opus",
                                    "Great Expectations.opus"]

    def test_it_leaves_the_input_tree_exactly_as_it_was(self, run):
        _, source, _, before, _ = run
        assert blackbox.tree_of(source) == before

    def test_the_unpacking_scratch_is_handed_back(self, run):
        """`-c` creates one even though it builds no opus tree, so it needs
        handing back in this mode too."""
        wrapper, _, _, _, _ = run
        _assert_no_leak(wrapper)


# Each case is a tree and the options it is handed, at or away from the depth the
# run makes one output.
_REFUSED = [
    # A folder mixing two types is never a book.
    ("a mixed folder", (), [("Alpha", "mp3", 2), ("Alpha", "flac", 1)]),
    # Audio loose at the root is in no subfolder, so it is not a book.
    ("loose root audio", (), [("", "mp3", 2)]),
    # Under -s the book lies one level deeper, and a mixed one is still not one.
    ("a mixed sub-subfolder", ("-s",),
     [("Author/Alpha", "mp3", 2), ("Author/Alpha", "opus", 1)]),
    # Under -s a folder of audio at the group's own level is not a book either -
    # the very same tree that IS one when run normally.
    ("a book one level too shallow", ("-s",), [("Book", "mp3", 2)]),
]

_ACCEPTED = [
    ("a one-type folder", (), "Alpha.mp3", False),
    ("a one-type sub-subfolder", ("-s",), "Author/Alpha.mp3", False),
    ("an archive standing in for the folder", (), "Alpha.mp3", True),
    ("an archive at the book depth", ("-s",), "Author/Alpha.mp3", True),
]


class TestTheBookShapeProbe:
    """Refused up front, or carried all the way to a finished output. The two
    together pin the probe to exactly the shape a finished book has, at exactly
    the depth the run works at."""

    @pytest.mark.parametrize("label,options,tracks", _REFUSED,
                             ids=[case[0] for case in _REFUSED])
    def test_a_tree_with_no_book_at_the_runs_depth_is_refused(
            self, wrapper, tmp_path, label, options, tracks):
        """It only reads, so the input is left exactly as it was found - and the
        output path, which does not exist yet, must not be created."""
        source = tmp_path / "rin"
        source.mkdir()
        for relative, extension, count in tracks:
            folder = source / relative if relative else source
            _book(folder, extension, count)
        before = blackbox.tree_of(source)
        outputs = tmp_path / "rout" / "out"
        log = wrapper.wrapper(*options, source, outputs, expect=1)
        assert "Nothing to concatenate" in log, log
        assert not outputs.exists()
        assert blackbox.tree_of(source) == before

    @pytest.mark.parametrize("label,options,expected,archived", _ACCEPTED,
                             ids=[case[0] for case in _ACCEPTED])
    def test_a_book_at_the_runs_depth_carries_it_to_an_output(
            self, wrapper, tmp_path, label, options, expected, archived):
        source = tmp_path / "ain"
        source.mkdir()
        parent = source / "Author" if "-s" in options else source
        parent.mkdir(parents=True, exist_ok=True)
        if archived:
            _pack_with_folder(parent / "Alpha.zip",
                              _book(wrapper.packs / "Alpha", "mp3"))
        else:
            _book(parent / "Alpha", "mp3")
        outputs = tmp_path / "aout"
        wrapper.wrapper(*options, source, outputs)
        assert (outputs / expected).is_file()

    def test_a_book_may_hold_its_audio_one_level_deeper(self, wrapper,
                                                        tmp_path):
        """The concatenation gathers a folder's tracks recursively, so the tree
        `-s` is for, handed over WITHOUT it - what a user who meant `-s` and
        forgot it hands over - is not a refusal but a coarser run: one output per
        top-level subfolder, made of everything below it.

        The whole output tree is asserted, because one file per top-level
        subfolder is what a per-file check could not tell from one per disc.
        """
        source = tmp_path / "a5in"
        _book(source / "Alpha" / "Disc 1", "mp3")
        _book(source / "Alpha" / "Disc 2", "mp3")
        _book(source / "Beta" / "Disc 1", "mp3")
        outputs = tmp_path / "a5out"
        wrapper.wrapper(source, outputs)
        assert blackbox.tree_of(outputs) == ["Alpha.mp3", "Beta.mp3"]
