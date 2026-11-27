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


def _run(tmp_path, min_pixels=0, **options):
    (tmp_path / "out").mkdir(exist_ok=True)
    return find_covers.Run(str(tmp_path / "in"), str(tmp_path / "out"),
                           str(tmp_path), str(tmp_path / "meter"), "1920x1080>",
                           min_pixels=min_pixels, **options)


QUARTER_MP = 250_000


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


class TestSmallCovers:
    @pytest.fixture
    def sizes(self, monkeypatch):
        """The size each image (by file name) and each file's embedded cover
        (by its path) is read as; anything not named reads as unknown."""
        table = {}
        monkeypatch.setattr(find_covers, "image_size",
                            lambda path: table.get(os.path.basename(path),
                                                   (0, 0)))
        monkeypatch.setattr(onlinecover, "embedded_cover_size",
                            lambda path: table.get(os.path.basename(path),
                                                   (0, 0)))
        return table

    @pytest.fixture
    def embedded(self, monkeypatch):
        monkeypatch.setattr(onlinecover, "has_embedded_cover",
                            lambda _path: True)

    def test_a_small_embedded_cover_is_looked_past(self, tmp_path, embedded,
                                                   sizes):
        sizes["track.mp3"] = (300, 300)
        _touch(tmp_path, "in/track.mp3")
        run = _run(tmp_path, QUARTER_MP)
        assert run.wanted("track.mp3", False)
        assert run.replacing == {"track.mp3": (300, 300)}
        assert run.label("track.mp3") == "track.mp3 (its cover is 300x300)"

    @pytest.mark.parametrize("size", [(500, 500), (0, 0)])
    def test_one_at_the_threshold_or_unreadable_is_kept(self, tmp_path,
                                                        embedded, sizes, size):
        sizes["track.mp3"] = size
        _touch(tmp_path, "in/track.mp3")
        run = _run(tmp_path, QUARTER_MP)
        assert not run.wanted("track.mp3", False)
        assert run.tally.own_cover == 1

    def test_a_threshold_of_zero_keeps_every_cover(self, tmp_path, embedded,
                                                   sizes):
        sizes["track.mp3"] = (10, 10)
        _touch(tmp_path, "in/track.mp3")
        assert not _run(tmp_path, 0).wanted("track.mp3", False)

    def test_a_small_folder_cover_is_looked_past(self, tmp_path, no_embedded,
                                                 sizes):
        sizes["folder.jpg"] = (200, 200)
        _touch(tmp_path, "in/a/track.mp3", "in/a/folder.jpg")
        run = _run(tmp_path, QUARTER_MP)
        relative = os.path.join("a", "track.mp3")
        assert run.wanted(relative, False)
        assert run.tally.folder_cover == 0
        assert run.replacing == {relative: (200, 200)}

    def test_a_large_embedded_cover_keeps_a_small_folder_one(
            self, tmp_path, embedded, sizes):
        sizes["folder.jpg"] = (200, 200)
        sizes["track.mp3"] = (1000, 1000)
        _touch(tmp_path, "in/a/track.mp3", "in/a/folder.jpg")
        run = _run(tmp_path, QUARTER_MP)
        assert not run.wanted(os.path.join("a", "track.mp3"), False)
        assert run.tally.own_cover == 1

    def test_the_larger_of_two_small_covers_is_the_one_to_beat(
            self, tmp_path, embedded, sizes):
        sizes["folder.jpg"] = (200, 200)
        sizes["track.mp3"] = (400, 400)
        _touch(tmp_path, "in/a/track.mp3", "in/a/folder.jpg")
        run = _run(tmp_path, QUARTER_MP)
        relative = os.path.join("a", "track.mp3")
        assert run.wanted(relative, False)
        assert run.replacing == {relative: (400, 400)}


def test_the_largest_size_among_lines_is_taken():
    assert onlinecover.largest_size("300,300\n\n1200,1200\n600,600\n") \
        == (1200, 1200)
    assert onlinecover.largest_size("500 400\n") == (500, 400)
    assert onlinecover.largest_size("N/A,N/A\n") == (0, 0)


