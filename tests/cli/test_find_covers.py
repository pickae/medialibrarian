"""The white box for medialib/cli/find_covers.py: which files are looked for,
and what is written for one a cover was found for - with the search itself
stood in for, since that is onlinecover's and aicover's to be tested by.

The process-level surface (the help page, the refusals) is the recorded CLI
contract under tests/data/cliContract/find-covers.
"""

import os

import pytest

from medialib.cli import find_covers
from medialib.lib import mutagentags, onlinecover, thumbnails

pytestmark = pytest.mark.fs


def _touch(root, *paths, data=b"x"):
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _run(tmp_path):
    (tmp_path / "out").mkdir(exist_ok=True)
    return find_covers.Run(str(tmp_path / "in"), str(tmp_path / "out"),
                           str(tmp_path), str(tmp_path / "meter"), "1920x1080>")


@pytest.fixture
def no_embedded(monkeypatch):
    monkeypatch.setattr(onlinecover, "has_embedded_cover", lambda _path: False)


class TestWhichFiles:
    def test_every_audio_file_at_every_depth_and_nothing_hidden(self, tmp_path):
        _touch(tmp_path, "a/01.mp3", "a/b/02.opus", "a/notes.txt",
               "a/.hidden.mp3", "a/.cache/03.mp3", "c.flac")
        assert find_covers.audio_files(str(tmp_path)) == [
            "c.flac", os.path.join("a", "01.mp3"),
            os.path.join("a", "b", "02.opus")]

    @pytest.mark.parametrize("image", [
        "folder.jpg", "Cover.PNG", "front.webp", "AlbumArt_Large.jpg",
        "track.jpg"])
    def test_a_cover_named_image_or_the_files_own_is_its_cover(
            self, tmp_path, image):
        _touch(tmp_path, "track.mp3", image)
        assert find_covers.folder_cover(str(tmp_path / "track.mp3")) \
            == str(tmp_path / image)

    @pytest.mark.parametrize("image", ["back.jpg", "booklet.jpg",
                                       "other.jpg", "cover.txt"])
    def test_any_other_image_is_not(self, tmp_path, image):
        _touch(tmp_path, "track.mp3", image)
        assert find_covers.folder_cover(str(tmp_path / "track.mp3")) == ""

    def test_only_the_files_own_folder_counts(self, tmp_path):
        _touch(tmp_path, "cover.jpg", "sub/track.mp3")
        assert find_covers.folder_cover(str(tmp_path / "sub/track.mp3")) == ""

    def test_the_only_audio_in_its_folder_is_a_book(self, tmp_path):
        _touch(tmp_path, "Book/Book.m4b", "Book/cover.txt",
               "Show/01.mp3", "Show/02.mp3")
        assert find_covers.is_book(str(tmp_path / "Book/Book.m4b"))
        assert not find_covers.is_book(str(tmp_path / "Show/01.mp3"))


