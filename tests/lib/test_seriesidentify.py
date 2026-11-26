"""medialib/lib/seriesidentify.py: the ladder that names a show, against a
catalogue written for each case (TMDb's and TheTVDB's answers stood in for
at their one way out to the network). Every show here is invented."""

import json

import pytest

from medialib.lib import seriesidentify, tmdbclient, tvdbclient

pytestmark = pytest.mark.pure


class Catalogue:
    """TMDb and TheTVDB as one dict of {path: answer}; a question nobody
    wrote an answer for is no answer."""

    def __init__(self, monkeypatch, tmdb=None, tvdb=None):
        self.tmdb = tmdb or {}
        self.tvdb = tvdb or {}
        self.asked = []
        monkeypatch.setenv("tmdbApiKey", "k")
        monkeypatch.setattr(tmdbclient, "_curl", self._tmdb)
        monkeypatch.setattr(tvdbclient, "_get", self._tvdb)
        seriesidentify.imdb_of.cache_clear()

    def _tmdb(self, url, params):
        path = url[len(tmdbclient._BASE):]
        self.asked.append(path)
        query = dict(params).get("query")
        key = "%s?%s" % (path, query) if query else path
        answer = self.tmdb.get(key)
        return None if answer is None else json.dumps(answer)

    def _tvdb(self, path, params=()):
        query = dict(params).get("query")
        key = "%s?%s" % (path, query) if query else path
        return self.tvdb.get(key, {})


def _tmdb_show(number, name, year, seasons, episodes=None, imdb="tt1",
               tvdb=9):
    detail = {"id": number, "name": name, "original_name": name,
              "first_air_date": "%s-01-01" % year,
              "original_language": "en",
              "seasons": [{"season_number": s, "episode_count": c}
                          for s, c in seasons.items()],
              "external_ids": {"imdb_id": imdb, "tvdb_id": tvdb}}
    answers = {"/tv/%d" % number: detail}
    for season, rows in (episodes or {}).items():
        answers["/tv/%d/season/%d" % (number, season)] = {"episodes": [
            {"episode_number": n, "name": title, "runtime": runtime,
             "air_date": aired}
            for n, title, runtime, aired in rows]}
    return answers


def _disk(episodes, durations=None):
    return seriesidentify.OnDisk(episodes, durations or {})


class TestTheStructureCheck:
    def test_seasons_have_to_hold_the_episodes(self):
        assert seriesidentify.fits({1: 10, 2: 8}, _disk({1: {1, 10}, 2: {8}}))
        assert not seriesidentify.fits({1: 10}, _disk({1: {1}, 2: {1}}))
        assert not seriesidentify.fits({1: 6}, _disk({1: {7}}))

    def test_specials_are_asked_only_of_a_catalogue_that_keeps_them(self):
        assert seriesidentify.fits({1: 3}, _disk({0: {1}, 1: {1}}))
        assert not seriesidentify.fits({0: 1, 1: 3}, _disk({0: {2}, 1: {1}}))