class TestFound:
    @pytest.fixture
    def found(self, tmp_path, monkeypatch):
        """The search finds a JPEG; the embed copies it into a
        marker file beside the target and answers ``status``."""
        calls = {"status": 0, "track": [], "book": [], "seconds": [],
                 "suffix": ".jpg"}

        def track_image(source, seconds, dest_dir, search=None, **_kw):
            calls["track"].append(source)
            image = os.path.join(dest_dir, "online" + calls["suffix"])
            with open(image, "wb") as handle:
                handle.write(b"JPEG")
            return image

        def book_cover(folder, seconds, stereo, ram_base, meter="", meta=None):
            calls["book"].append(folder)
            calls["seconds"].append(seconds)
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
        assert run.tally.unwritten == [os.path.join("a", "01.mp3")]
        assert os.listdir(tmp_path / "out/a") == []

    @pytest.mark.parametrize("found_size,written", [
        ((1000, 1000), True), ((300, 300), False), ((0, 0), False)])
    def test_a_small_cover_is_replaced_only_by_a_larger_one(
            self, tmp_path, found, monkeypatch, found_size, written):
        monkeypatch.setattr(find_covers, "image_size", lambda _p: found_size)
        _touch(tmp_path, "in/a/01.mp3", "in/a/02.mp3", data=b"audio")
        relative = os.path.join("a", "01.mp3")
        run = _run(tmp_path, QUARTER_MP)
        run.replacing[relative] = (300, 300)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        run._find_in(relative, os.path.join(run.input_dir, relative),
                     str(scratch))
        assert (tmp_path / "out/a/01.mp3").exists() == written
        assert run.tally.embedded == ([relative] if written else [])
        assert run.tally.no_larger == ([] if written else [relative])

    def test_nothing_found_writes_nothing(self, tmp_path, found, monkeypatch):
        monkeypatch.setattr(onlinecover, "track_image", lambda *a, **k: "")
        _touch(tmp_path, "in/a/01.mp3", "in/a/02.mp3")
        run = self._find(tmp_path, os.path.join("a", "01.mp3"))
        assert run.tally.missed == [os.path.join("a", "01.mp3")]
        assert os.listdir(tmp_path / "out") == []

    def _find_album(self, tmp_path, folder, tracks, **options):
        run = _run(tmp_path, **options)
        run.albums[folder] = tracks
        scratch = tmp_path / "scratch"
        scratch.mkdir(exist_ok=True)
        run._find_in(folder, os.path.join(run.input_dir, folder), str(scratch))
        return run

    def test_an_album_gets_one_folder_image_and_no_copies(self, tmp_path,
                                                          found):
        _touch(tmp_path, "in/Artist/Album/01 One.flac",
               "in/Artist/Album/02 Two.flac", data=b"audio")
        album = os.path.join("Artist", "Album")
        tracks = [os.path.join(album, "01 One.flac"),
                  os.path.join(album, "02 Two.flac")]
        run = self._find_album(tmp_path, album, tracks)

        assert found["book"] == [str(tmp_path / "in/Artist/Album")]
        # Looked for as the whole album, by all its tracks' length.
        assert found["seconds"] == [7200.0]
        assert run.tally.beside == [album]
        assert os.listdir(tmp_path / "out/Artist/Album") == ["folder.jpg"]
        assert (tmp_path / "out/Artist/Album/folder.jpg").read_bytes() \
            == b"JPEG"
        assert sorted(os.listdir(tmp_path / "in/Artist/Album")) \
            == ["01 One.flac", "02 Two.flac"]

    def test_an_album_keeps_a_png_as_one(self, tmp_path, found):
        found["suffix"] = ".png"
        _touch(tmp_path, "in/Album/01.mp3", "in/Album/02.mp3")
        self._find_album(tmp_path, "Album", [os.path.join("Album", "01.mp3"),
                                             os.path.join("Album", "02.mp3")])
        assert os.listdir(tmp_path / "out/Album") == ["folder.png"]

    def test_an_album_at_the_top_writes_into_the_output_itself(
            self, tmp_path, found):
        _touch(tmp_path, "in/01.mp3", "in/02.mp3")
        self._find_album(tmp_path, "", ["01.mp3", "02.mp3"])
        assert os.listdir(tmp_path / "out") == ["folder.jpg"]

    def test_a_small_album_cover_is_replaced_only_by_a_larger_one(
            self, tmp_path, found, monkeypatch):
        monkeypatch.setattr(find_covers, "image_size", lambda _p: (300, 300))
        _touch(tmp_path, "in/Album/01.mp3", "in/Album/02.mp3")
        run = _run(tmp_path, QUARTER_MP)
        run.albums["Album"] = ["01.mp3", "02.mp3"]
        run.replacing["Album"] = (400, 400)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        run._find_in("Album", str(tmp_path / "in/Album"), str(scratch))
        assert run.tally.no_larger == ["Album"]
        assert not (tmp_path / "out/Album").exists()

    def test_a_sidecar_is_named_after_its_file(self, tmp_path, found):
        _touch(tmp_path, "in/Show/Episode.mp3", "in/Show/Other.mp3",
               data=b"audio")
        run = _run(tmp_path, sidecar=True)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        relative = os.path.join("Show", "Episode.mp3")
        run._find_in(relative, os.path.join(run.input_dir, relative),
                     str(scratch))
        assert run.tally.beside == [relative]
        assert run.tally.embedded == []
        assert os.listdir(tmp_path / "out/Show") == ["Episode.jpg"]

    def test_an_image_of_another_kind_is_turned_into_a_jpeg(
            self, tmp_path, found, monkeypatch):
        found["suffix"] = ".webp"
        converted = []

        def convert(argv, **_kw):
            converted.append(argv)
            with open(argv[-1], "wb") as handle:
                handle.write(b"JPEG")
            return 0

        monkeypatch.setattr(find_covers.toolcapture, "run", convert)
        _touch(tmp_path, "in/Show/Episode.mp3", "in/Show/Other.mp3")
        run = _run(tmp_path, sidecar=True)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        relative = os.path.join("Show", "Episode.mp3")
        run._find_in(relative, os.path.join(run.input_dir, relative),
                     str(scratch))
        assert len(converted) == 1
        assert os.listdir(tmp_path / "out/Show") == ["Episode.jpg"]


