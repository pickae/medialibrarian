"""`content-census-bi` as a process: its option parsing, and what the flags do.

The reports are the LAST argument and there can be any number of them, which makes
the order people actually type

    content-census-bi ~/reports -s -e ~/cubes

rather than the one the usage line shows. A parser that stops at the first path
takes every flag written after the reports for another report, and the run refuses
with '"-s" is neither a file nor a folder' - a flag silently not doing its job, or
the whole run refused, over nothing but word order. Both orders are one command.

`duckdb` is a hard requirement rather than a skip: half the claims here are about
what the flags BUILD, and a case that quietly does not run reports as a pass,
which makes a green suite on a half-equipped host meaningless.
"""

from __future__ import annotations

import shutil

import pytest

pytestmark = pytest.mark.fs


@pytest.fixture
def bi(sandbox, tmp_path):
    """A books report, the cheapest real one: five columns the census writes
    itself, with no probe of any kind behind them."""
    if not shutil.which("duckdb"):
        pytest.fail("the host has no duckdb: the cubes are what is under test")
    work = tmp_path / "work"
    reports = work / "reports"
    cubes = work / "cubes"
    reports.mkdir(parents=True)
    cubes.mkdir(parents=True)
    (reports / "booksShelf.csv").write_text(
        "path,sizeBytes,pages,words,characters\n"
        "%s/Books/a.txt,50000,300,80000,450000\n"
        "%s/Books/b.txt,70000,410,96000,530000\n" % (work, work))

    def run(*args, expect=0, **kwargs):
        done = sandbox.run("content-census-bi", *args, **kwargs)
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.work = work
    sandbox.reports = reports
    sandbox.cubes = cubes
    sandbox.bi = run
    return sandbox


class TestTheOptionsAreReadInAnyPosition:
    """Before the reports, after them, in between, and clustered - one parse, so
    the last three come for free and are exactly what a half-fixed parser still
    gets wrong."""

    def test_before_the_reports_which_is_what_the_usage_line_shows(self, bi):
        log = bi.bi("-s", "-e", bi.cubes, "-o", bi.work / "before.duckdb",
                    bi.reports)
        assert "books: 1 report(s)" in log
        assert "neither a file nor a folder" not in log

    def test_after_the_reports_which_is_what_people_type(self, bi):
        log = bi.bi(bi.reports, "-s", "-e", bi.cubes,
                    "-o", bi.work / "after.duckdb")
        assert "books: 1 report(s)" in log
        assert "neither a file nor a folder" not in log

    def test_a_path_between_two_options_is_still_a_path(self, bi):
        log = bi.bi("-e", bi.cubes, bi.reports, "-s",
                    "-o", bi.work / "mixed.duckdb")
        assert "books: 1 report(s)" in log
        assert "neither a file nor a folder" not in log


class TestTheOptionsAreReadRatherThanTolerated:
    """Asserted through the refusals they own: only a parsed `-e` can complain
    about its folder, so a complaint proves the flag was read and not merely
    stepped over."""

    def test_the_export_folder_is_checked_when_named_after_the_reports(
            self, bi):
        log = bi.bi(bi.reports, "-e", bi.work / "notAFolder", expect=1)
        assert "export folder" in log

    def test_the_database_path_is_checked_when_named_after_the_reports(
            self, bi):
        log = bi.bi(bi.reports, "-o", bi.work / "notAFolder" / "x.duckdb",
                    expect=1)
        assert "the database cannot be written there" in log

    def test_a_clustered_pair_after_the_reports_is_read_as_both(self, bi):
        log = bi.bi(bi.reports, "-se", bi.work / "notAFolder", expect=1)
        assert "export folder" in log


class TestTheEndOfOptions:
    """`--` is what keeps a path beginning with a dash reachable. It has to be a
    folder rather than a report: a report's type comes from the start of its
    NAME, so a file called "-books..." is not a books report at all."""

    def test_a_path_after_it_is_a_path_and_not_an_option(self, bi):
        dashed = bi.work / "-dashed"
        dashed.mkdir()
        shutil.copy(bi.reports / "booksShelf.csv", dashed / "booksShelf.csv")
        # Relative, because an absolute path never starts with a dash.
        log = bi.bi("-o", bi.work / "dashed.duckdb", "--", "-dashed",
                    cwd=bi.work)
        assert "books: 1 report(s)" in log
        assert "Usage:" not in log


class TestWhatIsRefused:
    """A flag that does not exist was meant to do something, and a run that
    silently did not do it is worse than one that did not start."""

    def test_an_unknown_option_is_refused_with_the_usage(self, bi):
        log = bi.bi("-x", bi.reports, expect=1)
        assert "Usage:" in log
        assert "Nothing was changed" in log

    def test_an_option_missing_its_argument_is_refused(self, bi):
        bi.bi(bi.reports, "-o", expect=1)

    def test_no_arguments_at_all_is_the_usage(self, bi):
        log = bi.bi(expect=1)
        assert "Usage:" in log

    def test_the_help_flag_exits_zero_and_prints_the_usage(self, bi):
        assert "Usage:" in bi.bi("-h")

    def test_the_help_flag_after_the_reports_prints_it_too(self, bi):
        """And builds nothing on the way."""
        log = bi.bi(bi.reports, "-h")
        assert "Usage:" in log
        assert not list(bi.cubes.iterdir())

    def test_the_page_says_the_options_are_position_free(self, bi):
        assert "may be given before the reports or after them" in bi.bi("-h")


class TestWhatTheFlagsBuild:
    """The same two flags, in both positions, doing their job rather than merely
    being accepted."""

    def test_with_the_options_at_the_end(self, bi):
        log = bi.bi(bi.reports, "-s", "-e", bi.cubes,
                    "-o", bi.work / "real.duckdb")
        assert (bi.cubes / "booksCube.csv").is_file()
        assert "books, all of it" in log
        assert (bi.work / "real.duckdb").is_file()

    def test_with_the_options_at_the_front(self, bi):
        log = bi.bi("-s", "-e", bi.cubes, "-o", bi.work / "real2.duckdb",
                    bi.reports)
        assert (bi.cubes / "booksCube.csv").is_file()
        assert "books, all of it" in log
        assert (bi.work / "real2.duckdb").is_file()
