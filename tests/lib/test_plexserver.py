"""Tests for medialib.lib.plexserver - a Plex library and its play history
read as films, and what is sent to curl to ask and to change them.

The server is not asked: curl is stood in for, answering each path with the
XML a real server answered it with."""

import pytest

from medialib.lib import plexserver
from medialib.lib.filmmatch import Film

pytestmark = pytest.mark.pure

_ANSWERS = {
    "/library/sections": """<MediaContainer>
<Directory key="2" type="movie" title="Films"/>
<Directory key="3" type="show" title="Series"/></MediaContainer>""",
    "/library/sections/2/all": """<MediaContainer>
<Video ratingKey="11" title="Watch Out, the Boss Is Back!" originalTitle="Achtung, der Chef"
       year="1991" viewCount="2" userRating="7.0" lastViewedAt="1791921616">
  <Guid id="imdb://tt0101000"/><Guid id="tmdb://4242"/><Guid id="tvdb://9"/>
</Video>
<Video ratingKey="12" title="Lantern" year="2007"/>
</MediaContainer>""",
    # Two plays of one film on one day are one diary day; somebody else's
    # play, and an episode's, are not this account's films.
    "/status/sessions/history/all": """<MediaContainer>
<Video ratingKey="11" type="movie" viewedAt="1700000000" accountID="1"/>
<Video ratingKey="11" type="movie" viewedAt="1700000100" accountID="1"/>
<Video ratingKey="11" type="movie" viewedAt="1600000000" accountID="1"/>
<Video ratingKey="12" type="movie" viewedAt="1700000000" accountID="2"/>
<Video ratingKey="99" type="episode" viewedAt="1700000000" accountID="1"/>
</MediaContainer>""",
}


# Three films and a show, newest first, over two pages.
_WATCHLIST = {
    0: """<MediaContainer totalSize="4">
<Video type="movie" ratingKey="c3" title="Lantern" year="2007"><Guid id="tmdb://3"/></Video>
<Directory type="show" ratingKey="s1" title="Harbour Tales"/>
</MediaContainer>""",
    2: """<MediaContainer totalSize="4">
<Video type="movie" ratingKey="c2" title="Sandfall" year="2021"/>
<Video type="movie" ratingKey="c1" title="Hollow Night" year="2018"/>
</MediaContainer>""",
}


@pytest.fixture
def curl(monkeypatch):
    """The configs curl was handed, one per call, answered from _ANSWERS."""
    sent = []

    class Done:
        returncode = 0
        stderr = b""

        def __init__(self, stdout):
            self.stdout = stdout

    def run(argv, input=b"", **_kwargs):
        config = input.decode()
        sent.append(config)
        address = config.split("\n", 1)[0].split('"')[1]
        path = address.split("32400", 1)[1] if "32400" in address else address
        if "Container-Start=" in config and "watchlist" in path:
            start = int(config.split("X-Plex-Container-Start=", 1)[1].split('"')[0])
            return Done(_WATCHLIST[start].encode())
        return Done(_ANSWERS.get(path, "").encode())

    monkeypatch.setattr(plexserver.subprocess, "run", run)
    return sent


def test_the_films_carry_their_ids_rating_and_play_days(curl):
    played, unplayed = plexserver.Server("http://plex:32400", "t").movies()
    assert played.key == "11" and played.played and played.section == "Films"
    assert played.film == Film(
        "Watch Out, the Boss Is Back!", "1991", tmdb="4242", imdb="tt0101000",
        aliases=("Achtung, der Chef",), rating=7,
        dates=(plexserver._day("1600000000"), plexserver._day("1700000000")))
    assert unplayed == plexserver.Movie("12", Film("Lantern", "2007"), False, "Films")


def test_only_the_libraries_named(curl):
    assert plexserver.Server("http://plex:32400", "t").movies(("Other",)) == []