class TestAlbums:
    @pytest.mark.parametrize("name", [
        "01 One.flac", "1-03 Three.mp3", "CD2-04 Four.mp3",
        "Disc 1 - 05 Five.mp3", "03. Three.opus", "Track 07.m4a", "07.mp3",
        "12_Twelve.ogg"])
    def test_a_name_opening_with_a_track_number_is_a_track(self, name):
        assert find_covers.is_track_name(name)

    @pytest.mark.parametrize("name", [
        "2019-05-01 Episode.mp3", "Episode 3.mp3", "The 39 Steps.mp3",
        "1984 Chapter.mp3", "Intro.mp3"])
    def test_any_other_name_is_not(self, name):
        assert not find_covers.is_track_name(name)

    def test_numbered_tracks_are_an_album(self, tmp_path):
        assert find_covers.is_album(str(tmp_path), ["01 A.mp3", "02 B.mp3",
                                                    "Bonus.mp3"],
                                    probe=lambda _p: pytest.fail("probed"))

    def test_one_file_is_never_an_album(self, tmp_path):
        assert not find_covers.is_album(str(tmp_path), ["01 A.mp3"])

    @pytest.mark.parametrize("last,expected", [
        ({"album": "Blue Hour", "track": "9"}, True),
        ({"album": "Other", "track": "9"}, False),
        ({"album": "Blue Hour"}, False)])
    def test_unnumbered_names_are_an_album_by_their_tags(self, tmp_path,
                                                         last, expected):
        tags = {"A.mp3": {"album": "blue hour", "track": "1/9"}, "Z.mp3": last}
        assert find_covers.is_album(
            str(tmp_path), ["Z.mp3", "A.mp3"],
            probe=lambda path: tags[os.path.basename(path)]) is expected

    @pytest.fixture
    def embedded(self, monkeypatch):
        monkeypatch.setattr(onlinecover, "has_embedded_cover",
                            lambda _path: True)

    def _plan(self, run, ignore=False):
        return run.plan(find_covers.audio_files(run.input_dir), ignore)

    def test_an_album_is_looked_for_once_whatever_its_tracks_carry(
            self, tmp_path, embedded):
        _touch(tmp_path, "in/Album/01 A.mp3", "in/Album/02 B.mp3",
               "in/Album/03 C.wav", "in/Lone/Talk.mp3")
        run = _run(tmp_path)
        assert self._plan(run) == ["Album"]
        assert run.albums == {"Album": [os.path.join("Album", "01 A.mp3"),
                                        os.path.join("Album", "02 B.mp3"),
                                        os.path.join("Album", "03 C.wav")]}
        assert run.label("Album") == "Album (album of 3 tracks)"
        # The lone file carries its own, so it is left alone.
        assert run.tally.own_cover == 1

    @pytest.mark.parametrize("image", ["folder.jpg", "Cover.png",
                                       "front.webp"])
    def test_an_album_with_a_cover_image_is_left_alone_unless_told(
            self, tmp_path, no_embedded, image):
        _touch(tmp_path, "in/Album/01.mp3", "in/Album/02.mp3",
               "in/Album/" + image)
        run = _run(tmp_path)
        assert self._plan(run) == []
        assert run.tally.album_cover == 1
        assert self._plan(_run(tmp_path), ignore=True) == ["Album"]

    def test_a_track_named_image_is_no_album_cover(self, tmp_path,
                                                   no_embedded):
        _touch(tmp_path, "in/Album/01.mp3", "in/Album/02.mp3",
               "in/Album/01.jpg")
        assert self._plan(_run(tmp_path)) == ["Album"]

    def test_an_album_written_before_is_not_looked_for_again(
            self, tmp_path, no_embedded):
        _touch(tmp_path, "in/Album/01.mp3", "in/Album/02.mp3",
               "out/Album/Folder.PNG")
        run = _run(tmp_path)
        assert self._plan(run) == []
        assert run.tally.done_before == 1

    def test_a_small_album_cover_is_looked_past(self, tmp_path, no_embedded,
                                                monkeypatch):
        monkeypatch.setattr(find_covers, "image_size", lambda _p: (200, 200))
        _touch(tmp_path, "in/Album/01.mp3", "in/Album/02.mp3",
               "in/Album/folder.jpg")
        run = _run(tmp_path, QUARTER_MP)
        assert self._plan(run) == ["Album"]
        assert run.replacing == {"Album": (200, 200)}

    def test_per_track_looks_for_each_track_by_itself(self, tmp_path,
                                                      no_embedded):
        _touch(tmp_path, "in/Album/01.mp3", "in/Album/02.mp3")
        run = _run(tmp_path, per_track=True)
        assert self._plan(run) == [os.path.join("Album", "01.mp3"),
                                   os.path.join("Album", "02.mp3")]
        assert run.albums == {}

    def test_an_album_is_not_held_to_the_minimums(self, tmp_path,
                                                  no_embedded, monkeypatch):
        monkeypatch.setattr(onlinecover, "media_seconds", lambda _p: 10.0)
        _touch(tmp_path, "in/Album/01.mp3", "in/Album/02.mp3")
        run = _run(tmp_path, min_seconds=600, min_bytes=10_000_000)
        assert self._plan(run) == ["Album"]


