"""`find-fragment-candidates` as a process: the recurring leftovers in a tree of
names, so they can be reviewed and added to the fragments file the name cleaners
read.

No media and no tool beyond `tree`, and that only for the folder input mode - so
the parsing and every tokenising rule are exercised against a hand-written `.tree`
fixture, and only the mode that generates its own snapshot needs the binary.
"""

from __future__ import annotations

import shutil

import pytest

from tests import blackbox

pytestmark = pytest.mark.fs

# In exactly the shape the command's own `tree` invocation produces. The names are
# chosen so every tokenising rule has a witness: "Some"/"Show"/"1080p"/"x264"
# recur; "The"/"of" are common words, "2019" a pure number, "a" a lone character;
# "webrip"/"720p"/"bluray" are quality tags, kept on purpose; "Some -- Thing"
# carries a literal "-- " inside a real name; and the root line, which has no
# connector, must contribute nothing.
_TREE = """myFolder
|-- Some.Show.1080p.x264-GROUP
|   |-- Some.Show.S01E01.1080p.x264.mkv
|   `-- Some.Show.S01E02.1080p.x264.mkv
|-- The.Movie.of.a.Lifetime.2019.720p.webrip.mkv
|-- Some -- Thing.mkv
`-- Another.Show.bluray.mkv
"""

_REPEAT = """repeat
|-- Alpha.Alpha.Alpha.Beta.mkv
`-- Alpha.Gamma.mkv
"""

_UNICODE = """uni
├── Delta.Epsilon.mkv
│   └── Delta.Zeta.mkv
└── Delta.Eta.mkv
"""


def _table(report) -> list[tuple[str, str]]:
    """The report's data rows as (prevalence, candidate), header dropped."""
    rows = []
    for line in report.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line:
            continue
        count, tab, candidate = line.partition("\t")
        assert tab, "the columns are tab-separated, got %r" % line
        rows.append((count, candidate))
    return rows


@pytest.fixture
def fragments(sandbox, tmp_path):
    work = tmp_path / "trees"
    work.mkdir()
    (work / "myFolder.tree").write_text(_TREE, encoding="utf-8")

    def run(*args, expect=0):
        done = sandbox.run("find-fragment-candidates", *args)
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    def table(*options, tree=None):
        """Run over a tree file and read back its report."""
        tree = tree or work / "myFolder.tree"
        run(*options, tree)
        return _table(tree.with_suffix(".fragmentCandidates.txt"))

    sandbox.work = work
    sandbox.tree = work / "myFolder.tree"
    sandbox.report = work / "myFolder.fragmentCandidates.txt"
    sandbox.ffc = run
    sandbox.table = table
    return sandbox


class TestTheTreeFileInput:
    """The report lands beside the tree file it parsed, named after it."""

    def test_the_report_is_written_beside_the_tree_file_and_named(
            self, fragments):
        log = fragments.ffc(fragments.tree)
        assert fragments.report.is_file()
        assert str(fragments.report) in log

    def test_the_header_names_the_source_the_floor_and_the_format(
            self, fragments):
        fragments.ffc(fragments.tree)
        header = fragments.report.read_text(encoding="utf-8")
        assert "# Fragment candidates from: %s\n" % fragments.tree in header
        assert "# Minimum prevalence reported: 2\n" in header
        assert "# Columns: <prevalence><TAB><candidate>\n" in header
        # Named with its folder, since that is the path the cleaners look in.
        assert "data/fragments.txt" in header


class TestTheTable:
    """Descending prevalence, ties alphabetical.

    "show" is in four names (three `Some.Show` and `Another.Show`) and "some" in
    four as well (the three plus "Some -- Thing"); "1080p" and "x264" are in the
    three `Some.Show` names. Everything else is in one name, so the default floor
    of 2 leaves exactly these four.
    """

    def test_the_default_report_is_sorted_by_prevalence_then_alphabetically(
            self, fragments):
        assert fragments.table() == [("4", "show"), ("4", "some"),
                                     ("3", "1080p"), ("3", "x264")]

    def test_a_token_repeated_within_one_name_counts_once(self, fragments):
        """Prevalence counts NAMES, not occurrences: "Alpha" three times in one
        name and once in another is a prevalence of two."""
        tree = fragments.work / "repeat.tree"
        tree.write_text(_REPEAT, encoding="utf-8")
        assert fragments.table(tree=tree) == [("2", "alpha")]


class TestThePrevalenceFloor:
    """`-m` hides the one-off title words by default and can be moved either
    way."""

    def test_a_floor_of_one_lists_the_one_offs_and_keeps_the_recurring(
            self, fragments):
        table = fragments.table("-m", "1")
        assert ("1", "group") in table
        assert ("4", "show") in table

    def test_a_floor_above_every_count_leaves_no_rows_but_still_a_header(
            self, fragments):
        assert fragments.table("-m", "99") == []
        assert "# Minimum prevalence reported: 99\n" \
            in fragments.report.read_text(encoding="utf-8")

    def test_a_floor_keeps_only_what_clears_it(self, fragments):
        assert fragments.table("-m", "4") == [("4", "show"), ("4", "some")]

    @pytest.mark.parametrize("value", ["0", "abc", "-3", "2.5", ""])
    def test_a_floor_that_is_not_a_whole_number_of_one_or_more_is_refused(
            self, fragments, value):
        fragments.ffc("-m", value, fragments.tree, expect=1)

    def test_the_refusal_names_the_option_what_it_wanted_and_what_it_got(
            self, fragments):
        log = fragments.ffc("-m", "0", fragments.tree, expect=1)
        assert ("The -m prevalence threshold must be a whole number of 1 or "
                "more") in log
        assert 'got "0"' in log


