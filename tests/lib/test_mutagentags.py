"""medialib.lib.mutagentags against a file mutagen itself can stand up.

The 42-byte FLAC below is the `fLaC` marker plus a STREAMINFO block and
nothing else, and mutagen loads, tags and saves it. That is all the writer
needs to see, so the chapter and cover decisions run here, in the default
run, and the media tier keeps what an encoder must make: the opus file and
the MP4. The two Vorbis containers carry the same comments, so what is
decided here is the decision itself - keep or replace, which picture wins -
not the container.
"""

import io
import re
import struct

import pytest

from medialib.lib import mutagentags

pytestmark = pytest.mark.fs

# `fLaC`, then a STREAMINFO block: the 4-byte header (last block, type 0,
# length 34) over 34 bytes that say 44100 Hz, two channels, 16 bits and no
# audio at all. 42 bytes is the whole file.
_MINI_FLAC = (b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22])
              + struct.pack(">HH", 4092, 4092)
              + b"\x00" * 6
              + struct.pack(">Q", (44100 << 44) | (2 << 41) | (16 << 36))
              + b"\x00" * 16)
assert len(_MINI_FLAC) == 42

THREE = ("CHAPTER01=00:00:00.000\nCHAPTER01NAME=First\n"
         "CHAPTER02=00:00:01.000\nCHAPTER02NAME=Second\n"
         "CHAPTER03=00:00:02.000\nCHAPTER03NAME=Third\n")
ONE = "CHAPTER01=00:00:00.000\nCHAPTER01NAME=Replaced\n"


def _flac(tmp_path, name="audio.flac"):
    """The 42-byte file, standing in for one the pipeline has just produced."""
    path = tmp_path / name
    path.write_bytes(_MINI_FLAC)
    return path


def _chapters_file(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="ascii")
    return path


def _load(path):
    from mutagen.flac import FLAC
    return FLAC(str(path))


def _tag(path, field):
    """Read a tag back through the same library that wrote it."""
    return "|".join(_load(path).get(field, []))


def _chapters(path):
    """The CHAPTERnn= marks, not the NAME half of each pair."""
    return sum(1 for key in _load(path)
               if re.fullmatch(r"CHAPTER\d+", key.upper()))


def _write(audio, chapter_file, title="", force=False):
    """The call's status and whatever it said while making the decision."""
    said = io.StringIO()
    status = mutagentags.embed_chapters(str(audio), str(chapter_file), title,
                                        force=force, error=said)
    return status, said.getvalue()


class TestChapters:
    def test_a_plain_file_gets_them_with_the_title(self, tmp_path):
        flac = _flac(tmp_path)
        status, _ = _write(flac,
                           _chapters_file(tmp_path, "three.chapters", THREE),
                           "Plain Title")
        assert status == 0
        assert _chapters(flac) == 3
        assert _tag(flac, "TITLE") == "Plain Title"
        assert _tag(flac, "CHAPTER01NAME") == "First"

    def test_a_second_run_keeps_what_is_there(self, tmp_path):
        """Marks placed by hand survive a rerun that would otherwise flatten
        them: the run keeps them and says so, naming the flag that would
        change its mind, and it still writes the title it was handed."""
        flac = _flac(tmp_path)
        _write(flac, _chapters_file(tmp_path, "three.chapters", THREE),
               "Plain Title")
        status, said = _write(flac, _chapters_file(tmp_path, "one.chapters",
                                                   ONE), "Second Title")
        assert status == 0
        assert _chapters(flac) == 3
        assert _tag(flac, "CHAPTER01NAME") == "First"
        assert _tag(flac, "TITLE") == "Second Title"
        assert mutagentags.FORCE_FLAG in said

    def test_force_makes_the_chapter_file_the_whole_truth(self, tmp_path):
        flac = _flac(tmp_path)
        _write(flac, _chapters_file(tmp_path, "three.chapters", THREE),
               "Plain Title")
        status, _ = _write(flac, _chapters_file(tmp_path, "one.chapters", ONE),
                           "Forced Title", force=True)
        assert status == 0
        assert _chapters(flac) == 1
        assert _tag(flac, "CHAPTER01NAME") == "Replaced"
        # the ones past the end of the new set are gone
        assert _tag(flac, "CHAPTER03NAME") == ""
        assert _tag(flac, "TITLE") == "Forced Title"

    def test_an_empty_chapter_file_writes_the_title_only(self, tmp_path):
        """The caller hands over /dev/null when it built no chapters."""
        flac = _flac(tmp_path)
        status, _ = _write(flac, "/dev/null", "Only A Title")
        assert status == 0
        assert _chapters(flac) == 0
        assert _tag(flac, "TITLE") == "Only A Title"


class TestTheCover:
    def test_a_rerun_replaces_rather_than_accumulates(self, tmp_path):
        """A library re-ingested twice must not carry two copies of its art:
        that is the whole reason the path clears what is there first."""
        flac = _flac(tmp_path)
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"jpeg bytes")
        for _ in range(2):
            assert mutagentags.embed_cover(str(flac), str(cover)) == 0
        pictures = _load(flac).pictures
        assert len(pictures) == 1
        assert pictures[0].type == 3
        assert pictures[0].mime == "image/jpeg"
        assert pictures[0].data == cover.read_bytes()


class TestTheStatusIsAStatus:
    """A corrupt file is one file a caller counts as failed, where a raise
    would take the whole run down."""

    def test_junk_input_is_a_status_not_a_traceback(self, tmp_path):
        junk = tmp_path / "notaudio.flac"
        junk.write_bytes(b"this is not a FLAC file")
        assert mutagentags.embed_chapters(
            str(junk),
            str(_chapters_file(tmp_path, "c.chapters", THREE))) == 1

    def test_the_same_for_a_cover(self, tmp_path):
        junk = tmp_path / "notaudio.flac"
        junk.write_bytes(b"this is not a FLAC file")
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"jpeg bytes")
        assert mutagentags.embed_cover(str(junk), str(cover)) == 1

    def test_and_for_a_file_that_is_not_there_at_all(self, tmp_path):
        missing = str(tmp_path / "gone.flac")
        assert mutagentags.embed_chapters(
            missing, str(_chapters_file(tmp_path, "c.chapters", THREE))) == 1
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"jpeg bytes")
        assert mutagentags.embed_cover(missing, str(cover)) == 1

    def test_a_cover_file_that_is_not_there_is_a_status_too(self, tmp_path):
        flac = _flac(tmp_path)
        assert mutagentags.embed_cover(
            str(flac), str(tmp_path / "gone.jpg")) == 1
