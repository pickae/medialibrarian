"""Several movie folders in one run, worked through one at a time.

What is pinned here is what only shows up once there is more than one: that each
folder is ingested in full before the next is started and in the order it was
typed, that the lists `-t` leaves behind are one file per folder named after it
rather than one file the last folder overwrites, and that the two ways a set of
folders is not a set - the same one twice, and two of the same name - are
settled before any of them is touched.
"""

from __future__ import annotations

import pytest

from medialib.cli import ingest_movies_run as run
from medialib.cli.ingest_movies import spec

pytestmark = pytest.mark.fs


def _library(root, name: str, film: str = "The Movie (1999)"):
    """A folder holding one film, which is what makes it worth ingesting."""
    folder = root / name / film
    folder.mkdir(parents=True)
    (folder / (film + ".mkv")).touch()
    return root / name


class TestTheFoldersGiven:
    """Resolved and named before any of them is worked on, so a mistake in the
    third of five is found now rather than after the first two are done."""

    def test_each_folder_is_a_folder_and_a_name(self, tmp_path):
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        roots, names = run._resolve_roots(
            spec("ingest-movies"),
            [str(tmp_path / "Films"), str(tmp_path / "Documentaries")])
        assert names == ["Films", "Documentaries"]
        assert roots == [str(tmp_path / "Films"),
                         str(tmp_path / "Documentaries")]

    def test_the_same_folder_twice_is_one_folder(self, tmp_path, capsys):
        _library(tmp_path, "Films")
        roots, names = run._resolve_roots(
            spec("ingest-movies"),
            [str(tmp_path / "Films"), str(tmp_path / "Films") + "/"])
        assert names == ["Films"]
        assert roots == [str(tmp_path / "Films")]

    def test_two_folders_of_one_name_are_refused(self, tmp_path, capsys):
        """Their lists would be written to one path and the second would
        silently replace the first."""
        _library(tmp_path / "a", "Films")
        _library(tmp_path / "b", "Films")
        roots, names = run._resolve_roots(
            spec("ingest-movies"),
            [str(tmp_path / "a/Films"), str(tmp_path / "b/Films")])
        assert (roots, names) == (None, None)
        error = capsys.readouterr().err
        assert 'both named "Films"' in error
        assert "Nothing was changed." in error

    def test_a_folder_that_is_not_there_is_refused(self, tmp_path, capsys):
        _library(tmp_path, "Films")
        roots, _names = run._resolve_roots(
            spec("ingest-movies"),
            [str(tmp_path / "Films"), str(tmp_path / "nope")])
        assert roots is None
        assert 'Directory "%s" does not exist.' % (tmp_path / "nope") \
            in capsys.readouterr().err


class TestTheFullRunWorksThroughThemInTurn:
    def _stubbed(self, monkeypatch, tmp_path):
        """Everything a full run sets up around the ingest itself, stood down:
        what the cases below are about is which folders reach `_ingest` and in
        what order."""
        scratch = tmp_path / "ram"
        scratch.mkdir()
        monkeypatch.setattr(run.ffmpegselect, "select_ffmpeg", lambda: None)
        monkeypatch.setattr(run.ffmpegselect, "report_ffmpeg_selection",
                            lambda: None)
        monkeypatch.setattr(run.tooldeps, "require_tools",
                            lambda *_a, **_k: False)
        monkeypatch.setattr(run, "_settle_subtitle_work", lambda: False)
        monkeypatch.setattr(run, "_settle_ffsubsync_quality", lambda: "")
        monkeypatch.setattr(run.ramscratch, "init_ram_base", lambda: None)
        monkeypatch.setattr(run.ramscratch, "ram_scratch_dir",
                            lambda _name: (str(scratch), 0))
        monkeypatch.setattr(run.ramscratch, "add_exit_cleanup", lambda _p: None)
        monkeypatch.setattr(run.ramscratch, "run_exit_cleanup", lambda: None)
        monkeypatch.setattr(run.safety, "trap_run_abort", lambda: None)
        monkeypatch.setattr(run.workerpool, "exit_status", lambda status: status)
        ingested = []
        monkeypatch.setattr(run, "_ingest",
                            lambda _state, root, _subs: ingested.append(root))
        return ingested

    def test_each_folder_is_ingested_in_the_order_it_was_typed(
            self, monkeypatch, tmp_path):
        ingested = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        assert run.main([str(tmp_path / "Films"),
                         str(tmp_path / "Documentaries")]) == 0
        assert ingested == [str(tmp_path / "Films"),
                            str(tmp_path / "Documentaries")]

    def test_a_folder_with_nothing_to_ingest_is_skipped_not_the_run(
            self, monkeypatch, tmp_path, capsys):
        """The other folders were named in the same breath and are not
        answerable for it."""
        ingested = self._stubbed(monkeypatch, tmp_path)
        empty = tmp_path / "Empty"
        empty.mkdir()
        _library(tmp_path, "Films")
        assert run.main([str(empty), str(tmp_path / "Films")]) == 0
        assert ingested == [str(tmp_path / "Films")]
        assert "Nothing this ingest can read" in capsys.readouterr().err

    def test_and_one_folder_on_its_own_still_gets_the_whole_refusal(
            self, monkeypatch, tmp_path, capsys):
        ingested = self._stubbed(monkeypatch, tmp_path)
        empty = tmp_path / "Empty"
        empty.mkdir()
        assert run.main([str(empty)]) == 1
        assert ingested == []
        assert "Nothing to do" in capsys.readouterr().err


