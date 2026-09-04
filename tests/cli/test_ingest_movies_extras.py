"""Bonus material sorted into the folders Plex recognises, and loose films given
folders of their own.

These two phases only; the subtitle and extension phases the script also drives
are library functions and keep their coverage there.

The classification is asserted through `bonus_category_for` rather than against
hard-coded destinations, so these hold for whatever the table is revised to say -
which is the point of the table being data.
"""

import os

import pytest

from medialib.cli import ingest_movies as im
from medialib.lib import safety

pytestmark = pytest.mark.fs

# A name that matches no keyword in any row, so it can be used as filler.
FILLER = "zzz"

ALL_KEYWORDS = [keyword for _name, keywords in im.BONUS_CATEGORIES
                for keyword in keywords]
FIRST_KEYWORDS = [keywords[0] for _name, keywords in im.BONUS_CATEGORIES]
SUBFOLDERS = [name for name, _keywords in im.BONUS_CATEGORIES]


def _write(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(content)


def _tree(root):
    found = []
    for parent, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(dirs) + sorted(names):
            found.append(os.path.relpath(os.path.join(parent, name), root))
    return sorted(found)


def _film(root, title="Film (2020)"):
    """A film folder holding its film, which is what marks a sub-folder of it as
    bonus material."""
    folder = os.path.join(root, title)
    _write(os.path.join(folder, title + ".mkv"), "m")
    return folder


@pytest.fixture
def skips():
    return safety.RunSkipLog()


class TestMoviesIntoSubfolders:
    """A loose .mkv gets a folder of its own, named after it - the layout Plex
    wants."""

    def test_a_loose_film_is_given_its_folder(self, tmp_path):
        _write(str(tmp_path / "A Film (2020).mkv"), "m")
        im.movies_into_subfolders(str(tmp_path))
        assert (tmp_path / "A Film (2020)" / "A Film (2020).mkv").is_file()

    def test_the_content_survives_the_move(self, tmp_path):
        _write(str(tmp_path / "A Film (2020).mkv"), "content")
        im.movies_into_subfolders(str(tmp_path))
        assert (tmp_path / "A Film (2020)"
                / "A Film (2020).mkv").read_text() == "content"

    def test_a_film_already_in_a_folder_is_left_alone(self, tmp_path):
        _film(str(tmp_path))
        before = _tree(str(tmp_path))
        im.movies_into_subfolders(str(tmp_path))
        assert _tree(str(tmp_path)) == before

    def test_only_the_top_level_is_considered(self, tmp_path):
        """A film one level down is already in a folder of its own."""
        _write(str(tmp_path / "Folder" / "Film.mkv"), "m")
        before = _tree(str(tmp_path))
        im.movies_into_subfolders(str(tmp_path))
        assert _tree(str(tmp_path)) == before

    def test_a_non_mkv_is_not_given_a_folder(self, tmp_path):
        _write(str(tmp_path / "notes.txt"))
        im.movies_into_subfolders(str(tmp_path))
        assert (tmp_path / "notes.txt").is_file()
        assert not (tmp_path / "notes").exists()


class TestTheFeaturettesRename:
    """Extras / Specials / Bonus are renamed to Featurettes, which Plex knows."""

    @pytest.mark.parametrize("word", ["Extras", "Specials", "Bonus"])
    def test_each_spelling_is_renamed_and_then_classified(self, tmp_path, word,
                                                          skips):
        folder = _film(str(tmp_path))
        _write(os.path.join(folder, word, "A trailer.mkv"))
        im.extras_into_subfolders(str(tmp_path), skips)
        assert not os.path.exists(os.path.join(folder, word))
        want = im.bonus_category_for("A trailer.mkv")
        assert os.path.isfile(os.path.join(folder, want, "A trailer.mkv"))

    def test_an_existing_featurettes_blocks_the_rename(self, tmp_path, skips):
        """Nothing is merged or lost: the folder that could not be renamed keeps
        its content, and the refusal is recorded."""
        folder = _film(str(tmp_path))
        _write(os.path.join(folder, "Extras", "keepme.mkv"), "e")
        _write(os.path.join(folder, "Featurettes", "other.mkv"), "f")
        im.extras_into_subfolders(str(tmp_path), skips)
        assert os.path.isdir(os.path.join(folder, "Extras"))
        with open(os.path.join(folder, "Extras", "keepme.mkv")) as kept:
            assert kept.read() == "e"
        assert skips.skips


class TestFlattening:
    """Nested folders inside Featurettes are flattened up to it, and the emptied
    sub-folders pruned, so the classification sees one level of files."""

    def test_a_nested_tree_is_flattened_and_classified(self, tmp_path, skips):
        folder = _film(str(tmp_path))
        featurettes = os.path.join(folder, "Featurettes")
        _write(os.path.join(featurettes, "Disc 1", "An interview.mkv"), "1")
        _write(os.path.join(featurettes, "Disc 1", "Deep", "A blooper.mkv"),
               "2")
        _write(os.path.join(featurettes, "top level trailer.mkv"), "3")

        im.extras_into_subfolders(str(tmp_path), skips)

        assert not os.path.exists(os.path.join(featurettes, "Disc 1"))
        for name, content in (("An interview.mkv", "1"),
                              ("A blooper.mkv", "2"),
                              ("top level trailer.mkv", "3")):
            want = im.bonus_category_for(name)
            landed = os.path.join(folder, want, name)
            assert os.path.isfile(landed)
            with open(landed) as handle:
                assert handle.read() == content

    def test_a_collision_across_the_flattened_folders_keeps_both(self, tmp_path,
                                                                 skips):
        folder = _film(str(tmp_path))
        featurettes = os.path.join(folder, "Featurettes")
        _write(os.path.join(featurettes, "Disc 1", "An interview.mkv"), "one")
        _write(os.path.join(featurettes, "Disc 2", "An interview.mkv"), "two")

        im.extras_into_subfolders(str(tmp_path), skips)

        want = os.path.join(folder, im.bonus_category_for("An interview.mkv"))
        landed = [name for name in os.listdir(want)
                  if name.startswith("An interview")]
        assert len(landed) == 2
        assert os.path.isfile(os.path.join(want, "An interview (2).mkv"))


class TestClassification:
    """The bonusCategories table walked in full: the table's ORDER is the
    priority, and the first matching row wins."""

    def test_a_name_carrying_no_keyword_stays_in_featurettes(self, tmp_path,
                                                             skips):
        folder = _film(str(tmp_path))
        _write(os.path.join(folder, "Featurettes", FILLER + ".mkv"))
        im.extras_into_subfolders(str(tmp_path), skips)
        assert os.path.isfile(os.path.join(folder, "Featurettes",
                                           FILLER + ".mkv"))

    @pytest.mark.parametrize("keyword", ALL_KEYWORDS)
    def test_one_keyword_lands_in_its_first_matching_category(self, tmp_path,
                                                              keyword, skips):
        """Not necessarily the row the keyword itself sits in: an earlier row's
        keyword may also occur inside the name, and that is the rule working."""
        folder = _film(str(tmp_path))
        name = "%s %s.mkv" % (FILLER, keyword)
        _write(os.path.join(folder, "Featurettes", name))
        im.extras_into_subfolders(str(tmp_path), skips)
        want = im.bonus_category_for(name)
        assert os.path.isfile(os.path.join(folder, want, name))

    @pytest.mark.parametrize("keyword", ALL_KEYWORDS)
    def test_the_same_keyword_repeated_changes_nothing(self, tmp_path, keyword,
                                                       skips):
        """It is a membership test, not a tally."""
        folder = _film(str(tmp_path))
        name = "%s %s %s %s.mkv" % (FILLER, keyword, keyword, keyword)
        _write(os.path.join(folder, "Featurettes", name))
        im.extras_into_subfolders(str(tmp_path), skips)
        want = im.bonus_category_for("%s %s.mkv" % (FILLER, keyword))
        assert os.path.isfile(os.path.join(folder, want, name))

    @pytest.mark.parametrize("early,late", [
        (FIRST_KEYWORDS[i], FIRST_KEYWORDS[j])
        for i in range(len(FIRST_KEYWORDS))
        for j in range(i + 1, len(FIRST_KEYWORDS))
    ])
    def test_two_keywords_land_together_whichever_order_they_are_written_in(
            self, tmp_path, early, late, skips):
        """The TABLE's order decides, not the order the words happen to appear
        in the name."""
        folder = _film(str(tmp_path))
        forward = "%s %s then %s.mkv" % (FILLER, early, late)
        reverse = "%s %s then %s.mkv" % (FILLER, late, early)
        _write(os.path.join(folder, "Featurettes", forward))
        _write(os.path.join(folder, "Featurettes", reverse))

        im.extras_into_subfolders(str(tmp_path), skips)

        want = im.bonus_category_for(forward)
        assert os.path.isfile(os.path.join(folder, want, forward))
        assert os.path.isfile(os.path.join(folder, want, reverse))

    def test_an_extra_matching_two_rows_is_filed_exactly_once(self, tmp_path,
                                                              skips):
        """The classification stops at the first match, so no second copy is
        made and no second destination is created."""
        folder = _film(str(tmp_path))
        name = "%s %s then %s.mkv" % (FILLER, FIRST_KEYWORDS[0],
                                      FIRST_KEYWORDS[-1])
        _write(os.path.join(folder, "Featurettes", name))
        im.extras_into_subfolders(str(tmp_path), skips)
        landed = [os.path.join(parent, found)
                  for parent, _dirs, names in os.walk(folder)
                  for found in names if found == name]
        assert len(landed) == 1
        assert not os.path.exists(os.path.join(folder, SUBFOLDERS[-1], name))

    def test_no_empty_category_folder_is_left_behind(self, tmp_path, skips):
        """A category folder is only ever made for a file that goes into it."""
        folder = _film(str(tmp_path))
        _write(os.path.join(folder, "Featurettes", FILLER + ".mkv"))
        _write(os.path.join(folder, "Featurettes",
                            "%s %s.mkv" % (FILLER, FIRST_KEYWORDS[0])))
        im.extras_into_subfolders(str(tmp_path), skips)
        empty = [os.path.join(parent, name)
                 for parent, dirs, _names in os.walk(folder)
                 for name in dirs
                 if not os.listdir(os.path.join(parent, name))]
        assert empty == []

    def test_the_classification_is_case_insensitive(self, tmp_path, skips):
        folder = _film(str(tmp_path))
        _write(os.path.join(folder, "Featurettes", "THEATRICAL TRAILER.mkv"))
        im.extras_into_subfolders(str(tmp_path), skips)
        want = im.bonus_category_for("THEATRICAL TRAILER.mkv")
        assert os.path.isfile(os.path.join(folder, want,
                                           "THEATRICAL TRAILER.mkv"))

    def test_a_rerun_changes_nothing(self, tmp_path, skips):
        """The categorised folders are not themselves Featurettes, so nothing is
        reclassified a second time."""
        folder = _film(str(tmp_path))
        _write(os.path.join(folder, "Extras", "Theatrical trailer.mkv"))
        _write(os.path.join(folder, "Extras", "Untagged.mkv"))
        im.extras_into_subfolders(str(tmp_path), skips)
        before = _tree(str(tmp_path))
        im.extras_into_subfolders(str(tmp_path), skips)
        assert _tree(str(tmp_path)) == before

    def test_a_tree_with_no_extras_at_all_is_untouched(self, tmp_path, skips):
        _film(str(tmp_path))
        before = _tree(str(tmp_path))
        im.extras_into_subfolders(str(tmp_path), skips)
        assert _tree(str(tmp_path)) == before


class TestOnlyInsideAMovieFolder:
    """The name match alone would also catch a LIBRARY or a FILM called "Bonus"
    and reorganise it as if it were extras.

    What tells them apart is the parent: an extras folder's parent is the movie
    folder and holds the film itself, while a movie folder's parent is a library
    or box-set folder and holds no film directly. That also keeps a box set
    working, which a fixed depth would not.
    """

    def test_a_library_called_bonus_is_not_renamed(self, tmp_path, skips):
        """Nothing above the given path may be consulted at all - the loose film
        beside the root would otherwise make it look like bonus material sitting
        inside a movie folder."""
        outside = tmp_path / "outside"
        _write(str(outside / "A Loose Film.mkv"), "stray")
        root = str(outside / "Bonus")
        folder = _film(root)
        _write(os.path.join(folder, "A trailer.mkv"))

        before = _tree(root)
        im.extras_into_subfolders(root, skips)

        assert os.path.isdir(root)
        assert not (outside / "Featurettes").exists()
        assert _tree(root) == before
        assert (outside / "A Loose Film.mkv").is_file()
        assert len(os.listdir(str(outside))) == 2

    @pytest.mark.parametrize("word", ["Extras", "Specials"])
    def test_a_library_ending_in_one_of_the_words_is_not_renamed(self, tmp_path,
                                                                 word, skips):
        library = str(tmp_path / ("My " + word))
        _film(library)
        before = _tree(library)
        im.extras_into_subfolders(library, skips)
        assert os.path.isdir(library)
        assert _tree(library) == before

    def test_a_film_of_its_own_called_bonus_keeps_its_folder(self, tmp_path,
                                                             skips):
        """The folder name has to END with the word to be considered at all, so a
        bare "Bonus" is the case that matters - "Bonus (2019)" ends in a
        parenthesis and would never have matched."""
        _write(str(tmp_path / "Bonus" / "Bonus.mkv"), "m")
        _write(str(tmp_path / "Bonus" / "Bonus.en.srt"), "s")
        before = _tree(str(tmp_path))
        im.extras_into_subfolders(str(tmp_path), skips)
        assert (tmp_path / "Bonus" / "Bonus.mkv").is_file()
        assert not (tmp_path / "Featurettes").exists()
        assert _tree(str(tmp_path)) == before

    def test_the_same_film_inside_a_box_set(self, tmp_path, skips):
        """One level deeper, so the protection cannot be a fixed depth - and a
        real extras folder at the very same depth still works."""
        box = tmp_path / "Box Set"
        _write(str(box / "Bonus" / "Bonus.mkv"), "m")
        film = _film(str(box), "Film (2018)")
        _write(os.path.join(film, "Extras", "A trailer.mkv"))

        im.extras_into_subfolders(str(tmp_path), skips)

        assert (box / "Bonus" / "Bonus.mkv").is_file()
        assert not (box / "Featurettes").exists()
        want = im.bonus_category_for("A trailer.mkv")
        assert os.path.isfile(os.path.join(film, want, "A trailer.mkv"))
        assert not os.path.exists(os.path.join(film, "Extras"))
