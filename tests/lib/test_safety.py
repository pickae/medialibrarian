"""Tests for medialib.lib.safety - fileSafety's rename group.

What is pinned here:

  * the shapes a generated corpus has to exclude, because a case that can write
    outside its own folder can corrupt the next one;
  * the second-resolution rounding, which is only visible when you know what the
    date was before;
  * clean_input_path, which is pure text and touches no folder at all.
"""

import errno
import io
import os
import sys

import pytest

from medialib.lib import safety

pytestmark = pytest.mark.pure

# A symlink is not something an unprivileged Windows account can make, and the
# pure tier runs there.
_POSIX_LINKS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="making a symlink needs a privilege the CI account does not have")


class TestTheDirnameTheShellMeans:
    """``dirname(1)``, not ``os.path.dirname``.

    safe_rename asks dirname for the folder whose date it must restore, so an
    answer that is off by one component freshens the wrong folder - a mistake that
    leaves every file in the right place and is invisible in a listing of names.
    """

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("a.mp3", "."),
            ("full/a.mp3", "full"),
            ("a/b/c", "a/b"),
            ("/a/b", "/a"),
            ("/a", "/"),
            ("/", "/"),
            ("", "."),
            (".", "."),
            ("..", "."),
        ],
    )
    def test_it_matches_the_command(self, path, expected):
        assert safety._dirname(path) == expected

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("full/", "."),
            ("a/b/", "a"),
            ("a//b", "a"),
            ("a//", "."),
            ("//", "/"),
        ],
    )
    def test_a_trailing_slash_is_where_the_two_part_company(self, path, expected):
        assert safety._dirname(path) == expected


@pytest.mark.fs
class TestARenameKeepsEveryDate:
    def _stamp(self, path, seconds, nanoseconds=123456789):
        os.utime(path, ns=(seconds * 10**9 + nanoseconds, seconds * 10**9 + nanoseconds))

    def test_the_moved_file_keeps_its_own_date(self, tmp_path):
        source = tmp_path / "before.mp3"
        source.touch()
        self._stamp(source, 1551973337)
        assert safety.safe_rename(str(source), str(tmp_path / "after.mp3"))
        assert int((tmp_path / "after.mp3").stat().st_mtime) == 1551973337

    def test_the_date_comes_back_rounded_to_the_second(self, tmp_path):
        """bash restores through ``touch -t``, whose format has no fraction.

        Keeping the nanoseconds would be more accurate and would also be a
        divergence, so the port rounds exactly where bash rounds.
        """
        source = tmp_path / "before.mp3"
        source.touch()
        self._stamp(source, 1551973337, nanoseconds=123456789)
        assert safety.safe_rename(str(source), str(tmp_path / "after.mp3"))
        assert (tmp_path / "after.mp3").stat().st_mtime_ns % 10**9 == 0

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="on a Windows path _dirname answers '.', so the "
                               "parents' dates are never restored")
    def test_both_folders_keep_theirs(self, tmp_path):
        origin = tmp_path / "origin"
        target = tmp_path / "target"
        origin.mkdir()
        target.mkdir()
        (origin / "x.mp3").touch()
        self._stamp(origin, 1551973337)
        self._stamp(target, 1551973337)

        assert safety.safe_rename(str(origin / "x.mp3"), str(target / "x.mp3"))
        assert int(origin.stat().st_mtime) == 1551973337
        assert int(target.stat().st_mtime) == 1551973337


@pytest.mark.fs
class TestARenameThatMustNotHappen:
    def test_the_same_path_is_not_a_rename(self, tmp_path):
        same = tmp_path / "x.mp3"
        same.touch()
        log = safety.SkipLog()
        assert safety.safe_rename(str(same), str(same), log) is False
        assert same.exists()
        assert log.skips == []

    def test_an_occupied_destination_is_refused_and_recorded(self, tmp_path):
        source = tmp_path / "x.mp3"
        occupied = tmp_path / "y.mp3"
        source.touch()
        occupied.write_text("keep me")
        log = safety.SkipLog()

        assert safety.safe_rename(str(source), str(occupied), log) is False
        assert source.exists()
        assert occupied.read_text() == "keep me"
        assert log.skips == [(str(source), str(occupied))]

    def test_a_source_that_is_not_there_fails_quietly(self, tmp_path):
        log = safety.SkipLog()
        assert safety.safe_rename(str(tmp_path / "gone"), str(tmp_path / "x"), log) is False
        assert log.skips == []

    def test_a_destination_folder_that_is_not_there_fails_quietly(self, tmp_path):
        source = tmp_path / "x.mp3"
        source.touch()
        assert safety.safe_rename(str(source), str(tmp_path / "nope" / "x.mp3")) is False
        assert source.exists()