def test_the_token_goes_to_curl_on_stdin(monkeypatch, curl):
    argv_seen = []
    real = plexserver.subprocess.run
    monkeypatch.setattr(plexserver.subprocess, "run",
                        lambda argv, **kw: argv_seen.append(argv) or real(argv, **kw))
    plexserver.Server("http://plex:32400/", "s3cret").movie_sections()
    assert all("s3cret" not in " ".join(argv) for argv in argv_seen)
    assert 'header = "X-Plex-Token: s3cret"' in curl[0]
    assert 'url = "http://plex:32400/library/sections"' in curl[0]


def test_a_rating_is_put(curl):
    plexserver.Server("http://plex:32400", "t").rate(
        plexserver.Movie("12", Film("Lantern"), False, "Films"), 8)
    assert 'request = "PUT"' in curl[0]
    assert 'data-urlencode = "rating=8"' in curl[0]
    assert 'data-urlencode = "key=12"' in curl[0]


def test_no_server_until_both_are_set(monkeypatch):
    monkeypatch.delenv("plexToken", raising=False)
    monkeypatch.setenv("plexUrl", "http://plex:32400")
    assert plexserver.from_environment() is None


def test_the_watchlist_is_read_page_by_page_and_its_shows_left_out(curl):
    wanted = plexserver.Server("http://plex:32400", "t").watchlist()
    assert [(entry.key, entry.film.title, entry.film.tmdb) for entry in wanted] == [
        ("c3", "Lantern", "3"), ("c2", "Sandfall", ""), ("c1", "Hollow Night", "")]


def test_a_known_watchlist_is_read_only_up_to_what_it_knows(curl):
    known = [plexserver.Wanted("c2", Film("Sandfall", "2021")),
             plexserver.Wanted("c1", Film("Hollow Night", "2018"))]
    wanted = plexserver.Server("http://plex:32400", "t").watchlist(known)
    assert [entry.key for entry in wanted] == ["c3", "c2", "c1"]
    assert len(curl) == 2       # both pages, stopping at the first known film


def test_a_cached_library_reads_only_what_changed(curl):
    server = plexserver.Server("http://plex:32400", "t")
    whole = server.library(None)
    assert whole.whole and len(whole.movies) == 2
    curl.clear()
    again = server.library(whole.cache)
    assert not again.whole
    assert {movie.key: movie for movie in again.movies} == \
        {movie.key: movie for movie in whole.movies}
    asked = " ".join(curl)
    assert all(field + ">>=" in asked for field in plexserver._CHANGED)
    assert "viewedAt>=" in asked


def test_a_cache_from_elsewhere_or_too_old_is_read_whole(curl):
    server = plexserver.Server("http://plex:32400", "t")
    cache = server.library(None).cache
    assert server.library(dict(cache, server="http://other:32400")).whole
    assert server.library(dict(cache, full=0)).whole
    assert server.library(cache, whole=True).whole


def test_a_film_is_found_in_plex_s_catalogue_by_its_tmdb_id(curl, monkeypatch):
    monkeypatch.setitem(_ANSWERS,
                        "https://metadata.provider.plex.tv/library/metadata/matches",
                        '<MediaContainer><Video ratingKey="c9"/></MediaContainer>')
    server = plexserver.Server("http://plex:32400", "t")
    assert server.catalogue_key(Film("Lantern", "2007", tmdb="3")) == "c9"
    assert 'guid=tmdb://3' in curl[-1]
    assert server.catalogue_key(Film("Lantern", "2007")) == ""
    server.add_to_watchlist("c9")
    assert 'request = "PUT"' in curl[-1] and "ratingKey=c9" in curl[-1]


def test_another_account_s_plays_when_asked(curl):
    server = plexserver.Server("http://plex:32400", "t", account="2")
    lantern = [movie for movie in server.movies() if movie.key == "12"][0]
    assert lantern.film.dates == (plexserver._day("1700000000"),)
    assert 'accountID=2' in " ".join(curl)
