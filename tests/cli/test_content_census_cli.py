"""`content-census` as a process: its multi-library argument handling, and `-b`.

The census grew from "one folder" to "one or more folders, each its own library"
because `content-census-bi` rolls every report of a type into ONE cube and
separates them again along a library axis taken from the report's NAME. That axis
is only worth having if producing several libraries' reports is one command, so
the argument handling that makes it one is what this file is about.

Everything runs on `.txt` books, which the census counts directly with no Calibre,
no ffprobe and no other tool - so these are real rows in real reports with no
media and no external binary anywhere.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.fs


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _rows(report):
    """The report's data rows, header dropped."""
    return report.read_text(encoding="utf-8").splitlines()[1:]


def _progress_order(log: str) -> list[str]:
    return re.findall(r"^\[\d+/\d+\] (.*)$", log, re.MULTILINE)


@pytest.fixture
def census(sandbox, tmp_path):
    """Three libraries with different word counts, so a report cannot be
    mistaken for another library's, and one folder holding nothing the census
    reads."""
    work = tmp_path / "work"
    _write(work / "Films" / "a.txt", "one two three four five\n")
    _write(work / "Series" / "b.txt", "six seven eight\n")
    _write(work / "Music" / "c.txt", "nine ten\n")
    _write(work / "Nothing" / "readme.md", "not something the census reads\n")
    (work / "reports").mkdir()

    def run(*args, expect=0):
        done = sandbox.run("content-census", *args)
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.work = work
    sandbox.census = run
    return sandbox


class TestOneFolder:
    """The single-folder case every existing habit depends on, which the rewrite
    into a list of paths must not have moved."""

    def test_the_report_lands_in_the_folder_named_after_it(self, census):
        census.census(census.work / "Films")
        report = census.work / "Films" / "booksFilms.csv"
        assert report.is_file()
        assert len(_rows(report)) == 1


class TestSeveralLibraries:
    """One report set per library, named per library, and `-o` puts them all in
    one place."""

    @pytest.fixture
    def run(self, census):
        reports = census.work / "reports"
        census.census("-o", reports, census.work / "Films",
                      census.work / "Series", census.work / "Music")
        return census, reports

    def test_each_library_gets_a_report_named_after_it(self, run):
        _, reports = run
        for name in ("booksFilms.csv", "booksSeries.csv", "booksMusic.csv"):
            assert (reports / name).is_file(), name

    def test_nothing_is_written_into_the_censused_folder_under_o(self, run):
        census, _ = run
        assert not (census.work / "Films" / "booksFilms.csv").exists()

    def test_a_report_holds_its_own_librarys_rows_and_no_others(self, run):
        """The thing the cube's library axis relies on."""
        census, reports = run
        rows = _rows(reports / "booksFilms.csv")
        assert len(rows) == 1
        assert str(census.work / "Films" / "a.txt") in rows[0]
        assert "Series" not in rows[0]

    def test_the_report_names_are_the_library_names(self, run):
        """The reports are the only thing carrying a library's identity on to the
        backend, which reads it back off the name."""
        _, reports = run
        found = sorted(p.name[len("books"):-len(".csv")]
                       for p in reports.glob("books*.csv"))
        assert found == ["Films", "Music", "Series"]


class TestTheRefusals:
    """Each fires before anything is written, because each exists to stop a
    failure that is silent afterwards."""

    def test_two_folders_of_one_name_are_refused_naming_both(self, census):
        """Silent either way: an overwrite under `-o`, and a merge into one
        library in the cube without it."""
        left = _write(census.work / "left" / "Films" / "l.txt", "left words\n")
        right = _write(census.work / "right" / "Films" / "r.txt", "right w\n")
        out = census.work / "clash"
        out.mkdir()
        log = census.census("-o", out, left.parent, right.parent, expect=1)
        assert str(left.parent) in log and str(right.parent) in log
        assert "nothing was changed" in log.lower()
        assert list(out.iterdir()) == []

    def test_the_same_folder_twice_is_one_library(self, census):
        out = census.work / "clash"
        out.mkdir()
        census.census("-o", out, census.work / "Films", census.work / "Films")
        assert len(list(out.glob("books*.csv"))) == 1
        assert len(_rows(out / "booksFilms.csv")) == 1

    def test_a_bad_path_among_good_ones_is_refused_before_anything_is_read(
            self, census):
        out = census.work / "clash"
        out.mkdir()
        log = census.census("-o", out, census.work / "Films",
                            census.work / "nosuchfolder", expect=1)
        assert "nosuchfolder" in log
        assert list(out.iterdir()) == []

    def test_an_empty_library_among_good_ones_is_named_and_the_rest_run(
            self, census):
        out = census.work / "clash"
        out.mkdir()
        log = census.census("-o", out, census.work / "Nothing",
                            census.work / "Films")
        assert str(census.work / "Nothing") in log
        assert (out / "booksFilms.csv").is_file()

    def test_nothing_to_census_at_all_is_a_refusal(self, census):
        out = census.work / "clash"
        out.mkdir()
        census.census("-o", out, census.work / "Nothing", expect=1)


