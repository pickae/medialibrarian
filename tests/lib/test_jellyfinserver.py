"""Tests for medialib.lib.jellyfinserver - a Jellyfin library read as films,
and what is sent to curl to ask and to change it.

The server is not asked: curl is stood in for, answering each path with JSON
shaped the way a Jellyfin 12 server answered it."""

import json
import urllib.parse

import pytest

from medialib.lib import jellyfinserver, mediaserver
from medialib.lib.mediaserver import Movie

pytestmark = pytest.mark.pure

_USER = "0123456789abcdef0123456789abcdef"

_ANSWERS = {
    "/Users": [{"Name": "someone", "Id": _USER}],
    "/UserViews": {"Items": [
        {"Name": "Films", "Id": "lib1", "CollectionType": "movies"},
        {"Name": "Series", "Id": "lib2", "CollectionType": "tvshows"}]},
    "/Items": {"Items": [
        {"Name": "Watch Out, the Boss Is Back!", "OriginalTitle": "Achtung, der Chef",
         "Id": "a1", "ProductionYear": 1991,
         "ProviderIds": {"Imdb": "tt0101000", "Tmdb": "4242"},
         "UserData": {"Rating": 7.0, "Played": True, "PlayCount": 2,
                      "LastPlayedDate": "2024-05-01T12:00:00.0000000Z"},
         "MediaSources": [
             {"Name": "Watch Out (1991) [imdbid-tt0101000]",
              "Path": "/films/Watch Out (1991) [imdbid-tt0101000]/"
                      "Watch Out (1991) [imdbid-tt0101000].mkv"},
             {"Name": "AV1",
              "Path": "/films/Watch Out (1991) [imdbid-tt0101000]/"
                      "Watch Out (1991) [imdbid-tt0101000] - AV1.mkv"}]},
        {"Name": "Lantern", "Id": "b2", "ProductionYear": 2007, "PartCount": 2,
         "ProviderIds": {},
         "UserData": {"Played": False, "PlayCount": 0},
         "MediaSources": [{"Name": "Lantern (2007) Part1",
                           "Path": "/films/Lantern (2007)/Lantern (2007) Part1.mkv"}]},
    ]},
    "/Videos/b2/AdditionalParts": {"Items": [
        {"Path": "/films/Lantern (2007)/Lantern (2007) Part2.mkv"}]},
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
        path = urllib.parse.urlsplit(address).path
        answer = _ANSWERS.get(path)
        return Done(json.dumps(answer).encode() if answer is not None else b"")

    monkeypatch.setattr(jellyfinserver.subprocess, "run", run)
    return sent


def _server(user="someone"):
    return jellyfinserver.Server("http://jelly:8096", "k", user)


def _query(config):
    address = config.split("\n", 1)[0].split('"')[1]
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(address).query))


def test_the_films_carry_their_ids_rating_and_last_play(curl):
    played, unplayed = _server().movies()
    assert played.key == "a1" and played.played and played.section == "Films"
    film = played.film
    assert (film.title, film.year, film.tmdb, film.imdb, film.rating) == (
        "Watch Out, the Boss Is Back!", "1991", "4242", "tt0101000", 7)
    assert film.aliases == ("Achtung, der Chef",)
    assert film.dates == (jellyfinserver._day("2024-05-01T12:00:00.0000000Z"),)
    assert not unplayed.played and unplayed.film.dates == ()


def test_only_the_movie_libraries_of_the_user_named(curl):
    assert _server().movie_sections() == [("lib1", "Films")]
    assert _query(curl[-1])["userId"] == _USER


def test_a_user_named_nowhere_is_refused(curl):
    with pytest.raises(jellyfinserver.JellyfinError, match="someone"):
        _server("nobody").movie_sections()


def test_with_no_user_named_the_only_one_is_taken(curl):
    assert _server("").user_id() == _USER


