"""ingest-series' tagging: a show named from the catalogue, its seasons and
episodes renamed with their sidecars, the dry run, the lists and -i. The
catalogue is stood in for at TMDb's one way out; every show is invented."""

import json
import os

import pytest

from medialib.cli import ingest_series as rules
from medialib.cli import ingest_series_run as run
from medialib.lib import seriesidentify, tmdbclient, tooldeps, tvdbclient
from medialib.lib.safety import SkipLog

pytestmark = pytest.mark.fs


def _touch(root, *paths):
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")


def _files(root):
    return sorted(os.path.relpath(os.path.join(parent, name), root)
                  .replace(os.sep, "/")
                  for parent, _dirs, names in os.walk(root) for name in names)


SHOW = seriesidentify.Show(
    "tmdb", "123", "Moss Harbour", "2005", ("Moss Harbour",), "en", (),
    "tt7", "123", "77", "aired", {1: 3},
    {1: [seriesidentify.Episode(1, "Pilot"),
         seriesidentify.Episode(2, "The Tide: Part One"),
         seriesidentify.Episode(3, "Low Water")]})


@pytest.fixture
def catalogue(monkeypatch):
    """TMDb answering for Moss Harbour (2005), and nothing else."""
    answers = {
        "/search/tv": {"results": [{"id": 123, "name": "Moss Harbour"}]},
        "/tv/123": {"id": 123, "name": "Moss Harbour",
                    "original_name": "Moss Harbour",
                    "first_air_date": "2005-03-01", "original_language": "en",
                    "seasons": [{"season_number": 1, "episode_count": 3}],
                    "external_ids": {"imdb_id": "tt7", "tvdb_id": 77}},
        "/tv/123/season/1": {"episodes": [
            {"episode_number": 1, "name": "Pilot"},
            {"episode_number": 2, "name": "The Tide: Part One"},
            {"episode_number": 3, "name": "Low Water"}]},
    }

    def curl(url, params):
        path = url[len(tmdbclient._BASE):]
        if path == "/search/tv" and dict(params).get("query") != "Moss Harbour":
            return json.dumps({"results": []})
        return json.dumps(answers[path]) if path in answers else None

    monkeypatch.setenv("tmdbApiKey", "k")
    monkeypatch.delenv("tvdbApiKey", raising=False)
    monkeypatch.setenv("mediaServerNaming", "plex")
    monkeypatch.setattr(tmdbclient, "_curl", curl)
    monkeypatch.setattr(tooldeps, "require_tools", lambda *a, **k: 0)
    tvdbclient.reset()


