"""What a music layout that lets beets name the library does after beets has
moved the albums: where each download folder is now, and the files beets left
behind following their album there."""

import os
import pathlib

import pytest

from medialib.cli import ingest_music as im

pytestmark = pytest.mark.fs


def _pairs(path, pairs) -> str:
    with open(path, "wb") as handle:
        for source, library in pairs:
            handle.write(source.encode() + b"\0" + library.encode() + b"\0")
    return str(path)


class TestWhereBeetsPut:
    def test_a_folder_is_where_its_flacs_are_now(self, tmp_path):
        library = str(tmp_path / "library")
        pairs = _pairs(tmp_path / "pairs", [
            ("Download Album/01.flac",
             os.path.join(library, "Artist", "Album", "01 - One.flac"))])
        assert im.where_beets_put(pairs, library) == {
            "Download Album": "Artist/Album"}

    def test_the_discs_of_one_album_are_the_album_they_share(self, tmp_path):
        library = str(tmp_path / "library")
        pairs = _pairs(tmp_path / "pairs", [
            ("Box/1.flac", os.path.join(library, "A", "Box", "Disc 1", "1.flac")),
            ("Box/2.flac", os.path.join(library, "A", "Box", "Disc 2", "2.flac"))])
        assert im.where_beets_put(pairs, library) == {"Box": "A/Box"}

    def test_a_folder_spread_over_two_artists_stays_unmapped(self, tmp_path):
        library = str(tmp_path / "library")
        pairs = _pairs(tmp_path / "pairs", [
            ("Mix/1.flac", os.path.join(library, "A", "X", "1.flac")),
            ("Mix/2.flac", os.path.join(library, "B", "Y", "2.flac"))])
        assert im.where_beets_put(pairs, library) == {}


class _Counters:
    def start_phase(self, *args, **kwargs):
        pass

    def end_phase(self):
        pass


@pytest.fixture
def moved(tmp_path, monkeypatch):
    """A library beets has just moved one album out of: the flac is in
    Artist/Album, and the rip log, the scans and a cover are still in the
    folder the run wrote."""
    download = tmp_path / "download"
    library = tmp_path / "library"
    counters = tmp_path / "counters"
    for folder in (download / "Album" / "Scans", counters):
        folder.mkdir(parents=True)
    (download / "Album" / "01.flac").write_bytes(b"")
    left = library / "Album"
    (left / "Scans").mkdir(parents=True)
    (left / "rip.log").write_text("log")
    (left / "cover.jpg").write_text("downloaded cover")
    (left / "Scans" / "back.jpg").write_text("scan")
    album = library / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - One.flac").write_bytes(b"")
    (album / "cover.jpg").write_text("cover beets fetched")

    tags = {str(album / "01 - One.flac"): "Album/01.flac"}

    run = im.Run(download_dir=str(download), ingest_dir=str(library),
                 folder_map={}, refused=frozenset(), counters=_Counters())

    def read_ingest_source(flac):
        with open(run.ingested_pairs, "ab") as handle:
            handle.write(tags[flac].encode() + b"\0" + flac.encode() + b"\0")

    run.read_ingest_source = read_ingest_source
    monkeypatch.setattr(
        im.workerpool, "map",
        lambda items, jobs, work: [work(item) for item in items])
    logged = []
    monkeypatch.setattr(im, "log", logged.append)
    im._carry_after_beets(run, str(download), str(library), str(counters), 1)
    return library, album, logged


class TestCarryAfterBeets:
    def test_what_beets_left_behind_follows_its_album(self, moved):
        _library, album, _logged = moved
        assert (album / "rip.log").read_text() == "log"
        assert (album / "Scans" / "back.jpg").read_text() == "scan"

    def test_a_name_the_album_already_holds_stays_where_it_was(self, moved):
        library, album, logged = moved
        assert (album / "cover.jpg").read_text() == "cover beets fetched"
        assert (library / "Album" / "cover.jpg").read_text() == \
            "downloaded cover"
        assert any("stays where it is" in line for line in logged), logged

    def test_the_folders_beets_emptied_are_removed(self, moved):
        library, _album, logged = moved
        assert not (library / "Album" / "Scans").exists()
        assert any("2 file(s) moved after their album, 1 left" in line
                   for line in logged), logged

    def test_a_folder_that_still_holds_a_flac_is_left_as_it_is(
            self, tmp_path, monkeypatch):
        download = tmp_path / "download"
        (download / "Album").mkdir(parents=True)
        library = tmp_path / "library"
        (library / "Album").mkdir(parents=True)
        (library / "Album" / "02.flac").write_bytes(b"")
        (library / "Album" / "rip.log").write_text("log")
        moved_flac = library / "Artist" / "Album" / "01 - One.flac"
        moved_flac.parent.mkdir(parents=True)
        moved_flac.write_bytes(b"")
        run = im.Run(download_dir=str(download), ingest_dir=str(library),
                     folder_map={}, refused=frozenset(), counters=_Counters())

        def read_ingest_source(flac):
            if pathlib.Path(flac) == moved_flac:
                with open(run.ingested_pairs, "ab") as handle:
                    handle.write(b"Album/01.flac\0" + flac.encode() + b"\0")

        run.read_ingest_source = read_ingest_source
        monkeypatch.setattr(
            im.workerpool, "map",
            lambda items, jobs, work: [work(item) for item in items])
        monkeypatch.setattr(im, "log", lambda line: None)
        counters = tmp_path / "counters"
        counters.mkdir()
        im._carry_after_beets(run, str(download), str(library),
                              str(counters), 1)
        assert (library / "Album" / "rip.log").exists()
