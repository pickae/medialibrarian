"""The local IMDb lists: fetched only when stale, built into DuckDB, and looked
up by the folded keys the lookup compares.

The build and the lookup are run against the real DuckDB over two lists
written here, a handful of rows long - the SQL is the thing under test, and a
stand-in would only say that it was handed some. The fetch is never made: curl
is stood in for.
"""

import gzip
import os
import shutil
import time

import pytest

from medialib.lib import imdbdata, titlematch

pytestmark = pytest.mark.fs

_BASICS = [
    ("tconst", "titleType", "primaryTitle", "originalTitle", "isAdult",
     "startYear", "endYear", "runtimeMinutes", "genres"),
    ("tt0000101", "movie", "Mother Moss", "Frau Möss", "0", "1954", "\\N",
     "90", "Fantasy"),
    ("tt0000102", "tvMovie", "The Harbour Kid", "The Harbour Kid", "0",
     "2008", "\\N", "58", "Family"),
    ("tt0000103", "tvEpisode", "Frau Möss", "Frau Möss", "0", "2010",
     "\\N", "25", "Family"),
    ("tt0000104", "movie", "Frau Möss", "Frau Möss", "1", "2011", "\\N",
     "80", "Adult"),
    ("tt0000105", "short", "Quote \"Me\"", "Quote \"Me\"", "0", "1925",
     "\\N", "\\N", "Short"),
]

_AKAS = [
    ("titleId", "ordering", "title", "region", "language", "types",
     "attributes", "isOriginalTitle"),
    ("tt0000101", "1", "Mère Mousse", "FR", "\\N", "\\N", "\\N", "0"),
    ("tt0000102", "1", "Der Hafenjunge", "DE", "\\N", "\\N", "\\N", "0"),
    ("tt0000103", "1", "Episode Title", "US", "\\N", "\\N", "\\N", "0"),
]


