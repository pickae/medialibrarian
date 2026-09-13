"""Tests for medialib.lib.plexnames - the Plex naming conventions a movie
folder is renamed into.

What is pinned here is the part that cannot be read off any one name: the ORDER
the three conventions go in (a stacking token has to stay last or the parts stop
stacking), which file belongs to which film when several share a prefix, and
that feeding the module its own output returns no work - the property the whole
phase rests on, since it runs on every ingest over a library that is mostly
already named.
"""

import pytest

from medialib.lib import plexnames

pytestmark = pytest.mark.pure

TAG = "{imdb-tt0000001}"


class TestReadingAStem:
    def test_the_plain_film_carries_neither_marker(self):
        assert plexnames.read_stem("The Movie (1999)", "The Movie (1999)") == ("", "")

    def test_a_parenthesised_version_becomes_an_edition(self):
        assert plexnames.read_stem(
            "The Movie (1999)", "The Movie (1999) (Original Mono Track)") \
            == ("Original Mono Track", "")

    def test_a_bare_version_word_becomes_an_edition(self):
        assert plexnames.read_stem("The Movie (1999)", "The Movie (1999) colorized") \
            == ("Colorized", "")

    def test_a_stacking_token_is_read_as_a_part_and_not_as_an_edition(self):
        assert plexnames.read_stem("The Movie (1968)", "The Movie (1968) Part1") \
            == ("", "Part1")

    @pytest.mark.parametrize("token", ["cd1", "CD2", "part3", "pt4", "disc5",
                                       "disk6", "dvd7", "Part.8"])
    def test_every_token_plex_stacks_on_is_recognised(self, token):
        assert plexnames.read_stem("Film (2020)", "Film (2020) " + token) \
            == ("", token)

    def test_a_word_that_merely_ends_in_a_number_is_an_edition(self):
        assert plexnames.read_stem("Film (2020)", "Film (2020) Remux2") \
            == ("Remux2", "")

    def test_an_edition_already_tagged_is_read_back_verbatim(self):
        assert plexnames.read_stem(
            "The Movie (1999)", "The Movie (1999) {imdb-tt1} {edition-Colorized}") \
            == ("Colorized", "")

    def test_an_edition_and_a_part_are_read_together(self):
        assert plexnames.read_stem(
            "Film (2020)", "Film (2020) {edition-Director's Cut} cd2") \
            == ("Director's Cut", "cd2")


class TestAnEditionName:
    def test_a_lower_case_name_gets_its_capital(self):
        assert plexnames.edition_name("colorized") == "Colorized"

    def test_a_name_that_has_capitals_of_its_own_is_left_alone(self):
        assert plexnames.edition_name("Reissue mono soundtrack") \
            == "Reissue mono soundtrack"

    @pytest.mark.parametrize("wrapped", ["(Director's Cut)", "[Director's Cut]"])
    def test_one_layer_of_wrapping_comes_off(self, wrapped):
        assert plexnames.edition_name(wrapped) == "Director's Cut"

    def test_braces_cannot_survive_into_the_tag_that_uses_them(self):
        # The tag has no escape for its own delimiters, so a name carrying one
        # would end the tag early and leave the rest as loose text.
        assert plexnames.edition_name("a {weird} print") == "A (weird) print"


class TestAssemblingAStem:
    def test_the_stacking_token_goes_last(self):
        """Plex matches the stacking suffix at the END of the name: a tag after
        it stops the parts stacking at all."""
        assert plexnames.plex_stem("The Movie (1968)", TAG, "", "Part1") \
            == "The Movie (1968) " + TAG + " Part1"

    def test_an_edition_sits_between_the_id_and_the_part(self):
        assert plexnames.plex_stem("Film (2020)", TAG, "Colorized", "cd1") \
            == "Film (2020) " + TAG + " {edition-Colorized} cd1"

    def test_without_a_tag_the_name_is_the_film_itself(self):
        assert plexnames.plex_stem("Film (2020)", "", "", "") == "Film (2020)"


