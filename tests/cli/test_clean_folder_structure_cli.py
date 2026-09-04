"""`clean-folder-structure` as a process: a tree of names in, a tidier one out.

The command only ever looks at names, so every fixture here is `touch`ed files -
no media, no stubs, no codecs. Three groups:

  * the option surface, one small synthetic tree per option (`-n`, `-y`, `-d`,
    the cover-art pass, and the collective cleaning that is scoped to a folder's
    plurality filetype);
  * `-s`, which must preview a whole run without touching the input, asserted
    against a real run on an identical copy rather than against a transcript;
  * the recorded real-world cases under `tests/data/nameCleaning`,
    where a `tree` snapshot is compared byte for byte with a committed answer.
    The comment on each case in `_CASES` is its specification.

**Every run names its fragments file explicitly**, and the file holds no
fragments. Without `-f` the default is `data/fragments.txt` beside the package -
a gitignored file that exists in a working checkout and not in a fresh one - so
the recorded answers held only where it was absent. Naming an empty one makes
"no fragments" the claim rather than a property of the checkout; it is
byte-for-byte the same run.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests import blackbox

pytestmark = pytest.mark.fs

_FIXTURES = blackbox.DATA / "nameCleaning"


def _snapshot(folder) -> str:
    """The layout as `tree` renders it, rooted at the folder's own basename -
    exactly the form the fixtures are recorded in.

    `tree` and not a walk written here: it is the tool the command itself writes
    its before/after artifacts with, so rendering the comparison any other way
    would compare two different formats.
    """
    done = subprocess.run(
        ["tree", "-a", "-n", "--charset", "ascii", "--noreport", folder.name],
        cwd=str(folder.parent), capture_output=True, text=True, check=True)
    return done.stdout


def _parse(text: str) -> list[tuple[int, str]]:
    """The (depth, name) pairs of a `tree` rendering, depth 0 being the root.

    The branch marker's COLUMN carries the depth - four characters per level -
    which is the only thing in the format that does.
    """
    lines = text.splitlines()
    nodes = [(0, lines[0])]
    for line in lines[1:]:
        if not line.strip():
            continue
        columns = [line.find(marker) for marker in ("|-- ", "`-- ")]
        found = [column for column in columns if column >= 0]
        if not found:
            continue
        column = min(found)
        nodes.append((column // 4 + 1, line[column + 4:]))
    return nodes


def _reconstruct(tree_file, destination) -> str:
    """Recreate a recorded layout as empty files and folders, and answer the
    root's name.

    A node is a folder when something sits one level deeper than it; everything
    else is a file. The command reads names and never contents, so empty files
    exercise it fully.
    """
    nodes = _parse(tree_file.read_text(encoding="utf-8"))
    deeper = {index for index, (depth, _) in enumerate(nodes)
              if any(other == depth + 1
                     for other, _ in _until_shallower(nodes, index, depth))}
    stack: list[str] = []
    for index, (depth, name) in enumerate(nodes):
        stack = stack[:depth] + [name]
        full = destination.joinpath(*stack)
        if index in deeper:
            full.mkdir(parents=True, exist_ok=True)
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.touch()
    return nodes[0][1]


def _until_shallower(nodes, index, depth):
    """The nodes after ``index`` that are still inside it - its subtree."""
    for other_depth, name in nodes[index + 1:]:
        if other_depth <= depth:
            return
        yield other_depth, name


def _tree(root, *paths):
    for path in paths:
        full = root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.touch()
    return root


@pytest.fixture
def cleaner(sandbox, tmp_path):
    """The command, a fragments file that removes nothing, and a folder to fill.

    `tree` is a hard requirement rather than a skip: it renders every comparison
    below and the command's own `-s` artifacts, so a host without it cannot make
    a claim here - and a skipped case reports as a pass, which is how a green
    suite on a half-equipped host stops meaning anything.
    """
    if not shutil.which("tree"):
        pytest.fail("the host has no `tree`: it renders every layout compared "
                    "here and the command's own -s artifacts")
    fragments = tmp_path / "no-fragments.txt"
    # Not empty: an unreadable or zero-byte -f argument is refused, and rightly
    # so. A comment is a usable file that yields nothing.
    fragments.write_text("# no fragments\n", encoding="utf-8")

    def clean(folder, *options, expect=0):
        done = sandbox.run("clean-folder-structure", *options,
                           "-f", fragments, folder)
        assert done.returncode == expect, done.stdout + done.stderr
        return done

    sandbox.clean = clean
    sandbox.folder = tmp_path / "root"
    sandbox.folder.mkdir()
    return sandbox


class TestPlainNameCleaning:
    """Underscores become spaces, at every level of the tree."""

    @pytest.fixture
    def run(self, cleaner):
        _tree(cleaner.folder, "My_Show/My_Movie.mp4")
        cleaner.clean(cleaner.folder)
        return cleaner.folder

    def test_the_folder_name_is_cleaned(self, run):
        assert (run / "My Show").is_dir()
        assert not (run / "My_Show").exists()

    def test_the_nested_file_name_is_cleaned_too(self, run):
        assert (run / "My Show" / "My Movie.mp4").is_file()


class TestNumbering:
    """`-n` renumbers a folder's plurality filetype and leaves everything else
    to the other passes. Eleven files, so the padding is two wide."""

    @pytest.fixture
    def run(self, cleaner):
        album = cleaner.folder / "album"
        album.mkdir()
        # The original number as the file's CONTENT, which is what makes the
        # ordering claim below checkable after the names are gone.
        for number in range(1, 12):
            (album / ("%d.mp3" % number)).write_text(str(number))
        (album / "cover.jpg").touch()
        cleaner.clean(cleaner.folder, "-n")
        return cleaner, album

    def test_the_numbers_are_zero_padded_to_the_width_of_the_count(self, run):
        _, album = run
        for name in ("01.mp3", "09.mp3", "10.mp3", "11.mp3"):
            assert (album / name).is_file(), name
        assert not (album / "1.mp3").exists()

    def test_the_ordering_is_natural_and_not_lexical(self, run):
        """Lexically "10" and "11" sort before "2", which would have made the
        file that was 2 come out as 04."""
        _, album = run
        assert (album / "02.mp3").read_text() == "2"
        assert (album / "10.mp3").read_text() == "10"
        assert (album / "11.mp3").read_text() == "11"

    def test_the_non_plurality_file_is_not_numbered_but_is_still_the_cover(
            self, run):
        """Numbering leaves it alone; the cover pass, which runs either way,
        normalises it."""
        _, album = run
        assert (album / "folder.jpg").is_file()
        assert not (album / "cover.jpg").exists()

    def test_the_folder_name_itself_is_untouched(self, run):
        _, album = run
        assert album.is_dir()

    def test_a_second_run_changes_nothing(self, run):
        cleaner, _ = run
        before = blackbox.tree_of(cleaner.folder)
        cleaner.clean(cleaner.folder, "-n")
        assert blackbox.tree_of(cleaner.folder) == before


class TestYearSorting:
    """`-y` files a date-named file under its own year, per folder."""

    @pytest.fixture
    def run(self, cleaner):
        docs = _tree(cleaner.folder, "docs/20230101 alpha.txt",
                     "docs/20240202 beta.txt", "docs/notes.txt") / "docs"
        cleaner.clean(cleaner.folder, "-y")
        return cleaner, docs

    def test_each_dated_file_moves_into_its_year(self, run):
        _, docs = run
        assert (docs / "2023" / "20230101 alpha.txt").is_file()
        assert (docs / "2024" / "20240202 beta.txt").is_file()
        assert not (docs / "20230101 alpha.txt").exists()

    def test_a_file_with_no_date_stays_where_it_is(self, run):
        _, docs = run
        assert (docs / "notes.txt").is_file()

    def test_a_second_run_does_not_nest_a_year_inside_its_own_year(self, run):
        """The year folder is itself named for a year, so a run that reads it as
        a candidate builds 2023/2023 and then 2023/2023/2023."""
        cleaner, docs = run
        before = blackbox.tree_of(cleaner.folder)
        cleaner.clean(cleaner.folder, "-y")
        assert blackbox.tree_of(cleaner.folder) == before
        assert not (docs / "2023" / "2023").exists()


class TestTheInputFolderSurvivesItsOwnPruning:
    """An input holding nothing but an empty sub-folder: the run has names to
    work on, so it proceeds, prunes the sub-folder, and leaves the input itself
    empty - the one shape in which a prune that did not exclude its own root
    would take the folder it was given.

    Not reachable through the input guards, which is why it lives here: an input
    with no sub-folder at all is refused before the prune is ever reached, so the
    refusal hides this.
    """

    def test_the_input_is_still_there_after_its_last_subfolder_goes(
            self, cleaner):
        (cleaner.folder / "Some_Folder").mkdir()
        cleaner.clean(cleaner.folder)
        assert cleaner.folder.is_dir()
        assert list(cleaner.folder.iterdir()) == []


class TestCollectiveCleaningIsScopedToThePlurality:
    """Three `.mp3` sharing a leading word, and one `.jpg` that does not.

    Read as one group there is no common leading word at all - the jpg breaks it
    - so the scoping is what lets "Show " go. The odd file is not dragged into
    the rename either.
    """

    @pytest.fixture
    def run(self, cleaner):
        group = _tree(cleaner.folder, "set/Show Alpha.mp3", "set/Show Bravo.mp3",
                      "set/Show Charlie.mp3", "set/Cover.jpg") / "set"
        cleaner.clean(cleaner.folder)
        return group

    def test_the_shared_leading_word_is_stripped_from_the_plurality(self, run):
        for name in ("Alpha.mp3", "Bravo.mp3", "Charlie.mp3"):
            assert (run / name).is_file(), name
        assert not (run / "Show Alpha.mp3").exists()

    def test_the_odd_file_is_not_collectively_renamed_but_is_the_cover(
            self, run):
        assert (run / "folder.jpg").is_file()
        assert not (run / "Cover.jpg").exists()


class TestDotlessFilenames:
    """A file with no extension has an empty one, and an empty extension tallies
    as its own filetype group rather than breaking the count."""

    @pytest.fixture
    def run(self, cleaner):
        return _tree(cleaner.folder, "mix/Readme_alpha", "mix/License_bravo",
                     "mix/lonely.mp3") / "mix"

    def test_the_run_survives_them_and_cleans_their_names(self, cleaner, run):
        cleaner.clean(cleaner.folder)
        assert (run / "Readme alpha").is_file()
        assert (run / "License bravo").is_file()

    def test_the_lone_file_of_another_type_is_left_alone(self, cleaner, run):
        cleaner.clean(cleaner.folder)
        assert (run / "lonely.mp3").is_file()


class TestDateFixing:
    """`-d` compacts a loose leading date into `YYYYMMDD `, across the tree,
    before the names are cleaned."""

    @pytest.fixture
    def run(self, cleaner):
        docs = _tree(cleaner.folder,
                     "docs/2021.03.05 Report.pdf",       # dots
                     "docs/sub/1999_12_31_Party.jpg",    # underscores
                     "docs/2020 01 02 Notes.txt",        # spaces
                     "docs/20210305 Already.txt",        # already compact
                     "docs/Holiday Trip.txt",            # no date at all
                     "docs/2021.99.09 Weird.txt") / "docs"  # month 99
        cleaner.clean(cleaner.folder, "-d")
        return cleaner, docs

    def test_every_separator_a_date_can_use_is_compacted(self, run):
        _, docs = run
        assert (docs / "20210305 Report.pdf").is_file()
        assert (docs / "sub" / "19991231 Party.jpg").is_file()
        assert (docs / "20200102 Notes.txt").is_file()
        assert not (docs / "2021.03.05 Report.pdf").exists()

    def test_a_name_with_nothing_to_compact_is_untouched(self, run):
        _, docs = run
        assert (docs / "20210305 Already.txt").is_file()
        assert (docs / "Holiday Trip.txt").is_file()

    def test_a_number_that_is_not_a_month_is_not_a_date(self, run):
        """Month 99: three numbers in date shape are not enough to be one."""
        _, docs = run
        assert not (docs / "20219909 Weird.txt").exists()

    def test_a_second_run_changes_nothing(self, run):
        cleaner, _ = run
        before = blackbox.tree_of(cleaner.folder)
        cleaner.clean(cleaner.folder, "-d")
        assert blackbox.tree_of(cleaner.folder) == before


class TestDateFixingIsOptIn:
    """Only `-d` compacts a date. `-n` in particular must not: renumbering owns
    the files it touches, and a date fix underneath it would rename twice."""

    def test_a_plain_run_leaves_a_loose_date_alone(self, cleaner):
        plain = _tree(cleaner.folder, "plain/2021.03.05 Report.pdf") / "plain"
        cleaner.clean(cleaner.folder)
        assert not (plain / "20210305 Report.pdf").exists()

    def test_a_numbering_run_leaves_a_loose_date_alone(self, cleaner):
        numbered = _tree(cleaner.folder, "num/2021.03.05 Report.pdf") / "num"
        cleaner.clean(cleaner.folder, "-n")
        assert not (numbered / "20210305 Report.pdf").exists()
        assert (numbered / "2021.03.05 Report.pdf").is_file()


class TestAUniformPrefixOverNumericCores:
    """A group whose cores are themselves numbers: "1 1", "1 2", "1 3" share a
    leading "1" that distinguishes nothing, and must come out "1"/"2"/"3" rather
    than "1"/"1 2"/"1 3".

    The three names are the whole claim. Which pass produces them is
    `tests/lib/test_prefixes.py`'s business and deliberately not named here: a
    black-box case cannot see it, so a sentence about it can go wrong without
    anything going red - and this one had.
    """

    @pytest.fixture
    def run(self, cleaner):
        group = _tree(cleaner.folder, "grp/1 1", "grp/1 2", "grp/1 3") / "grp"
        cleaner.clean(cleaner.folder)
        return cleaner, group

    def test_the_prefix_is_wiped_from_every_member(self, run):
        _, group = run
        assert sorted(p.name for p in group.iterdir()) == ["1", "2", "3"]

    def test_a_second_run_changes_nothing(self, run):
        cleaner, _ = run
        before = blackbox.tree_of(cleaner.folder)
        cleaner.clean(cleaner.folder)
        assert blackbox.tree_of(cleaner.folder) == before


class TestCoverArt:
    """One image per folder becomes `folder.<ext>`, because that is the name
    mobile filesystems read an external thumbnail from. The winner is chosen by
    word - folder over front over cover - then by size, and never by clobbering
    something already there."""

    def test_the_folder_word_wins_and_the_extension_is_lower_cased(
            self, cleaner):
        group = _tree(cleaner.folder, "pri/cover.png", "pri/front image.jpg",
                      "pri/album Folder.JPG") / "pri"
        cleaner.clean(cleaner.folder)
        assert (group / "folder.jpg").is_file()
        assert not (group / "album Folder.JPG").exists()

    def test_the_losing_candidates_are_left_exactly_where_they_were(
            self, cleaner):
        group = _tree(cleaner.folder, "pri/cover.png", "pri/front image.jpg",
                      "pri/album Folder.JPG") / "pri"
        cleaner.clean(cleaner.folder)
        assert (group / "front image.jpg").is_file()
        assert (group / "cover.png").is_file()

    def test_within_the_winning_word_the_largest_file_wins(self, cleaner):
        group = cleaner.folder / "big"
        group.mkdir()
        (group / "cover a.jpg").write_text("small")
        (group / "cover b.png").write_text("much more bytes!")
        cleaner.clean(cleaner.folder)
        assert (group / "folder.png").is_file()
        assert not (group / "cover b.png").exists()
        assert (group / "cover a.jpg").is_file()

    def test_an_existing_folder_image_is_never_overwritten(self, cleaner):
        group = cleaner.folder / "clob"
        group.mkdir()
        (group / "folder.png").write_text("keep")
        (group / "cover.png").write_text("x")
        cleaner.clean(cleaner.folder)
        assert (group / "folder.png").read_text() == "keep"
        assert (group / "cover.png").is_file()

    def test_a_cover_that_is_not_an_image_is_not_a_candidate(self, cleaner):
        group = _tree(cleaner.folder, "txt/cover.txt") / "txt"
        cleaner.clean(cleaner.folder)
        assert (group / "cover.txt").is_file()
        assert not (group / "folder.txt").exists()

    def test_a_second_run_changes_nothing(self, cleaner):
        _tree(cleaner.folder, "idem/Cover.JPG")
        cleaner.clean(cleaner.folder)
        before = blackbox.tree_of(cleaner.folder)
        cleaner.clean(cleaner.folder)
        assert blackbox.tree_of(cleaner.folder) == before


class TestSimulation:
    """`-s` previews a whole run without touching the input: it mirrors the
    structure into a sandbox, cleans THAT, and writes `before.tree` and
    `after.tree` into the folder it was given.

    The preview is checked against a real run on an identical copy rather than
    against a transcript, because "what a real run would have produced" is the
    entire claim.
    """

    def _fixture(self, root):
        return _tree(root,
                     "My_Show/Season_01/My_Show Episode_01.mp4",
                     "My_Show/Season_01/My_Show Episode_02.mp4",
                     "Loose_Files/Some_Document.pdf",
                     "Loose_Files/cover.jpg")

    @pytest.fixture
    def simulated(self, cleaner, tmp_path):
        preview = tmp_path / "sim" / "data"
        self._fixture(preview)
        before = _snapshot(preview)
        cleaner.clean(preview, "-s")
        return cleaner, preview, before

    def test_both_artifacts_are_written_into_the_folder_it_was_given(
            self, simulated):
        _, preview, _ = simulated
        assert (preview / "before.tree").is_file()
        assert (preview / "after.tree").is_file()

    def test_the_real_input_is_not_touched(self, simulated):
        _, preview, _ = simulated
        assert (preview / "My_Show" / "Season_01").is_dir()
        assert (preview / "My_Show" / "Season_01"
                / "My_Show Episode_01.mp4").is_file()
        assert not (preview / "My Show").exists()

    def test_before_tree_records_the_layout_as_it_was(self, simulated):
        _, preview, before = simulated
        assert (preview / "before.tree").read_text(encoding="utf-8") == before

    def test_after_tree_equals_what_a_real_run_produces(self, simulated,
                                                        tmp_path):
        cleaner, preview, _ = simulated
        real = tmp_path / "real" / "data"
        self._fixture(real)
        cleaner.clean(real)
        assert (preview / "after.tree").read_text(encoding="utf-8") \
            == _snapshot(real)

    def test_the_options_reach_the_preview(self, cleaner, tmp_path):
        """`-n` renumbers inside the sandbox and still not in the input."""
        def album(root):
            folder = root / "set"
            folder.mkdir(parents=True)
            for number in (1, 2, 3, 10):
                (folder / ("track_%d.mp3" % number)).touch()
            (folder / "cover.jpg").touch()
            return root

        preview = album(tmp_path / "simn" / "album")
        cleaner.clean(preview, "-s", "-n")
        real = album(tmp_path / "realn" / "album")
        cleaner.clean(real, "-n")

        assert (preview / "after.tree").read_text(encoding="utf-8") \
            == _snapshot(real)
        assert (preview / "set" / "track_10.mp3").is_file()
        assert not (preview / "set" / "02.mp3").exists()

    def test_a_second_simulation_reads_past_its_own_leftovers(self, simulated):
        """The artifacts sit in the folder being previewed, so a re-run that
        counted them would grow its own snapshots every time."""
        cleaner, preview, _ = simulated
        first = ((preview / "before.tree").read_text(encoding="utf-8"),
                 (preview / "after.tree").read_text(encoding="utf-8"))
        cleaner.clean(preview, "-s")
        assert ((preview / "before.tree").read_text(encoding="utf-8"),
                (preview / "after.tree").read_text(encoding="utf-8")) == first


# Each case is a real-world folder that was recorded because it was wrong, or
# because it was right and must stay that way. The text is the specification;
# the two .tree files beside it are the executable form.
_CASES = [
    # A pure regression guard: nothing is changed here today, and a future run
    # that changes something should raise the question rather than pass.
    "a16z",
    # Duplicate prefix numbers separated by a common string, the second set
    # unpadded, and one padded sequence must survive. The near-common suffix
    # differs only by where the longer prefixes cropped it - the first nine
    # files keep a " K" the rest lost - and suffixes that match but for their
    # cropping are stripped. The lone Cover.jpg is not the plurality filetype so
    # the collective pass leaves it, and the cover pass makes it folder.jpg.
    "Abenteuer1",
    # Abenteuer1's harder twin: the cropping difference is one character INSIDE
    # a word ("Ka" against "K") rather than a whole space-separated token. Still
    # stripped, because the cropped suffix spans several tokens and only its last
    # one is partial.
    "Abenteuer2",
    # Double two-digit prefixes, then a common substring, a common "8", another
    # common substring. Whatever strips what, nothing here says more than 01..21,
    # so padded integers are the whole answer.
    "Tochter",
    # An 8-digit date to keep, a podcast acronym to lose (the folder already says
    # it), and an episode number to keep separate from the date - date, space,
    # episode. The acronym is a common middle segment at a fixed index with no
    # separator around it, and goes anyway.
    "Lage",
    # Two nearly-identical numeric prefixes, "36 10903" and "36 10963". Losing
    # the "36" is right and cropping to "1090"/"1096" is defensible; losing
    # everything else was not. "Jahresrueckblick" is common to both names but at
    # different positions, so the middle-substring pass must never have taken it.
    # Guarded by the truncation-consistency check on the original name lengths.
    "36",
    # The parent of all the other cases. "a16z" was renamed when its parent was
    # cleaned around it but not when it was cleaned directly: the individual
    # prefix rule matched the "16" inside it and took the letters with it.
    # Stripping around a number is now allowed to remove separators and brackets
    # only, never letters or digits.
    "folders",
    # Abenteuer1/2 again, with the cropped common suffix only ONE token long and
    # cropped inside that token. Here it must NOT count as a common suffix - the
    # risk of iteratively eating into genuinely different words is too high. "Die"
    # and "Di" both survive. Two guards make cropped-suffix stripping safe, and
    # apply to files only: truncation-consistency on the input lengths (which
    # catches case 36, whose inputs happen to be the same length, so not this
    # one), and a minimum of two separator-delimited tokens, which catches this.
    "Abenteuer3",
    # Every name is "NN(...)", most "NN(years) (publisher year)", so the group
    # shares a leading "(" and a trailing ")". Those look like one matched pair,
    # which holds for the single-pair names and not for the two-pair ones, where
    # the leading "(" closes mid-name. Stripping both left half a pair at each
    # end. Consecutive pairs are each their own thing now - a stripped bracket
    # takes its real partner with it - so they come out as if each pair had a
    # name to itself. Nested pairs are unaffected; an unmatched bracket has no
    # partner and is never touched.
    "Brackets",
    # An anonymised comics folder where only 2 of 29 files carry a numeric prefix
    # and most names open with a stray space. Those two numbers are exactly what
    # distinguishes those files and must survive; only the spaces go. They were
    # dropped by renameSiblings' third pass, which recovers a prefix hidden
    # behind common leading text the collective pass removed. Whether it runs was
    # decided by the group's FIRST item - here a space-less name with no prefix -
    # so it ran, re-cleaned every core, and wrote the empty result over the real
    # prefix. It only touches names that have no prefix yet.
    "Sparse",
    # Two files, two difficulties. The part numbering "(1/2)"/"(2/2)", where
    # yt-dlp writes the slash as a lookalike character, was spelled out as " - "
    # by the individual pass: that turned the 2-character token into an
    # 8-character shared run, well past the middle-removal minimum, and the
    # numbering was eaten down to "(1)"/"(2)" - and before the bracket gate, to
    # "(1 Die ..." with an orphaned "(". The lookalike is one dash now, so the
    # shared run is short enough to be left alone. And the date: both parts were
    # published the same day, so the group shares one date prefix - coincidence
    # in a group this small rather than redundancy, and a date is informative and
    # the sort key besides. A uniform DATE is kept where a uniform plain number
    # is still wiped. What should go still goes: the shared episode title is the
    # cores' common prefix and is stripped, the date sitting outside it.
    "Verbrechen",
    # The colon a Windows-safe renamer wrote as "∶", which is not ASCII
    # punctuation and so a letter to affix_char_class: the shared "∶ Nebel am
    # Morgen (Folge 12)" suffix stopped one character short of it and every
    # file kept a trailing "∶". It is spelled out as " - " now.
    "Doppelpunkt",
]


class TestTheRecordedCases:
    @pytest.fixture(params=_CASES)
    def case(self, request, cleaner, tmp_path):
        name = request.param
        destination = tmp_path / "cases" / name
        destination.mkdir(parents=True)
        root = destination / _reconstruct(
            _FIXTURES / ("%s before.tree" % name), destination)
        cleaner.clean(root)
        return name, cleaner, root

    def test_the_cleaned_layout_is_the_recorded_one(self, case):
        name, _, root = case
        expected = (_FIXTURES / ("%s after.tree" % name)).read_text(
            encoding="utf-8")
        assert _snapshot(root) == expected

    def test_a_second_run_changes_nothing(self, case):
        _, cleaner, root = case
        produced = _snapshot(root)
        cleaner.clean(root)
        assert _snapshot(root) == produced
