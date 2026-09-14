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


class TestTheKeptCopy:
    """Whether a name is the original an improved remux kept - the one rule
    every phase reads to leave a finished copy alone."""

    @pytest.mark.parametrize("name", [
        "The Movie (1999) (old).mkv",
        "The Movie (1999) " + TAG + " (old).mkv",
        "The Movie (1999) " + TAG + " {edition-Colorized} (old).mkv",
        "The Movie (1968) " + TAG + " Part1 (old).mkv"])
    def test_the_suffix_comes_last_whatever_tags_precede_it(self, name):
        assert plexnames.is_kept_copy(name) is True

    def test_a_whole_path_is_read_by_its_last_segment(self):
        assert plexnames.is_kept_copy(
            "/library/The Movie (1999)/The Movie (1999) (old).mkv") is True

    @pytest.mark.parametrize("name", [
        "The Movie (1999).mkv",
        "The Movie (1999) {edition-Old}.mkv",
        "The Movie (1999) (old).en.srt",
        "Something (old).mp4"])
    def test_and_nothing_else_is_one(self, name):
        assert plexnames.is_kept_copy(name) is False


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


BASE = "Le comte de Monte-Cristo (1961)"


class TestAPartWrittenWithItsNumberHeldOff:
    """Plex's scanner wants the number against the keyword. "Part 1" is the
    same token written by a person, and is a spelling to correct rather than a
    folder to give up on."""

    @pytest.mark.parametrize("written,tight", [
        ("Part 1", "Part1"),
        ("part 2", "part2"),
        ("cd 3", "cd3"),
        ("disc  4", "disc4"),
        ("pt. 5", "pt5"),
        ("Part - 6", "Part6"),
    ])
    def test_it_is_read_as_the_token_it_is(self, written, tight):
        assert plexnames.read_stem("Film (2020)", "Film (2020) " + written) \
            == ("", tight)

    def test_and_written_back_where_plex_reads_it(self):
        assert plexnames.folder_renames(
            "Film (2020)", TAG,
            ["Film (2020) Part 1.mkv", "Film (2020) Part 2.mkv"]) \
            == [("Film (2020) Part 1.mkv", "Film (2020) " + TAG + " Part1.mkv"),
                ("Film (2020) Part 2.mkv", "Film (2020) " + TAG + " Part2.mkv")]

    def test_a_part_with_a_title_after_it_is_still_not_a_token(self):
        """Nothing can make it stack: the token has to come last, and there is
        a title sitting after it."""
        edition = plexnames.read_stem(
            "Film (2020)", "Film (2020) Part 1 - The Escape")[0]
        assert edition == "Part 1 - The Escape"
        assert plexnames.names_a_part(edition)

    def test_the_parts_of_one_film_are_one_film(self):
        assert plexnames.one_film_in(
            "Film (2020)", ["Film (2020) Part 1.mkv", "Film (2020) Part 2.mkv"]) \
            == ["Film (2020) Part 1.mkv", "Film (2020) Part 2.mkv"]


class TestBringingANameOntoTheFoldersOwn:
    """A file that says the folder's film in a different spelling is that film
    written by another hand, not a second film."""

    @pytest.mark.parametrize("name,wanted", [
        ("le comte de monte cristo (1961).mkv", BASE + ".mkv"),
        ("Le Comte De Monte-Cristo (1961).mkv", BASE + ".mkv"),
        ("Le comte de Monte Cristo (1961).mkv", BASE + ".mkv"),
        ("Le comte de Monte-Cristo.mkv", BASE + ".mkv"),
        ("le comte de monte cristo (1961) part 1.mkv", BASE + " part 1.mkv"),
        ("Le Comte de Monte Cristo.en.srt", BASE + ".en.srt"),
    ])
    def test_the_spelling_is_corrected_and_the_rest_comes_through(self, name,
                                                                   wanted):
        assert plexnames.onto_base(BASE, name) == wanted

    @pytest.mark.parametrize("name", [
        "Some Other Film.mkv",
        "The Count of Monte Cristo (1961).mkv",
        "Le comte de Monte-Cristo (1961).mkv",
    ])
    def test_and_a_name_this_film_cannot_be_read_out_of_is_left(self, name):
        assert plexnames.onto_base(BASE, name) == ""

    def test_the_copy_an_improved_remux_kept_is_never_respelled(self):
        assert plexnames.onto_base(BASE, "le comte de monte cristo (old).mkv") == ""

    def test_a_rename_that_would_land_on_a_held_name_is_dropped(self):
        """The two files are then genuinely two, whatever their names say."""
        assert plexnames.spelling_renames(
            BASE, [BASE + ".mkv", "le comte de monte cristo (1961).mkv"]) == {}

    def test_the_whole_folder_is_respelled_and_tagged_in_one_plan(self):
        assert plexnames.folder_renames(
            BASE, TAG, ["le comte de monte cristo (1961) part 1.mkv",
                        "le comte de monte cristo (1961) part 2.mkv"]) \
            == [("le comte de monte cristo (1961) part 1.mkv",
                 BASE + " " + TAG + " part1.mkv"),
                ("le comte de monte cristo (1961) part 2.mkv",
                 BASE + " " + TAG + " part2.mkv")]

    def test_a_folder_already_right_still_returns_no_work(self):
        assert plexnames.folder_renames(
            BASE + " " + TAG, "", [BASE + " " + TAG + ".mkv"]) == []