def _write(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write("\t".join(row) + "\n")


@pytest.fixture
def lists(tmp_path):
    """The two lists, freshly written, in a directory of their own."""
    if not shutil.which("duckdb"):
        pytest.skip("needs the duckdb binary")
    where = tmp_path / "imdb"
    where.mkdir()
    _write(where / imdbdata.BASICS, _BASICS)
    _write(where / imdbdata.AKAS, _AKAS)
    return where


def _keys(title):
    return {key.replace(" ", "") for key in titlematch.title_keys(title)}


@pytest.mark.imdb_lists
class TestTheBuild:
    def test_fresh_lists_are_built_and_not_fetched(self, lists, monkeypatch):
        fetched = []
        monkeypatch.setattr(imdbdata, "_fetch",
                            lambda *arguments: fetched.append(arguments))
        database = imdbdata.prepare(str(lists), [].append)
        assert database == str(lists / imdbdata.DATABASE)
        assert os.path.isfile(database)
        assert len(fetched) == 2

    def test_a_title_is_reached_under_its_folded_keys(self, lists):
        database = imdbdata.prepare(str(lists), [].append)
        rows = imdbdata.titles_under(database, _keys("Frau Moess"))
        assert [row["tconst"] for row in rows] == ["tt0000101"]
        row = rows[0]
        assert (row["kind"], row["year"], row["minutes"]) == ("movie", 1954,
                                                              90)
        assert sorted(row["titles"]) == ["Frau Möss", "Mother Moss",
                                         "Mère Mousse"]

    @pytest.mark.parametrize("folder", ["Frau Moss", "Frau Moess",
                                        "FRAU MÖSS", "Mere Mousse"])
    def test_accents_dropped_or_spelled_out_both_reach_it(self, lists,
                                                          folder):
        database = imdbdata.prepare(str(lists), [].append)
        assert [row["tconst"] for row in
                imdbdata.titles_under(database, _keys(folder))] == ["tt0000101"]

    def test_a_leading_article_is_not_needed(self, lists):
        database = imdbdata.prepare(str(lists), [].append)
        assert [row["tconst"] for row in
                imdbdata.titles_under(database, _keys("Harbour Kid"))] \
            == ["tt0000102"]

    def test_episodes_and_adult_titles_are_not_films(self, lists):
        database = imdbdata.prepare(str(lists), [].append)
        assert imdbdata.titles_under(database, _keys("Episode Title")) == []
        # the 1954 film only, and neither the episode nor the adult title
        assert len(imdbdata.titles_under(database, _keys("Frau Möss"))) == 1

    def test_a_quote_in_a_title_is_read_as_a_character(self, lists):
        """The lists are unquoted, and a title with a quote in it must not
        swallow the rows after it."""
        database = imdbdata.prepare(str(lists), [].append)
        rows = imdbdata.titles_under(database, _keys('Quote "Me"'))
        assert [(row["tconst"], row["minutes"]) for row in rows] \
            == [("tt0000105", None)]

    def test_a_database_newer_than_its_lists_is_not_rebuilt(self, lists,
                                                            monkeypatch):
        imdbdata.prepare(str(lists), [].append)
        built = []
        monkeypatch.setattr(imdbdata, "_build",
                            lambda *arguments: built.append(arguments))
        imdbdata.prepare(str(lists), [].append)
        assert built == []

    def test_newer_lists_are_rebuilt(self, lists, monkeypatch):
        database = imdbdata.prepare(str(lists), [].append)
        old = time.time() - 3600
        os.utime(database, (old, old))
        built = []
        monkeypatch.setattr(imdbdata, "_build",
                            lambda *arguments: built.append(arguments) or True)
        imdbdata.prepare(str(lists), [].append)
        assert len(built) == 1

    @pytest.mark.parametrize("slip", [
        "harbourkd",       # one too few
        "harbourkidd",     # one too many
        "harbourkjd",      # one wrong
        "harbuorkid",      # two swapped
        "xarbourkid",      # the first letter, which the prefilter must allow
        "harbourkix",      # the last letter
    ])
    def test_a_title_one_slip_away_is_reached(self, lists, slip):
        database = imdbdata.prepare(str(lists), [].append)
        assert [row["tconst"] for row in
                imdbdata.titles_near(database, {slip})] == ["tt0000102"]

    def test_two_slips_away_is_not(self, lists):
        database = imdbdata.prepare(str(lists), [].append)
        assert imdbdata.titles_near(database, {"harbuorkd"}) == []

    def test_a_short_key_is_not_looked_for_a_slip_away(self, lists):
        database = imdbdata.prepare(str(lists), [].append)
        assert imdbdata.titles_near(database, {"moss"}) == []

    def test_the_title_itself_is_not_a_slip_away(self, lists):
        database = imdbdata.prepare(str(lists), [].append)
        assert imdbdata.titles_near(database, {"harbourkid"}) == []

    def test_nothing_to_look_under_is_no_answer(self, lists):
        database = imdbdata.prepare(str(lists), [].append)
        assert imdbdata.titles_under(database, set()) == []
        # a key is only ever letters and digits; anything else is not asked
        assert imdbdata.titles_under(database, {"x' OR '1'='1"}) == []

    def test_an_unreadable_database_is_no_answer(self, tmp_path):
        broken = tmp_path / "broken.duckdb"
        broken.write_text("not a database")
        assert imdbdata.titles_under(str(broken), {"fraumoss"}) == []


@pytest.mark.imdb_lists
class TestTheFetch:
    def _curl(self, monkeypatch, returncode=0, writes=True):
        calls = []

        class Done:
            def __init__(self, code):
                self.returncode = code

        def run(argv, **_kwargs):
            calls.append(argv)
            if writes:
                with open(argv[argv.index("-o") + 1], "wb") as handle:
                    handle.write(b"new")
            return Done(returncode)
        monkeypatch.setattr(imdbdata.subprocess, "run", run)
        return calls

    def test_a_fresh_copy_is_not_fetched(self, tmp_path, monkeypatch):
        (tmp_path / imdbdata.BASICS).write_bytes(b"old")
        calls = self._curl(monkeypatch)
        imdbdata._fetch(str(tmp_path), imdbdata.BASICS, [].append)
        assert calls == []

    def test_a_missing_copy_is_fetched(self, tmp_path, monkeypatch):
        calls = self._curl(monkeypatch)
        imdbdata._fetch(str(tmp_path), imdbdata.BASICS, [].append)
        assert calls[0][-1] == imdbdata.BASE_URL + imdbdata.BASICS
        assert "-z" not in calls[0]
        assert (tmp_path / imdbdata.BASICS).read_bytes() == b"new"

    def test_a_stale_copy_is_fetched_only_if_newer(self, tmp_path,
                                                   monkeypatch):
        stale = tmp_path / imdbdata.BASICS
        stale.write_bytes(b"old")
        old = time.time() - (imdbdata.MAX_AGE_DAYS + 1) * 86400
        os.utime(stale, (old, old))
        calls = self._curl(monkeypatch)
        imdbdata._fetch(str(tmp_path), imdbdata.BASICS, [].append)
        assert calls[0][calls[0].index("-z") + 1] == str(stale)

    def test_a_failed_fetch_keeps_the_copy_there_was(self, tmp_path,
                                                     monkeypatch):
        stale = tmp_path / imdbdata.BASICS
        stale.write_bytes(b"old")
        old = time.time() - (imdbdata.MAX_AGE_DAYS + 1) * 86400
        os.utime(stale, (old, old))
        self._curl(monkeypatch, returncode=22, writes=False)
        logs = []
        imdbdata._fetch(str(tmp_path), imdbdata.BASICS, logs.append)
        assert stale.read_bytes() == b"old"
        assert any("could not fetch" in line for line in logs)

    def test_without_duckdb_nothing_is_fetched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(imdbdata.shutil, "which", lambda _name: None)
        calls = self._curl(monkeypatch)
        logs = []
        assert imdbdata.prepare(str(tmp_path), logs.append) == ""
        assert calls == []
        assert any("duckdb not found" in line for line in logs)


def test_the_articles_are_the_folds():
    """The build drops the same leading articles the fold does."""
    assert set(imdbdata.ARTICLES) == set(titlematch.ARTICLES)


def test_the_suite_never_prepares_the_lists(tmp_path):
    """Outside the cases that ask for it, a tagging pass is handed no lists."""
    assert imdbdata.prepare(str(tmp_path), [].append) == ""
    assert list(tmp_path.iterdir()) == []


def test_the_lists_live_under_the_checkouts_data(tmp_path):
    assert imdbdata.directory(str(tmp_path)) == str(tmp_path / "data" / "imdb")
