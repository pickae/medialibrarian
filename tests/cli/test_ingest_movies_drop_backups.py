"""ingest-movies -d: the "(old)" originals the improved copies were kept from.

Each copy is asked the three things the copy was asked by - the same length as
the original, a video stream, at least the tracks the rules keep - and the
original is dropped where it passes. A dry run unless -w; with -w the copy is
tagged before the original goes, so the "already remuxed" check still answers
yes once the backup is gone.
"""

from __future__ import annotations

import io
import os
import subprocess

import pytest

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_convert as convert
from medialib.cli import ingest_movies_run as run

pytestmark = pytest.mark.fs


def _mkv(path, seconds: int = 2, audio: int = 1,
         languages: tuple = ()) -> None:
    """A small real Matroska: one video, the audio the test asks for."""
    argv = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc=duration=%d:size=64x64:rate=10" % seconds]
    for _index in range(audio):
        argv += ["-f", "lavfi", "-i", "sine=duration=%d" % seconds]
    argv += ["-map", "0:0"]
    for index in range(audio):
        argv += ["-map", "%d:a" % (index + 1)]
    for index, language in enumerate(languages):
        argv += ["-metadata:s:a:%d" % index, "language=" + language]
    argv += ["-c:v", "libx264", "-c:a", "aac", str(path)]
    subprocess.run(argv, check=True)


def _pair(root, seconds: int = 2, copy_seconds: int | None = None):
    """A film with the "(old)" original its improved copy was kept beside,
    both real Matroska of the lengths asked for."""
    folder = root / "Film (2020)"
    folder.mkdir(parents=True)
    movie = folder / "Film (2020).mkv"
    _mkv(movie, seconds=copy_seconds if copy_seconds is not None else seconds)
    old = folder / "Film (2020) (old).mkv"
    _mkv(old, seconds=seconds)
    return movie, old


class TestDropCandidates:
    """The walk the sweep asks about: the films with a backup, in walk order."""

    def test_the_film_and_its_backup_are_the_pair(self, tmp_path):
        movie, old = _pair(tmp_path)
        assert convert.drop_candidates([str(tmp_path)]) == \
            [(str(movie), str(old))]

    def test_a_folder_without_a_backup_has_no_pair(self, tmp_path):
        movie, _old = _pair(tmp_path)
        os.remove(str(_old))
        assert convert.drop_candidates([str(tmp_path)]) == []

    def test_a_nested_library_is_walked_to_the_bottom(self, tmp_path):
        movie, old = _pair(tmp_path / "Action" / "Crime")
        found = convert.drop_candidates([str(tmp_path)])
        assert found == [(str(movie), str(old))]

    def test_a_bonus_folder_is_not_asked_about(self, tmp_path):
        folder = tmp_path / "Trailers"
        folder.mkdir()
        (folder / "A trailer.mkv").touch()
        (folder / "A trailer (old).mkv").touch()
        assert convert.drop_candidates([str(tmp_path)]) == []

    def test_a_folder_whose_feature_cannot_be_told_is_not_asked_about(
            self, tmp_path):
        movie, _old = _pair(tmp_path)
        (movie.parent / "The Other.mkv").touch()
        assert convert.drop_candidates([str(tmp_path)]) == []