@_POSIX_LINKS
@pytest.mark.fs
class TestARenameMustNotFollowALinkOutOfTheTree:
    """A destination that is a symlink is occupied, whatever it resolves to.

    The one to watch is the BROKEN link, which os.path.exists answers False for.
    Across a filesystem boundary - every NAS and every phone mount these commands
    run over, and the reason safe_rename uses shutil.move at all - the fallback
    is a copy, and a copy opens the destination and FOLLOWS it.
    """

    def test_a_dangling_link_is_an_occupied_name(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        inside = tmp_path / "inside"
        inside.mkdir()
        source = inside / "source.mp3"
        source.write_text("payload")
        link = inside / "dangling.mp3"
        link.symlink_to(outside / "victim.mp3")  # its target does not exist
        log = safety.SkipLog()

        assert safety.safe_rename(str(source), str(link), log) is False
        assert log.skips == [(str(source), str(link))]
        assert source.read_text() == "payload"
        assert link.is_symlink()
        assert not (outside / "victim.mp3").exists()

    def test_nothing_is_written_outside_when_the_move_is_cross_device(
            self, tmp_path, monkeypatch):
        outside = tmp_path / "outside"
        outside.mkdir()
        inside = tmp_path / "inside"
        inside.mkdir()
        source = inside / "source.mp3"
        source.write_text("payload")
        link = inside / "dangling.mp3"
        link.symlink_to(outside / "victim.mp3")

        # What a NAS answers, and what sends shutil.move down its copy path.
        def cross_device(*_args, **_kwargs):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(os, "rename", cross_device)

        assert safety.safe_rename(str(source), str(link), safety.SkipLog()) is False
        assert not (outside / "victim.mp3").exists()

    def test_a_link_that_resolves_is_refused_too(self, tmp_path):
        target = tmp_path / "kept.mp3"
        target.write_text("keep me")
        source = tmp_path / "source.mp3"
        source.write_text("payload")
        link = tmp_path / "link.mp3"
        link.symlink_to(target)
        log = safety.SkipLog()

        assert safety.safe_rename(str(source), str(link), log) is False
        assert target.read_text() == "keep me"
        assert log.skips == [(str(source), str(link))]


class TestTheSkipReport:
    def test_nothing_refused_still_says_so(self):
        assert safety.SkipLog().report() == [
            "Safety: skipped 0 rename(s) to avoid overwrite"
        ]

    def test_every_refusal_is_named_in_full(self):
        log = safety.SkipLog()
        log.record("/in/a.mp3", "/out/a.mp3")
        log.record("/in/b.mp3", "/out/b.mp3")
        assert log.report() == [
            "Safety: skipped 2 rename(s) to avoid overwrite",
            "Safety skip details:",
            "  /in/a.mp3 -> /out/a.mp3",
            "  /in/b.mp3 -> /out/b.mp3",
        ]


@pytest.mark.fs
class TestKeepingBothFiles:
    def test_a_free_name_is_returned_unchanged(self, tmp_path):
        wanted = str(tmp_path / "x.mp3")
        assert safety.unique_suffix_path(wanted) == wanted

    @_POSIX_LINKS
    def test_a_name_a_dangling_link_holds_is_not_free(self, tmp_path):
        (tmp_path / "x.mp3").symlink_to(tmp_path / "gone.mp3")
        assert safety.unique_suffix_path(str(tmp_path / "x.mp3")) == str(
            tmp_path / "x (2).mp3")

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="unique_suffix_path's bare-name branch keys on "
                               "'/', so a Windows path lands in it")
    def test_the_suffix_goes_before_the_extension(self, tmp_path):
        (tmp_path / "x.mp3").touch()
        assert safety.unique_suffix_path(str(tmp_path / "x.mp3")) == str(tmp_path / "x (2).mp3")

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="unique_suffix_path's bare-name branch keys on "
                               "'/', so a Windows path lands in it")
    def test_it_counts_past_the_variants_that_exist(self, tmp_path):
        for name in ("x.mp3", "x (2).mp3", "x (3).mp3"):
            (tmp_path / name).touch()
        assert safety.unique_suffix_path(str(tmp_path / "x.mp3")) == str(tmp_path / "x (4).mp3")

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="unique_suffix_path's bare-name branch keys on "
                               "'/', so a Windows path lands in it")
    def test_no_extension_puts_the_suffix_at_the_end(self, tmp_path):
        (tmp_path / "album").touch()
        assert safety.unique_suffix_path(str(tmp_path / "album")) == str(tmp_path / "album (2)")

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="unique_suffix_path's bare-name branch keys on "
                               "'/', so a Windows path lands in it")
    def test_a_dotfile_is_all_stem(self, tmp_path):
        (tmp_path / ".hidden").touch()
        assert safety.unique_suffix_path(str(tmp_path / ".hidden")) == str(
            tmp_path / ".hidden (2)"
        )

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="unique_suffix_path's bare-name branch keys on "
                               "'/', so a Windows path lands in it")
    def test_a_folder_counts_as_taken(self, tmp_path):
        (tmp_path / "disc 1").mkdir()
        assert safety.unique_suffix_path(str(tmp_path / "disc 1")) == str(tmp_path / "disc 1 (2)")

    def test_a_bare_name_is_resolved_against_the_working_directory(self, tmp_path, monkeypatch):
        """The branch a caller reaches only by handing over a bare name: every
        other one puts a folder in front of it."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "x.mp3").touch()
        assert safety.unique_suffix_path("x.mp3") == "./x (2).mp3"


@pytest.mark.fs
class TestLowerCasingExtensions:
    def test_an_upper_case_extension_is_renamed(self, tmp_path):
        (tmp_path / "x.MP3").touch()
        safety.lower_case_ending(str(tmp_path / "x.MP3"))
        assert (tmp_path / "x.mp3").exists()

    def test_the_stem_is_left_alone(self, tmp_path):
        (tmp_path / "THE END.JPG").touch()
        safety.lower_case_ending(str(tmp_path / "THE END.JPG"))
        assert (tmp_path / "THE END.jpg").exists()

    def test_a_dotfile_has_no_extension_to_lower(self, tmp_path):
        (tmp_path / ".HIDDEN").touch()
        safety.lower_case_ending(str(tmp_path / ".HIDDEN"))
        assert (tmp_path / ".HIDDEN").exists()

    def test_a_collision_is_refused_rather_than_resolved(self, tmp_path):
        (tmp_path / "x.MP3").write_text("newcomer")
        (tmp_path / "x.mp3").write_text("already here")
        log = safety.SkipLog()

        safety.lower_case_ending(str(tmp_path / "x.MP3"), log)
        assert (tmp_path / "x.mp3").read_text() == "already here"
        assert (tmp_path / "x.MP3").exists()
        assert len(log.skips) == 1

    def test_the_whole_tree_at_any_depth(self, tmp_path):
        (tmp_path / "sub" / "deeper").mkdir(parents=True)
        (tmp_path / "a.MP3").touch()
        (tmp_path / "sub" / "b.JPG").touch()
        (tmp_path / "sub" / "deeper" / "c.PNG").touch()

        safety.lower_case_extensions(str(tmp_path))
        assert (tmp_path / "a.mp3").exists()
        assert (tmp_path / "sub" / "b.jpg").exists()
        assert (tmp_path / "sub" / "deeper" / "c.png").exists()

    def test_a_path_that_is_not_a_folder_is_not_an_error(self, tmp_path):
        (tmp_path / "x.MP3").touch()
        safety.lower_case_extensions(str(tmp_path / "x.MP3"))
        assert (tmp_path / "x.MP3").exists()

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="needs a case-sensitive filesystem; on Windows "
                               "x.MP3 and x.Mp3 are one file")
    def test_which_of_two_colliding_names_wins_does_not_depend_on_the_filesystem(
        self, tmp_path
    ):
        """``find -type f`` returns readdir order, so nothing but a defined sort
        keeps this from being decided by whatever the filesystem hands over first.
        Byte order, both sides."""
        (tmp_path / "x.MP3").touch()
        (tmp_path / "x.Mp3").touch()
        log = safety.SkipLog()

        safety.lower_case_extensions(str(tmp_path), log)
        assert (tmp_path / "x.mp3").exists()
        assert (tmp_path / "x.Mp3").exists()
        assert not (tmp_path / "x.MP3").exists()

    def test_the_dotted_capital_i_follows_the_shell(self, tmp_path):
        """U+0130 is the one codepoint where ``${x,,}`` and ``str.lower`` differ,
        and an extension is exactly where a filename can carry one."""
        (tmp_path / "Şarkı.MÜZİK").touch()
        safety.lower_case_ending(str(tmp_path / "Şarkı.MÜZİK"))
        assert (tmp_path / "Şarkı.müzik").exists()

    @_POSIX_LINKS
    def test_a_symlink_is_not_a_file_to_walk(self, tmp_path):
        (tmp_path / "real.mp3").touch()
        (tmp_path / "link.MP3").symlink_to(tmp_path / "real.mp3")

        safety.lower_case_extensions(str(tmp_path))
        assert (tmp_path / "link.MP3").is_symlink()


@pytest.mark.fs
class TestAnEmptyFolder:
    def test_nothing_in_it(self, tmp_path):
        assert safety.is_empty_folder(str(tmp_path))

    def test_a_hidden_file_is_still_something(self, tmp_path):
        (tmp_path / ".stray").touch()
        assert not safety.is_empty_folder(str(tmp_path))

    def test_an_empty_sub_folder_is_still_something(self, tmp_path):
        (tmp_path / "sub").mkdir()
        assert not safety.is_empty_folder(str(tmp_path))

    def test_a_file_is_not_an_empty_folder(self, tmp_path):
        (tmp_path / "x").touch()
        assert not safety.is_empty_folder(str(tmp_path / "x"))

    def test_nor_is_a_path_that_is_not_there(self, tmp_path):
        assert not safety.is_empty_folder(str(tmp_path / "gone"))


class TestCleaningAPath:
    """A closed list. Everything on it goes, and everything else stays: the caller
    renames the file to whatever comes back."""

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("a_b_c", "a b c"),
            ("don't stop", "dont stop"),
            ("Hey!", "Hey"),
            ("back`tick`", "backtick"),
            ("  padded  ", "padded"),
            ("double  space", "double space"),
            ("tab\there", "tab here"),
            ("line\nbreak", "line break"),
            ("/srv/media/Album 1", "/srv/media/Album 1"),
        ],
    )
    def test_the_list(self, given, expected):
        assert safety.clean_input_path(given) == expected

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("/srv/My_Books/A_Book", "/srv/My_Books/A Book"),
            ("/srv/My_Books/Series", "/srv/My_Books/Series"),
            ("/a b/c'd/e!f", "/a b/c'd/ef"),
            ("bare_name", "bare name"),
        ],
    )
    @pytest.mark.skipif(sys.platform == "win32",
                        reason="the test data is a POSIX path, which on Windows "
                               "holds no os.sep and reads as a bare name")
    def test_only_the_last_component_is_cleaned(self, given, expected):
        """The parents are where the input lives, not something this run may
        rename. Cleaning them named a destination directory that was not there,
        which `shutil.move` then created - copying the tree out of the input and
        deleting the original."""
        assert safety.clean_input_path(given) == expected

    @pytest.mark.parametrize(
        "given",
        ['The "Best" Of', "AC\\DC", "Album [2019] (Remaster)", "R&D $100 100%", "naïve"],
    )
    def test_what_is_not_on_the_list_survives(self, given):
        assert safety.clean_input_path(given) == given

    def test_a_name_made_only_of_removals_cleans_away_to_nothing(self):
        assert safety.clean_input_path("'!`") == ""

    @pytest.mark.parametrize("blank", ["\r", "\x0b", "\x0c", " ", " "])
    def test_only_the_shell_s_three_blanks_split_a_word(self, blank):
        """``str.split()`` would also split on these, and the result is a rename.

        The shell splits on space, tab and newline and nothing else, so a name with
        a non-breaking space in it keeps it rather than losing it to a plain one.
        """
        assert safety.clean_input_path(f"a{blank}b") == f"a{blank}b"


class TestRunSkipLog:
    """The run-level log is a FILE, because the report is read back from it and
    phases that are still bash append to the same one."""

    def test_a_refused_rename_reaches_the_file(self, tmp_path, monkeypatch,
                                               capsys):
        monkeypatch.setenv("SAFETY_LOG", str(tmp_path / "skips.log"))
        safety.init_safety_log(str(tmp_path / "skips.log"))
        (tmp_path / "a").write_text("one")
        (tmp_path / "b").write_text("two")
        log = safety.RunSkipLog()
        assert safety.safe_rename(str(tmp_path / "a"), str(tmp_path / "b"),
                                  log) is False
        safety.report_safety_skips()
        report = capsys.readouterr().err
        assert "Safety: skipped 1 rename(s) to avoid overwrite" in report
        assert str(tmp_path / "a") + " -> " + str(tmp_path / "b") in report

    def test_it_still_answers_in_memory(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SAFETY_LOG", raising=False)
        log = safety.RunSkipLog()
        log.record("x", "y")
        assert log.skips == [("x", "y")]

    def test_an_unwritable_log_is_swallowed(self, tmp_path, monkeypatch):
        """It runs inside a rename, where raising would end a run over a report."""
        monkeypatch.setenv("SAFETY_LOG", str(tmp_path / "gone" / "skips.log"))
        safety.RunSkipLog().record("x", "y")


# --- the interrupt, and the refusal an unusable input gets ---------------------

class TestAbortFlag:
    def test_a_run_that_has_not_been_interrupted_says_so(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.delenv("ABORT_FLAG", raising=False)
        flag = safety.init_abort_flag(str(tmp_path / "abortRequested"))
        assert flag == str(tmp_path / "abortRequested")
        assert safety.abort_requested() is False

    def test_the_flag_is_a_file_the_whole_run_can_see(self, tmp_path,
                                                     monkeypatch):
        monkeypatch.delenv("ABORT_FLAG", raising=False)
        safety.init_abort_flag(str(tmp_path / "abortRequested"))
        safety.request_abort()
        assert safety.abort_requested() is True
        assert (tmp_path / "abortRequested").exists()

    def test_an_inherited_flag_wins_over_the_path_offered(self, tmp_path,
                                                          monkeypatch):
        """A wrapper that has already started a run owns the flag: one Ctrl+C
        has to stop every layer, and a private flag per layer would not."""
        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "outer"))
        assert safety.init_abort_flag(str(tmp_path / "inner")) == str(
            tmp_path / "outer")
        safety.request_abort()
        assert (tmp_path / "outer").exists()
        assert not (tmp_path / "inner").exists()

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="the assertion joins the expected prefix with a '/'")
    def test_without_a_path_it_draws_one_under_tmpdir(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.delenv("ABORT_FLAG", raising=False)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        flag = safety.init_abort_flag()
        assert flag.startswith(str(tmp_path) + "/abortRequested.")
        # drawn, not created: the flag's existence is the interrupt
        assert not os.path.exists(flag)

    def test_the_layer_that_made_the_flag_removes_it(self, tmp_path,
                                                     monkeypatch):
        monkeypatch.delenv("ABORT_FLAG", raising=False)
        monkeypatch.delenv("ABORT_FLAG_OWNER", raising=False)
        flag = safety.init_abort_flag(str(tmp_path / "abortRequested"))
        safety.request_abort()
        safety.release_abort_flag()
        assert not os.path.exists(flag)

    def test_an_inherited_flag_is_left_alone(self, tmp_path, monkeypatch):
        """The wrapper above is still using it: a phase that removed the flag on
        its way out would report the run as never interrupted, and the wrapper
        would carry straight on into its next phase."""
        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "outer"))
        monkeypatch.setenv("ABORT_FLAG_OWNER", str(os.getpid() + 1))
        safety.init_abort_flag()
        safety.request_abort()
        safety.release_abort_flag()
        assert (tmp_path / "outer").exists()

    def test_releasing_a_flag_that_is_already_gone_is_harmless(self, tmp_path,
                                                               monkeypatch):
        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "never-made"))
        monkeypatch.setenv("ABORT_FLAG_OWNER", str(os.getpid()))
        safety.release_abort_flag()

    def test_requesting_an_abort_with_no_flag_is_harmless(self, monkeypatch):
        """It runs inside a signal handler, where raising would take down the
        run it exists to end tidily."""
        monkeypatch.delenv("ABORT_FLAG", raising=False)
        safety.request_abort()
        assert safety.abort_requested() is False

    def test_an_unwritable_flag_is_swallowed_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "gone" / "flag"))
        safety.request_abort()
        assert safety.abort_requested() is False


class TestFailNoRelevantInput:
    def test_an_empty_folder_is_told_it_is_empty(self, tmp_path):
        out = io.StringIO()
        status = safety.fail_no_relevant_input(str(tmp_path), "audio files",
                                               stream=out)
        assert status == 1
        assert out.getvalue() == (
            '\nNothing to do: "%s" is empty.\n'
            "Expected it to hold audio files.\n"
            "Nothing was changed.\n" % tmp_path)

    def test_a_full_folder_is_told_what_is_missing_from_it(self, tmp_path):
        (tmp_path / "notes.doc").write_text("x")
        out = io.StringIO()
        status = safety.fail_no_relevant_input(str(tmp_path), "audio files",
                                               stream=out)
        assert status == 1
        assert out.getvalue() == (
            '\nNothing to do: no audio files found in "%s".\n'
            "The folder is not empty, but holds nothing this script can work "
            "with.\nNothing was changed.\n" % tmp_path)

    def test_a_path_that_is_not_a_folder_reads_as_the_full_case(self, tmp_path):
        """is_empty_folder answers False for a file, so the sentence is the one
        about a folder holding nothing usable - the same as the shell's."""
        movie = tmp_path / "film.mkv"
        movie.write_text("x")
        out = io.StringIO()
        safety.fail_no_relevant_input(str(movie), "audio files", stream=out)
        assert "is not empty" in out.getvalue()


