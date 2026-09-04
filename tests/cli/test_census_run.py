"""The white box for the census run's resolution half.

What is pinned here is what the shell decides before it reads a single file:
which folders are libraries, what each is reported as, where its reports go, and
which of those questions is a refusal. The refusal texts are compared as whole
strings on purpose - they are the script's user interface, and the bash suite
compares the same words from the other side.
"""

import os

import pytest

from medialib.cli import census_run

pytestmark = pytest.mark.fs


def _tree(root, *paths):
    for path in paths:
        full = root / path
        if path.endswith("/"):
            full.mkdir(parents=True, exist_ok=True)
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("x")
    return root


class TestResolveInputPaths:
    def test_a_folder_becomes_an_absolute_path_and_a_capitalised_name(
            self, tmp_path):
        _tree(tmp_path, "films/")
        paths, names = census_run.resolve_input_paths([str(tmp_path / "films")])
        assert paths == [os.path.realpath(tmp_path / "films")]
        assert names == ["Films"]

    def test_the_same_folder_named_twice_is_one_library(self, tmp_path):
        _tree(tmp_path, "films/")
        given = str(tmp_path / "films")
        paths, names = census_run.resolve_input_paths([given, given + "/"])
        assert len(paths) == 1

    def test_a_path_that_is_not_a_folder_is_refused(self, tmp_path):
        with pytest.raises(census_run.Refusal) as raised:
            census_run.resolve_input_paths([str(tmp_path / "gone")])
        assert raised.value.text == (
            '\nError: "%s" is not a folder.\nNothing was changed.\n'
            % (tmp_path / "gone"))

    def test_two_folders_of_one_name_are_refused_naming_both(self, tmp_path):
        _tree(tmp_path, "a/Films/", "b/Films/")
        with pytest.raises(census_run.Refusal) as raised:
            census_run.resolve_input_paths([str(tmp_path / "a" / "Films"),
                                            str(tmp_path / "b" / "Films")])
        text = raised.value.text
        assert text.startswith(
            '\nError: two of the folders given are both named "Films":\n')
        assert str(tmp_path / "a" / "Films") in text
        assert str(tmp_path / "b" / "Films") in text
        assert text.endswith("Nothing was changed.\n")

    def test_nothing_left_after_the_duplicates_is_a_refusal(self, tmp_path):
        with pytest.raises(census_run.Refusal) as raised:
            census_run.resolve_input_paths([])
        assert raised.value.text == (
            "\nError: no folder to census.\nNothing was changed.\n")


class TestResolveLibraries:
    def test_without_depth_the_paths_given_are_the_libraries(self, tmp_path):
        _tree(tmp_path, "films/")
        paths, names = census_run.resolve_input_paths([str(tmp_path / "films")])
        lib_paths, lib_names, lib_roots = census_run.resolve_libraries(
            paths, names, 0)
        assert (lib_paths, lib_names, lib_roots) == (paths, ["Films"], [0])

    def test_depth_one_makes_every_subfolder_a_library(self, tmp_path):
        _tree(tmp_path, "media/Films/", "media/Music/", "media/loose.mp3")
        paths, names = census_run.resolve_input_paths([str(tmp_path / "media")])
        lib_paths, lib_names, _ = census_run.resolve_libraries(paths, names, 1)
        assert lib_names == ["MediaFilms", "MediaMusic"]
        assert lib_paths == [str(tmp_path / "media" / "Films"),
                             str(tmp_path / "media" / "Music")]

    def test_the_name_carries_every_step_down_capitalised(self, tmp_path):
        _tree(tmp_path, "media/films/marvel/")
        paths, names = census_run.resolve_input_paths([str(tmp_path / "media")])
        _, lib_names, _ = census_run.resolve_libraries(paths, names, 2)
        assert lib_names == ["MediaFilmsMarvel"]

    def test_a_folder_reached_twice_is_one_library(self, tmp_path):
        """Nested paths given on one command line can reach the same folder."""
        _tree(tmp_path, "media/Films/")
        paths, names = census_run.resolve_input_paths(
            [str(tmp_path / "media"), str(tmp_path / "media" / "Films")])
        lib_paths, _, _ = census_run.resolve_libraries(paths, names, 0)
        assert len(lib_paths) == 2      # both were named, and both are libraries

    def test_nothing_that_deep_anywhere_is_a_refusal(self, tmp_path):
        _tree(tmp_path, "media/Films/")
        paths, names = census_run.resolve_input_paths([str(tmp_path / "media")])
        with pytest.raises(census_run.Refusal) as raised:
            census_run.resolve_libraries(paths, names, 3)
        assert raised.value.text == (
            "\nError: -d 3 was asked for, and none of the 1 folder(s) given "
            "holds a\nfolder that many levels down, so there is no library to "
            "census.\nNothing was changed.\n")

    def test_two_libraries_that_would_report_the_same_are_refused(
            self, tmp_path):
        """"Films" holding "ExtraX" against "FilmsExtra" holding "X" - one
        report name from two different libraries, and the collision is as silent
        as the one a level up."""
        _tree(tmp_path, "Films/ExtraX/", "FilmsExtra/X/")
        paths, names = census_run.resolve_input_paths(
            [str(tmp_path / "Films"), str(tmp_path / "FilmsExtra")])
        with pytest.raises(census_run.Refusal) as raised:
            census_run.resolve_libraries(paths, names, 1)
        assert 'both be reported as "FilmsExtraX"' in raised.value.text


