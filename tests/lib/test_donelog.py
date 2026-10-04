"""Tests for medialib.lib.donelog - the films a heavy phase has finished,
kept across runs by id."""

import pytest

from medialib.lib import donelog

pytestmark = pytest.mark.fs

TAGGED = "Films/The Movie (1999)/The Movie (1999) {imdb-tt0000001}.mkv"


def test_a_film_added_is_known_to_the_next_run_wherever_it_has_moved(
        tmp_path):
    path = str(tmp_path / "logs" / "done.txt")
    assert donelog.DoneLog(path).add(TAGGED)
    later = donelog.DoneLog(path)
    assert later.has("Elsewhere/Movie {imdb-tt0000001}/Movie {imdb-tt0000001}"
                     ".mkv")
    assert not later.has("Films/Other {imdb-tt0000002}/Other.mkv")


def test_an_untagged_film_is_never_recorded(tmp_path):
    done = donelog.DoneLog(str(tmp_path / "done.txt"))
    assert not done.add("Films/The Movie (1999)/The Movie (1999).mkv")
    assert not (tmp_path / "done.txt").exists()


def test_each_film_is_written_once_with_its_path(tmp_path):
    path = tmp_path / "done.txt"
    done = donelog.DoneLog(str(path))
    done.add(TAGGED)
    done.add(TAGGED)
    assert path.read_text(encoding="utf-8") == "{imdb-tt0000001}\t%s\n" % TAGGED


def test_comments_and_blank_lines_are_not_films(tmp_path):
    path = tmp_path / "done.txt"
    path.write_text("# a note\n\n{tmdb-7}\n", encoding="utf-8")
    assert len(donelog.DoneLog(str(path))) == 1