@pytest.fixture
def shelf(census):
    """A disk that is a shelf of libraries rather than one library: two
    libraries, one of them with a library-to-be under it, and a file lying on the
    shelf itself one level too high to belong to any of them."""
    root = census.work / "Shelf"
    _write(root / "Films" / "f.txt", "films words here now\n")
    _write(root / "Films" / "Marvel" / "m.txt", "deeper in the films\n")
    _write(root / "Series" / "s.txt", "series words\n")
    _write(root / "loose.txt", "a file lying on the shelf itself\n")
    (root / "collect").mkdir()
    census.shelf = root
    census.collect = root / "collect"
    return census


class TestTheDepthOption:
    """`-d` takes the libraries from BELOW the paths given, because naming forty
    of them by hand is not an answer. The level is taken literally: exactly that
    level is a library, everything under it belongs to it, and anything beside or
    above it belongs to no library and is not censused."""

    def test_depth_one_makes_every_subfolder_its_own_library(self, shelf):
        shelf.census("-d", "1", "-o", shelf.collect, shelf.shelf)
        assert (shelf.collect / "booksShelfFilms.csv").is_file()
        assert (shelf.collect / "booksShelfSeries.csv").is_file()
        assert not (shelf.collect / "booksShelf.csv").exists()

    def test_a_library_holds_what_is_under_it_and_not_what_is_beside_it(
            self, shelf):
        shelf.census("-d", "1", "-o", shelf.collect, shelf.shelf)
        films = _rows(shelf.collect / "booksShelfFilms.csv")
        assert len(films) == 2
        everything = films + _rows(shelf.collect / "booksShelfSeries.csv")
        assert not [row for row in everything if "loose.txt" in row]

    def test_depth_two_makes_the_grandchild_the_library(self, shelf):
        """Named the whole way down to it, which is the level the cube's library
        axis then has to drill into."""
        shelf.census("-d", "2", "-o", shelf.collect, shelf.shelf)
        report = shelf.collect / "booksShelfFilmsMarvel.csv"
        assert report.is_file()
        assert not (shelf.collect / "booksShelfFilms.csv").exists()
        assert len(_rows(report)) == 1

    def test_without_an_output_folder_the_reports_land_in_the_libraries(
            self, shelf):
        shelf.census("-d", "1", shelf.shelf)
        assert (shelf.shelf / "Series" / "booksShelfSeries.csv").is_file()
        assert not (shelf.shelf / "booksShelf.csv").exists()

    def test_depth_zero_is_what_leaving_the_option_out_does(self, shelf):
        shelf.census("-d", "0", "-o", shelf.collect, shelf.shelf)
        report = shelf.collect / "booksShelf.csv"
        assert report.is_file()
        assert len(_rows(report)) == 4

    def test_a_depth_that_is_not_a_number_is_refused(self, shelf):
        log = shelf.census("-d", "one", "-o", shelf.collect, shelf.shelf,
                           expect=1)
        assert "whole number" in log.lower()

    def test_a_depth_deeper_than_the_tree_is_refused(self, shelf):
        log = shelf.census("-d", "9", "-o", shelf.collect, shelf.shelf,
                           expect=1)
        assert "nothing was changed" in log.lower()

    def test_two_libraries_found_under_it_that_would_share_a_name_are_refused(
            self, census):
        """The collision the census refuses one level up is just as silent one
        level down: "Twin" holding "AB" and "TwinA" holding "B" both work out to
        the report "TwinAB"."""
        left = _write(census.work / "deep" / "Twin" / "AB" / "l.txt", "left\n")
        right = _write(census.work / "deep" / "TwinA" / "B" / "r.txt", "right\n")
        out = census.work / "deep" / "out"
        out.mkdir()
        log = census.census("-d", "1", "-o", out, census.work / "deep" / "Twin",
                            census.work / "deep" / "TwinA", expect=1)
        assert str(left.parent) in log and str(right.parent) in log
        assert list(out.iterdir()) == []