def test_the_key_goes_to_curl_on_stdin_and_not_in_the_address(curl):
    _server().movie_sections()
    assert 'Authorization: MediaBrowser Token=\\"k\\"' in curl[0]
    assert "k" not in _query(curl[0]).values()


def test_every_version_with_its_label_and_every_part(curl):
    plain, converted, split = _server().copies()
    assert plain.edition == "" and converted.edition == "AV1"
    assert split.files == ("/films/Lantern (2007)/Lantern (2007) Part1.mkv",
                           "/films/Lantern (2007)/Lantern (2007) Part2.mkv")


def test_a_film_is_marked_played_on_its_day(curl):
    server = _server()
    movie = Movie("a1", None, False, "Films")
    assert server.mark_played(movie, "2020-07-08") == "2020-07-08"
    query = _query(curl[-1])
    assert "/UserPlayedItems/a1" in curl[-1] and 'request = "POST"' in curl[-1]
    assert query["datePlayed"] == "2020-07-08T12:00:00.000Z"


def test_a_film_marked_with_no_day_is_stamped_today(curl):
    day = _server().mark_played(Movie("a1", None, False, "Films"))
    assert day and _query(curl[-1])["datePlayed"].startswith(day)


def test_a_rating_is_posted_as_the_user_s_own(curl):
    _server().rate(Movie("a1", None, False, "Films"), 8)
    assert "/UserItems/a1/UserData" in curl[-1]
    assert 'data-binary = "{\\"Rating\\": 8}"' in curl[-1]


def test_there_is_no_watchlist(curl):
    server = _server()
    assert server.watchlist() == [] and server.catalogue_key(None) == ""
    with pytest.raises(jellyfinserver.JellyfinError):
        server.add_to_watchlist("x")


def test_the_library_keeps_every_day_a_film_was_seen_played(curl):
    server = _server()
    first = server.library(None)
    entry = next(e for e in first.cache["movies"] if e["key"] == "a1")
    entry["dates"] = ["2019-01-01"]
    again = server.library(first.cache)
    film = next(m for m in again.movies if m.key == "a1").film
    assert "2019-01-01" in film.dates and len(film.dates) == 2
    assert again.cache["provider"] == "jellyfin" and again.whole


def test_a_day_the_run_stamped_is_no_day_watched(curl):
    server = _server()
    first = server.library(None)
    stamped = jellyfinserver._day("2024-05-01T12:00:00.0000000Z")
    movie = next(m for m in first.movies if m.key == "a1")
    mediaserver.updated(first.cache, movie, stamped)
    again = server.library(first.cache)
    assert next(m for m in again.movies if m.key == "a1").film.dates == ()
    assert stamped in next(e for e in again.cache["movies"]
                           if e["key"] == "a1")["marked"]


def test_a_cache_from_another_server_is_not_read(curl):
    server = _server()
    other = server.library(None).cache
    other["movies"][0]["dates"] = ["2019-01-01"]
    other["provider"] = "plex"
    film = next(m for m in server.library(other).movies if m.key == "a1").film
    assert "2019-01-01" not in film.dates


def test_a_moment_is_read_as_its_local_day():
    assert jellyfinserver._day("") == ""
    assert jellyfinserver._day("not a date") == ""
    assert jellyfinserver._day("2024-05-01T12:00:00.0000000Z").startswith("2024-05-0")


def test_no_server_until_the_key_is_set(monkeypatch):
    monkeypatch.delenv("jellyfinApiKey", raising=False)
    assert jellyfinserver.from_environment() is None
    monkeypatch.setenv("jellyfinApiKey", "k")
    monkeypatch.delenv("jellyfinUrl", raising=False)
    monkeypatch.setenv("jellyfinUser", "someone")
    server = jellyfinserver.from_environment()
    assert server.url == jellyfinserver.LOCAL and server.user == "someone"
    assert jellyfinserver.from_environment("other").user == "other"
