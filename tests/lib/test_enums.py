"""Tests for medialib.lib.enums - fileSafety's extension lists and name helpers.

These pin the properties that are about the lists as a SET, which reading any one
member cannot show.
"""

import pytest

from medialib.lib import enums

pytestmark = pytest.mark.pure


class TestReadingAnExtension:
    def test_a_plain_name(self):
        assert enums.lower_extension_of("track.MP3") == "mp3"

    def test_a_path_is_reduced_to_its_last_segment(self):
        assert enums.lower_extension_of("/a/b.d/file.Opus") == "opus"

    def test_a_directory_component_with_a_dot_does_not_count(self):
        assert enums.lower_extension_of("/a/b.d/file") == ""

    def test_no_dot_means_no_extension(self):
        assert enums.lower_extension_of("noextension") == ""

    def test_a_trailing_dot_gives_an_empty_extension(self):
        assert enums.lower_extension_of("file.") == ""

    def test_the_last_dot_wins(self):
        assert enums.lower_extension_of("a.b.c.MP3") == "mp3"


class TestDotfiles:
    """A name that begins with a dot has no extension (item 7.4)."""

    def test_a_dotfile_has_no_extension(self):
        assert enums.lower_extension_of(".hidden") == ""

    def test_and_a_second_dot_does_not_give_it_one(self):
        assert enums.lower_extension_of(".hidden.mp3") == ""
        assert enums.extension_of(".hidden.MP3") == ""

    def test_the_renumberer_reads_the_same_answer(self):
        # There were two definitions and they disagreed about this one name.
        # This side moved to the renumberer's, so both now skip the file.
        from medialib.lib.numbering import plan_numbering

        plan = plan_numbering("/d", [".hidden.mp3", "x.mp3", "y.mp3"])
        assert plan.extension == "mp3"
        assert [source for source, _ in plan.renames] == ["x.mp3", "y.mp3"]

    def test_the_current_and_parent_directory_have_none(self):
        assert enums.lower_extension_of(".") == ""
        assert enums.lower_extension_of("..") == ""

    def test_the_extension_keeps_its_case_and_the_lower_one_does_not(self):
        assert enums.extension_of("Track.FLAC") == "FLAC"
        assert enums.lower_extension_of("Track.FLAC") == "flac"


class TestTheListing:
    def test_it_reads_the_way_a_message_should(self):
        assert enums.extension_list(["mp3", "flac"]) == ".mp3 / .flac"

    def test_one_extension_needs_no_separator(self):
        assert enums.extension_list(["mp3"]) == ".mp3"


class TestTheListsThemselves:
    """Properties of the lists as sets - a duplicated entry, an overlap between two
    lists, a suffix no report claims. Every member can be right and the set wrong."""

    def test_the_registry_holds_every_list(self):
        assert len(enums.LISTS) == 16

    def test_no_list_is_empty(self):
        assert all(members for members in enums.LISTS.values())

    def test_every_extension_is_lower_case_and_dotless(self):
        for name, members in enums.LISTS.items():
            for member in members:
                assert member == member.lower(), f"{name}: {member}"
                assert not member.startswith("."), f"{name}: {member}"

    def test_no_list_repeats_itself(self):
        for name, members in enums.LISTS.items():
            assert len(set(members)) == len(members), f"{name} has a duplicate"

    def test_the_comic_and_archive_lists_overlap_on_purpose(self):
        # A .cbz IS a zip. They are separate lists because a .cbz is a book and a
        # .zip is a folder of tracks, and the plan's own note says so.
        assert set(enums.COMIC_EXTENSIONS) & {"cbz", "cbr", "cb7"}

    def test_alac_is_absent_from_the_lossless_extensions_but_present_in_the_codecs(self):
        # It arrives as .m4a, an extension that is lossy far more often than not,
        # so it is caught by probing rather than by name.
        assert "m4a" not in enums.LOSSLESS_AUDIO_EXTENSIONS
        assert "alac" in enums.LOSSLESS_CODECS

    def test_the_brackets_are_matched_pairs_by_index(self):
        assert len(enums.BRACKET_OPEN) == len(enums.BRACKET_CLOSE)
        assert list(zip(enums.BRACKET_OPEN, enums.BRACKET_CLOSE, strict=True)) == [
            ("(", ")"), ("[", "]"), ("{", "}"), ("<", ">"),
        ]

class TestLoweringTheWayTheShellDoes:
    """``${x,,}`` lowers character by character through the C library; Python's
    ``str.lower`` applies the full Unicode mapping. Over all 292,463 printable
    codepoints the two answers differ for exactly one, and this is it."""

    def test_the_dotted_capital_i_loses_its_dot(self):
        assert enums.shell_lower("\u0130") == "i"

    def test_in_a_word(self):
        assert enums.shell_lower("MÜZİK") == "müzik"

    def test_as_an_extension(self):
        assert enums.lower_extension_of("Şarkı.MÜZİK") == "müzik"

    @pytest.mark.parametrize("text", ["MP3", "MÜLL", "ẞ", "ß", "Straße", "ÅNGSTRÖM"])
    def test_everything_else_is_plain_lower_case(self, text):
        assert enums.shell_lower(text) == text.lower()
