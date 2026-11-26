"""medialib/lib/seriesnames.py: the names a show, a season and an episode are
written under for each server, and read back either way."""

import os

import pytest

from medialib.lib import mediaserver, seriesnames

pytestmark = pytest.mark.pure

PLEX = mediaserver.PROVIDERS["plex"].series
JELLYFIN = mediaserver.PROVIDERS["jellyfin"].series
BASE = "Moss Harbour (2005)"


class TestFolders:
    def test_the_show_folder_carries_its_title_year_and_tag(self):
        assert seriesnames.series_folder_name("Moss Harbour", "2005",
                                              "{tmdb-123}") \
            == "Moss Harbour (2005) {tmdb-123}"

    def test_a_colon_is_written_as_a_dash(self):
        assert seriesnames.safe_title("Moss Harbour: The Return") \
            == "Moss Harbour - The Return"

    def test_a_season_is_padded_and_the_specials_are_season_zero(self):
        assert seriesnames.season_folder_name(1) == "Season 01"
        assert seriesnames.season_folder_name(0) == "Season 00"


class TestEpisodeNames:
    def test_plex(self):
        assert seriesnames.episode_name(BASE, 1, [2], "The Tide",
                                        naming=PLEX) \
            == "Moss Harbour (2005) - s01e02 - The Tide.mkv"

    def test_jellyfin_sets_the_title_off_without_a_hyphen(self):
        assert seriesnames.episode_name(BASE, 1, [2], "The Tide",
                                        naming=JELLYFIN) \
            == "Moss Harbour (2005) S01E02 The Tide.mkv"

    def test_two_episodes_in_one_file(self):
        assert seriesnames.episode_name(BASE, 1, [2, 3], "First",
                                        naming=PLEX) \
            == "Moss Harbour (2005) - s01e02-e03 - First.mkv"

    @pytest.mark.parametrize("naming,name", [
        (PLEX, "Moss Harbour (2005) - s01e02 - The Tide [AV1].mkv"),
        (JELLYFIN, "Moss Harbour (2005) S01E02 The Tide - AV1.mkv"),
    ])
    def test_the_converted_copy_is_a_second_version(self, naming, name):
        assert seriesnames.episode_name(BASE, 1, [2], "The Tide",
                                        version="AV1", naming=naming) == name

    def test_a_long_title_is_shortened_and_nothing_else(self):
        name = seriesnames.episode_name(BASE, 1, [2], "word " * 80,
                                        naming=PLEX)
        assert name.startswith("Moss Harbour (2005) - s01e02 - word")
        assert name.endswith(".mkv")
        assert len(name.encode()) <= seriesnames.EPISODE_NAME_BYTES

    def test_a_whole_path_too_long_shortens_the_title(self):
        folder = os.path.join("x" * 150, "Season 01")
        name = seriesnames.episode_name(BASE, 1, [2], "word " * 30,
                                        naming=PLEX, folder=folder,
                                        path_limit=255)
        assert len(os.path.join(folder, name)) <= 255


class TestReadingBack:
    @pytest.mark.parametrize("name,season,numbers,title,version,part", [
        ("Moss Harbour (2005) - s01e02 - The Tide.mkv", 1, (2,), "The Tide",
         "", ""),
        ("Moss Harbour (2005) S01E02-E03 First - AV1.mkv", 1, (2, 3), "First",
         "AV1", ""),
        ("Moss Harbour (2005) - s01e02 - The Tide [AV1].mkv", 1, (2,),
         "The Tide", "AV1", ""),
        ("Moss Harbour (2005) - s01e02 - The Tide - pt2.mkv", 1, (2,),
         "The Tide", "", "2"),
    ])
    def test_either_spelling(self, name, season, numbers, title, version, part):
        read = seriesnames.read_episode_name(name)
        assert (read.series, read.season, read.episodes, read.title,
                read.version, read.part) \
            == (BASE, season, numbers, title, version, part)

    def test_a_name_without_a_token_is_no_episode(self):
        assert seriesnames.read_episode_name("Moss Harbour.mkv") is None


class TestTheKey:
    def test_an_episode_is_known_by_its_show_and_token(self, tmp_path):
        episode = (tmp_path / "Moss Harbour (2005) [tmdbid-123]" / "Season 01"
                   / "Moss Harbour (2005) S01E02 The Tide.mkv")
        assert seriesnames.series_key(str(episode)) == "{tmdb-123} s01e02"

    def test_a_show_without_an_id_has_no_key(self, tmp_path):
        episode = (tmp_path / "Moss Harbour (2005)" / "Season 01"
                   / "Moss Harbour (2005) - s01e02.mkv")
        assert seriesnames.series_key(str(episode)) == ""


class TestInlineExtras:
    def test_an_episodes_extra_is_named_after_it(self):
        stem = "Moss Harbour (2005) - s01e02 - The Tide"
        assert seriesnames.inline_extra_name(stem, "Deleted Scenes", 1) \
            == stem + "-deleted.mkv"
        assert seriesnames.inline_extra_name(stem, "Deleted Scenes", 2) \
            == stem + "-deleted2.mkv"
        assert seriesnames.is_inline_extra(stem + "-deleted2.mkv")
        assert not seriesnames.is_inline_extra(stem + ".mkv")