class TestTheFilesOfOneFolder:
    def _one_folder(self):
        return ["The Movie (1999) (Original Mono Track).mkv",
                "The Movie (1999) (Original Mono Track).en.srt",
                "The Movie (1999) (Original Mono Track) 2 Commentary.srt",
                "The Movie (1999) colorized.mkv",
                "The Movie (1999) colorized.nl.srt",
                "The Movie (1999) (old).mkv",
                "cover.jpg"]

    def test_each_film_and_its_sidecars_take_the_same_new_stem(self):
        plan = dict(plexnames.folder_renames("The Movie (1999)", TAG,
                                             self._one_folder()))
        edition = "The Movie (1999) " + TAG + " {edition-Original Mono Track}"
        assert plan["The Movie (1999) (Original Mono Track).mkv"] \
            == edition + ".mkv"
        assert plan["The Movie (1999) (Original Mono Track).en.srt"] \
            == edition + ".en.srt"
        assert plan["The Movie (1999) colorized.nl.srt"] \
            == "The Movie (1999) " + TAG + " {edition-Colorized}.nl.srt"

    def test_a_commentary_transcript_follows_its_own_film(self):
        """It shares a prefix with the shorter film name in the same folder, so
        the longest match is what keeps it with the film it transcribes."""
        plan = dict(plexnames.folder_renames("The Movie (1999)", TAG,
                                             self._one_folder()))
        assert plan["The Movie (1999) (Original Mono Track) 2 Commentary.srt"] \
            == ("The Movie (1999) " + TAG
                + " {edition-Original Mono Track} 2 Commentary.srt")

    def test_the_copy_an_improvement_kept_is_not_renamed(self):
        plan = dict(plexnames.folder_renames("The Movie (1999)", TAG,
                                             self._one_folder()))
        assert "The Movie (1999) (old).mkv" not in plan

    def test_a_file_belonging_to_no_film_is_left_alone(self):
        plan = dict(plexnames.folder_renames("The Movie (1999)", TAG,
                                             self._one_folder()))
        assert "cover.jpg" not in plan

    def test_the_parts_of_a_split_film_keep_their_tokens(self):
        plan = dict(plexnames.folder_renames(
            "The Movie (1968)", TAG,
            ["The Movie (1968) Part1.mkv", "The Movie (1968) Part2.mkv",
             "The Movie (1968) Part1.en.srt"]))
        assert plan["The Movie (1968) Part2.mkv"] \
            == "The Movie (1968) " + TAG + " Part2.mkv"
        assert plan["The Movie (1968) Part1.en.srt"] \
            == "The Movie (1968) " + TAG + " Part1.en.srt"

    def test_a_second_pass_over_its_own_output_has_nothing_to_do(self):
        names = self._one_folder()
        renamed = dict(plexnames.folder_renames("The Movie (1999)", TAG, names))
        settled = [renamed.get(name, name) for name in names]
        assert plexnames.folder_renames("The Movie (1999)", TAG, settled) == []

    def test_a_folder_tagged_but_never_renamed_inside_is_put_right(self):
        """What the version that renamed only the folder left behind."""
        plan = dict(plexnames.folder_renames(
            "The Movie (1999)", TAG,
            ["The Movie (1999).mkv", "The Movie (1999).en.srt"]))
        assert plan == {
            "The Movie (1999).mkv": "The Movie (1999) " + TAG + ".mkv",
            "The Movie (1999).en.srt": "The Movie (1999) " + TAG + ".en.srt"}


class TestReadingAFolderName:
    def test_an_untagged_folder_keeps_its_whole_name(self):
        assert plexnames.untagged_base("The Movie (1999)") == ("The Movie (1999)", "")

    def test_a_tagged_folder_hands_back_both_halves(self):
        assert plexnames.untagged_base("The Movie (1999) " + TAG) \
            == ("The Movie (1999)", TAG)

    def test_a_tmdb_tag_is_read_and_kept(self):
        assert plexnames.untagged_base("The Movie (1999) {tmdb-1234}") \
            == ("The Movie (1999)", "{tmdb-1234}")


class TestWhetherAFolderHoldsOneFilm:
    def test_a_single_movie_is_that_film(self):
        assert plexnames.one_film_in("Film (2020)", ["Film (2020).mkv"]) \
            == ["Film (2020).mkv"]

    def test_tagged_editions_are_one_film(self):
        names = ["Film (2020) " + TAG + " {edition-Colorized}.mkv",
                 "Film (2020) " + TAG + " {edition-Theatrical}.mkv"]
        assert plexnames.one_film_in("Film (2020) " + TAG, names) == sorted(names)

    def test_parts_are_one_film_even_before_the_library_is_tagged(self):
        """A stacking token is already systematic, so a split film needs no tag
        to be told apart from a folder holding two features."""
        names = ["The Movie (1968) Part1.mkv", "The Movie (1968) Part2.mkv"]
        assert plexnames.one_film_in("The Movie (1968)", names) == names

    def test_untagged_versions_are_not_yet_one_film(self):
        """They become one once the tagging has written their editions - which
        is the run after this one, tagging being the last phase."""
        assert plexnames.one_film_in(
            "The Movie (1999)", ["The Movie (1999) colorized.mkv",
                             "The Movie (1999) (Reissue mono soundtrack).mkv"]) == []

    def test_two_features_in_one_folder_are_still_ambiguous(self):
        assert plexnames.one_film_in("Box Set",
                                     ["Part One.mkv", "Part Two.mkv"]) == []

    def test_the_kept_copy_is_not_one_of_the_films(self):
        assert plexnames.one_film_in(
            "Film (2020)", ["Film (2020).mkv", "Film (2020) (old).mkv"]) \
            == ["Film (2020).mkv"]
