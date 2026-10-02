"""The release junk the first phase deletes, and what it must leave.

A release comes with notes, checksums and sample clips beside the film. They
go, and so do the folders left empty once they have. The film stays, however
small it is, as long as it is not small enough to be a sample.
"""

import os

import pytest

from medialib.cli import ingest_movies as im

pytestmark = pytest.mark.fs

# What is left of a folder holding the film and junk, once the junk is gone.
THE_FILM = ["Film (2020)", "Film (2020)/Film (2020).mkv"]


def _sized(path, size):
    """A file of ``size`` bytes, sparse so a film costs no disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)


def _left(root):
    return sorted(os.path.relpath(os.path.join(parent, name), root)
                  for parent, dirs, files in os.walk(root)
                  for name in dirs + files)


class TestTheJunk:
    @pytest.mark.parametrize("name", ["Film.nfo", "Release notes.txt",
                                      "Film.sfv", "setup.exe"])
    def test_a_release_s_notes_and_checksums_go(self, tmp_path, name):
        _sized(tmp_path / "Film (2020)" / "Film (2020).mkv", im.MIN_MOVIE_BYTES)
        _sized(tmp_path / "Film (2020)" / name, 10)

        im.cleanup(str(tmp_path))

        assert _left(tmp_path) == THE_FILM

    @pytest.mark.parametrize("name", ["sample.mkv", "Sample.mkv"])
    def test_a_sample_goes_whatever_its_size(self, tmp_path, name):
        _sized(tmp_path / "Film (2020)" / "Film (2020).mkv", im.MIN_MOVIE_BYTES)
        _sized(tmp_path / "Film (2020)" / name, im.MIN_MOVIE_BYTES * 2)

        im.cleanup(str(tmp_path))

        assert _left(tmp_path) == THE_FILM

    def test_an_mkv_one_byte_under_the_threshold_is_a_sample_too(self,
                                                                  tmp_path):
        _sized(tmp_path / "Film (2020)" / "Film (2020).mkv", im.MIN_MOVIE_BYTES)
        _sized(tmp_path / "Film (2020)" / "Film (2020)-clip.mkv",
               im.MIN_MOVIE_BYTES - 1)

        im.cleanup(str(tmp_path))

        assert _left(tmp_path) == THE_FILM


class TestWhatStays:
    def test_an_mkv_exactly_at_the_threshold_is_a_film(self, tmp_path):
        _sized(tmp_path / "Short (2020)" / "Short (2020).mkv",
               im.MIN_MOVIE_BYTES)

        im.cleanup(str(tmp_path))

        assert _left(tmp_path) == ["Short (2020)",
                                   "Short (2020)/Short (2020).mkv"]

    def test_the_film_s_subtitles_and_artwork_stay_with_it(self, tmp_path):
        folder = tmp_path / "Film (2020)"
        _sized(folder / "Film (2020).mkv", im.MIN_MOVIE_BYTES)
        _sized(folder / "Film (2020).en.srt", 10)
        _sized(folder / "poster.jpg", 10)

        im.cleanup(str(tmp_path))

        assert _left(tmp_path) == THE_FILM[:1] + [
            "Film (2020)/Film (2020).en.srt", "Film (2020)/Film (2020).mkv",
            "Film (2020)/poster.jpg"]


class TestTheEmptyFolders:
    def test_a_folder_that_only_held_junk_goes_with_it(self, tmp_path):
        _sized(tmp_path / "Film (2020)" / "Film (2020).mkv", im.MIN_MOVIE_BYTES)
        _sized(tmp_path / "Film (2020)" / "Sample" / "sample.mkv", 10)
        (tmp_path / "Film (2020)" / "Proof" / "empty").mkdir(parents=True)

        im.cleanup(str(tmp_path))

        assert _left(tmp_path) == THE_FILM

    def test_the_folder_cleaned_survives_even_left_empty(self, tmp_path):
        root = tmp_path / "Downloads"
        _sized(root / "Film (2020)" / "Film (2020).nfo", 10)
        _sized(root / "Film (2020)" / "sample.mkv", 10)

        im.cleanup(str(root))

        assert root.is_dir() and _left(root) == []
