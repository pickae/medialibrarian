"""The white box for medialib/cli/content_census_bi.py.

test_content_census_bi_cli.py drives the whole thing through DuckDB; what is
pinned here is the preflight - which paths are reports, which refusals come
before anything is written, and the one distinction the command is built around:
a path NAMED on the command line is a claim and is refused by name, while a path
FOUND inside a folder is a guess and is passed over.
"""

import os

import pytest

from medialib.cli import content_census_bi as bi
from medialib.lib import cubes

pytestmark = pytest.mark.fs


def _report(tmp_path, name, content=None, header=None):
    path = tmp_path / name
    if header is None:
        separator = "\t" if name.lower().endswith(".tsv") else ","
        # column_names lists the names space-separated; the header is those
        # names joined by the separator the report is written with.
        names = cubes.column_names(content or cubes.report_type(str(path)))
        header = separator.join(names.split()) if names else None
    path.write_text((header or "not a header") + "\n")
    return str(path)


class TestCollectPaths:
    def test_a_named_file_is_taken_as_claimed(self, tmp_path):
        path = _report(tmp_path, "audioFilms.csv")
        assert bi.collect_paths([path]) == [(path, True)]

    def test_a_folder_is_searched_and_what_it_holds_is_a_guess(self, tmp_path):
        _report(tmp_path, "audioFilms.csv")
        _report(tmp_path, "videoFilms.csv")
        (tmp_path / "spreadsheet.ods").write_text("x")
        found = bi.collect_paths([str(tmp_path)])
        assert [os.path.basename(p) for p, _ in found] == ["audioFilms.csv",
                                                           "videoFilms.csv"]
        assert all(explicit is False for _, explicit in found)

    def test_a_folder_is_searched_recursively(self, tmp_path):
        (tmp_path / "deep").mkdir()
        _report(tmp_path / "deep", "comicsThings.csv")
        assert len(bi.collect_paths([str(tmp_path)])) == 1

    def test_a_path_that_is_neither_is_refused(self, tmp_path):
        with pytest.raises(bi.Refusal) as raised:
            bi.collect_paths([str(tmp_path / "gone")])
        assert "is neither a file nor a folder" in raised.value.text

    def test_a_folder_holding_no_report_is_refused(self, tmp_path):
        (tmp_path / "spreadsheet.ods").write_text("x")
        with pytest.raises(bi.Refusal) as raised:
            bi.collect_paths([str(tmp_path)])
        assert "none of the given paths holds a census report" in \
            raised.value.text


class TestClassify:
    def test_a_report_is_filed_by_type_and_made_absolute(self, tmp_path):
        path = _report(tmp_path, "audioFilms.csv", "audio")
        by_type, skipped = bi.classify([(path, True)])
        assert by_type["audio"] == [os.path.realpath(path)]
        assert skipped == []

    def test_a_named_file_with_the_wrong_name_is_refused(self, tmp_path):
        path = _report(tmp_path, "notacensus.csv", header="a,b,c")
        with pytest.raises(bi.Refusal) as raised:
            bi.classify([(path, True)])
        assert "is not a census report" in raised.value.text

    def test_a_named_file_with_the_wrong_header_is_refused(self, tmp_path):
        path = _report(tmp_path, "audioFilms.csv", header="a,b,c")
        with pytest.raises(bi.Refusal) as raised:
            bi.classify([(path, True)])
        assert "cannot be read" in raised.value.text
        assert "Re-run content-census" in raised.value.text

    def test_a_found_file_with_the_wrong_header_is_only_passed_over(
            self, tmp_path):
        """A folder holding a census and somebody's spreadsheet is not an
        error, and refusing the run over the spreadsheet would be."""
        good = _report(tmp_path, "audioFilms.csv", "audio")
        bad = _report(tmp_path, "audioOther.csv", header="a,b,c")
        by_type, skipped = bi.classify([(good, False), (bad, False)])
        assert by_type["audio"] == [os.path.realpath(good)]
        assert len(skipped) == 1 and "first line is not the header" in skipped[0]

    def test_nothing_usable_at_all_is_refused_and_lists_why(self, tmp_path):
        bad = _report(tmp_path, "audioOther.csv", header="a,b,c")
        with pytest.raises(bi.Refusal) as raised:
            bi.classify([(bad, False)])
        assert "none of the 1 file(s) found is a census report" in \
            raised.value.text
        assert "first line is not the header" in raised.value.text


