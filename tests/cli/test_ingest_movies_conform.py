"""A movie whose name never got the dot before its mkv, conformed to the
spelling its folder spells it.

The folder is the anchor: a file is only ever the folder's own film when it
carries the folder's id tag, and it is conformed only when nothing is left of it
once the folder's words, a year said a second time, and a stacking token are
accounted for. What is pinned here is the boundary of that: the mangle undone
without a guess, the sidecars that follow the film, the rename that is refused
rather than made over an existing name, and the names that are not the folder's
film at all left exactly where they were.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medialib.cli import ingest_movies as im
from medialib.lib import safety

pytestmark = pytest.mark.fs

BASE = "Hollow Ridge (1981)"
TAG = "{imdb-tt0000001}"
FOLDER = "%s %s" % (BASE, TAG)


def _write(path, content="m"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _folder(root, folder=FOLDER):
    """A movie folder whose name is the anchor its films are conformed to."""
    path = Path(root) / folder
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def skips():
    return safety.RunSkipLog()


class TestTheConform:
    def test_a_name_with_no_dot_before_its_mkv_is_conformed(self, tmp_path,
                                                             skips):
        folder = _folder(tmp_path)
        mangled = folder / (FOLDER + " 1981mkv")
        _write(mangled)
        assert im.conform_movie_names(str(tmp_path), skips) == 1
        assert not mangled.exists()
        assert (folder / (FOLDER + ".mkv")).is_file()

    def test_the_repaired_name_is_the_folder_s_spelling(self, tmp_path, skips):
        folder = _folder(tmp_path)
        _write(folder / (FOLDER + " 1981mkv"))
        im.conform_movie_names(str(tmp_path), skips)
        # the year said a second time is dropped, the film and its tag kept, and
        # the dot the name never had is what the repair puts back
        assert sorted(p.name for p in folder.iterdir()) == [FOLDER + ".mkv"]

    def test_a_stacking_token_is_kept_last(self, tmp_path, skips):
        folder = _folder(tmp_path)
        _write(folder / (FOLDER + " 1981 cd2mkv"))
        im.conform_movie_names(str(tmp_path), skips)
        assert (folder / (FOLDER + " cd2.mkv")).is_file()

    def test_a_name_that_is_already_conformed_is_left_alone(self, tmp_path,
                                                             skips):
        folder = _folder(tmp_path)
        _write(folder / (FOLDER + ".mkv"))
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert (folder / (FOLDER + ".mkv")).is_file()

    def test_the_content_survives_the_rename(self, tmp_path, skips):
        folder = _folder(tmp_path)
        _write(folder / (FOLDER + " 1981mkv"), "the film")
        im.conform_movie_names(str(tmp_path), skips)
        assert (folder / (FOLDER + ".mkv")).read_text() == "the film"


class TestTheSidecarsThatFollow:
    def test_a_subtitle_named_by_the_old_stem_follows_the_film(self, tmp_path,
                                                                skips):
        folder = _folder(tmp_path)
        _write(folder / (FOLDER + " 1981mkv"))
        _write(folder / (FOLDER + " 1981.en.srt"), "sub")
        im.conform_movie_names(str(tmp_path), skips)
        assert (folder / (FOLDER + ".en.srt")).is_file()
        assert not (folder / (FOLDER + " 1981.en.srt")).exists()

    def test_a_transcript_named_by_the_old_stem_follows_the_film(self,
                                                                 tmp_path,
                                                                 skips):
        folder = _folder(tmp_path)
        _write(folder / (FOLDER + " 1981mkv"))
        _write(folder / (FOLDER + " 1981 1 Commentary.en.srt"), "t")
        im.conform_movie_names(str(tmp_path), skips)
        assert (folder / (FOLDER + " 1 Commentary.en.srt")).is_file()


class TestWhatIsRefused:
    def test_a_rename_is_not_made_over_an_existing_name(self, tmp_path, skips):
        """The conformed name is already taken, so the mangled name is left
        where it is and the clash is the caller's to see, not papered over."""
        folder = _folder(tmp_path)
        _write(folder / (FOLDER + ".mkv"))
        mangled = folder / (FOLDER + " 1981mkv")
        _write(mangled, "the other")
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert mangled.is_file()
        assert (folder / (FOLDER + ".mkv")).is_file()
        assert len(skips.skips) == 1

    def test_a_name_with_a_word_of_its_own_is_left_alone(self, tmp_path, skips):
        """An edition the folder's spelling does not name is not the folder's
        film misspelt: it is left to be reported, not renamed to a guess."""
        folder = _folder(tmp_path)
        mangled = folder / (FOLDER + " Remasteredmkv")
        _write(mangled)
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert mangled.is_file()

    def test_a_name_that_carries_no_tag_is_left_alone(self, tmp_path, skips):
        folder = _folder(tmp_path)
        mangled = folder / (BASE + " 1981mkv")
        _write(mangled)
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert mangled.is_file()

    def test_a_name_with_another_tag_is_left_alone(self, tmp_path, skips):
        folder = _folder(tmp_path)
        mangled = folder / (BASE + " {tmdb-9} 1981mkv")
        _write(mangled)
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert mangled.is_file()


class TestTheFoldersItPassesOver:
    def test_a_bonus_folder_is_not_conformed(self, tmp_path, skips):
        folder = tmp_path / "Extras"
        folder.mkdir()
        mangled = folder / (FOLDER + " 1981mkv")
        _write(mangled)
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert mangled.is_file()

    def test_a_kept_copy_is_not_conformed(self, tmp_path, skips):
        folder = _folder(tmp_path)
        # the original an improved remux kept, named the way that keeping goes
        kept = folder / (FOLDER + " (old).mkv")
        _write(kept)
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert kept.is_file()

    def test_a_folder_with_no_tag_is_not_an_anchor(self, tmp_path, skips):
        folder = tmp_path / BASE
        folder.mkdir()
        mangled = folder / (BASE + " 1981mkv")
        _write(mangled)
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert mangled.is_file()

    def test_a_folder_with_no_year_is_not_an_anchor(self, tmp_path, skips):
        folder = tmp_path / ("Hollow Ridge %s" % TAG)
        folder.mkdir()
        mangled = folder / ("Hollow Ridge %s 1981mkv" % TAG)
        _write(mangled)
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert mangled.is_file()


class TestIdempotent:
    def test_a_second_run_finds_no_work(self, tmp_path, skips):
        folder = _folder(tmp_path)
        _write(folder / (FOLDER + " 1981mkv"))
        assert im.conform_movie_names(str(tmp_path), skips) == 1
        assert im.conform_movie_names(str(tmp_path), skips) == 0
        assert (folder / (FOLDER + ".mkv")).is_file()