class TestWanted:
    def test_a_folder_cover_leaves_it_alone_unless_told_otherwise(
            self, tmp_path, no_embedded):
        _touch(tmp_path, "in/a/track.mp3", "in/a/folder.jpg")
        run = _run(tmp_path)
        assert not run.wanted(os.path.join("a", "track.mp3"), False)
        assert run.tally.folder_cover == 1
        assert run.wanted(os.path.join("a", "track.mp3"), True)

    def test_a_file_with_its_own_cover_is_left_alone(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setattr(onlinecover, "has_embedded_cover",
                            lambda _path: True)
        _touch(tmp_path, "in/track.mp3")
        run = _run(tmp_path)
        assert not run.wanted("track.mp3", True)
        assert run.tally.own_cover == 1

    def test_what_an_earlier_run_wrote_is_not_looked_for_again(
            self, tmp_path, no_embedded):
        _touch(tmp_path, "in/track.mp3", "out/track.mp3")
        run = _run(tmp_path)
        assert not run.wanted("track.mp3", True)
        assert run.tally.done_before == 1

    @pytest.mark.parametrize("name", ["track.wav", "track.ape", "track.mka"])
    def test_a_format_that_cannot_carry_one_is_not_looked_for(
            self, tmp_path, no_embedded, name):
        _touch(tmp_path, "in/" + name)
        run = _run(tmp_path)
        assert not run.wanted(name, True)
        assert run.tally.cannot_carry == 1


class TestFound:
    @pytest.fixture
    def found(self, tmp_path, monkeypatch):
        """The search finds a JPEG; the embed copies it into a
        marker file beside the target and answers ``status``."""
        calls = {"status": 0, "track": [], "book": []}

        def track_image(source, seconds, dest_dir, search=None, **_kw):
            calls["track"].append(source)
            image = os.path.join(dest_dir, "online.jpg")
            with open(image, "wb") as handle:
                handle.write(b"JPEG")
            return image

        def book_cover(folder, seconds, stereo, ram_base, meter="", meta=None):
            calls["book"].append(folder)
            return track_image(folder, seconds, os.path.dirname(ram_base))

        def embed(track, target, image, cover, *args, **kwargs):
            with open(image, "rb") as src, open(cover, "wb") as out:
                out.write(src.read())
            with open(target, "ab") as handle:
                handle.write(b"+cover")
            return calls["status"]

        monkeypatch.setattr(onlinecover, "track_image", track_image)
        monkeypatch.setattr(onlinecover, "media_seconds", lambda _p: 3600.0)
        monkeypatch.setattr(onlinecover, "media_channels", lambda _p: 1)
        monkeypatch.setattr(thumbnails, "online_book_cover", book_cover)
        monkeypatch.setattr(thumbnails, "embed_cover_file", embed)
        return calls

    def _find(self, tmp_path, relative):
        run = _run(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir(exist_ok=True)
        run._find_in(relative, os.path.join(run.input_dir, relative),
                     str(scratch))
        return run

    def test_a_copy_with_the_cover_and_nothing_else(self, tmp_path, found):
        _touch(tmp_path, "in/Show/01.mp3", "in/Show/02.mp3", data=b"audio")
        source = tmp_path / "in/Show/01.mp3"
        os.utime(source, (1_000_000_000, 1_000_000_000))
        run = self._find(tmp_path, os.path.join("Show", "01.mp3"))

        assert run.tally.embedded == [os.path.join("Show", "01.mp3")]
        assert found["track"] == [str(source)]
        copy = tmp_path / "out/Show/01.mp3"
        assert copy.read_bytes() == b"audio+cover"
        assert os.stat(copy).st_mtime == 1_000_000_000
        # The input is only read, and nothing half-written is left over.
        assert source.read_bytes() == b"audio"
        assert sorted(os.listdir(tmp_path / "in/Show")) == ["01.mp3", "02.mp3"]
        assert os.listdir(tmp_path / "out/Show") == ["01.mp3"]

    def test_a_lone_file_is_looked_for_as_its_folders_book(self, tmp_path,
                                                           found):
        _touch(tmp_path, "in/Author/Book/Book.m4b")
        self._find(tmp_path, os.path.join("Author", "Book", "Book.m4b"))
        assert found["book"] == [str(tmp_path / "in/Author/Book")]
        assert found["track"] == [str(tmp_path / "in/Author/Book")]

    def test_a_failed_embed_leaves_no_copy(self, tmp_path, found):
        found["status"] = 1
        _touch(tmp_path, "in/a/01.mp3", "in/a/02.mp3")
        run = self._find(tmp_path, os.path.join("a", "01.mp3"))
        assert run.tally.unembedded == [os.path.join("a", "01.mp3")]
        assert os.listdir(tmp_path / "out/a") == []

    def test_nothing_found_writes_nothing(self, tmp_path, found, monkeypatch):
        monkeypatch.setattr(onlinecover, "track_image", lambda *a, **k: "")
        _touch(tmp_path, "in/a/01.mp3", "in/a/02.mp3")
        run = self._find(tmp_path, os.path.join("a", "01.mp3"))
        assert run.tally.missed == [os.path.join("a", "01.mp3")]
        assert os.listdir(tmp_path / "out") == []


def test_an_mp3_takes_its_cover_in_an_id3_frame(tmp_path):
    from mutagen.id3 import ID3

    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"\x00" * 64)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8JPEG")
    assert mutagentags.embed_cover(str(audio), str(cover)) == 0
    assert mutagentags.embed_cover(str(audio), str(cover)) == 0
    frames = ID3(str(audio)).getall("APIC")
    assert [frame.data for frame in frames] == [b"\xff\xd8JPEG"]
