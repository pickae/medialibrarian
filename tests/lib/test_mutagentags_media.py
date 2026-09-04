"""Tier D for medialib.lib.mutagentags: what it writes into a REAL file, and
which of the two chapter sets wins when the file already has some.

The stubbed tests can only show that the writer was called. The tags it leaves
behind need real mutagen to read back, and a real encoder to make a file worth
reading - so this tier encodes one and reads it with the same library the writer
used. It is opt-in (`pytest -m media`) because of exactly that.

What decides between the two chapter sets is ``--force``: without it, chapters
already in the file are kept, so marks placed by hand survive a rerun that would
otherwise flatten them; with it, the chapter file is the whole truth.
"""

import io
import re
import shutil
import subprocess
import sys

import pytest

from medialib.lib import mutagentags

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None,
                       reason="tier D needs a real ffmpeg"),
    pytest.mark.skipif(
        subprocess.run([sys.executable, "-c", "import mutagen"],
                       capture_output=True).returncode != 0,
        reason="tier D reads the tags back with real mutagen"),
]

THREE = ("CHAPTER01=00:00:00.000\nCHAPTER01NAME=First\n"
         "CHAPTER02=00:00:01.000\nCHAPTER02NAME=Second\n"
         "CHAPTER03=00:00:02.000\nCHAPTER03NAME=Third\n")
ONE = "CHAPTER01=00:00:00.000\nCHAPTER01NAME=Replaced\n"


def _load(path):
    from mutagen.flac import FLAC
    from mutagen.oggopus import OggOpus
    return OggOpus(path) if str(path).lower().endswith(".opus") else FLAC(path)


def _tag(path, field):
    """Read a tag back through the same library that wrote it, not ffprobe."""
    return "|".join(_load(path).get(field, []))


def _chapters(path):
    """The CHAPTERnn= marks, not the NAME half of each pair."""
    return sum(1 for key in _load(path)
               if re.fullmatch(r"CHAPTER\d+", key.upper()))


class _Wrote:
    """What the call answered: the status, and whatever it said while doing it.

    The same two things the subprocess this replaces handed back, so every
    assertion below reads as it did.
    """

    def __init__(self, returncode, stderr):
        self.returncode = returncode
        self.stderr = stderr


def _write(*args):
    force = bool(args) and args[0] == "--force"
    audio, chapters, *rest = args[1:] if force else args
    said = io.StringIO()
    status = mutagentags.embed_chapters(str(audio), str(chapters),
                                        str(rest[0]) if rest else "",
                                        force=force, error=said)
    return _Wrote(status, said.getvalue())


@pytest.fixture
def audio(tmp_path):
    """A real encoded file of each container, chapterless, standing in for one
    the pipeline has just produced."""
    def make(name):
        path = tmp_path / name
        codec = ["-c:a", "libopus", "-b:a", "64k"] if name.endswith(".opus") \
            else ["-c:a", "flac"]
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=3", *codec, str(path)],
            check=True, stdin=subprocess.DEVNULL)
        return path
    return make


@pytest.fixture
def chapter_file(tmp_path):
    def make(name, text):
        path = tmp_path / name
        path.write_text(text)
        return path
    return make


class TestAFileWithNoChaptersYet:
    def test_it_gets_them_with_no_flag_needed(self, audio, chapter_file):
        flac = audio("plain.flac")
        _write(flac, chapter_file("three.chapters", THREE), "Plain Title")
        assert _chapters(flac) == 3
        assert _tag(flac, "TITLE") == "Plain Title"
        assert _tag(flac, "CHAPTER01NAME") == "First"


class TestAFileThatAlreadyHasChapters:
    @pytest.fixture
    def already(self, audio, chapter_file):
        flac = audio("plain.flac")
        _write(flac, chapter_file("three.chapters", THREE), "Plain Title")
        return flac, chapter_file("one.chapters", ONE)

    def test_a_second_run_leaves_them_alone_down_to_their_names(self, already):
        flac, one = already
        _write(flac, one, "Second Title")
        assert _chapters(flac) == 3
        assert _tag(flac, "CHAPTER01NAME") == "First"

    def test_but_it_still_writes_the_title_it_was_handed(self, already):
        flac, one = already
        _write(flac, one, "Second Title")
        assert _tag(flac, "TITLE") == "Second Title"

    def test_it_says_so_naming_the_flag(self, already):
        """Rather than quietly doing nothing."""
        flac, one = already
        assert "--force" in _write(flac, one).stderr

    def test_and_keeping_them_is_not_a_failure(self, already):
        """A caller checking the status must not read it as one."""
        flac, one = already
        assert _write(flac, one).returncode == 0


class TestForceMakesTheChapterFileTheWholeTruth:
    @pytest.fixture
    def already(self, audio, chapter_file):
        flac = audio("plain.flac")
        _write(flac, chapter_file("three.chapters", THREE), "Plain Title")
        return flac, chapter_file("one.chapters", ONE)

    def test_the_set_is_replaced_wholesale(self, already):
        flac, one = already
        _write("--force", flac, one, "Forced Title")
        assert _chapters(flac) == 1
        assert _tag(flac, "CHAPTER01NAME") == "Replaced"
        # the ones past the end of the new set are gone
        assert _tag(flac, "CHAPTER03NAME") == ""
        assert _tag(flac, "TITLE") == "Forced Title"


