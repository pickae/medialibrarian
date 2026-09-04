"""Tests for medialib.lib.cover - choosing a folder's cover art.

The three selection rules are stacked, so each is only visible when the one above
it ties. These build the folder each rule needs in order to be the one that
decides, which is the thing a generated corpus does by accident and a reader
cannot see it doing.
"""

import pytest

from medialib.lib import cover, safety

pytestmark = pytest.mark.fs


def build(directory, *entries):
    """``name`` or ``(name, size)`` entries, as real files."""
    for entry in entries:
        name, size = entry if isinstance(entry, tuple) else (entry, 0)
        path = directory / name
        path.write_bytes(b"x" * size)
    return directory


class TestWhichWordWins:
    def test_folder_beats_front(self, tmp_path):
        build(tmp_path, "front.jpg", "folder art.png")
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/folder art.png"

    def test_front_beats_cover(self, tmp_path):
        build(tmp_path, "cover.jpg", "front.png")
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/front.png"

    def test_outright_even_when_the_loser_is_far_larger(self, tmp_path):
        """The priority is not a tie-break: it settles the group before size is
        looked at, so a 9 KB cover loses to an empty front."""
        build(tmp_path, ("cover.jpg", 9001), ("front.png", 0))
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/front.png"

    def test_the_word_may_sit_inside_a_longer_name(self, tmp_path):
        build(tmp_path, "scan of the front 2.jpg")
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/scan of the front 2.jpg"

    def test_case_does_not_matter(self, tmp_path):
        build(tmp_path, "COVER.JPG")
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/COVER.JPG"

    def test_the_word_must_be_in_the_name_and_not_the_extension(self, tmp_path):
        """A file called ``scan.cover`` is not cover art; it is not even an image."""
        build(tmp_path, "scan.cover")
        assert cover.choose_cover(str(tmp_path)) is None


class TestWhenTheWordTies:
    def test_the_largest_file_wins(self, tmp_path):
        build(tmp_path, ("cover a.jpg", 10), ("cover b.jpg", 4096), ("cover c.jpg", 512))
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/cover b.jpg"

    def test_an_empty_file_is_still_a_candidate(self, tmp_path):
        """The "nothing seen yet" mark has to be below zero, because zero bytes is
        a real size and a folder can hold nothing but empty candidates."""
        build(tmp_path, ("cover.jpg", 0))
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/cover.jpg"


class TestWhenTheSizeTiesToo:
    def test_natural_order_decides(self, tmp_path):
        build(tmp_path, ("cover 10.jpg", 7), ("cover 2.jpg", 7), ("cover 1.jpg", 7))
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/cover 1.jpg"

    def test_natural_order_is_not_byte_order(self, tmp_path):
        """Byte order would put "cover 10" second; the version sort puts it last."""
        build(tmp_path, ("cover 9.jpg", 7), ("cover 10.jpg", 7))
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/cover 9.jpg"


class TestWhatIsNotACandidate:
    def test_a_folder_with_no_images_has_no_cover(self, tmp_path):
        build(tmp_path, "cover.txt", "front.nfo")
        assert cover.choose_cover(str(tmp_path)) is None

    def test_an_image_with_no_cover_word_is_passed_over(self, tmp_path):
        build(tmp_path, "scan.jpg", "page 1.png")
        assert cover.choose_cover(str(tmp_path)) is None

    def test_a_format_only_the_cover_list_knows_still_counts(self, tmp_path):
        """imageExtensions has no svg and coverImageExtensions has no webp; the
        union of the two is what an image means here."""
        build(tmp_path, "cover.svg")
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/cover.svg"

    def test_a_subfolder_named_cover_is_not_a_file(self, tmp_path):
        (tmp_path / "cover art").mkdir()
        assert cover.choose_cover(str(tmp_path)) is None

    def test_only_the_immediate_folder_is_looked_at(self, tmp_path):
        (tmp_path / "scans").mkdir()
        build(tmp_path / "scans", "cover.jpg")
        assert cover.choose_cover(str(tmp_path)) is None

    def test_an_extensionless_name_is_not_an_image(self, tmp_path):
        build(tmp_path, "cover")
        assert cover.choose_cover(str(tmp_path)) is None

    def test_a_hidden_image_is_not_a_candidate(self, tmp_path):
        """The behaviour item 7.4 changed: a dot-leading name reached every filter
        built from the central lists as the extension it ended in, so a
        ".cover.jpg" a tool left behind could be chosen as a folder's art."""
        build(tmp_path, ".cover.jpg")
        assert cover.choose_cover(str(tmp_path)) is None

    def test_and_a_hidden_one_does_not_beat_a_real_one(self, tmp_path):
        build(tmp_path, (".folder.jpg", 100), ("front.jpg", 10))
        assert cover.choose_cover(str(tmp_path)) == f"{tmp_path}/front.jpg"

    def test_the_word_cannot_hide_in_the_extension(self, tmp_path):
        """No image extension contains one of the three words, so looking for the
        word in the whole name instead of the stem would pick the same file. The
        stem is used because that is the rule; this pins the reason it is safe."""
        assert not [w for w in cover.COVER_WORDS
                    for e in cover._IMAGE_EXTENSIONS if w in f".{e}"]


class TestTheRename:
    def test_the_winner_is_promoted_and_the_extension_lowered(self, tmp_path):
        build(tmp_path, "Front Scan.JPG")
        message = cover.rename_cover_to_folder(str(tmp_path))
        assert (tmp_path / "folder.jpg").exists()
        assert message == f'  "{tmp_path.name}": cover art "Front Scan.JPG" -> "folder.jpg"'

    def test_a_winner_already_called_folder_is_not_an_event(self, tmp_path):
        build(tmp_path, "folder.jpg")
        assert cover.rename_cover_to_folder(str(tmp_path)) is None
        assert (tmp_path / "folder.jpg").exists()

    def test_an_occupied_destination_is_refused_and_recorded(self, tmp_path):
        # The winner has to be a "folder" file too: anything called folder.<ext>
        # carries the top-priority word itself, so a lower-priority file can never
        # be the one that finds it in the way.
        build(tmp_path, ("folder.jpg", 1), ("my folder.jpg", 4096))
        log = safety.SkipLog()

        assert cover.rename_cover_to_folder(str(tmp_path), log) is None
        assert (tmp_path / "my folder.jpg").exists()
        assert (tmp_path / "folder.jpg").stat().st_size == 1
        assert log.skips == [(f"{tmp_path}/my folder.jpg", f"{tmp_path}/folder.jpg")]

    def test_exactly_one_file_is_ever_moved(self, tmp_path):
        build(tmp_path, ("front a.jpg", 1), ("front b.jpg", 2), ("cover.png", 3))
        cover.rename_cover_to_folder(str(tmp_path))
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "cover.png",
            "folder.jpg",
            "front a.jpg",
        ]

    def test_a_path_that_is_not_a_folder_is_not_an_error(self, tmp_path):
        build(tmp_path, "cover.jpg")
        assert cover.rename_cover_to_folder(str(tmp_path / "cover.jpg")) is None

    def test_a_folder_that_is_not_there_is_not_an_error(self, tmp_path):
        assert cover.rename_cover_to_folder(str(tmp_path / "gone")) is None
