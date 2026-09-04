"""Tests for medialib.lib.plurality - which of a folder's siblings take part in
the collective name-cleaning pass.

These pin the contract in its own right - what the behaviour is FOR, and the
tie-break in particular, because that rule was made explicit during the port
rather than inherited.
"""

import pytest

from medialib.lib.plurality import plurality_group_indices as group

pytestmark = pytest.mark.pure


class TestFoldersMode:
    """Folders have no extension, so they always form one group."""

    def test_every_item_takes_part(self):
        assert group("folders", ["", "", ""]) == [0, 1, 2]

    def test_even_when_they_look_like_extensions(self):
        assert group("folders", ["mp3", "jpg"]) == [0, 1]

    def test_anything_that_is_not_files_means_folders(self):
        # bash tests `!= files`, so this is the contract rather than an accident
        assert group("", ["mp3", "jpg"]) == [0, 1]
        assert group("anything", ["mp3", "jpg"]) == [0, 1]

    def test_no_siblings_at_all(self):
        assert group("folders", []) == []


class TestFilesMode:
    """Only the commonest extension takes part."""

    def test_the_odd_file_out_is_excluded(self):
        assert group("files", ["mp3", "mp3", "jpg"]) == [0, 1]

    def test_a_single_filetype_is_the_whole_group(self):
        assert group("files", ["opus", "opus", "opus"]) == [0, 1, 2]

    def test_case_is_ignored_when_tallying(self):
        assert group("files", ["mp3", "MP3", "jpg"]) == [0, 1]

    def test_case_is_ignored_when_selecting(self):
        assert group("files", ["JPG", "mp3", "jpg", "Jpg"]) == [0, 2, 3]

    def test_the_group_need_not_be_contiguous(self):
        assert group("files", ["mp3", "jpg", "mp3"]) == [0, 2]

    def test_no_siblings_at_all(self):
        assert group("files", []) == []


class TestDotlessNames:
    """A filename with no extension is a filetype of its own, not an error."""

    def test_dotless_names_form_their_own_plurality(self):
        assert group("files", ["", "", "mp3"]) == [0, 1]

    def test_dotless_names_can_be_the_minority(self):
        assert group("files", ["mp3", "mp3", ""]) == [0, 1]

    def test_all_dotless(self):
        assert group("files", ["", "", ""]) == [0, 1, 2]


class TestTieBreak:
    """A tie goes to the filetype that appears first among the siblings.

    This rule was made explicit during the port. bash settled ties by its hash
    order, which meant "jpg" beat "mp3" and "flac" beat "opus" whichever way round
    they were given - stable for one build of bash, reproducible nowhere else.
    """

    def test_first_appearance_wins(self):
        assert group("files", ["mp3", "jpg"]) == [0]

    def test_and_wins_the_other_way_round_too(self):
        assert group("files", ["jpg", "mp3"]) == [0]

    def test_the_rule_does_not_depend_on_the_names(self):
        # the pairs bash's hash order used to decide, now decided by position
        assert group("files", ["flac", "opus"]) == [0]
        assert group("files", ["opus", "flac"]) == [0]
        assert group("files", ["zzz", "aaa"]) == [0]
        assert group("files", ["aaa", "zzz"]) == [0]

    def test_a_three_way_tie_still_goes_to_the_first(self):
        assert group("files", ["a", "b", "c"]) == [0]

    def test_a_later_filetype_needs_strictly_more_to_win(self):
        assert group("files", ["a", "b", "b"]) == [1, 2]
        assert group("files", ["a", "a", "b", "b"]) == [0, 1]

    def test_a_tie_between_dotless_and_an_extension(self):
        assert group("files", ["", "mp3"]) == [0]
        assert group("files", ["mp3", ""]) == [0]