class TestTheRunFooter:
    def test_the_report_prints_once(self, monkeypatch):
        monkeypatch.delenv("ABORT_FLAG", raising=False)
        printed = []
        safety.set_run_footer(lambda: printed.append(1))
        safety.print_run_footer()
        safety.print_run_footer()
        assert printed == [1]

    def test_a_report_that_raises_is_still_best_effort(self, monkeypatch):
        """A failing line in the report - a counter a phase never wrote - must
        not become another thing that can end the run."""
        def broken():
            raise ValueError("a counter that was never written")
        safety.set_run_footer(broken)
        safety.print_run_footer()          # does not raise

    def test_a_process_that_does_not_own_it_prints_nothing(self, monkeypatch):
        printed = []
        safety.set_run_footer(lambda: printed.append(1))
        monkeypatch.setattr(safety.os, "getpid", lambda: -1)
        safety.print_run_footer()
        assert printed == []

    def test_with_no_report_named_it_is_a_no_op(self):
        safety.set_run_footer(None)
        safety.print_run_footer()


class TestExitIfAborted:
    def test_an_uninterrupted_run_carries_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "flag"))
        safety.set_run_footer(None)
        safety.exit_if_aborted()

    def test_an_interrupted_run_stops_with_the_repo_s_status(self, tmp_path,
                                                             monkeypatch):
        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "flag"))
        safety.request_abort()
        printed = []
        safety.set_run_footer(lambda: printed.append(1))
        with pytest.raises(SystemExit) as raised:
            safety.exit_if_aborted()
        assert raised.value.code == safety.INTERRUPTED_EXIT_STATUS
        assert printed == [1]