class TestAnEmptyChapterFile:
    """The caller hands over /dev/null when it built no chapters."""

    def test_it_writes_no_chapters_and_the_title_still_lands(self, audio):
        flac = audio("titleOnly.flac")
        _write("--force", flac, "/dev/null", "Only A Title")
        assert _chapters(flac) == 0
        assert _tag(flac, "TITLE") == "Only A Title"

    def test_with_force_it_clears_what_was_there(self, audio, chapter_file):
        """So nothing inherited survives a rebuild that produced none."""
        flac = audio("plain.flac")
        _write(flac, chapter_file("three.chapters", THREE), "Plain Title")
        _write("--force", flac, "/dev/null", "Cleared")
        assert _chapters(flac) == 0


class TestTheOtherContainer:
    """Opus takes a different mutagen loader, so both halves are checked there
    too."""

    def test_an_opus_takes_them_keeps_them_and_gives_them_up_to_force(
            self, audio, chapter_file):
        opus = audio("track.opus")
        three = chapter_file("three.chapters", THREE)
        one = chapter_file("one.chapters", ONE)
        _write(opus, three, "Opus Title")
        assert _chapters(opus) == 3
        _write(opus, one)
        assert _chapters(opus) == 3
        _write("--force", opus, one)
        assert _chapters(opus) == 1
        # the title it already had is left alone
        assert _tag(opus, "TITLE") == "Opus Title"


# --- the cover art ------------------------------------------------------------
# Never verified before this module existed: the writer was a subprocess, so the
# stubbed tests could assert the argv it was called with and nothing else. What
# a player actually reads is a picture block, and only mutagen can say whether
# one is there.

@pytest.fixture
def cover(tmp_path):
    """A real JPEG, the way the pipelines produce one."""
    path = tmp_path / "cover.jpg"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=64x64:duration=1", "-frames:v", "1", str(path)],
        check=True, stdin=subprocess.DEVNULL)
    return path


def _pictures(path):
    """The embedded pictures, however the container carries them."""
    handle = _load(path)
    if str(path).lower().endswith(".opus"):
        import base64

        from mutagen.flac import Picture
        return [Picture(base64.b64decode(blob))
                for blob in handle.get("METADATA_BLOCK_PICTURE", [])]
    return list(handle.pictures)


class TestTheCoverGoesIn:
    def test_a_flac_gets_a_front_cover_a_player_can_read(self, audio, cover):
        flac = audio("art.flac")
        assert mutagentags.embed_cover(str(flac), str(cover)) == 0
        pictures = _pictures(flac)
        assert len(pictures) == 1
        assert pictures[0].type == 3
        assert pictures[0].mime == "image/jpeg"
        assert pictures[0].data == cover.read_bytes()

    def test_an_opus_gets_the_same_thing_base64_in_a_comment(self, audio, cover):
        opus = audio("art.opus")
        assert mutagentags.embed_cover(str(opus), str(cover)) == 0
        pictures = _pictures(opus)
        assert len(pictures) == 1
        assert pictures[0].data == cover.read_bytes()

    def test_a_rerun_replaces_rather_than_accumulates(self, audio, cover):
        """Which is the whole reason the FLAC path clears first: a library
        re-ingested twice would otherwise carry two copies of its own art."""
        for path in ("art.flac", "art.opus"):
            target = audio(path)
            mutagentags.embed_cover(str(target), str(cover))
            mutagentags.embed_cover(str(target), str(cover))
            assert len(_pictures(target)) == 1

    def test_and_comes_back_off_an_opus(self, audio, cover):
        opus = audio("art.opus")
        mutagentags.embed_cover(str(opus), str(cover))
        assert mutagentags.remove_cover(str(opus)) == 0
        assert _pictures(opus) == []


class TestTheStatusIsAStatus:
    """The process boundary these functions replace turned any failure into a
    non-zero exit, and two callers read that: ingest_music counts a chapter write
    it could not make, and thumbnails drops its sidecar copies only on success. A
    direct call that raised instead would take the run down.
    """

    def test_a_file_mutagen_cannot_read_is_a_status_not_a_traceback(self, tmp_path):
        junk = tmp_path / "notaudio.flac"
        junk.write_text("this is not a FLAC file")
        chapters = tmp_path / "c.chapters"
        chapters.write_text(THREE)
        assert mutagentags.embed_chapters(str(junk), str(chapters)) == 1

    def test_the_same_for_a_cover(self, tmp_path, cover):
        junk = tmp_path / "notaudio.flac"
        junk.write_text("this is not a FLAC file")
        assert mutagentags.embed_cover(str(junk), str(cover)) == 1

    def test_and_for_a_file_that_is_not_there_at_all(self, tmp_path, cover):
        missing = str(tmp_path / "gone.opus")
        assert mutagentags.embed_cover(missing, str(cover)) == 1
        assert mutagentags.remove_cover(missing) == 1
        assert mutagentags.embed_chapters(missing, "/dev/null") == 1

    def test_a_cover_file_that_is_not_there_is_a_status_too(self, audio, tmp_path):
        flac = audio("art.flac")
        assert mutagentags.embed_cover(str(flac), str(tmp_path / "gone.jpg")) == 1