class TestTheLadder:
    def test_one_show_that_fits_is_named(self, monkeypatch):
        answers = {"/search/tv?Moss Harbour": {"results": [
            {"id": 11, "name": "Moss Harbour"}]}}
        answers.update(_tmdb_show(11, "Moss Harbour", 2005, {1: 10}))
        Catalogue(monkeypatch, tmdb=answers)
        result = seriesidentify.identify("Moss Harbour", "2005",
                                         _disk({1: {1, 2}}))
        assert result.show.tag_provider() == ("tmdb", "11")
        assert result.show.imdb == "tt1"
        assert result.show.tvdb == "9"

    def test_a_namesake_whose_seasons_do_not_fit_is_passed_over(self, monkeypatch):
        answers = {"/search/tv?Moss Harbour": {"results": [
            {"id": 11, "name": "Moss Harbour"},
            {"id": 12, "name": "Moss Harbour"}]}}
        answers.update(_tmdb_show(11, "Moss Harbour", 1990, {1: 6}))
        answers.update(_tmdb_show(12, "Moss Harbour", 2005, {1: 10, 2: 10}))
        Catalogue(monkeypatch, tmdb=answers)
        result = seriesidentify.identify("Moss Harbour", "",
                                         _disk({1: {1}, 2: {9}}))
        assert result.show.id == "12"

    def test_two_that_fit_are_told_apart_by_the_runtimes(self, monkeypatch):
        answers = {"/search/tv?Moss Harbour": {"results": [
            {"id": 11, "name": "Moss Harbour"},
            {"id": 12, "name": "Moss Harbour"}]}}
        answers.update(_tmdb_show(11, "Moss Harbour", 1990, {1: 6},
                                  {1: [(1, "A", 25, "")]}))
        answers.update(_tmdb_show(12, "Moss Harbour", 2005, {1: 6},
                                  {1: [(1, "B", 55, "")]}))
        Catalogue(monkeypatch, tmdb=answers)
        result = seriesidentify.identify("Moss Harbour", "",
                                         _disk({1: {1}}, {(1, 1): 54 * 60}))
        assert result.show.id == "12"

    def test_two_nothing_tells_apart_are_ambiguous(self, monkeypatch):
        answers = {"/search/tv?Moss Harbour": {"results": [
            {"id": 11, "name": "Moss Harbour"},
            {"id": 12, "name": "Moss Harbour"}]}}
        answers.update(_tmdb_show(11, "Moss Harbour", 1990, {1: 6}))
        answers.update(_tmdb_show(12, "Moss Harbour", 2005, {1: 6}))
        Catalogue(monkeypatch, tmdb=answers)
        result = seriesidentify.identify("Moss Harbour", "", _disk({1: {1}}))
        assert result.show is None
        assert len(result.ambiguous) == 2

    def test_tvdb_is_asked_when_tmdb_names_none_and_its_order_is_said(
            self, monkeypatch):
        monkeypatch.setenv("tvdbApiKey", "t")
        episodes = [{"seasonNumber": 1, "number": n, "name": "E%d" % n}
                    for n in range(1, 13)]
        tvdb = {"/search?Moss Harbour": {"data": [
                    {"tvdb_id": "77", "name": "Moss Harbour"}]},
                "/series/77/extended": {"data": {
                    "id": 77, "name": "Moss Harbour", "year": "2005",
                    "originalLanguage": "eng"}},
                "/series/77/episodes/default": {"data": {"episodes": episodes[:6]}},
                "/series/77/episodes/dvd": {"data": {"episodes": episodes}}}
        Catalogue(monkeypatch, tvdb=tvdb)
        result = seriesidentify.identify("Moss Harbour", "",
                                         _disk({1: {12}}))
        assert result.show.tag_provider() == ("tvdb", "77")
        assert result.show.order == "dvd"

    def test_without_a_tvdb_key_tvdb_is_not_asked(self, monkeypatch):
        monkeypatch.delenv("tvdbApiKey", raising=False)
        Catalogue(monkeypatch)
        result = seriesidentify.identify("Moss Harbour", "", _disk({1: {1}}))
        assert result.show is None


class TestTheShow:
    def test_an_episode_is_found_by_its_air_date(self):
        show = seriesidentify.Show(
            "tmdb", "1", "Moss Harbour",
            episodes={1: [seriesidentify.Episode(1, "A", 0, "2011-11-15"),
                          seriesidentify.Episode(2, "B", 0, "2011-11-16")]})
        assert show.by_date("2011-11-16") == (1, 2)
        assert show.by_date("2011-11-17") is None

    def test_its_languages_are_what_the_remux_keeps(self):
        show = seriesidentify.Show("tmdb", "1", "Moss Harbour",
                                   original_language="de", spoken=("de", "en"))
        languages = seriesidentify.languages_of(show)
        assert (languages.original, languages.spoken) == ("de", ("de", "en"))