class TestTheCheck:
    """The three things the copy is asked of, one failure at a time."""

    def test_a_copy_of_the_whole_passes(self, tmp_path):
        movie, old = _pair(tmp_path)
        assert convert.backup_is_whole(str(movie), str(old)) == ""

    def test_a_copy_not_as_long_as_the_original(self, tmp_path):
        movie, old = _pair(tmp_path, seconds=4, copy_seconds=2)
        reason = convert.backup_is_whole(str(movie), str(old))
        assert "not the same length" in reason

    def test_a_copy_nothing_can_measure(self, tmp_path):
        movie, old = _pair(tmp_path)
        open(movie, "w").close()
        reason = convert.backup_is_whole(str(movie), str(old))
        assert "could not be read" in reason

    def test_a_copy_with_no_video(self, tmp_path):
        folder = tmp_path / "Film (2020)"
        folder.mkdir()
        movie = folder / "Film (2020).mkv"
        old = folder / "Film (2020) (old).mkv"
        _mkv(old)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "sine=duration=2", "-c:a", "aac", str(movie)],
                       check=True)
        reason = convert.backup_is_whole(str(movie), str(old))
        assert "no video stream" in reason

    def test_a_soundtrack_the_rules_drop_is_missing_just_the_same(
            self, tmp_path):
        """The original's second language is not kept by the rules, so a copy
        without it is the whole of the original as far as the rules are
        concerned."""
        folder = tmp_path / "Film (2020)"
        folder.mkdir()
        old = folder / "Film (2020) (old).mkv"
        _mkv(old, audio=2, languages=("eng", "fra"))
        movie = folder / "Film (2020).mkv"
        _mkv(movie, audio=1, languages=("eng",))
        assert convert.backup_is_whole(str(movie), str(old)) == ""

    def test_a_soundtrack_the_rules_keep_is_not_missing(self, tmp_path):
        """The same copy, with both languages kept: the second is missing and
        the original is not dropped."""
        folder = tmp_path / "Film (2020)"
        folder.mkdir()
        old = folder / "Film (2020) (old).mkv"
        _mkv(old, audio=2, languages=("eng", "fra"))
        movie = folder / "Film (2020).mkv"
        _mkv(movie, audio=1, languages=("eng",))
        reason = convert.backup_is_whole(str(movie), str(old),
                                         kept=("en", "fr"))
        assert "1 audio track" in reason and "keep 2" in reason


class TestTheTag:
    """The tag that stands in for the backup once it is gone."""

    def test_the_tag_is_written_and_read(self, tmp_path):
        movie = tmp_path / "Film.mkv"
        _mkv(movie)
        assert not convert.has_ingested_tag(str(movie))
        assert convert.mark_ingested(str(movie)) == ""
        assert convert.has_ingested_tag(str(movie))
        # Twice over, the way a rerun does it: the tag is not doubled.
        assert convert.mark_ingested(str(movie)) == ""
        assert convert.has_ingested_tag(str(movie))

    def test_the_write_puts_the_file_time_back(self, tmp_path):
        movie = tmp_path / "Film.mkv"
        _mkv(movie)
        before = os.stat(movie).st_mtime
        assert convert.mark_ingested(str(movie)) == ""
        assert os.stat(movie).st_mtime == before

    def test_the_tag_stands_in_for_the_backup_on_a_later_run(self, tmp_path):
        movie, _old = _pair(tmp_path)
        os.remove(str(_old))
        # Without the backup the improve walk remuxes the film again - and with
        # the tag it does not.
        assert run.main_movies(str(tmp_path)) == [str(movie)]
        assert convert.mark_ingested(str(movie)) == ""
        assert run.main_movies(str(tmp_path)) == []


class TestTheTagRead:
    pytestmark = pytest.mark.pure

    @pytest.mark.parametrize("text, expected", [
        ("", False),
        ("TAG:ENCODER=Lavf63\n", False),
        ("TAG:MEDIALIB_INGESTED=1\n", True),
        ("TAG:medialib_ingested=1\n", True),
        ("TAG:SOMETHING_ELSE=x\nTAG:MEDIALIB_INGESTED=1\n", True),
        ("TAG:MEDIALIB_INGESTED_EXTRA=1\n", False),
    ])
    def test_the_tag_is_told_from_its_name(self, text, expected, monkeypatch):
        monkeypatch.setattr(rules, "_probe", lambda argv: text)
        assert convert.has_ingested_tag("movie") is expected


