"""ingest-movies -r: which films are remuxes, what their converted copy is
called, and how the copies are handed to convert-video.

A remux is told by its name, read before the cleaning takes the word out and
recognised at the end by inode, or by a video only a disc carries. Its copy is
written beside it as the AV1 edition of the same film, and every other phase
leaves that copy alone the way it leaves the "(old)" one.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from medialib.cli import ingest_movies_convert as convert
from medialib.cli import ingest_movies_run as run
from medialib.lib import plexnames


class TestTheCopysName:
    pytestmark = pytest.mark.pure

    @pytest.mark.parametrize("stem, wanted", [
        ("Film (2020) {imdb-tt1}", "Film (2020) {imdb-tt1} {edition-AV1}"),
        ("Film (2020) {imdb-tt1} {edition-Director's Cut}",
         "Film (2020) {imdb-tt1} {edition-Director's Cut AV1}"),
        ("Film (2020) {imdb-tt1} Part1",
         "Film (2020) {imdb-tt1} {edition-AV1} Part1"),
    ])
    def test_it_is_the_av1_edition_of_the_same_film(self, stem, wanted):
        assert plexnames.converted_stem(stem) == wanted
        assert plexnames.film_key(wanted) == plexnames.film_key(stem)

    @pytest.mark.parametrize("name", [
        "Film (2020) {imdb-tt1} {edition-AV1}.mkv",
        "Film (2020) {edition-Director's Cut AV1}.mkv",
        "Film (2020) {imdb-tt1} {edition-AV1} (video only).mkv",
    ])
    def test_the_copy_is_left_alone_like_the_old_one(self, name):
        assert plexnames.is_converted_copy(name)
        assert plexnames.is_kept_copy(name)
        assert not plexnames.is_movie_file(name)

    @pytest.mark.parametrize("name", [
        "Film (2020) {imdb-tt1}.mkv",
        "Film (2020) {imdb-tt1} {edition-Colorized}.mkv",
        "Film (2020) {imdb-tt1} {edition-AV1}.en.srt",
    ])
    def test_anything_else_is_not(self, name):
        assert not plexnames.is_converted_copy(name)

    def test_a_folder_with_its_copy_still_holds_one_film(self):
        names = ["Film (2020).mkv", "Film (2020) {edition-AV1}.mkv",
                 "Film (2020) (old).mkv"]
        assert plexnames.one_film_in("Film (2020)", names) == ["Film (2020).mkv"]


class TestWhatIsARemux:
    pytestmark = pytest.mark.pure

    def test_one_its_name_calls_one(self):
        assert convert.remux_reason(True, "", "", "", "", "", "", "")

    def test_dolby_vision_profile_7_at_a_generous_hevc_bitrate(self):
        assert convert.remux_reason(False, "hevc", "3840", "2160", "23.976",
                                    "55000", "7", "smpte2084")

    @pytest.mark.parametrize("kbit, profile, codec, height", [
        ("15000", "7", "hevc", "2160"),  # not generous: an encode's bitrate
        ("35000", "8", "hevc", "2160"),  # a profile an encode carries too
        ("35000", "", "hevc", "2160"),  # plain HDR10 at a web bitrate
        ("55000", "7", "hevc", "1080"),  # not 2160p
    ])
    def test_but_not_without_all_of_it(self, kbit, profile, codec, height):
        width = "3840" if height == "2160" else "1920"
        assert not convert.remux_reason(False, codec, width, height, "23.976",
                                        kbit, profile, "smpte2084")

    @pytest.mark.parametrize("profile", ["", "8"])
    def test_any_2160p_hevc_at_a_very_generous_bitrate(self, profile):
        # HDR10 alone, or 8.1 whose "(old)" profile 7 is gone.
        assert convert.remux_reason(False, "hevc", "3840", "2160", "23.976",
                                    "88000", profile, "smpte2084")

    def test_sdr_h264_1080p_at_a_very_generous_bitrate(self):
        assert convert.remux_reason(False, "h264", "1920", "1080", "23.976",
                                    "28000", "", "bt709")

    @pytest.mark.parametrize("kbit, transfer, codec", [
        ("12000", "bt709", "h264"),  # generous, but an encode's
        ("28000", "smpte2084", "h264"),  # HDR
        ("28000", "bt709", "hevc"),  # not H.264
    ])
    def test_but_not_a_good_encode(self, kbit, transfer, codec):
        assert not convert.remux_reason(False, codec, "1920", "1080",
                                        "23.976", kbit, "", transfer)

    def test_nor_one_that_could_not_be_measured(self):
        assert not convert.remux_reason(False, "h264", "1920", "1080",
                                        "23.976", "", "", "")


class TestTheNameIsReadBeforeItIsCleaned:
    pytestmark = pytest.mark.fs

    def test_a_film_named_remux_is_known_again_after_renames(self, tmp_path):
        folder = tmp_path / "Film.2020.1080p.BluRay.REMUX"
        folder.mkdir()
        (folder / "film.mkv").write_text("x")
        (tmp_path / "Other (2020).mkv").write_text("y")

        named = convert.named_remuxes(str(tmp_path))

        clean = tmp_path / "Film (2020)"
        folder.rename(clean)
        (clean / "film.mkv").rename(clean / "Film (2020).mkv")
        movie = str(clean / "Film (2020).mkv")
        assert convert._reason_for(movie, named)
        assert len(named) == 1

    def test_and_after_the_improved_remux_kept_it_as_the_old_copy(
            self, tmp_path):
        folder = tmp_path / "Film (2020)"
        folder.mkdir()
        (folder / "Film (2020) REMUX.mkv").write_text("x")
        named = convert.named_remuxes(str(tmp_path))
        (folder / "Film (2020) REMUX.mkv").rename(folder / "Film (2020) (old).mkv")
        (folder / "Film (2020).mkv").write_text("improved")

        movie = str(folder / "Film (2020).mkv")
        assert convert.kept_original(movie) == str(
            folder / "Film (2020) (old).mkv")
        assert convert._reason_for(movie, named)


class TestTheHandOver:
    pytestmark = pytest.mark.fs

    def test_one_convert_video_run_writes_each_copy_beside_its_film(
            self, tmp_path, monkeypatch):
        root = tmp_path / "Films"
        for title in ("A (2020)", "B (2021)"):
            (root / title).mkdir(parents=True)
            (root / title / (title + ".mkv")).write_text(title)
        (root / "C (2022)").mkdir()
        (root / "C (2022)" / "C (2022).mkv").write_text("not a remux")
        named = set()
        for title in ("A (2020)", "B (2021)"):
            stat = os.stat(root / title / (title + ".mkv"))
            named.add((stat.st_dev, stat.st_ino))
        ram = tmp_path / "ram"
        ram.mkdir()
        monkeypatch.setattr(convert, "_reason_for",
                            lambda movie, n: "named" if os.stat(movie).st_ino
                            in {i for _d, i in n} else "")
        seen = {}

        def fake_run(command, argv, script_dir="", **_k):
            seen["command"], seen["argv"] = command, argv
            links = argv[1]
            seen["links"] = sorted(
                (os.path.relpath(os.path.join(parent, name), links),
                 os.readlink(os.path.join(parent, name)))
                for parent, _dirs, names in os.walk(links) for name in names)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(convert.commands, "run_command", fake_run)

        status = convert.convert_remuxes(str(root), named, str(ram), "",
                                         lambda *_a: None)

        assert status == 0
        assert seen["command"] == "convert-video"
        assert seen["argv"][0] == "-k" and seen["argv"][2] == str(root)
        assert seen["links"] == [
            (os.path.join("A (2020)", "A (2020) {edition-AV1}.mkv"),
             str(root / "A (2020)" / "A (2020).mkv")),
            (os.path.join("B (2021)", "B (2021) {edition-AV1}.mkv"),
             str(root / "B (2021)" / "B (2021).mkv")),
        ]
        assert not os.path.exists(seen["argv"][1])

    def test_nothing_is_started_without_a_remux(self, tmp_path, monkeypatch):
        (tmp_path / "A (2020)").mkdir()
        (tmp_path / "A (2020)" / "A (2020).mkv").write_text("a")
        monkeypatch.setattr(convert, "_reason_for", lambda *_a: "")
        monkeypatch.setattr(convert.commands, "run_command",
                            lambda *_a, **_k: pytest.fail("started"))

        assert convert.convert_remuxes(str(tmp_path), set(), str(tmp_path),
                                       "", lambda *_a: None) == 0


class TestRefusals:
    pytestmark = pytest.mark.fs

    @pytest.mark.parametrize("other", [["-t"], ["-s"], ["-a"], ["-c"],
                                       ["-p"], ["-n"]])
    def test_beside_a_phase_of_its_own(self, other, tmp_path, capsys):
        folder = tmp_path / "Films" / "Film (2020)"
        folder.mkdir(parents=True)
        (folder / "Film (2020).mkv").touch()

        assert run.main(["-r"] + other + [str(tmp_path / "Films")]) == 1
        errors = capsys.readouterr().err
        assert "-r converts the remuxes at the end of a full ingest" in errors
        assert "Nothing was changed." in errors
