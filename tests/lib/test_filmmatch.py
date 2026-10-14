"""Tests for medialib.lib.filmmatch - which Letterboxd film a Plex film is.

Each rung is pinned from both sides, as titlematch's readings are: the pairs a
library and Letterboxd really do write differently and that MUST meet, and the
near misses that must not, because each of them is two films. The near misses
are the ones a real library turned up.
"""

import pytest

from medialib.lib.filmmatch import Film, FilmIndex

# The fold reaches iconv for an accented title.
pytestmark = pytest.mark.stubbed


def _found(plex: Film, *letterboxd: Film):
    return FilmIndex(letterboxd).find(plex)


@pytest.mark.parametrize("plex,theirs", [
    (Film("12 Lanterns", "1995"), Film("Twelve Lanterns", "1995")),
    (Film("Moss Rooms Part II", "2004"), Film("Moss Rooms Part 2", "2004")),
    (Film("N.E.S.T.", "1982"), Film("NEST", "1982")),
    (Film("Rooms & Shelves", "1990"), Film("Rooms and Shelves", "1990")),
    (Film("Big Kite", "2022"), Film("Bigkite", "2022")),
    (Film("Selene", "2001"), Film("Sélène", "2001")),
    (Film("39 Lanterns", "1935"), Film("The 39 Lanterns", "1935")),
    (Film("Harbour Tales: Episode VII - The Moonlight Wake", "2015"),
     Film("Harbour Tales: The Moonlight Wake", "2015")),
    (Film("Two Nights in Ghent", "2007"), Film("2 Nights in Ghent", "2007")),
])
def test_a_title_written_another_way_is_the_same_film(plex, theirs):
    found = _found(plex, theirs)
    assert found.film == theirs
    assert found.how == "title"


def test_the_original_title_is_asked_too():
    theirs = Film("Achtung, der Chef", "1991")
    found = _found(Film("Watch Out, the Boss Is Back!", "1991",
                        aliases=("Achtung, der Chef",)), theirs)
    assert found.film == theirs


def test_an_id_both_sides_carry_answers_before_any_title():
    theirs = Film("Something Else Entirely", "1999", tmdb="603")
    found = _found(Film("The Lattice", "1999", tmdb="603"), theirs,
                   Film("The Lattice", "1999"))
    assert (found.film, found.how) == (theirs, "id")


def test_two_different_ids_are_two_films_however_alike_the_titles():
    assert not _found(Film("Sandfall", "2021", tmdb="438631"),
                      Film("Sandfall", "2021", tmdb="1")).matched


def test_a_year_apart_needs_the_same_title():
    theirs = Film("Iron Orchard", "2015")
    assert _found(Film("Iron Orchard", "2014"), theirs).film == theirs
    # A year apart is one disagreement already; an article on top is two films.
    assert not _found(Film("Hound", "2022"), Film("The Hound", "2023")).matched


def test_two_years_apart_is_not_the_film():
    assert not _found(Film("Copper Lamp", "1992"), Film("Copper Lamp", "1994")).matched


def test_the_same_year_wins_over_a_year_apart():
    same = Film("Hollow Night", "2018")
    assert _found(Film("Hollow Night", "2018"), Film("Hollow Night", "2019"),
                  same).film == same


def test_a_film_with_no_year_matches_by_title():
    theirs = Film("The Cashier", "2020")
    found = _found(Film("Der Kassierer", "", aliases=("The Cashier",)), theirs)
    assert (found.film, found.how) == (theirs, "title, no year to compare")


def test_a_franchise_written_in_front_is_the_film():
    theirs = Film("Faithful", "2016")
    found = _found(Film("The Ember Saga: Faithful", "2016"), theirs)
    assert (found.film, found.how) == (theirs, "title, one part of the name")
    assert not _found(Film("The Ember Saga: Faithful", "2017"), theirs).matched


def test_the_first_part_of_a_name_is_the_film():
    theirs = Film("Sandfall", "2021")
    assert _found(Film("Sandfall: Part One", "2021"), theirs).film == theirs


def test_a_long_title_at_the_end_of_another_is_the_film_and_a_short_one_not():
    theirs = Film("Life of a Lantern", "1979")
    assert _found(Film("Moss Company's Life of a Lantern", "1979"), theirs).film == theirs
    assert not _found(Film("Copper Rain", "2018"),
                      Film("After the Copper Rain", "2018")).matched


def test_a_typo_is_the_film_in_the_same_year_only():
    theirs = Film("Thinner Than Air", "2010")
    found = _found(Film("Thinner Tahn Air", "2010"), theirs)
    assert (found.film, found.how) == (theirs, "title misspelt")
    assert not _found(Film("Spirits", "2019"), Film("Spirity", "2020")).matched
    assert not _found(Film("The Hounds", "2022"), Film("The Hound", "2023")).matched


def test_a_title_in_another_script_meets_nothing_written_in_latin():
    # The Japanese title folds to the only thing in it the fold can read, the
    # number - which is all a Latin "1.0" is too.
    assert not _found(Film("Kaiju Zero", "2023", aliases=("カイジュウ-1.0",)),
                      Film("1.0", "2023")).matched


def test_two_equally_good_films_are_ambiguous():
    one, other = Film("The Harbour Tales", "1977"), Film("Harbour Tales, The", "1977")
    found = _found(Film("Harbour Tales", "1977"), one, other)
    assert not found.matched
    assert set(found.ambiguous) == {one, other}


def test_the_one_written_identically_settles_a_pair():
    exact = Film("Harbour Tales", "1977")
    found = _found(Film("Harbour Tales", "1977"), exact,
                   Film("The Harbour Tales", "1977"))
    assert found.film == exact


def test_nothing_like_it_is_no_match():
    found = _found(Film("Lantern", "2007"), Film("Lanterns of Ghent", "2016"))
    assert not found.matched and not found.ambiguous


def test_a_weak_match_holds_only_both_ways():
    plex = FilmIndex([Film("Hollow", "2020"), Film("HOLLOW", "2021")])
    theirs = FilmIndex([Film("HOLLOW", "2021")])
    assert not theirs.find_from(Film("Hollow", "2020"), plex).matched
    assert theirs.find_from(Film("HOLLOW", "2021"), plex).matched


def test_copies_of_one_film_each_hold_both_ways():
    plex = FilmIndex([Film("Lantern", "2007"), Film("Lantern", "2007")])
    found = FilmIndex([Film("Lantern", "2008")]).find_from(Film("Lantern", "2007"), plex)
    assert found.matched


def test_an_id_needs_no_way_back():
    plex = FilmIndex([Film("Other", "1990", tmdb="5"), Film("Same", "2001")])
    found = FilmIndex([Film("Same", "2001", tmdb="5")]).find_from(
        Film("Other", "1990", tmdb="5"), plex)
    assert found.how == "id"