class TestTheSweep:
    """The run itself: the dry run's account, and the write run's effect."""

    def _scripts(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir(exist_ok=True)
        return scripts

    def _listing(self, scripts, root_name: str):
        # The run names its lists after the folder capitalised.
        name = root_name[:1].upper() + root_name[1:]
        return scripts / "logs" / "ingest-movies" / \
            ("ingest-movies-backups-%s.txt" % name)

    def test_the_dry_run_says_what_would_go(self, tmp_path, capsys):
        movie, old = _pair(tmp_path)
        scripts = self._scripts(tmp_path)
        assert run.main(["-O", "backups", str(tmp_path)], script_dir=str(scripts)) == 0
        err = capsys.readouterr().err
        assert "DRY RUN, nothing will be changed" in err
        assert "would be dropped" in err
        assert movie.exists() and old.exists()

    def test_the_write_run_tags_the_copy_and_drops_the_original(
            self, tmp_path, capsys):
        movie, old = _pair(tmp_path)
        scripts = self._scripts(tmp_path)
        assert run.main(["-wO", "backups", str(tmp_path)], script_dir=str(scripts)) == 0
        err = capsys.readouterr().err
        assert "Dropped 1 (old) original." in err
        assert not old.exists()
        assert movie.exists()
        assert convert.has_ingested_tag(str(movie))

    def test_the_check_that_fails_is_kept_and_listed(self, tmp_path, capsys):
        movie, old = _pair(tmp_path, seconds=4, copy_seconds=2)
        scripts = self._scripts(tmp_path)
        assert run.main(["-O", "backups", str(tmp_path)], script_dir=str(scripts)) == 0
        err = capsys.readouterr().err
        assert "would be kept" in err
        assert "not the same length" in err
        assert old.exists()
        listing = self._listing(scripts, tmp_path.name)
        assert listing.is_file()
        text = listing.read_text(encoding="utf-8")
        assert "# Would be kept, the copy failing its check:" in text
        assert "not the same length" in text

    def test_the_write_run_keeps_what_fails_and_leaves_no_tag_behind(
            self, tmp_path, capsys):
        movie, old = _pair(tmp_path, seconds=4, copy_seconds=2)
        scripts = self._scripts(tmp_path)
        assert run.main(["-wO", "backups", str(tmp_path)], script_dir=str(scripts)) == 0
        err = capsys.readouterr().err
        assert "1 kept, the copy failing its check" in err
        assert old.exists()
        assert not convert.has_ingested_tag(str(movie))

    def test_a_later_run_finds_nothing_where_the_backup_is_gone(
            self, tmp_path, capsys):
        _pair(tmp_path)
        scripts = self._scripts(tmp_path)
        assert run.main(["-wO", "backups", str(tmp_path)], script_dir=str(scripts)) == 0
        capsys.readouterr()
        assert run.main(["-O", "backups", str(tmp_path)], script_dir=str(scripts)) == 0
        assert "No (old) original kept beside an improved copy" in \
            capsys.readouterr().err

    def test_an_earlier_list_is_removed_where_nothing_is_kept(self, tmp_path,
                                                              monkeypatch,
                                                              capsys):
        movie, old = _pair(tmp_path, seconds=4, copy_seconds=2)
        scripts = self._scripts(tmp_path)
        assert run.main(["-O", "backups", str(tmp_path)], script_dir=str(scripts)) == 0
        listing = self._listing(scripts, tmp_path.name)
        assert listing.is_file()
        # The copy made whole in the meantime: the write run overwrites the
        # earlier list, drops the original and leaves no list of its own.
        _mkv(movie, seconds=4)
        monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
        assert run.main(["-wO", "backups", str(tmp_path)], script_dir=str(scripts)) == 0
        assert "An earlier run already wrote" in capsys.readouterr().err
        assert not old.exists()
        assert convert.has_ingested_tag(str(movie))
        assert not listing.exists()


class TestTheFlagsRefuse:
    """-d beside a phase of its own: two runs, not one."""

    def _library(self, tmp_path):
        folder = tmp_path / "Films" / "The Movie (1999)"
        folder.mkdir(parents=True)
        (folder / "The Movie (1999).mkv").touch()
        return str(tmp_path / "Films")




    def test_a_lone_write_is_refused_naming_the_sweep(self, tmp_path, capsys):
        assert run.main(["-w", self._library(tmp_path)]) == 1
        errors = capsys.readouterr().err
        assert "Did you mean -wO tagging?" in errors