class TestTheMarkerAFileManagerLeft:
    @pytest.mark.parametrize("name", [
        BASE + " (1).mkv",
        BASE + " (copy).mkv",
        BASE + " - Copy.mkv",
        BASE + " (another copy).mkv",
    ])
    def test_a_lone_copy_just_loses_its_marker(self, name):
        assert plexnames.spelling_renames(BASE, [name]) == {name: BASE + ".mkv"}

    def test_two_of_them_keep_both_names(self):
        """Which of the two to keep is not a naming question."""
        names = [BASE + ".mkv", BASE + " (1).mkv"]
        assert plexnames.spelling_renames(BASE, names) == {}

    def test_the_year_is_not_a_copy_number(self):
        assert plexnames.spelling_renames(BASE, [BASE + ".mkv"]) == {}


class TestTaggingAFolderNothingCanRename:
    """The id is known even where which file is which release is not."""

    @pytest.mark.parametrize("stem,wanted", [
        ("Some Other Film", "Some Other Film " + TAG),
        ("Film (2020) Part1", "Film (2020) " + TAG + " Part1"),
        ("Film (2020) {edition-Colorized}",
         "Film (2020) " + TAG + " {edition-Colorized}"),
    ])
    def test_the_tag_goes_in_front_of_what_has_to_stay_last(self, stem, wanted):
        assert plexnames.retag(stem, TAG) == wanted

    def test_a_stem_that_already_carries_one_is_left_alone(self):
        assert plexnames.retag("Film (2020) {imdb-tt9}", TAG) \
            == "Film (2020) {imdb-tt9}"

    def test_every_sidecar_follows_its_own_film(self):
        assert plexnames.id_tag_renames(
            TAG, ["A Film.mkv", "A Film.en.srt", "B Film.mkv"]) \
            == [("A Film.en.srt", "A Film " + TAG + ".en.srt"),
                ("A Film.mkv", "A Film " + TAG + ".mkv"),
                ("B Film.mkv", "B Film " + TAG + ".mkv")]

    def test_the_ids_a_folder_already_holds_are_read_off_its_films(self):
        assert plexnames.ids_in(["A " + TAG + ".mkv", "B " + TAG + ".mkv",
                                 "C {imdb-tt0000002}.mkv"]) \
            == {TAG, "{imdb-tt0000002}"}

    def test_a_sidecar_is_not_one_of_the_films(self):
        assert plexnames.ids_in(["A.mkv", "A " + TAG + ".en.srt"]) == set()


class TestAYearWithADigitWrong:
    """A folder dated 1988 beside a file of it dated 1998 is one of them
    mistyped. The leftover reads as a bare number, which is also what a copy's
    "(1)" reads as, and they are nothing like each other."""

    @pytest.mark.parametrize("text", ["1988", "1998", "2017", " 1979 "])
    def test_a_bare_year_is_a_year(self, text):
        assert plexnames.is_only_a_year(text)

    @pytest.mark.parametrize("text", ["1", "2", "12", "0998", "99", "12345",
                                      "2019 Restoration", "Colorized"])
    def test_and_anything_else_is_not(self, text):
        assert not plexnames.is_only_a_year(text)

    def test_the_file_is_read_as_this_film_dated_differently(self):
        assert plexnames.read_stem("Dragons Forever (1988)",
                                   "Dragons Forever (1988) (1998)") \
            == ("1998", "")

    def test_a_file_dated_differently_is_brought_onto_the_folders_name(self):
        """It is this film - the title says so and only the year differs - so
        it is not a stray; what it is instead is the folder's own business."""
        assert plexnames.onto_base("Dragons Forever (1988)",
                                   "Dragons Forever (1998).mkv") \
            == "Dragons Forever (1988) (1998).mkv"


class TestANumberedSequelIsNotAnEdition:
    """The reading that makes "Winnetou" match the folder "Winnetou I (1963)"
    also makes "Winnetou II (1964).mkv" match it, with the "II" left over as
    though it were an edition. It is the sequel."""

    @pytest.mark.parametrize("name", [
        "Winnetou II (1964).mkv",
        "Winnetou 2.mkv",
        "Winnetou II.mkv",
        "Winnetou III (1965).mkv",
    ])
    def test_a_bare_number_after_the_title_names_another_film(self, name):
        assert plexnames.onto_base("Winnetou I (1963)", name) == ""

    @pytest.mark.parametrize("name,wanted", [
        ("Winnetou (1963).mkv", "Winnetou I (1963).mkv"),
        ("Winnetou I (1963) (1).mkv", "Winnetou I (1963).mkv"),
        ("Winnetou 1 (1963).mkv", "Winnetou I (1963).mkv"),
    ])
    def test_while_the_first_of_the_series_still_comes_home(self, name, wanted):
        assert plexnames.onto_base("Winnetou I (1963)", name) == wanted

    def test_a_number_in_brackets_is_not_a_sequel(self):
        """It is a year or a copy's marker, and both say something about THIS
        film rather than naming another."""
        assert plexnames.onto_base("Dragons Forever (1988)",
                                   "Dragons Forever (1998).mkv") \
            == "Dragons Forever (1988) (1998).mkv"

    def test_nor_is_a_part(self):
        assert plexnames.onto_base("Film (2020)", "film (2020) part 1.mkv") \
            == "Film (2020) part 1.mkv"
