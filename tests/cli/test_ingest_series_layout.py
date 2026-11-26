"""ingest-series' layout: loose episodes and season packs into their shows,
seasons into Season NN, extras where the servers read them, subtitles beside
their episode - and the film a show folder must not be mistaken for. Every
show here is invented."""

import os

import pytest

from medialib.cli import ingest_series as rules
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


class TestReadingARelease:
    @pytest.mark.parametrize("name,title,year", [
        ("Moss.Harbour.S01E02.1080p.WEB.mkv", "Moss Harbour", ""),
        ("Moss Harbour (2005) - S01E02 - The Tide.mkv", "Moss Harbour", "2005"),
        ("Moss.Harbour.2005.S02.1080p.BluRay-GRP", "Moss Harbour", "2005"),
    ])
    def test_the_show_is_what_stands_before_the_token(self, name, title, year):
        assert rules.show_title_of(name) == (title, year)


class TestShowsAndSeasons:
    def test_a_loose_episode_gets_a_folder_for_its_show(self, tmp_path):
        _touch(tmp_path, "Moss.Harbour.S01E02.mkv")
        rules.loose_episodes_into_shows(str(tmp_path), SkipLog())
        assert _files(tmp_path) == ["Moss Harbour/Moss.Harbour.S01E02.mkv"]

    def test_a_season_pack_becomes_its_shows_season(self, tmp_path):
        _touch(tmp_path, "Moss.Harbour.S02.1080p/Moss.Harbour.S02E01.mkv",
               "Moss Harbour/Season 01/Moss.Harbour.S01E01.mkv")
        rules.season_packs_into_shows(str(tmp_path), SkipLog())
        assert _files(tmp_path) == [
            "Moss Harbour/Season 01/Moss.Harbour.S01E01.mkv",
            "Moss Harbour/Season 02/Moss.Harbour.S02E01.mkv"]

    def test_season_folders_are_named_and_merged(self, tmp_path):
        show = tmp_path / "Moss Harbour"
        _touch(show, "S1/a S01E01.mkv", "Season 1/b S01E02.mkv",
               "Staffel 2/c S02E01.mkv", "Specials/d S00E01.mkv")
        rules.season_folders_named(str(show), SkipLog())
        assert _files(show) == ["Season 00/d S00E01.mkv",
                                "Season 01/a S01E01.mkv",
                                "Season 01/b S01E02.mkv",
                                "Season 02/c S02E01.mkv"]

    def test_loose_episodes_go_into_their_season_with_their_sidecars(
            self, tmp_path):
        show = tmp_path / "Moss Harbour"
        _touch(show, "Moss Harbour S01E02.mkv", "Moss Harbour S01E02.en.srt",
               "Moss Harbour S00E01.mkv", "Moss Harbour S01E02-E03.mkv")
        rules.episodes_into_seasons(str(show), SkipLog())
        assert _files(show) == ["Season 00/Moss Harbour S00E01.mkv",
                                "Season 01/Moss Harbour S01E02-E03.mkv",
                                "Season 01/Moss Harbour S01E02.en.srt",
                                "Season 01/Moss Harbour S01E02.mkv"]

    def test_a_target_already_there_is_left_and_recorded(self, tmp_path):
        show = tmp_path / "Moss Harbour"
        _touch(show, "Moss Harbour S01E02.mkv",
               "Season 01/Moss Harbour S01E02.mkv")
        skips = SkipLog()
        rules.episodes_into_seasons(str(show), skips)
        assert len(skips.skips) == 1
        assert (show / "Moss Harbour S01E02.mkv").exists()


class TestExtras:
    def test_extras_are_sorted_and_an_episodes_own_goes_beside_it(
            self, tmp_path):
        show = tmp_path / "Moss Harbour"
        _touch(show, "Season 01/Moss Harbour S01E02.mkv",
               "Extras/Making of the Harbour.mkv",
               "Extras/Photo Gallery.mkv",
               "Extras/S01E02 Deleted Scene.mkv")
        rules.extras_sorted(str(show), SkipLog())
        assert _files(show) == [
            "Behind The Scenes/Making of the Harbour.mkv",
            "Featurettes/Photo Gallery.mkv",
            "Season 01/Moss Harbour S01E02-deleted.mkv",
            "Season 01/Moss Harbour S01E02.mkv"]

    def test_an_extra_in_the_season_folder_beside_its_episode(self, tmp_path):
        show = tmp_path / "Moss Harbour"
        _touch(show, "Season 01/Moss Harbour S01E02.mkv",
               "Season 01/Moss Harbour S01E02 Deleted Scene.mkv")
        rules.extras_sorted(str(show), SkipLog())
        assert _files(show) == ["Season 01/Moss Harbour S01E02-deleted.mkv",
                                "Season 01/Moss Harbour S01E02.mkv"]


class TestSubtitles:
    def test_a_subs_folders_subtitle_goes_beside_its_episode(self, tmp_path):
        show = tmp_path / "Moss Harbour"
        _touch(show, "Season 01/Moss Harbour S01E02.mkv",
               "Season 01/Subs/Moss Harbour S01E02/2_English.srt",
               "Subs/Moss.Harbour.S01E02.fr.srt")
        rules.subtitles_beside(str(show), SkipLog())
        assert _files(show) == ["Season 01/Moss Harbour S01E02.en.srt",
                                "Season 01/Moss Harbour S01E02.fr.srt",
                                "Season 01/Moss Harbour S01E02.mkv"]


class TestTheWalk:
    def test_the_episodes_and_not_the_extras_or_copies(self, tmp_path):
        show = tmp_path / "Moss Harbour (2005)"
        _touch(show, "Season 01/a - s01e01.mkv", "Season 01/a - s01e01 - old.mkv",
               "Season 01/a - s01e02 [AV1].mkv", "Season 01/a - s01e01-deleted.mkv",
               "Featurettes/f.mkv")
        found = [os.path.basename(path)
                 for path in rules.all_episode_files(str(tmp_path))]
        assert found == ["a - s01e01.mkv"]

    def test_a_film_beside_the_shows_is_held_out(self, tmp_path):
        _touch(tmp_path, "Moss Harbour/Season 01/a S01E01.mkv",
               "Copper Lantern (1999)/Copper Lantern (1999).mkv")
        assert [os.path.basename(path) for path in
                rules.films_in(str(tmp_path), lambda path: 6000)] \
            == ["Copper Lantern (1999)"]
        assert [os.path.basename(path) for path in rules.shows_in(str(tmp_path))] \
            == ["Moss Harbour"]