class TestTheListsEachFolderLeaves:
    """One file per folder, named after it: two libraries' unnamed films in one
    file would be a worklist nobody could tell apart, and each folder
    overwriting the last one's would be worse."""

    def _tagging(self, monkeypatch, unmatched=(), ambiguous=()):
        """The tagging itself stood down: what a folder's films ARE is
        `tests/lib/test_tmdblookup.py`, and what is here is where what it could
        not name is written."""
        monkeypatch.setenv("tmdbApiKey", "apikey")
        monkeypatch.setattr(run.tooldeps, "require_tools",
                            lambda *_a, **_k: False)
        asked = []

        def fake_tag(root, _log, _skips, dry_run=False, ids=None,
                     unmatched=None, recursive=False, ambiguous=None,
                     planned=None, near_misses=None, aliases=None):
            asked.append(root)
            name = root.rsplit("/", 1)[-1]
            if aliases is not None:
                aliases += [(root + "/Il buono (1966)", "Il buono (1966)",
                             "{imdb-tt0060196}",
                             {"The Good the Bad and the Ugly (1966).mkv":
                              "the good the bad and the ugly"})]
            if near_misses is not None:
                near_misses += [(root + "/A film (1999)",
                                 "no confident TMDb match",
                                 ['asked "A film": 0 result(s)'])]
            if unmatched is not None:
                unmatched += ["%s film (1999)" % name]
            if ambiguous is not None:
                ambiguous += [(root + "/Double (1999)",
                               "holds a film that is not its own",
                               ["One.mkv", "Two.mkv"])]
            if planned is not None and dry_run:
                planned += [(root + "/A film (1999)/A film (1999).mkv",
                             root + "/A film (1999)/A film (1999) "
                             "{imdb-tt0000001}.mkv")]
            return 0

        monkeypatch.setattr(run.tmdblookup, "tag_plex_ids", fake_tag)
        return asked

    def test_each_folder_gets_a_list_of_its_own(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        self._tagging(monkeypatch)
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        assert run.main(["-t", str(tmp_path / "Films"),
                         str(tmp_path / "Documentaries")]) == 0
        assert (tmp_path / "ingest-movies-unmatched-Films.tsv").is_file()
        assert (tmp_path / "ingest-movies-unmatched-Documentaries.tsv").is_file()
        assert "Films film (1999)" in (
            tmp_path / "ingest-movies-unmatched-Films.tsv").read_text()
        assert "Documentaries film (1999)" in (
            tmp_path / "ingest-movies-unmatched-Documentaries.tsv").read_text()

    def test_and_so_does_each_folder_holding_more_than_one_film(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        self._tagging(monkeypatch)
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        run.main(["-t", str(tmp_path / "Films"),
                  str(tmp_path / "Documentaries")])
        assert (tmp_path / "ingest-movies-ambiguous-Films.txt").is_file()
        assert (tmp_path / "ingest-movies-ambiguous-Documentaries.txt").is_file()

    def test_and_a_dry_run_writes_down_what_it_would_have_renamed(
            self, monkeypatch, tmp_path):
        """A library of any size prints thousands of those lines: the file is
        where they can be read through rather than scrolled past."""
        monkeypatch.chdir(tmp_path)
        self._tagging(monkeypatch)
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        run.main(["-t", str(tmp_path / "Films"),
                  str(tmp_path / "Documentaries")])
        listing = tmp_path / "ingest-movies-renames-Films.txt"
        assert listing.is_file()
        body = [line for line in listing.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        assert body == ["A film (1999)/A film (1999).mkv",
                        "    -> A film (1999)/A film (1999) {imdb-tt0000001}.mkv"]
        assert (tmp_path / "ingest-movies-renames-Documentaries.txt").is_file()

    def test_and_how_close_the_folders_it_left_alone_came(
            self, monkeypatch, tmp_path):
        """The list that says whether the other two are the right length: a
        page of candidates that are plainly the film is a reading the matching
        does not have yet."""
        monkeypatch.chdir(tmp_path)
        self._tagging(monkeypatch)
        _library(tmp_path, "Films")
        run.main(["-t", str(tmp_path / "Films")])
        listing = tmp_path / "ingest-movies-nearmisses-Films.txt"
        assert listing.is_file()
        body = [line for line in listing.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        assert body == ["A film (1999)  -  no confident TMDb match",
                        '    asked "A film": 0 result(s)']

    def test_and_the_folders_held_together_by_another_of_their_titles(
            self, monkeypatch, tmp_path):
        """Nothing is renamed for those, so no other list mentions them: this
        is the only way to see the recognition working."""
        monkeypatch.chdir(tmp_path)
        self._tagging(monkeypatch)
        _library(tmp_path, "Films")
        run.main(["-t", str(tmp_path / "Films")])
        listing = tmp_path / "ingest-movies-othertitles-Films.txt"
        assert listing.is_file()
        body = [line for line in listing.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        assert body == ["Il buono (1966)  -  {imdb-tt0060196}",
                        "    matched as: Il buono (1966)",
                        '    "The Good the Bad and the Ugly (1966).mkv"',
                        "        is a title TMDb holds it under: "
                        "the good the bad and the ugly"]

    def test_and_a_real_run_leaves_it_too(self, monkeypatch, tmp_path):
        """Unlike the near-miss list: this one records what HAPPENED, and is
        worth having after the renaming as much as before it."""
        monkeypatch.chdir(tmp_path)
        self._tagging(monkeypatch)
        _library(tmp_path, "Films")
        run.main(["-t", "-w", str(tmp_path / "Films")])
        assert (tmp_path / "ingest-movies-othertitles-Films.txt").is_file()

    def test_but_a_real_run_leaves_no_near_miss_list(self, monkeypatch,
                                                     tmp_path):
        """It is a dry run's question. Once the renames have happened, the
        answer to "should this have matched?" is the library itself."""
        monkeypatch.chdir(tmp_path)
        self._tagging(monkeypatch)
        _library(tmp_path, "Films")
        run.main(["-t", "-w", str(tmp_path / "Films")])
        assert not (tmp_path / "ingest-movies-nearmisses-Films.txt").exists()

    def test_but_a_real_run_writes_no_such_list(self, monkeypatch, tmp_path):
        """There is nothing to preview once it has happened, and the renames
        that DID happen are the library itself."""
        monkeypatch.chdir(tmp_path)
        self._tagging(monkeypatch)
        _library(tmp_path, "Films")
        run.main(["-t", "-w", str(tmp_path / "Films")])
        assert not (tmp_path / "ingest-movies-renames-Films.txt").exists()

    def test_but_one_id_list_named_by_hand_holds_them_all(self, monkeypatch,
                                                          tmp_path):
        """Someone who named a file meant that file: it is read once for every
        folder and written once at the end, so the ids in it survive."""
        monkeypatch.chdir(tmp_path)
        self._tagging(monkeypatch)
        listing = tmp_path / "by-hand.tsv"
        listing.write_text("Old film (1970)\ttt0000001\n", encoding="utf-8")
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        assert run.main(["-t", "-i", str(listing), str(tmp_path / "Films"),
                         str(tmp_path / "Documentaries")]) == 0
        written = listing.read_text(encoding="utf-8")
        assert "Old film (1970)\t{imdb-tt0000001}" in written
        assert "Films film (1999)\t" in written
        assert "Documentaries film (1999)\t" in written
        assert not list(tmp_path.glob("ingest-movies-unmatched-*.tsv"))