class TestWorkerAbort:
    def test_a_worker_dispatched_after_the_interrupt_is_a_no_op(self, tmp_path,
                                                                monkeypatch):
        """It stops the QUEUE rather than failing its own item."""
        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "flag"))
        safety.request_abort()
        with pytest.raises(SystemExit) as raised:
            safety.trap_worker_abort()
        assert raised.value.code == safety.XARGS_STOP_EXIT_STATUS

    def test_a_worker_in_an_uninterrupted_run_carries_on(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "flag"))
        safety.trap_worker_abort()


class TestTheOutputMustNotSitInsideTheInput:
    """What these scripts write to the output is what they look for in the
    input, so a later run would convert its own output - and the cleanup of the
    input tree would reach into the finished one."""

    @pytest.mark.parametrize("output", [
        "{input}", "{input}/out", "{input}/deep/deeper", "{input}/",
        # taken at face value this leads out of the tree and back in again
        "{work}/other/../lib/out",
    ])
    def test_inside(self, tmp_path, output):
        source = tmp_path / "lib"
        source.mkdir()
        assert safety.output_inside_input(
            str(source), output.format(input=source, work=tmp_path))

    @pytest.mark.parametrize("output", [
        "{work}/other",
        # a sibling whose name merely STARTS with the input's
        "{work}/libout",
        # the parent: an input inside an output is allowed
        "{work}",
        # judged on the path, not on what exists
        "{work}/notyet/out",
    ])
    def test_beside(self, tmp_path, output):
        source = tmp_path / "lib"
        source.mkdir()
        assert not safety.output_inside_input(
            str(source), output.format(input=source, work=tmp_path))

    @_POSIX_LINKS
    def test_an_output_that_is_a_link_into_the_input_is_caught(self, tmp_path):
        """A lexical comparison calls these two separate folders; the
        filesystem calls them one."""
        source = tmp_path / "lib"
        (source / "inner").mkdir(parents=True)
        disguised = tmp_path / "output"
        disguised.symlink_to(source / "inner")
        assert safety.output_inside_input(str(source), str(disguised))

    def test_a_separate_output_passes_silently(self, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()
        said = io.StringIO()
        assert safety.require_separate_output(
            str(source), str(tmp_path / "other"), said) == 0
        assert said.getvalue() == ""

    def test_a_nested_one_is_refused_naming_both_folders(self, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()
        output = source / "out"
        said = io.StringIO()
        assert safety.require_separate_output(str(source), str(output),
                                              said) == 1
        text = said.getvalue()
        assert "Refusing to write the output inside the input" in text
        assert str(source) in text and str(output) in text
        assert "Nothing was changed." in text


@pytest.mark.fs
class TestIsWithin:
    """One answer to "is this path inside that tree?"."""

    def test_a_child_is_inside(self, tmp_path):
        assert safety.is_within(str(tmp_path), str(tmp_path / "a" / "b"))

    def test_the_tree_itself_is_inside(self, tmp_path):
        assert safety.is_within(str(tmp_path), str(tmp_path))

    def test_a_sibling_whose_name_starts_the_same_is_not(self, tmp_path):
        """The boundary a bare startswith has no way to see."""
        root = tmp_path / "music"
        root.mkdir()
        (tmp_path / "musicExtra").mkdir()
        assert not safety.is_within(str(root), str(tmp_path / "musicExtra" / "a"))

    def test_a_parent_is_not(self, tmp_path):
        assert not safety.is_within(str(tmp_path / "a"), str(tmp_path))

    def test_it_judges_a_path_that_does_not_exist_yet(self, tmp_path):
        assert safety.is_within(str(tmp_path), str(tmp_path / "notyet" / "x"))

    @_POSIX_LINKS
    def test_a_link_is_judged_by_where_it_lands(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "root"
        root.mkdir()
        (root / "escape").symlink_to(outside)
        assert not safety.is_within(str(root), str(root / "escape" / "x"))


@pytest.mark.fs
class TestAssertWithin:
    def test_a_destination_inside_comes_straight_back(self, tmp_path):
        wanted = str(tmp_path / "a")
        assert safety.assert_within(str(tmp_path), wanted) == wanted

    def test_one_outside_stops_the_run_naming_both(self, tmp_path):
        with pytest.raises(safety.OutsideTheRun) as raised:
            safety.assert_within(str(tmp_path / "root"),
                                 str(tmp_path / "elsewhere" / "x"), "output")
        text = str(raised.value)
        assert "output is outside the tree this run was given" in text
        assert str(tmp_path / "root") in text
        assert str(tmp_path / "elsewhere" / "x") in text
