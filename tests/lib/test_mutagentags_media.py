"""Tier D for medialib.lib.mutagentags: what it writes into a file only an
encoder can make, and which of the two chapter sets wins when the file already
has some.

The FLAC decisions run in the default tier against a file mutagen stands up
itself; what is left here needs a real encoder to make the file worth reading,
and a real mutagen to read the tags back - so this tier encodes one of each and
reads it with the same library the writer used. It is opt-in (`pytest -m media`)
because of exactly that.

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
    from mutagen.oggopus import OggOpus
    return OggOpus(str(path))


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
    """A real encoded file of each container an encoder must make, chapterless,
    standing in for one the pipeline has just produced."""
    def make(name):
        path = tmp_path / name
        codec = ["-c:a", "libopus", "-b:a", "64k"] if name.endswith(".opus") \
            else ["-c:a", "aac"]
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


class TestTheOpus:
    """Opus takes a different mutagen loader than the FLAC the default tier
    checks, so the decisions are checked there too."""

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
    """The pictures the opus file carries, base64 in a comment."""
    import base64

    from mutagen.flac import Picture
    return [Picture(base64.b64decode(blob))
            for blob in _load(path).get("METADATA_BLOCK_PICTURE", [])]


class TestTheOpusCoverGoesIn:
    def test_an_opus_gets_the_same_thing_base64_in_a_comment(self, audio, cover):
        opus = audio("art.opus")
        assert mutagentags.embed_cover(str(opus), str(cover)) == 0
        pictures = _pictures(opus)
        assert len(pictures) == 1
        assert pictures[0].data == cover.read_bytes()

    def test_a_rerun_replaces_rather_than_accumulates(self, audio, cover):
        """Which is the whole reason the path clears first: a library
        re-ingested twice would otherwise carry two copies of its own art."""
        target = audio("art.opus")
        mutagentags.embed_cover(str(target), str(cover))
        mutagentags.embed_cover(str(target), str(cover))
        assert len(_pictures(target)) == 1


class TestTheMP4CoverGoesIn:
    """The `covr` branch does not build a picture structure around the bytes,
    and it had never been read back in any tier: a rerun must replace the atom
    rather than stack a second one."""

    def test_a_rerun_leaves_exactly_one_covr_with_the_bytes_handed_in(
            self, audio, cover):
        from mutagen.mp4 import MP4, MP4Cover

        mp4 = audio("art.m4a")
        for _ in range(2):
            assert mutagentags.embed_cover(str(mp4), str(cover)) == 0
        covr = MP4(str(mp4))["covr"]
        assert len(covr) == 1
        # the cover is the JPEG itself: MP4Cover is the bytes, tagged
        assert covr[0] == cover.read_bytes()
        assert covr[0].imageformat == MP4Cover.FORMAT_JPEG


class TestTheStatusIsAStatus:
    """The process boundary these functions replace turned any failure into a
    non-zero exit, and two callers read that: ingest_music counts a chapter write
    it could not make, and thumbnails drops its sidecar copies only on success. A
    direct call that raised instead would take the run down.
    """

    def test_and_for_a_file_that_is_not_there_at_all(self, tmp_path, cover):
        missing = str(tmp_path / "gone.opus")
        assert mutagentags.embed_cover(missing, str(cover)) == 1
        assert mutagentags.embed_chapters(missing, "/dev/null") == 1