class TestTheOutputOverride:
    """`-o` changes only WHERE the report goes."""

    def test_it_writes_there_with_the_same_content_and_leaves_the_default_alone(
            self, fragments):
        fragments.ffc(fragments.tree)
        default = _table(fragments.report)
        elsewhere = fragments.work / "elsewhere.txt"
        fragments.ffc("-o", elsewhere, fragments.tree)
        assert elsewhere.is_file()
        assert _table(elsewhere) == default
        assert _table(fragments.report) == default


class TestTheTokenisingRules:
    """One witness per rule, read off the `-m 1` table so a rule that wrongly
    KEPT something shows up as an extra row."""

    @pytest.fixture
    def candidates(self, fragments):
        return [candidate for _, candidate in fragments.table("-m", "1")]

    @pytest.mark.parametrize("tag", ["1080p", "720p", "x264", "webrip",
                                     "bluray"])
    def test_a_quality_or_source_tag_is_kept(self, candidates, tag):
        """Which is the whole point of the command: these are exactly what a
        reviewer wants surfaced."""
        assert tag in candidates

    @pytest.mark.parametrize("word", ["the", "of"])
    def test_a_common_word_is_dropped_by_the_stop_list(self, candidates, word):
        """These two, and not "a" or "mkv": emptying the stop list surfaces only
        these. "a" goes to the single-character rule below, and "mkv" is only
        ever a trailing extension in this fixture, so it never becomes a token
        for the list to reject - three rules that would read as one if they
        shared a case."""
        assert word not in candidates

    def test_a_pure_number_is_dropped(self, candidates):
        assert "2019" not in candidates

    def test_no_single_character_candidate_is_reported(self, candidates):
        assert [c for c in candidates if len(c) < 2] == []

    def test_a_trailing_extension_leaves_no_token_behind(self, candidates):
        assert "mkv" not in candidates

    def test_an_episode_token_is_surfaced_for_review(self, candidates):
        """Word-ish, so it survives: a genuine candidate the reviewer decides
        about rather than something the command prejudges."""
        assert "s01e01" in candidates

    def test_the_names_words_survive_the_extension_strip(self, candidates):
        assert "another" in candidates

    def test_a_name_containing_a_literal_branch_connector_is_still_parsed(
            self, candidates):
        """The tree syntax is stripped by exact cell structure, so the "-- " in
        a real name is not mistaken for a connector."""
        assert "thing" in candidates

    def test_the_root_line_is_not_a_candidate(self, candidates):
        assert "myfolder" not in candidates


class TestTheOtherSpellingsOfATree:
    def test_a_unicode_glyph_tree_is_parsed(self, fragments):
        tree = fragments.work / "unicode.tree"
        tree.write_text(_UNICODE, encoding="utf-8")
        assert fragments.table(tree=tree) == [("3", "delta")]

    def test_a_tree_file_with_crlf_line_endings_parses_identically(
            self, fragments):
        tree = fragments.work / "crlf.tree"
        tree.write_text(_REPEAT.replace("\n", "\r\n"), encoding="utf-8")
        assert fragments.table(tree=tree) == [("2", "alpha")]


class TestARefusedPath:
    def test_neither_a_folder_nor_a_readable_file_is_refused(self, fragments):
        log = fragments.ffc(fragments.work / "does-not-exist", expect=1)
        assert "neither a folder nor a readable file" in log.lower()


class TestTheFolderInput:
    """The one mode that needs the real `tree`: it writes its own snapshot INTO
    the folder and reports beside it."""

    @pytest.fixture
    def library(self, fragments):
        if not shutil.which("tree"):
            pytest.fail("the host has no `tree`: the folder input mode "
                        "generates its own snapshot with it")
        folder = fragments.work / "library"
        group = folder / "Some.Show.1080p.x264-GROUP"
        group.mkdir(parents=True)
        (group / "Some.Show.S01E01.1080p.x264.mkv").touch()
        (group / "Some.Show.S01E02.1080p.x264.mkv").touch()
        (folder / "Another.Show.1080p.mkv").touch()
        return fragments, folder

    def test_it_writes_the_snapshot_and_the_report_into_the_folder(
            self, library):
        fragments, folder = library
        fragments.ffc(folder)
        assert (folder / "library.tree").is_file()
        report = folder / "fragmentCandidates.txt"
        assert report.is_file()
        assert "# Fragment candidates from: %s\n" % (folder / "library.tree") \
            in report.read_text(encoding="utf-8")

    def test_it_finds_the_recurring_tags_of_the_library(self, library):
        fragments, folder = library
        fragments.ffc(folder)
        assert "1080p" in [c for _, c
                           in _table(folder / "fragmentCandidates.txt")]

    def test_no_input_file_is_lost(self, library):
        fragments, folder = library
        fragments.ffc(folder)
        assert (folder / "Some.Show.1080p.x264-GROUP"
                / "Some.Show.S01E01.1080p.x264.mkv").is_file()

    def test_a_rerun_overwrites_its_artefacts_rather_than_accumulating(
            self, library):
        fragments, folder = library
        fragments.ffc(folder)
        before = blackbox.tree_of(folder)
        fragments.ffc(folder)
        assert blackbox.tree_of(folder) == before