class TestResolveOutDirs:
    def test_without_o_each_library_reports_into_itself(self, tmp_path):
        _tree(tmp_path, "films/")
        out_dirs, created = census_run.resolve_out_dirs(
            [str(tmp_path / "films")], "")
        assert out_dirs == [str(tmp_path / "films")]
        assert created == ""

    def test_with_o_every_library_reports_into_the_one_folder(self, tmp_path):
        _tree(tmp_path, "films/", "music/", "out/")
        out_dirs, created = census_run.resolve_out_dirs(
            [str(tmp_path / "films"), str(tmp_path / "music")],
            str(tmp_path / "out"))
        assert out_dirs == [os.path.realpath(tmp_path / "out")] * 2
        assert created == ""

    def test_a_missing_o_folder_is_made_not_refused(self, tmp_path):
        _tree(tmp_path, "films/")
        out_dirs, created = census_run.resolve_out_dirs(
            [str(tmp_path / "films")], str(tmp_path / "fresh"))
        assert os.path.isdir(tmp_path / "fresh")
        assert out_dirs == [os.path.realpath(tmp_path / "fresh")]
        assert created == str(tmp_path / "fresh")

    def test_a_whole_missing_path_is_made_and_its_topmost_level_remembered(
            self, tmp_path):
        """What the run gives back afterwards is exactly what it created, so the
        level remembered is the topmost missing one, not the leaf."""
        _tree(tmp_path, "films/")
        _, created = census_run.resolve_out_dirs(
            [str(tmp_path / "films")], str(tmp_path / "a" / "b" / "c"))
        assert created == str(tmp_path / "a")
        assert os.path.isdir(tmp_path / "a" / "b" / "c")

    def test_an_o_whose_name_is_taken_by_a_file_is_refused(self, tmp_path):
        _tree(tmp_path, "films/", "taken")
        with pytest.raises(census_run.Refusal) as raised:
            census_run.resolve_out_dirs([str(tmp_path / "films")],
                                        str(tmp_path / "taken"))
        assert "is not a folder" in raised.value.text

    def test_an_o_under_a_file_is_refused_as_unmakeable(self, tmp_path):
        _tree(tmp_path, "films/", "taken")
        with pytest.raises(census_run.Refusal) as raised:
            census_run.resolve_out_dirs([str(tmp_path / "films")],
                                        str(tmp_path / "taken" / "reports"))
        assert "could not be made" in raised.value.text

    def test_a_library_that_cannot_be_written_into_is_refused(self, tmp_path):
        _tree(tmp_path, "films/")
        os.chmod(tmp_path / "films", 0o500)
        try:
            with pytest.raises(census_run.Refusal) as raised:
                census_run.resolve_out_dirs([str(tmp_path / "films")], "")
            assert "no write permission" in raised.value.text
        finally:
            os.chmod(tmp_path / "films", 0o700)


class TestCollectFiles:
    def test_only_the_extensions_the_census_reads(self, tmp_path):
        _tree(tmp_path, "lib/a.mp3", "lib/b.txt", "lib/c.doc")
        files, totals, starts = census_run.collect_files(
            [str(tmp_path / "lib")], ["mp3", "txt"])
        assert [os.path.basename(f) for f in files] == ["a.mp3", "b.txt"]
        assert (totals, starts) == ([2], [0])

    def test_the_match_is_case_insensitive_the_way_iname_is(self, tmp_path):
        _tree(tmp_path, "lib/A.MP3", "lib/b.Mp3")
        files, _, _ = census_run.collect_files([str(tmp_path / "lib")], ["mp3"])
        assert len(files) == 2

    def test_a_library_is_walked_recursively(self, tmp_path):
        _tree(tmp_path, "lib/top.mp3", "lib/deep/down/low.mp3")
        files, totals, _ = census_run.collect_files([str(tmp_path / "lib")],
                                                    ["mp3"])
        assert totals == [2]
        assert any("low.mp3" in f for f in files)

    def test_the_list_is_sorted_so_two_runs_diff(self, tmp_path):
        _tree(tmp_path, "lib/b.mp3", "lib/a.mp3", "lib/C.mp3")
        files, _, _ = census_run.collect_files([str(tmp_path / "lib")], ["mp3"])
        assert files == sorted(files, key=os.fsencode)

    def test_each_library_is_a_contiguous_range_of_the_one_list(self, tmp_path):
        _tree(tmp_path, "one/a.mp3", "one/b.mp3", "two/c.mp3")
        files, totals, starts = census_run.collect_files(
            [str(tmp_path / "one"), str(tmp_path / "two")], ["mp3"])
        assert totals == [2, 1]
        assert starts == [0, 2]
        assert len(files) == 3


class TestBuildCubes:
    """What -b hands to `content-census-bi`, and when it hands over nothing."""

    def _fake_bi(self, fake_command, tmp_path):
        record = tmp_path / "argv"
        fake_command("content-census-bi", (
            "import sys, pathlib\n"
            "pathlib.Path(%r).write_text('\\n'.join(sys.argv[1:]))\n"
            % str(record)))
        return record

    def test_the_reports_are_handed_over_by_name(self, tmp_path, fake_command):
        record = self._fake_bi(fake_command, tmp_path)
        reports = [str(tmp_path / "audio.csv"), str(tmp_path / "video.csv")]
        census_run._build_cubes(reports, 2, False, "")
        assert record.read_text().splitlines() == reports

    def test_an_interrupted_run_builds_nothing_and_names_the_command(
            self, tmp_path, fake_command, capsys):
        record = self._fake_bi(fake_command, tmp_path)
        census_run._build_cubes([str(tmp_path / "audio.csv")], 1, True, "")
        assert not record.exists()
        # What it prints is what a user would type, not a file they would find.
        assert "content-census-bi %s" % tmp_path in capsys.readouterr().err

    def test_nothing_written_starts_nothing(self, tmp_path, fake_command):
        record = self._fake_bi(fake_command, tmp_path)
        census_run._build_cubes([], 0, False, "")
        assert not record.exists()