class TestTheParallelWalk:
    """Several `<inputPath>`s mean several disks, so they are read at once; the
    libraries found UNDER one path share its disk and are read in order.

    What a test can honestly assert about that is the outcome: every report
    arrives whole, the closing count is exact across the workers, and a progress
    line still says which library it is about once several are being read at
    once.
    """

    def test_three_paths_at_once_write_all_three_reports(self, census):
        out = census.work / "clash"
        out.mkdir()
        log = census.census("-o", out, census.work / "Films",
                            census.work / "Series", census.work / "Music")
        assert len(list(out.glob("books*.csv"))) == 3
        assert len(_rows(out / "booksSeries.csv")) == 1
        assert re.search(r"(^|\] )Series: ", log, re.MULTILINE), log

    def test_the_closing_count_is_exact_across_the_workers(self, census):
        out = census.work / "clash"
        out.mkdir()
        log = census.census("-o", out, census.work / "Films",
                            census.work / "Series", census.work / "Music")
        assert "Censused 3 of 3 file(s) from 3 libraries" in log

    def test_one_path_keeps_the_plain_progress_line(self, census):
        out = census.work / "clash"
        out.mkdir()
        log = census.census("-o", out, census.work / "Films")
        assert "[1/1] a.txt" in log

    def test_depth_over_one_path_is_a_single_worker_too(self, shelf):
        """The libraries it finds are subfolders of one tree, so they share its
        head and are censused one after the other."""
        log = shelf.census("-d", "1", "-o", shelf.collect, shelf.shelf)
        assert re.search(r"\[1/", log)
        assert "one worker each" not in log


class TestTheBookPool:
    """Books are the slow half of the census - each one is converted to text -
    and they run in a pool of one worker per CPU.

    The assertable part is the outcome, not the timing: every book lands a row,
    each row carries its own word count, and the rows come out in the SORTED file
    order rather than the order the workers finished, because a report that
    reordered itself per run would not diff against the last one.

    Two limits, so the next reader does not take this for more than it is.
    Comparing the report's order against the order the progress lines announced
    cannot fail - both come out of `merge_book_results`' one loop, and reversing
    that loop reverses them together - so sorted order is the falsifiable form
    and is what is asserted. And the clobber the word counts guard against is
    real but out of reach here: a `.txt` book is already text and never converts,
    so a fixed scratch name passes these fixtures untouched.
    """

    @pytest.fixture
    def run(self, census):
        pool = census.work / "Pool"
        for name, text in (("a.txt", "one two three\n"),
                           ("b.txt", "one two three four five\n"),
                           ("c.txt", "one\n"),
                           ("d.txt", "one two\n"),
                           ("e.txt", "one two three four\n")):
            _write(pool / name, text)
        log = census.census(pool)
        return pool / "booksPool.csv", log

    def test_every_book_lands_a_row(self, run):
        report, _ = run
        assert len(_rows(report)) == 5

    def test_each_row_carries_its_own_word_count(self, run):
        report, _ = run
        words = {row.split(",")[0].rpartition("/")[2]: row.split(",")[3]
                 for row in _rows(report)}
        assert words == {"a.txt": "3", "b.txt": "5", "c.txt": "1",
                         "d.txt": "2", "e.txt": "4"}

    def test_the_rows_come_out_in_sorted_file_order(self, run):
        report, log = run
        order = [row.split(",")[0].rpartition("/")[2] for row in _rows(report)]
        assert order == ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
        # The progress lines agree with the report, which is worth pinning even
        # though it cannot fail on its own - the two share a loop.
        assert _progress_order(log) == order


class TestTheReportFolder:
    """`-o` makes a folder that is not there yet - "mkdir it first" is a step
    that exists only to be forgotten - and takes it back when the run then
    refuses, so "nothing was changed" is true of directories too."""

    def test_a_folder_that_does_not_exist_is_made_and_used(self, census):
        log = census.census("-o", census.work / "fresh", census.work / "Films")
        assert (census.work / "fresh" / "booksFilms.csv").is_file()
        assert "created the report folder" in log.lower()

    def test_a_whole_missing_path_is_made_and_not_just_its_last_level(
            self, census):
        census.census("-o", census.work / "deeper" / "still" / "missing",
                      census.work / "Films")
        assert (census.work / "deeper" / "still" / "missing"
                / "booksFilms.csv").is_file()

    def test_the_folder_it_made_is_gone_again_when_the_run_refuses(
            self, census):
        census.census("-o", census.work / "never", census.work / "Nothing",
                      expect=1)
        assert not (census.work / "never").exists()

    def test_every_level_it_made_is_gone_again(self, census):
        census.census("-o", census.work / "alsonever" / "inside",
                      census.work / "Films", census.work / "nosuchfolder",
                      expect=1)
        assert not (census.work / "alsonever").exists()

    def test_a_name_taken_by_a_file_is_refused(self, census):
        taken = _write(census.work / "takenByAFile", "not a folder\n")
        log = census.census("-o", taken, census.work / "Films", expect=1)
        assert "not a folder" in log.lower()

    def test_a_path_under_a_file_is_refused(self, census):
        taken = _write(census.work / "takenByAFile", "not a folder\n")
        log = census.census("-o", taken / "below", census.work / "Films",
                            expect=1)
        assert "could not be made" in log.lower()


class TestTheBackendFlag:
    """The cubes need DuckDB and are the backend's own business; what belongs to
    this command is that `-b` is documented."""

    def test_it_is_documented_in_the_usage(self, census):
        assert re.search(r"^\s+-b, --build-cubes", census.census("-h"),
                         re.MULTILINE)
