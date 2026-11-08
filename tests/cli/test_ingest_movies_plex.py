"""The Plex comparison: each film found in the library by its id (or by its
title and year), its library copies printed, and the verdict said - and -p,
which is that phase and nothing else.

The server and the files are stood in for: what the comparison weighs is
pinned in tests/lib/test_plexcompare.py."""

from __future__ import annotations

import pytest

from medialib.cli import ingest_movies_plex as phase
from medialib.cli import ingest_movies_run as run
from medialib.lib import plexcompare, plexserver
from medialib.lib.filmmatch import Film

pytestmark = pytest.mark.fs


def _film(root, folder: str, stem: str = ""):
    path = root / folder
    path.mkdir(parents=True, exist_ok=True)
    movie = path / ((stem or folder) + ".mkv")
    movie.touch()
    return movie


def _profile(path, width=1920, commentaries=()):
    return plexcompare.Profile(str(path), 6000.0, width, 800, "SDR",
                               (plexcompare.Audio(0, "eng", 6, False, False, ""),),
                               tuple(commentaries), (), 0, False, "")


@pytest.fixture
def library(monkeypatch, tmp_path):
    """A server holding whatever the test puts in ``copies``, and profiles
    read from ``profiles`` by path."""
    held = {"copies": [], "profiles": {}, "fail": None}

    class Server:
        url = "http://plex:32400"
        token = "t"

        def copies(self):
            if held["fail"]:
                raise plexserver.PlexError(held["fail"])
            return held["copies"]

    monkeypatch.setattr(phase.plexserver, "local", Server)
    monkeypatch.setattr(phase.plexcompare, "read_profile",
                        lambda path, edition="": held["profiles"].get(str(path)))
    monkeypatch.setattr(phase.plexcompare, "measure_offset", lambda *a: plexcompare.Offset(
        (1.0, 2.0, 3.0), (0, 0, 0)))
    return held


def _compare(tmp_path, *roots):
    said: list = []
    status = phase.compare_roots([str(root) for root in roots], said.append)
    return status, said


class TestTheVerdicts:

    def test_a_film_not_in_the_library(self, library, tmp_path):
        _film(tmp_path / "in", "Lantern (2007) {imdb-tt0101000}")
        status, said = _compare(tmp_path, tmp_path / "in")
        assert status == 0
        assert "  not in the library (looked for by its id {imdb-tt0101000})" in said
        assert said[-1] == "Plex: 1 not in the library"

    def test_a_mixed_one_is_located_and_listed_again(self, library, tmp_path):
        new = _film(tmp_path / "in", "Lantern (2007) {imdb-tt0101000}")
        old = _film(tmp_path / "lib", "Lantern (2007) {imdb-tt0101000}")
        library["copies"] = [plexserver.Copy(
            "1", Film("Lantern", "2007", tmdb="4242", imdb="tt0101000"),
            "Films", "", (str(old),))]
        library["profiles"] = {
            str(new): _profile(new, width=3840),
            str(old): _profile(old, commentaries=[
                plexcompare.Audio(1, "eng", 2, False, False, "Commentary")])}
        _status, said = _compare(tmp_path, tmp_path / "in")
        assert "    " + str(old) in said
        assert "  MIXED: each has something the other lacks" in said
        assert "    new has the better resolution: 2160p vs 1080p" in said
        assert "    existing has the better commentaries: 1 (Commentary) vs none" in said
        assert "    in sync: the existing file's tracks can be muxed in as they are" \
            in said
        assert said[-1] == "Plex: MIXED, needs a look: " + str(new)

    def test_an_untagged_film_is_found_by_title_and_year(self, library, tmp_path):
        new = _film(tmp_path / "in", "Lantern (2007)")
        library["copies"] = [plexserver.Copy(
            "1", Film("Lantern", "2007"), "Films", "", ("/elsewhere/Lantern.mkv",))]
        library["profiles"] = {str(new): _profile(new)}
        _status, said = _compare(tmp_path, tmp_path / "in")
        assert any("found by title and year" in line for line in said)
        assert any("cannot be read from here" in line for line in said)

    def test_the_copy_in_the_films_own_folder_is_not_a_duplicate(
            self, library, tmp_path):
        new = _film(tmp_path / "lib", "Lantern (2007) {imdb-tt0101000}")
        library["copies"] = [plexserver.Copy(
            "1", Film("Lantern", "2007", imdb="tt0101000"), "Films", "", (str(new),))]
        _status, said = _compare(tmp_path, tmp_path / "lib")
        assert said[-1] == "Plex: 1 not in the library"

    def test_an_unreachable_server_skips_the_phase(self, library, tmp_path):
        library["fail"] = "connection refused"
        status, said = _compare(tmp_path, tmp_path)
        assert status == 1
        assert said[-1] == ("Plex: SKIPPED, the server could not be asked "
                            "(connection refused)")


class TestTheMode:

    @pytest.fixture
    def stubbed(self, monkeypatch, tmp_path):
        calls: list = []
        monkeypatch.setattr(run.tooldeps, "require_tools", lambda *_a: 0)
        monkeypatch.setattr(run.plexphase, "compare_roots",
                            lambda roots, _log: calls.append(roots) or 0)
        return calls

    def test_p_compares_and_does_nothing_else(self, stubbed, tmp_path):
        (tmp_path / "Films").mkdir()
        assert run.main(["-p", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 0
        assert stubbed == [[str(tmp_path / "Films")]]

    @pytest.mark.parametrize("other", ["-t", "-c", "-a"])
    def test_another_phase_of_its_own_is_refused(self, stubbed, tmp_path, capsys,
                                                 other):
        (tmp_path / "Films").mkdir()
        assert run.main(["-p", other, str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        assert "-p compares the films with the Plex library" in capsys.readouterr().err
        assert stubbed == []

    def test_w_has_no_part_in_it(self, stubbed, tmp_path, capsys):
        (tmp_path / "Films").mkdir()
        assert run.main(["-pw", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        assert "-w has no part in -p" in capsys.readouterr().err
