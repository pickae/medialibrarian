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


# The films are recorded for the languages their run was told to do, and a
# later run skips a film only when that record holds every language it wants.
def test_a_line_without_a_scope_is_read_as_the_default(tmp_path):
    """The lines an earlier run wrote name no languages: they were done for
    the languages that run fetched, which is the table's default."""
    path = tmp_path / "done.txt"
    path.write_text("{imdb-tt0000001}\t%s\n" % TAGGED, encoding="utf-8")
    done = donelog.DoneLog(str(path), default_scope=("en", "de"))
    assert done.has(TAGGED, ("en",))
    assert done.has(TAGGED, ("en", "de"))
    assert not done.has(TAGGED, ("pt",))


def test_a_film_is_done_only_for_what_its_record_says(tmp_path):
    path = str(tmp_path / "done.txt")
    assert donelog.DoneLog(path).add(TAGGED, ("en", "pt"))
    later = donelog.DoneLog(path)
    assert later.has(TAGGED, ("en",))
    assert later.has(TAGGED, ("pt", "en"))
    assert not later.has(TAGGED, ("de",))
    assert not later.has(TAGGED, ("de", "en"))


def test_the_recorded_scope_is_in_the_table_s_order(tmp_path):
    path = tmp_path / "done.txt"
    donelog.DoneLog(str(path)).add(TAGGED, ("pt", "en"))
    assert path.read_text(encoding="utf-8") == \
        "{imdb-tt0000001}\t%s\ten,pt\n" % TAGGED


def test_a_wider_run_rewrites_the_recorded_scope_in_place(tmp_path):
    path = tmp_path / "done.txt"
    donelog.DoneLog(str(path)).add(TAGGED, ("en",))
    again = donelog.DoneLog(str(path))
    assert again.add(TAGGED, ("en", "de"))
    assert len(again) == 1
    assert again.has(TAGGED, ("de",))
    later = donelog.DoneLog(str(path))
    assert later.has(TAGGED, ("de", "en"))
    assert path.read_text(encoding="utf-8") == \
        "{imdb-tt0000001}\t%s\ten,de\n" % TAGGED


def test_a_run_no_wider_than_the_record_leaves_it_alone(tmp_path):
    path = tmp_path / "done.txt"
    donelog.DoneLog(str(path)).add(TAGGED, ("en", "de"))
    before = path.read_text(encoding="utf-8")
    again = donelog.DoneLog(str(path))
    assert again.add(TAGGED, ("en",))
    assert path.read_text(encoding="utf-8") == before


def test_a_rewritten_line_leaves_the_other_films_where_they_are(tmp_path):
    path = tmp_path / "done.txt"
    other = "Films/Other {imdb-tt0000002}/Other {imdb-tt0000002}.mkv"
    donelog.DoneLog(str(path)).add(other, ("en",))
    again = donelog.DoneLog(str(path))
    again.add(TAGGED, ("en",))
    again.add(TAGGED, ("en", "pt"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "{imdb-tt0000002}" in lines[0]
    assert lines[1].endswith("en,pt")


def test_a_record_follows_the_film_across_a_server_switch(tmp_path):
    """The film's name is retagged for the other server: the record a run
    kept naming it the way the names used to be written still answers it."""
    path = str(tmp_path / "done.txt")
    assert donelog.DoneLog(path).add(TAGGED)
    later = donelog.DoneLog(path)
    assert later.has("Films/The Movie (1999) [imdbid-tt0000001]/"
                     "The Movie (1999) [imdbid-tt0000001].mkv")
    assert not later.has("Films/The Movie (1999) [imdbid-tt0000001]/"
                         "The Movie (1999) [imdbid-tt0000001] - Colorized.mkv")
