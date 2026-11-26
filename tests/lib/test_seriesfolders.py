"""medialib/lib/seriesfolders.py: reading a show's folder, and the guard that
tells a show from a film."""

import pytest

from medialib.lib import filmfolders, seriesfolders

pytestmark = pytest.mark.fs


def _touch(root, *paths):
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")


class TestTheName:
    @pytest.mark.parametrize("name,title,year,tag", [
        ("Moss Harbour (2005) {tmdb-123}", "Moss Harbour", "2005", "{tmdb-123}"),
        ("Moss Harbour (2005) [tvdbid-77]", "Moss Harbour", "2005",
         "[tvdbid-77]"),
        ("Moss.Harbour.2005", "Moss Harbour", "2005", ""),
        ("Moss Harbour", "Moss Harbour", "", ""),
    ])
    def test_title_year_and_tag(self, name, title, year, tag):
        read = seriesfolders.read_series_folder(name)
        assert (read.title, read.year, read.tag) == (title, year, tag)


class TestTheGuard:
    def test_season_folders_are_a_show(self, tmp_path):
        _touch(tmp_path, "Moss Harbour (2005)/Season 01/a.mkv")
        assert seriesfolders.looks_like_series(str(tmp_path / "Moss Harbour (2005)"))

    def test_episode_tokens_are_a_show(self, tmp_path):
        _touch(tmp_path, "Moss Harbour/Moss Harbour S01E01.mkv",
               "Moss Harbour/Moss Harbour S01E02.mkv")
        assert seriesfolders.looks_like_series(str(tmp_path / "Moss Harbour"))

    def test_a_film_is_not(self, tmp_path):
        _touch(tmp_path, "Copper Lantern (1999)/Copper Lantern (1999).mkv",
               "Copper Lantern (1999)/Featurettes/Making Of.mkv")
        assert not seriesfolders.looks_like_series(
            str(tmp_path / "Copper Lantern (1999)"))

    def test_an_empty_season_folder_is_not(self, tmp_path):
        (tmp_path / "Moss Harbour" / "Season 01").mkdir(parents=True)
        assert not seriesfolders.looks_like_series(str(tmp_path / "Moss Harbour"))

    def test_a_lone_feature_is_a_film(self, tmp_path):
        _touch(tmp_path, "Copper Lantern (1999)/Copper Lantern (1999).mkv")
        folder = str(tmp_path / "Copper Lantern (1999)")
        assert seriesfolders.looks_like_film(folder, lambda path: 6000)
        assert not seriesfolders.looks_like_film(folder, lambda path: 1500)

    def test_without_a_length_the_name_decides(self, tmp_path):
        _touch(tmp_path, "Copper Lantern (1999)/Copper Lantern (1999).mkv",
               "Pilot Only/Something Else.mkv")
        assert seriesfolders.looks_like_film(str(tmp_path / "Copper Lantern (1999)"))
        assert not seriesfolders.looks_like_film(str(tmp_path / "Pilot Only"))

    def test_the_film_side_never_takes_a_show_for_a_film(self, tmp_path):
        _touch(tmp_path, "Moss Harbour (2005)/Season 01/Moss Harbour S01E01.mkv")
        import os
        entry = next(os.scandir(tmp_path))
        assert not filmfolders.is_film_folder(entry)
        assert filmfolders._film_folders(str(tmp_path)) == []


class TestTheListing:
    def test_seasons_extras_and_loose_episodes(self, tmp_path):
        show = tmp_path / "Moss Harbour (2005)"
        _touch(show, "Season 02/b.mkv", "Season 01/a.mkv", "Specials/s.mkv",
               "Featurettes/f.mkv", "Moss Harbour S03E01.mkv")
        seasons = seriesfolders.season_folders(str(show))
        assert [season for season, _path in seasons] == [0, 1, 2]
        assert [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                for p in seriesfolders.bonus_folders(str(show))] == ["Featurettes"]
        assert len(seriesfolders.loose_episodes(str(show))) == 1
        assert seriesfolders.series_folders(str(tmp_path)) == [str(show)]
