"""The folder of skipped files a torrent client leaves beside a film.

qBittorrent puts the files a download was told to skip in a folder named
`.unwanted`, and `cleanup` deletes it with whatever is in it. That exact name
is the rule: a film folder whose name merely has the word in it is a film.
"""

import pytest

from medialib.cli import ingest_movies as im

pytestmark = pytest.mark.fs


def _film(folder, name):
    """A film big enough that the cleanup keeps it, without writing its bytes."""
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / name, "wb") as handle:
        handle.truncate(im.MIN_MOVIE_BYTES)


def _files_left(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                  if path.is_file())


class TestTheUnwantedFolder:
    def test_it_is_deleted_with_what_it_holds(self, tmp_path):
        _film(tmp_path / "A Film (2020)", "A Film (2020).mkv")
        _film(tmp_path / "A Film (2020)" / ".unwanted", "A Film (2020).mkv")
        (tmp_path / "A Film (2020)" / ".unwanted" / "cover.jpg").write_bytes(
            b"jpeg")

        im.cleanup(str(tmp_path))

        assert _files_left(tmp_path) == ["A Film (2020)/A Film (2020).mkv"]
        assert not (tmp_path / "A Film (2020)" / ".unwanted").exists()

    def test_so_is_one_deeper_down(self, tmp_path):
        nested = tmp_path / "Pack" / "A Film (2020)" / ".unwanted" / "Extras"
        _film(nested, "featurette.mkv")
        _film(tmp_path / "Pack" / "A Film (2020)", "A Film (2020).mkv")

        im.cleanup(str(tmp_path))

        assert _files_left(tmp_path) == [
            "Pack/A Film (2020)/A Film (2020).mkv"]

    @pytest.mark.parametrize("folder", ["some.unwanted.film.title.2019.1080p",
                                        "Some Unwanted Film (2019)", "unwanted"])
    def test_a_film_folder_with_the_word_in_its_name_stays(self, tmp_path,
                                                          folder):
        _film(tmp_path / folder, "film.mkv")

        im.cleanup(str(tmp_path))

        assert _files_left(tmp_path) == [folder + "/film.mkv"]
