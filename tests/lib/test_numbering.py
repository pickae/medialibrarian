"""Tests for medialib.lib.numbering - renaming a folder's commonest filetype to
01, 02, 03.

These say what the rules are, and two of them are rules that had to be worked out
rather than read: the tie-break, and the fact that the ORDER depends on the
directory.
"""

import pytest

from medialib.lib.numbering import NO_EXTENSION, plan_numbering

pytestmark = pytest.mark.pure

DIR = "/library/album"


def renames(names, directory=DIR):
    return plan_numbering(directory, names).renames


class TestOnlyThePluralityFiletype:
    def test_the_commonest_type_is_numbered(self):
        assert renames(["b.mp3", "a.mp3", "cover.jpg"]) == [("a.mp3", "1.mp3"), ("b.mp3", "2.mp3")]

    def test_the_stray_file_keeps_its_name(self):
        plan = dict(renames(["b.mp3", "a.mp3", "cover.jpg"]))
        assert "cover.jpg" not in plan

    def test_case_differing_extensions_are_one_filetype(self):
        assert len(renames(["a.MP3", "b.mp3", "c.Mp3"])) == 3

    def test_but_each_file_keeps_its_own_extension(self):
        assert dict(renames(["a.MP3", "b.mp3"]))["a.MP3"] == "1.MP3"

    def test_files_with_no_extension_are_a_filetype_of_their_own(self):
        assert renames(["a", "b", "one.mp3"]) == [("a", "1"), ("b", "2")]

    def test_a_dotfile_has_no_extension(self):
        # ".cover" is a name, not an extension
        assert plan_numbering(DIR, [".cover", "x", "one.mp3"]).extension == NO_EXTENSION


class TestWhenNothingHappens:
    def test_an_empty_folder(self):
        assert renames([]) == []

    def test_a_lone_file_of_a_type_is_not_a_series(self):
        assert renames(["only.mp3", "cover.jpg"]) == []

    def test_an_already_numbered_folder_is_left_completely_alone(self):
        assert renames(["1.mp3", "2.mp3", "3.mp3"]) == []

    def test_including_its_padding(self):
        assert renames([f"{i:02d}.mp3" for i in range(1, 13)]) == []

    def test_but_wrong_padding_is_redone(self):
        assert renames(["001.mp3", "002.mp3"]) == [("001.mp3", "1.mp3"), ("002.mp3", "2.mp3")]


class TestTheWidthComesFromTheCount:
    def test_nine_files_get_one_digit(self):
        assert renames([f"x{i}.mp3" for i in range(1, 10)])[0][1] == "1.mp3"

    def test_ten_files_get_two(self):
        assert renames([f"x{i}.mp3" for i in range(1, 11)])[0][1] == "01.mp3"


class TestTheOrderIsVersionOrderOverThePaths:
    def test_ten_is_numbered_after_nine(self):
        plan = dict(renames(["x10.mp3", "x9.mp3"]))
        assert plan["x9.mp3"] == "1.mp3"
        assert plan["x10.mp3"] == "2.mp3"

    def test_the_directory_changes_the_answer_and_must_be_passed(self):
        # Version sort puts a name beginning with a dot before everything; a PATH
        # beginning with a slash never triggers that, and a dot mid-path is just a
        # separator that sorts after letters. bash sorts the paths find printed,
        # so ".1 intro" is numbered LAST, and by basename it would be first. This
        # cost a debugging round on the real harness, so it is pinned here.
        names = [".1 intro", "a10", "b"]
        assert renames(names, DIR) == [("a10", "1"), ("b", "2"), (".1 intro", "3")]
        assert renames(names, "") == [(".1 intro", "1"), ("a10", "2"), ("b", "3")]


class TestTheTieBreak:
    """Two filetypes with the same count: first appearance wins. bash settled this
    by iterating an associative array, which is hash order."""

    def test_the_first_filetype_in_order_wins(self):
        assert plan_numbering(
            DIR, ["1 a.nfo", "2 b.webp", "3 c.nfo",
                  "4 d.webp"]).extension == "nfo"

    def test_and_the_other_one_when_it_comes_first(self):
        assert plan_numbering(
            DIR, ["1 a.webp", "2 b.nfo", "3 c.webp",
                  "4 d.nfo"]).extension == "webp"

    def test_a_clear_winner_is_not_affected_by_order(self):
        assert plan_numbering(DIR, ["z.jpg", "a.mp3", "b.mp3"]).extension == "mp3"


class TestPerformingTheRenames:
    """The plan above says what to do; this does it - the destructive half, over
    real folders."""

    def _folder(self, tmp_path, names):
        for name in names:
            (tmp_path / name).write_text(name)
        return sorted(p.name for p in tmp_path.iterdir())

    def _number(self, tmp_path, names):
        from medialib.lib.numbering import number_files_in_folder
        self._folder(tmp_path, names)
        number_files_in_folder(str(tmp_path),
                              [str(tmp_path / name) for name in names])
        return sorted(p.name for p in tmp_path.iterdir())

    def test_the_plurality_type_is_renumbered_on_disk(self, tmp_path):
        assert self._number(tmp_path, ["a.mp3", "b.mp3", "c.mp3"]) == \
            ["1.mp3", "2.mp3", "3.mp3"]

    def test_the_stray_file_is_left_where_it_was(self, tmp_path):
        assert "cover.jpg" in self._number(
            tmp_path, ["a.mp3", "b.mp3", "c.mp3", "cover.jpg"])

    def test_a_second_run_changes_nothing(self, tmp_path):
        from medialib.lib.numbering import number_files_in_folder
        once = self._number(tmp_path, ["a.mp3", "b.mp3", "c.mp3"])
        number_files_in_folder(str(tmp_path),
                               [str(tmp_path / name) for name in once])
        assert sorted(p.name for p in tmp_path.iterdir()) == once

    @pytest.mark.parametrize("names", [
        ["%d.mp3" % n for n in range(1, 11)],          # widening to two digits
        ["01.mp3", "02.mp3", "03.mp3"],                # narrowing to one
        ["1.mp3", "2.mp3", "3.mp3", "6.mp3", "7.mp3"],  # closing a gap
    ])
    def test_no_file_is_lost_or_duplicated(self, tmp_path, names):
        """The renumber MOVES every file and invents none: what the folder holds
        afterwards is the same set of contents under new names.

        The two phases through temporary names are what would keep that true if
        a target ever collided with a source not yet renamed. With this plan it
        cannot: the sources are walked in version order and the targets ascend,
        so the collision the staging guards against is unreachable from here -
        which is why this asserts the property and not the mechanism.
        """
        before = set(names)
        self._number(tmp_path, names)
        after = {path.read_text() for path in tmp_path.iterdir()}
        assert after == before

    def test_no_staging_name_is_left_behind(self, tmp_path):
        got = self._number(tmp_path, ["a.mp3", "b.mp3"])
        assert not any(name.startswith(".cfs_renumber") for name in got)