class TestSeparators:
    def test_a_csv_type_settles_on_a_comma(self, tmp_path):
        path = _report(tmp_path, "audioFilms.csv", "audio")
        assert bi.separators({"audio": [path]}) == {"audio": ","}

    def test_a_tsv_type_settles_on_a_tab(self, tmp_path):
        path = _report(tmp_path, "audioFilms.tsv", "audio")
        assert bi.separators({"audio": [path]}) == {"audio": "\t"}

    def test_one_type_in_two_formats_is_refused(self, tmp_path):
        """One reader call per type reads all of that type's reports at once,
        and a single read_csv has one delimiter."""
        with pytest.raises(bi.Refusal) as raised:
            bi.separators({"audio": [_report(tmp_path, "audioA.csv", "audio"),
                                     _report(tmp_path, "audioB.tsv", "audio")]})
        assert "not all in the same format" in raised.value.text

    def test_two_types_may_differ_from_each_other(self, tmp_path):
        settled = bi.separators({
            "audio": [_report(tmp_path, "audioA.csv", "audio")],
            "video": [_report(tmp_path, "videoA.tsv", "video")]})
        assert settled == {"audio": ",", "video": "\t"}


class TestWhereTheOutputGoes:
    def test_the_default_is_beside_the_first_report(self, tmp_path):
        first = _report(tmp_path, "audioFilms.csv", "audio")
        assert bi.resolve_db_path("", first) == os.path.join(
            os.path.realpath(tmp_path), "contentCensusBI.duckdb")

    def test_a_named_database_is_made_absolute(self, tmp_path):
        first = _report(tmp_path, "audioFilms.csv", "audio")
        named = str(tmp_path / "cubes.duckdb")
        assert bi.resolve_db_path(named, first) == os.path.realpath(named)

    def test_a_database_in_a_folder_that_is_not_one_is_refused(self, tmp_path):
        first = _report(tmp_path, "audioFilms.csv", "audio")
        with pytest.raises(bi.Refusal) as raised:
            bi.resolve_db_path(str(tmp_path / "gone" / "x.duckdb"), first)
        assert "is not a folder" in raised.value.text

    def test_an_unwritable_folder_is_refused_before_anything_is_written(
            self, tmp_path):
        first = _report(tmp_path, "audioFilms.csv", "audio")
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            with pytest.raises(bi.Refusal) as raised:
                bi.resolve_db_path(str(locked / "x.duckdb"), first)
            assert "no write permission" in raised.value.text
        finally:
            os.chmod(locked, 0o700)

    def test_an_export_folder_that_is_not_one_is_refused(self, tmp_path):
        with pytest.raises(bi.Refusal) as raised:
            bi.resolve_export_dir(str(tmp_path / "gone"))
        assert "is not a folder" in raised.value.text

    def test_no_export_folder_is_no_refusal(self):
        assert bi.resolve_export_dir("") == ""


class TestHumanSize:
    """The page's line says what `du -h` says, because that is what the shell
    printed."""

    @pytest.mark.parametrize("size,expected", [
        (0, "0"), (512, "512"), (1024, "1.0K"), (1536, "1.5K"),
        (10 * 1024, "10K"), (1024 * 1024, "1.0M"),
    ])
    def test_it_reads_as_du_does(self, tmp_path, size, expected):
        path = tmp_path / "page.html"
        path.write_bytes(b"x" * size)
        assert bi._human_size(str(path)) == expected