class TestThePlan:
    def test_episodes_sidecars_seasons_and_the_show(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.setenv("mediaServerNaming", "plex")
        show = tmp_path / "moss.harbour"
        _touch(show, "Season 1/Moss.Harbour.S01E02.1080p.mkv",
               "Season 1/Moss.Harbour.S01E02.1080p.en.srt",
               "Season 1/Moss.Harbour.S01E02.1080p - old.mkv",
               "Season 1/Moss.Harbour.S01E02.1080p-deleted.mkv",
               "Season 1/Moss.Harbour.S01E01E03.mkv")
        renames = rules.plan_renames(str(show), SHOW)
        rules.carry_out(renames, SkipLog(), lambda line: None)
        assert _files(tmp_path) == sorted([
            "Moss Harbour (2005) {tmdb-123}/Season 01/"
            "Moss Harbour (2005) - s01e01-e03 - Pilot.mkv",
            "Moss Harbour (2005) {tmdb-123}/Season 01/"
            "Moss Harbour (2005) - s01e02 - The Tide - Part One - old.mkv",
            "Moss Harbour (2005) {tmdb-123}/Season 01/"
            "Moss Harbour (2005) - s01e02 - The Tide - Part One-deleted.mkv",
            "Moss Harbour (2005) {tmdb-123}/Season 01/"
            "Moss Harbour (2005) - s01e02 - The Tide - Part One.en.srt",
            "Moss Harbour (2005) {tmdb-123}/Season 01/"
            "Moss Harbour (2005) - s01e02 - The Tide - Part One.mkv",
        ])

    def test_a_named_library_has_nothing_left_to_do(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.setenv("mediaServerNaming", "plex")
        show = tmp_path / "Moss Harbour (2005) {tmdb-123}"
        _touch(show, "Season 01/Moss Harbour (2005) - s01e03 - Low Water.mkv")
        assert rules.plan_renames(str(show), SHOW) == []

    def test_jellyfin_names(self, tmp_path, monkeypatch):
        monkeypatch.setenv("mediaServerNaming", "jellyfin")
        show = tmp_path / "Moss Harbour"
        _touch(show, "Season 01/x S01E03.mkv")
        targets = [os.path.basename(rename.target)
                   for rename in rules.plan_renames(str(show), SHOW)]
        assert targets == ["Moss Harbour (2005) S01E03 Low Water.mkv",
                           "Moss Harbour (2005) [tmdbid-123]"]

    def test_a_tvdb_show_in_another_order_says_what_to_set(self):
        hint = rules.order_hint(SHOW._replace(source="tvdb", order="dvd"))
        assert "DVD order" in hint
        assert rules.order_hint(SHOW) == ""

    def test_two_files_of_one_episode_are_a_duplicate(self, tmp_path):
        show = tmp_path / "Moss Harbour"
        _touch(show, "Season 01/a S01E01.mkv", "Season 01/b S01E01.mkv")
        renames = rules.plan_renames(str(show), SHOW)
        assert len(rules.duplicate_targets(renames)) == 1


class TestTheRun:
    def test_a_dry_run_renames_nothing_and_lists_it(self, tmp_path, catalogue):
        library = tmp_path / "lib"
        _touch(library, "Moss Harbour/Season 01/Moss.Harbour.S01E01.mkv",
               "Copper Gate/Season 01/Copper.Gate.S01E01.mkv")
        before = _files(library)
        logs = str(tmp_path / "logs-home")
        status = run._tags_only("ingest-series", logs, [str(library)], ["Lib"],
                                False, "")
        assert status == 0
        assert _files(library) == before
        found = _files(logs)
        assert any(name.endswith("ingest-series-renames-Lib.txt")
                   for name in found)
        unmatched = [name for name in found
                     if name.endswith("ingest-series-unmatched-Lib.tsv")]
        assert unmatched
        with open(os.path.join(logs, unmatched[0]), encoding="utf-8") as handle:
            text = handle.read()
        assert "Copper Gate\t" in text

    def test_write_renames(self, tmp_path, catalogue):
        _touch(tmp_path, "Moss Harbour/Season 1/Moss.Harbour.S01E03.mkv")
        run._tags_only("ingest-series", "", [str(tmp_path)], ["Lib"], True, "")
        assert _files(tmp_path) == [
            "Moss Harbour (2005) {tmdb-123}/Season 01/"
            "Moss Harbour (2005) - s01e03 - Low Water.mkv"]

    def test_an_id_list_names_a_show_the_search_could_not(self, tmp_path,
                                                          catalogue):
        _touch(tmp_path, "MH/Season 01/MH S01E01.mkv")
        ids = tmp_path / "ids.tsv"
        ids.write_text("# a comment\nMH\t123\n", encoding="utf-8")
        run._tags_only("ingest-series", "", [str(tmp_path / "")],
                       ["Lib"], True, str(ids))
        assert "Moss Harbour (2005) {tmdb-123}" in os.listdir(tmp_path)


class TestTheIdList:
    @pytest.mark.parametrize("text,parsed", [
        ("123", ("tmdb", "123")), ("tmdb-123", ("tmdb", "123")),
        ("tvdb-77", ("tvdb", "77")), ("{tvdb-77}", ("tvdb", "77")),
        ("tt0000007", ("imdb", "tt0000007")), ("hello", ("", "")),
    ])
    def test_every_way_an_id_is_written(self, text, parsed):
        assert rules.id_tag_for(text) == parsed

    def test_written_and_read_back(self, tmp_path):
        path = str(tmp_path / "ids.tsv")
        rules.write_id_list(path, ["Copper Gate"], {"MH": ("tvdb", "77")})
        blank: list = []
        assert rules.read_id_list(path, blank) == {"MH": ("tvdb", "77")}
        assert blank == ["Copper Gate"]
