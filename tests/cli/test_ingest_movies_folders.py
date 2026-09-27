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


def _worklists(tmp_path):
    """-t's lists in the logs folder. The conflicts and duplicates are not
    among them: those record what any tagging run did, -i included."""
    return [path for path in (tmp_path / "script").glob("**/ingest-movies-*")
            if "-conflicts-" not in path.name
            and "-duplicates" not in path.name]


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

    def _tagging(self, monkeypatch, tmp_path, unmatched=(), ambiguous=()):
        """The tagging itself stood down: what a folder's films ARE is
        `tests/lib/test_tmdblookup.py`, and what is here is where what it could
        not name is written. The lists are the script directory's to keep, so
        a script directory is stood in for the checkout."""
        monkeypatch.setenv("tmdbApiKey", "apikey")
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path / "script"))
        monkeypatch.setattr(run.tooldeps, "require_tools",
                            lambda *_a, **_k: False)
        asked = []
        not_asked = self.not_asked = []

        def fake_tag(root, _log, skips, dry_run=False, ids=None,
                     unmatched=None, recursive=False, ambiguous=None,
                     planned=None, near_misses=None, aliases=None,
                     seen=None, skip=None, long_names=None, imdb="",
                     tagged=None):
            asked.append(root)
            self.imdb = imdb
            skips.record(root + "/Name In Use (1999)",
                         root + "/Name In Use (1999) {imdb-tt0000002}")
            name = root.rsplit("/", 1)[-1]
            if seen is not None:
                seen.add("The Movie (1999)")
                seen.update(skip or ())
            not_asked.extend(skip or ())
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
                # As the real one does: a row skipped for being blank is still
                # a row to fill in, so it stays on the list.
                unmatched += ["%s film (1999)" % name] + list(skip or ())
            if ambiguous is not None:
                ambiguous += [(root + "/Double (1999)",
                               "holds a film that is not its own",
                               ["One.mkv", "Two.mkv"])]
            if tagged is not None:
                tagged.setdefault("{imdb-tt0000009}", []).append(
                    root + "/Kept Twice (1999) {imdb-tt0000009}")
            if planned is not None and dry_run:
                planned += [(root + "/A film (1999)/A film (1999).mkv",
                             root + "/A film (1999)/A film (1999) "
                             "{imdb-tt0000001}.mkv")]
            return 0

        monkeypatch.setattr(run.tmdblookup, "tag_plex_ids", fake_tag)
        return asked

    def test_each_folder_gets_a_list_of_its_own(self, monkeypatch, tmp_path):
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        assert run.main(["-t", str(tmp_path / "Films"),
                         str(tmp_path / "Documentaries")]) == 0
        logs = tmp_path / "script" / "logs" / "ingest-movies"
        assert (logs / "ingest-movies-unmatched-Films.tsv").is_file()
        assert (logs / "ingest-movies-unmatched-Documentaries.tsv").is_file()
        assert "Films film (1999)" in (
            logs / "ingest-movies-unmatched-Films.tsv").read_text()
        assert "Documentaries film (1999)" in (
            logs / "ingest-movies-unmatched-Documentaries.tsv").read_text()

    def test_the_tagging_pass_is_handed_the_imdb_lists(self, monkeypatch,
                                                         tmp_path):
        """Prepared under the checkout's data folder, once for the run."""
        self._tagging(monkeypatch, tmp_path)
        prepared = []
        monkeypatch.setattr(run.imdbdata, "prepare",
                            lambda where, _log: prepared.append(where)
                            or "the lists")
        _library(tmp_path, "Films")
        assert run.main(["-t", str(tmp_path / "Films")]) == 0
        assert prepared == [str(tmp_path / "script" / "data" / "imdb")]
        assert self.imdb == "the lists"

    def test_and_so_does_each_folder_holding_more_than_one_film(
            self, monkeypatch, tmp_path):
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        run.main(["-t", str(tmp_path / "Films"),
                  str(tmp_path / "Documentaries")])
        logs = tmp_path / "script" / "logs" / "ingest-movies"
        assert (logs / "ingest-movies-ambiguous-Films.txt").is_file()
        assert (logs / "ingest-movies-ambiguous-Documentaries.txt").is_file()

    def test_and_a_dry_run_writes_down_what_it_would_have_renamed(
            self, monkeypatch, tmp_path):
        """A library of any size prints thousands of those lines: the file is
        where they can be read through rather than scrolled past."""
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        run.main(["-t", str(tmp_path / "Films"),
                  str(tmp_path / "Documentaries")])
        logs = tmp_path / "script" / "logs" / "ingest-movies"
        listing = logs / "ingest-movies-renames-Films.txt"
        assert listing.is_file()
        body = [line for line in listing.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        assert body == ["A film (1999)/A film (1999).mkv",
                        "    -> A film (1999)/A film (1999) {imdb-tt0000001}.mkv"]
        assert (logs / "ingest-movies-renames-Documentaries.txt").is_file()

    def test_and_how_close_the_folders_it_left_alone_came(
            self, monkeypatch, tmp_path):
        """The list that says whether the other two are the right length: a
        page of candidates that are plainly the film is a reading the matching
        does not have yet."""
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        run.main(["-t", str(tmp_path / "Films")])
        listing = (tmp_path / "script" / "logs" / "ingest-movies"
                   / "ingest-movies-nearmisses-Films.txt")
        assert listing.is_file()
        body = [line for line in listing.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        assert body == ["A film (1999)  -  no confident TMDb match",
                        '    asked "A film": 0 result(s)']

    def test_and_the_folders_held_together_by_another_of_their_titles(
            self, monkeypatch, tmp_path):
        """Nothing is renamed for those, so no other list mentions them: this
        is the only way to see the recognition working."""
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        run.main(["-t", str(tmp_path / "Films")])
        listing = (tmp_path / "script" / "logs" / "ingest-movies"
                   / "ingest-movies-othertitles-Films.txt")
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
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        run.main(["-t", "-w", str(tmp_path / "Films")])
        assert (tmp_path / "script" / "logs" / "ingest-movies"
                / "ingest-movies-othertitles-Films.txt").is_file()

    def test_but_a_real_run_leaves_no_near_miss_list(self, monkeypatch,
                                                     tmp_path):
        """It is a dry run's question. Once the renames have happened, the
        answer to "should this have matched?" is the library itself."""
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        run.main(["-t", "-w", str(tmp_path / "Films")])
        assert not (tmp_path / "script" / "logs" / "ingest-movies"
                    / "ingest-movies-nearmisses-Films.txt").exists()

    def test_but_a_real_run_writes_no_such_list(self, monkeypatch, tmp_path):
        """There is nothing to preview once it has happened, and the renames
        that DID happen are the library itself."""
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        run.main(["-t", "-w", str(tmp_path / "Films")])
        assert not (tmp_path / "script" / "logs" / "ingest-movies"
                    / "ingest-movies-renames-Films.txt").exists()

    def test_a_film_in_the_folders_of_two_disks_is_warned_about(
            self, monkeypatch, tmp_path, capsys):
        """The folders given are read as one library: the same film on two
        disks is the copy most easily forgotten."""
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        run.main(["-t", str(tmp_path / "Films"),
                  str(tmp_path / "Documentaries")])
        errors = capsys.readouterr().err
        listing = (tmp_path / "script" / "logs" / "ingest-movies"
                   / "ingest-movies-duplicates.txt")
        assert "1 film(s) are kept in more than one folder" in errors
        assert str(listing) in errors
        # The screen says how many and where; the folders are in the file.
        assert "Kept Twice" not in errors
        body = [line for line in listing.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        assert body == [
            "{imdb-tt0000009}",
            '    "%s/Documentaries/Kept Twice (1999) {imdb-tt0000009}"'
            % tmp_path,
            '    "%s/Films/Kept Twice (1999) {imdb-tt0000009}"' % tmp_path]

    def test_renames_refused_for_a_taken_name_are_listed_per_folder(
            self, monkeypatch, tmp_path, capsys):
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        run.main(["-t", str(tmp_path / "Films"),
                  str(tmp_path / "Documentaries")])
        errors = capsys.readouterr().err
        logs = tmp_path / "script" / "logs" / "ingest-movies"
        listing = logs / "ingest-movies-conflicts-Films.txt"
        assert '1 rename(s) in "%s" were held back' % (tmp_path / "Films") \
            in errors
        assert str(listing) in errors
        assert "Safety skip details" not in errors
        body = [line for line in listing.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        assert body == ["Name In Use (1999)",
                        "    -> Name In Use (1999) {imdb-tt0000002}"]
        assert (logs / "ingest-movies-conflicts-Documentaries.txt").is_file()

    def test_but_one_folder_holding_it_is_not(self, monkeypatch, tmp_path,
                                              capsys):
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        run.main(["-t", str(tmp_path / "Films")])
        assert "more than one folder" not in capsys.readouterr().err

    def test_w_on_its_own_is_refused_rather_than_ignored(self, monkeypatch,
                                                         tmp_path, capsys):
        """-w carries out the dry run of -t or -i, and without either there is
        no dry run to carry out. A run meant to be -tw and typed as -w would
        otherwise start a full ingest - hours of converting and remuxing - on a
        library it was asked to do nothing but name."""
        _library(tmp_path, "Films")
        assert run.main(["-w", str(tmp_path / "Films")]) == 1
        errors = capsys.readouterr().err
        assert "-w on its own" in errors
        assert "Nothing was changed." in errors

    def test_an_id_list_asks_for_the_tagging_and_nothing_else(
            self, monkeypatch, tmp_path):
        """The list is only ever read by the tagging phase, so naming one names
        that phase: a run given -i alone used to fall through to the full
        ingest and remux films while the list went unread."""
        self._tagging(monkeypatch, tmp_path)

        def no_ingest(*_a, **_k):
            raise AssertionError("the full ingest ran")

        monkeypatch.setattr(run.rules, "mkv_mux", no_ingest)
        listing = tmp_path / "by-hand.tsv"
        listing.write_text("The Movie (1999)\ttt0000001\n", encoding="utf-8")
        _library(tmp_path, "Films")
        assert run.main(["-i", str(listing), str(tmp_path / "Films")]) == 0

    def test_but_a_list_that_cannot_be_read_stops_the_run(self, monkeypatch,
                                                          tmp_path, capsys):
        """The whole point of -i is the afternoon someone spent looking films
        up. A path typed wrong read as "no ids" throws all of it away and puts
        the         library back through the lookups that already failed - quietly,
        which is the worst way to lose it."""
        self._tagging(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        assert run.main(["-i", str(tmp_path / "typo.tsv"),
                         str(tmp_path / "Films")]) == 1
        assert "cannot be read" in capsys.readouterr().err

    def test_and_a_row_left_blank_is_never_asked_about_again(
            self, monkeypatch, tmp_path, capsys):
        """The pass that wrote the row is the one that already failed to name
        it. Asking the same catalogue the same question is a round trip whose
        answer is known, and the count at the end is all there is to say."""
        self._tagging(monkeypatch, tmp_path)
        listing = tmp_path / "by-hand.tsv"
        listing.write_text("The Movie (1999)\ttt0000001\n"
                           "Nobody Looked This Up (1988)\t\n",
                           encoding="utf-8")
        _library(tmp_path, "Films")
        assert run.main(["-i", str(listing), str(tmp_path / "Films")]) == 0
        assert self.not_asked == ["Nobody Looked This Up (1988)"]
        out = capsys.readouterr().err
        assert "1 film(s) had no id filled in and were not asked about" in out

    def test_and_a_blank_row_survives_the_rewrite(self, monkeypatch, tmp_path):
        """It is the work still to do. A run that dropped it would hand back a
        shorter list every time until there was nothing left to fill in."""
        self._tagging(monkeypatch, tmp_path)
        listing = tmp_path / "by-hand.tsv"
        listing.write_text("Nobody Looked This Up (1988)\t\n",
                           encoding="utf-8")
        _library(tmp_path, "Films")
        assert run.main(["-i", str(listing), "-w",
                         str(tmp_path / "Films")]) == 0
        assert "Nobody Looked This Up (1988)\t" in listing.read_text(
            encoding="utf-8")

    def test_and_writes_nothing_at_all_until_w(self, monkeypatch, tmp_path):
        """The lists are -t's: -t is the pass that has only TMDb to go on and
        writes down what it could not name, and -i is the pass that reads one
        back. Rewriting them from a half-filled answer would lose the question,
        and the file being filled in is the last thing to edit underneath
        someone."""
        self._tagging(monkeypatch, tmp_path)
        listing = tmp_path / "by-hand.tsv"
        written = "The Movie (1999)\ttt0000001\n"
        listing.write_text(written, encoding="utf-8")
        _library(tmp_path, "Films")
        assert run.main(["-i", str(listing), str(tmp_path / "Films")]) == 0
        assert not _worklists(tmp_path)
        assert listing.read_text(encoding="utf-8") == written

    def test_and_w_is_what_brings_the_file_up_to_date(self, monkeypatch,
                                                      tmp_path):
        """-i and -iw stand to each other as -t and -tw do, and -w is the only
        thing that renames anything or touches the list."""
        self._tagging(monkeypatch, tmp_path)
        listing = tmp_path / "by-hand.tsv"
        listing.write_text("The Movie (1999)\ttt0000001\n", encoding="utf-8")
        _library(tmp_path, "Films")
        assert run.main(["-i", str(listing), "-w",
                         str(tmp_path / "Films")]) == 0
        rewritten = listing.read_text(encoding="utf-8")
        assert "The Movie (1999)\t{imdb-tt0000001}" in rewritten
        assert "Films film (1999)\t" in rewritten
        assert not _worklists(tmp_path)

    def test_and_a_row_naming_no_folder_here_is_an_error_it_carries_on_past(
            self, monkeypatch, tmp_path, capsys):
        """The other ids are somebody's afternoon of looking things up. One row
        that names nothing on disk is worth saying loudly and is not worth
        dropping them over."""
        self._tagging(monkeypatch, tmp_path)
        listing = tmp_path / "by-hand.tsv"
        listing.write_text("The Movie (1999)\ttt0000001\n"
                           "Not here (1901)\ttt0000002\n", encoding="utf-8")
        _library(tmp_path, "Films")
        assert run.main(["-i", str(listing), str(tmp_path / "Films")]) == 0
        errors = capsys.readouterr().err
        assert "Not here (1901)" in errors
        assert "{imdb-tt0000002}" in errors
        assert "The Movie (1999)" not in errors

    def test_but_a_row_still_blank_is_simply_one_still_to_do(
            self, monkeypatch, tmp_path, capsys):
        """The file is filled in over several sittings. A blank line is the
        work remaining, not a mistake to report."""
        self._tagging(monkeypatch, tmp_path)
        listing = tmp_path / "by-hand.tsv"
        listing.write_text("Not here (1901)\t\n", encoding="utf-8")
        _library(tmp_path, "Films")
        assert run.main(["-i", str(listing), str(tmp_path / "Films")]) == 0
        assert "ERROR" not in capsys.readouterr().err

    def test_but_one_id_list_named_by_hand_holds_them_all(self, monkeypatch,
                                                          tmp_path):
        """Someone who named a file meant that file: it is read once for every
        folder and written once at the end, so the ids in it survive."""
        self._tagging(monkeypatch, tmp_path)
        listing = tmp_path / "by-hand.tsv"
        listing.write_text("Old film (1970)\ttt0000001\n", encoding="utf-8")
        _library(tmp_path, "Films")
        _library(tmp_path, "Documentaries")
        assert run.main(["-i", str(listing), "-w", str(tmp_path / "Films"),
                         str(tmp_path / "Documentaries")]) == 0
        written = listing.read_text(encoding="utf-8")
        assert "Old film (1970)\t{imdb-tt0000001}" in written
        assert "Films film (1999)\t" in written
        assert "Documentaries film (1999)\t" in written
        assert not _worklists(tmp_path)
