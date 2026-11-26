"""medialib/lib/episodes.py: what a television name says about its season
and episodes, and the token a server reads them by."""

import datetime

import pytest

from medialib.lib import episodes

pytestmark = pytest.mark.pure


class TestReadingEpisodes:
    @pytest.mark.parametrize("name,expected", [
        ("Moss Harbour S01E02 Title.mkv", [(1, 2)]),
        ("moss.harbour.s1e2.mkv", [(1, 2)]),
        ("Moss Harbour - S02E01E02.mkv", [(2, 1), (2, 2)]),
        ("Moss Harbour - s01e02-e04.mkv", [(1, 2), (1, 3), (1, 4)]),
        ("Moss Harbour 1x05.mkv", [(1, 5)]),
        ("Mossbs01e02.mkv", []),
        ("Moss Harbour (2005).mkv", []),
    ])
    def test_the_token_is_read_every_way_it_is_written(self, name, expected):
        assert episodes.episodes_of(name) == expected

    def test_the_span_is_where_the_token_stands(self):
        name = "Moss Harbour - s01e02 - Title"
        start, end = episodes.episode_span(name)
        assert name[start:end] == "s01e02"


class TestSeasonFolders:
    @pytest.mark.parametrize("name,season", [
        ("Season 1", 1), ("Season 01", 1), ("S02", 2), ("Staffel 3", 3),
        ("Series 4", 4), ("Saison 5 (2010)", 5), ("Specials", 0),
        ("Special", 0),
    ])
    def test_a_season_folder_names_its_season(self, name, season):
        assert episodes.season_of_folder(name) == season

    @pytest.mark.parametrize("name", ["S1m0ne (2002)", "Featurettes",
                                      "Moss Harbour (2005)", "Seasonal"])
    def test_other_folders_name_none(self, name):
        assert episodes.season_of_folder(name) is None


class TestOtherReadings:
    def test_a_numbered_episode_without_its_season(self):
        assert episodes.numbered_episode("Episode 4 - The Tide.mkv") == 4
        assert episodes.numbered_episode("Ep. 07.mkv") == 7
        assert episodes.numbered_episode("The Tide.mkv") is None

    @pytest.mark.parametrize("name,day", [
        ("Moss Harbour - 2011-11-15 - Guest.mkv", datetime.date(2011, 11, 15)),
        ("Moss Harbour 2011.11.15.mkv", datetime.date(2011, 11, 15)),
        ("Moss Harbour 15-11-2011.mkv", datetime.date(2011, 11, 15)),
        ("Moss Harbour 2011-13-40.mkv", None),
    ])
    def test_a_daily_show_is_named_by_its_day(self, name, day):
        assert episodes.date_of(name) == day


class TestWritingTheToken:
    def test_plex_writes_it_in_small_letters(self):
        assert episodes.write_episode_token(1, [2]) == "s01e02"

    def test_jellyfin_in_capitals(self):
        assert episodes.write_episode_token(1, [2], "jellyfin") == "S01E02"

    def test_several_episodes_are_a_range(self):
        assert episodes.write_episode_token(2, [3, 2, 4]) == "s02e02-e04"

    def test_a_long_season_widens_the_number(self):
        assert episodes.write_episode_token(1, [101]) == "s01e101"

    def test_no_episode_is_refused(self):
        with pytest.raises(ValueError):
            episodes.write_episode_token(1, [])
