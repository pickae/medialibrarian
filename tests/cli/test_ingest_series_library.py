"""ingest-series' carry-over from the media server's copy, its commentary
naming and its ChapterDB chapters: how each finds an episode's own, which is
where they differ from the movie ingest. Every show here is invented."""

import os

import pytest

from medialib.cli import ingest_series_run as run
from medialib.lib import chapterdb, mediaserver, seriesidentify

pytestmark = pytest.mark.fs

SHOW = "Moss Harbour (2005) {tmdb-123}"


def _touch(root, *paths):
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    return [str(root / path) for path in paths]


def _row(title, seconds=2700.0, set_id="1"):
    return chapterdb.Row(set_id, title, "", seconds)


def _existing(kind):
    names = {"none": (), "numbered": ("Chapter 1", "Chapter 2")}[kind]
    return chapterdb.Existing(2700.0, names, (0.0, 600.0)[:len(names)])


class TestTheCarryOver:
    def test_two_episodes_of_one_season_are_kept_apart(self, tmp_path):
        first, second = _touch(
            tmp_path, SHOW + "/Season 01/Moss Harbour (2005) - s01e01.mkv",
            SHOW + "/Season 01/Moss Harbour (2005) - s01e02.mkv")
        assert run.episode_key(first) != run.episode_key(second)
        assert run.episode_key(first)[-2:] == (1, 1)

    def test_an_episode_is_found_by_any_id_of_its_show(self, tmp_path):
        (episode,) = _touch(tmp_path, SHOW + "/Season 01/x - s01e02.mkv")
        elsewhere = mediaserver.EpisodeCopy(
            "k1", {"tvdb": "77"}, "Moss Harbour", 1, 2, "TV",
            ("/library/Moss/Season 01/x.mkv",))
        other_episode = elsewhere._replace(key="k2", episode=3)
        beside = elsewhere._replace(key="k3", files=(episode,))
        index = run._episode_index([elsewhere, other_episode, beside])

        class Shows:
            def of(self, _folder):
                return seriesidentify.Show("tmdb", "123", "Moss Harbour",
                                           tmdb="123", tvdb="77")

        found = run.found_episode(Shows(), episode, index)
        assert [copy.key for copy in found] == ["k1"]
        assert found[0].files == ("/library/Moss/Season 01/x.mkv",)

    def test_without_a_known_show_the_folder_tag_alone(self, tmp_path):
        (episode,) = _touch(tmp_path, SHOW + "/Season 01/x - s01e02.mkv")
        copy = mediaserver.EpisodeCopy("k1", {"tmdb": "123"}, "Moss Harbour",
                                       1, 2, "TV", ("/elsewhere/x.mkv",))
        assert len(run.found_episode(None, episode,
                                     run._episode_index([copy]))) == 1

    def test_episodes_have_no_edition_but_keep_their_part(self):
        assert run.episode_edition("Moss Harbour (2005) - s01e02 - T - pt2.mkv") \
            == ("", "2")


class TestTheChapters:
    def test_only_a_tagged_show_is_looked_up(self, tmp_path):
        tagged, untagged = _touch(
            tmp_path, SHOW + "/Season 01/a - s01e02.mkv",
            "Moss Harbour/Season 01/a - s01e02.mkv")
        assert run.show_title(tagged) == "Moss Harbour"
        assert run.show_title(untagged) == ""

    def test_a_set_has_to_name_this_episode(self, tmp_path):
        (episode,) = _touch(
            tmp_path, SHOW + "/Season 01/Moss Harbour (2005) - s01e02 - "
            "The Tide.mkv")
        fits = run.row_fits_for(episode, _existing("none"))
        assert fits(_row("Moss Harbour S01E02"))
        assert fits(_row("Moss.Harbour.S01E02.1080p"))
        assert fits(_row("Moss Harbour - The Tide"))
        assert not fits(_row("Moss Harbour S01E03"))
        assert not fits(_row("Moss Harbour Season 1 Disc 1"))
        assert not fits(_row("Copper Gate S01E02"))

    def test_a_disc_set_only_where_the_marks_can_be_held_to(self, tmp_path):
        (episode,) = _touch(tmp_path, SHOW + "/Season 01/x - s01e02.mkv")
        fits = run.row_fits_for(episode, _existing("numbered"))
        assert fits(_row("Moss Harbour Season 1 Disc 1"))

    def test_the_archive_is_asked_by_the_show_and_the_rows_are_narrowed(
            self, tmp_path, monkeypatch):
        (episode,) = _touch(tmp_path, SHOW + "/Season 01/x - s01e02.mkv")
        monkeypatch.setattr(chapterdb, "existing_chapters",
                            lambda movie: _existing("none"))
        asked = []

        def search(query, fetch):
            asked.append(query)
            return [_row("Moss Harbour S01E03", set_id="3"),
                    _row("Moss Harbour S01E02", set_id="2")]

        downloaded = []

        def fetch(path, params=()):
            downloaded.append(path)
            return None

        monkeypatch.setattr(chapterdb, "search", search)
        found = list(chapterdb.findings(
            str(tmp_path), lambda line: None, fetch, movies=[episode],
            title_of=run.show_title, row_fits_for=run.row_fits_for))
        assert asked == ["Moss Harbour"]
        assert downloaded == ["/browse/2.xml"]
        assert found[0][1] == "unmatched"


class TestTheCommentaryNames:
    def test_each_episode_is_named_for_its_show(self, tmp_path):
        _touch(tmp_path, SHOW + "/Season 01/a - s01e01.mkv",
               SHOW + "/Season 01/a - s01e01-deleted.mkv")
        found = list(run.commentary_episodes(str(tmp_path)))
        assert [(os.path.basename(path), base) for path, base in found] \
            == [("a - s01e01.mkv", "Moss Harbour (2005)")]

    def test_the_disc_database_is_never_asked(self):
        assert run._no_releases("Moss Harbour", "2005", "Blu-ray") is None