class TestMinimums:
    def test_a_file_under_the_size_is_left_alone(self, tmp_path, no_embedded):
        _touch(tmp_path, "in/small.mp3", data=b"x" * 10)
        _touch(tmp_path, "in/large.mp3", data=b"x" * 2000)
        run = _run(tmp_path, min_bytes=1000)
        assert not run.wanted("small.mp3", True)
        assert run.wanted("large.mp3", True)
        assert run.tally.below_minimum == 1

    @pytest.mark.parametrize("seconds,wanted", [
        (30.0, False), (600.0, True), (0.0, True)])
    def test_a_file_under_the_length_is_left_alone(self, tmp_path,
                                                   no_embedded, monkeypatch,
                                                   seconds, wanted):
        monkeypatch.setattr(onlinecover, "media_seconds", lambda _p: seconds)
        _touch(tmp_path, "in/track.mp3")
        run = _run(tmp_path, min_seconds=300)
        assert run.wanted("track.mp3", True) is wanted

    def test_no_minimum_reads_no_length(self, tmp_path, no_embedded,
                                        monkeypatch):
        monkeypatch.setattr(onlinecover, "media_seconds",
                            lambda _p: pytest.fail("probed"))
        _touch(tmp_path, "in/track.mp3")
        assert _run(tmp_path).wanted("track.mp3", True)


class TestSidecarWanted:
    def test_any_format_can_have_one(self, tmp_path, no_embedded):
        _touch(tmp_path, "in/track.wav")
        run = _run(tmp_path, sidecar=True)
        assert run.wanted("track.wav", True)
        assert run.tally.cannot_carry == 0

    def test_one_written_before_is_not_looked_for_again(self, tmp_path,
                                                        no_embedded):
        _touch(tmp_path, "in/a/track.mp3", "out/a/track.png")
        run = _run(tmp_path, sidecar=True)
        assert not run.wanted(os.path.join("a", "track.mp3"), True)
        assert run.tally.done_before == 1

    def test_an_embedded_copy_from_before_does_not_count(self, tmp_path,
                                                         no_embedded):
        _touch(tmp_path, "in/track.mp3", "out/track.mp3")
        assert _run(tmp_path, sidecar=True).wanted("track.mp3", True)


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
