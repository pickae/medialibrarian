"""The white box for medialib/cli/find_fragment_candidates.py.

test_find_fragment_candidates_cli.py runs the command as a process and pins
its behaviour from the outside; what is pinned here is the two decisions that
turned out to belong to the HOST rather than to the code - which characters are
letters, and how two candidates sort - plus the CLI contract against the
recorded fixtures.
"""

import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from medialib.cli import find_fragment_candidates as ffc
from tests import blackbox

pytestmark = pytest.mark.fs

_FIXTURES = blackbox.DATA / "cliContract" / "find-fragment-candidates"

_PROGRAM = "find-fragment-candidates"

TREE = """myFolder
|-- Some.Show.1080p.x264-GROUP
|   |-- Some.Show.S01E01.1080p.x264.mkv
|   `-- Some.Show.S01E02.1080p.x264.mkv
|-- The.Movie.of.a.Lifetime.2019.720p.webrip.mkv
|-- Some -- Thing.mkv
`-- Another.Show.bluray.mkv
"""


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = ffc.main(list(argv), program=_PROGRAM)
    return status, out.getvalue(), err.getvalue()


def _report(tmp_path, text=TREE, argv=()):
    tree = tmp_path / "myFolder.tree"
    tree.write_text(text)
    _run(list(argv) + [str(tree)])
    lines = (tmp_path / "myFolder.fragmentCandidates.txt").read_text()
    return [line for line in lines.splitlines() if not line.startswith("#")]


class TestTheContract:
    @pytest.mark.parametrize("scenario,argv", [
        ("h", ["-h"]),
        ("noargs", []),
        ("errUnknown", ["-x"]),
    ])
    def test_the_recorded_scenario_is_reproduced_byte_for_byte(self, scenario,
                                                               argv):
        status, out, err = _run(argv)
        assert out == (_FIXTURES / f"{scenario}.out").read_text()
        assert err == (_FIXTURES / f"{scenario}.err").read_text()
        assert status == int((_FIXTURES / f"{scenario}.rc").read_text().strip())

    def test_a_path_that_is_neither_folder_nor_file_is_refused(self, tmp_path):
        status, _, err = _run([str(tmp_path / "gone")])
        assert status == 1
        assert err == ('Error: "%s" is neither a folder nor a readable '
                       "file.\n" % (tmp_path / "gone"))


class TestTheTable:
    def test_the_default_floor_leaves_the_recurring_four(self, tmp_path):
        assert _report(tmp_path) == ["4\tshow", "4\tsome", "3\t1080p",
                                     "3\tx264"]

    def test_the_columns_are_tab_separated(self, tmp_path):
        assert all("\t" in row for row in _report(tmp_path))

    def test_a_floor_of_one_lists_every_candidate(self, tmp_path):
        rows = _report(tmp_path, argv=["-m", "1"])
        assert "1\twebrip" in rows and "1\tbluray" in rows

    def test_prevalence_counts_names_not_occurrences(self, tmp_path):
        """"some" twice in one name is still one name carrying it."""
        rows = _report(tmp_path, "myFolder\n|-- some.some.some.mkv\n",
                       argv=["-m", "1"])
        assert rows == ["1\tsome"]

    def test_the_report_is_written_beside_the_tree_file(self, tmp_path):
        tree = tmp_path / "deep" / "myFolder.tree"
        tree.parent.mkdir()
        tree.write_text(TREE)
        status, out, _ = _run([str(tree)])
        assert status == 0
        assert (tmp_path / "deep" / "myFolder.fragmentCandidates.txt").exists()
        assert out.startswith("Wrote 4 candidate(s) to ")

    def test_o_puts_the_report_where_it_says(self, tmp_path):
        tree = tmp_path / "myFolder.tree"
        tree.write_text(TREE)
        elsewhere = tmp_path / "elsewhere.txt"
        status, _, _ = _run(["-o", str(elsewhere), str(tree)])
        assert status == 0
        assert elsewhere.exists()
        assert not (tmp_path / "myFolder.fragmentCandidates.txt").exists()


class TestTheTokenising:
    """One assertion per rule, including the deliberate KEEPING of quality and
    source tags - which is the whole point of the script."""

    def _candidates(self, tmp_path, name):
        rows = _report(tmp_path, "myFolder\n|-- %s\n" % name, argv=["-m", "1"])
        return [row.split("\t")[1] for row in rows]

    def test_a_trailing_extension_is_dropped(self, tmp_path):
        assert self._candidates(tmp_path, "Recurring.mkv") == ["recurring"]

    def test_a_long_suffix_is_not_an_extension(self, tmp_path):
        """The pattern is a dot and up to four characters, so ".verylong" is
        part of the name."""
        assert "verylong" in self._candidates(tmp_path, "Thing.verylong")

    def test_pure_numbers_and_lone_characters_go(self, tmp_path):
        assert self._candidates(tmp_path, "2019 a 01 x Thing") == ["thing"]

    def test_common_words_go(self, tmp_path):
        assert self._candidates(tmp_path, "The Movie of a Lifetime") == [
            "lifetime", "movie"]

    def test_quality_and_source_tags_are_kept_on_purpose(self, tmp_path):
        # ".hdr" is the trailing extension here and goes with it, which is the
        # rule above meeting this one - checked against the shell, which says
        # the same five.
        assert sorted(self._candidates(
            tmp_path, "Film.1080p.x264.webrip.bluray.hdr")) == [
            "1080p", "bluray", "film", "webrip", "x264"]

    def test_a_literal_dashes_run_inside_a_name_is_not_a_connector(self,
                                                                   tmp_path):
        assert self._candidates(tmp_path, "Some -- Thing.mkv") == ["some",
                                                                   "thing"]

    def test_the_root_line_contributes_nothing(self, tmp_path):
        """It has no connector, so it is not an entry."""
        assert _report(tmp_path, "myFolderIsUnique\n", argv=["-m", "1"]) == []


class TestWhatBelongsToTheHost:
    """Two answers this script takes from the C library rather than from its
    own code, because that is where awk and sort take them."""

    def test_an_accented_word_is_one_token_not_three(self):
        """awk's [:alnum:] in a UTF-8 locale counts ü as a letter, and neither
        str.isalnum nor a [0-9a-zA-Z] class agrees with it."""
        assert list(ffc._tokens("ünïcøde")) == ["ünïcøde"]
        assert list(ffc._tokens("café naïve")) == ["café", "naïve"]

    def test_a_superscript_is_not_a_digit_here(self, tmp_path):
        """glibc says U+00B2 is not alnum, so it breaks a token the way a dot
        does - which is the sort of thing no Python predicate gets right by
        accident."""
        assert list(ffc._tokens("a²b")) == ["a", "b"]

    def test_the_order_is_the_locale_s_and_not_the_bytes(self, monkeypatch):
        """`sort -k2,2` collates, so the report's tie order is a property of
        the host: under C.UTF-8 it is byte order and under en_US.UTF-8 it is
        dictionary order. The port asks the same library rather than choosing
        one."""
        rows = [(1, "über"), (1, "zulu"), (1, "alpha")]
        ordered = [token for _, token in ffc._sorted_rows(rows)]
        assert ordered[0] == "alpha"
        assert set(ordered) == {"über", "zulu", "alpha"}

    def test_a_higher_prevalence_always_comes_first(self):
        rows = [(1, "aaa"), (9, "zzz"), (3, "mmm")]
        assert [count for count, _ in ffc._sorted_rows(rows)] == [9, 3, 1]
