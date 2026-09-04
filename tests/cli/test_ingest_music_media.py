"""Tier D for the provenance tag ingestMusic's resume check is built on.

A run ends by cleaning the library's names, so by the time the next run looks
for an already-ingested track it is not called what the encoder called it any
more. What survives the rename is a TAG: every ingested flac records the
download it was made from, and the next run reads that back out of the library
instead of guessing from names. Without it a re-run encodes the whole library a
second time, under the old names.

The stubbed test can only exercise the script's half of that round trip - its
stub encoder records the ``-metadata`` it was handed and its stub prober reads
it back. The half belonging to the real tools is what is checked here, in the
order it happens to a file during a run: ffmpeg writes it and ffprobe reads it;
it survives the mutagen chapter write, which edits the same Vorbis comment block
afterwards; it survives the rename; and it holds for a path with the characters
real releases have in them - spaces, accents, and the "=" that Vorbis comments
themselves separate on.
"""

import shutil
import subprocess

import pytest

from medialib.cli.ingest_music import INGEST_SOURCE_TAG
from medialib.lib import mutagentags

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]

# A folder, a track number, a space, an accent, and an "=" in the title.
REL_PATH = "Album/01 - Söng (mix=alt).flac"


def _read_tag(path):
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries",
         "format_tags=" + INGEST_SOURCE_TAG, "-of", "default=nk=1:nw=1",
         "--", str(path)],
        capture_output=True, text=True)
    return done.stdout.splitlines()[0] if done.stdout.strip() else ""


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.flac"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=2", "-c:a", "flac", str(path)],
        check=True, stdin=subprocess.DEVNULL)
    return path


@pytest.fixture
def ingested(source, tmp_path):
    """The encode ingestMusic performs, cut down to the part this is about: a
    16-bit FLAC carrying the tag."""
    path = tmp_path / "ingested.flac"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(source),
         "-map", "0:a:0", "-c:a", "flac", "-sample_fmt", "s16",
         "-ar", "44100", "-metadata",
         "%s=%s" % (INGEST_SOURCE_TAG, REL_PATH), str(path)],
        check=True, stdin=subprocess.DEVNULL)
    return path


def test_the_tag_name_is_the_scripts_own():
    """Read out of the script rather than repeated here: the two sides of the
    round trip have to spell it identically, and a test carrying its own
    spelling would keep passing after the script changed its."""
    assert INGEST_SOURCE_TAG == "INGESTSOURCE"


def test_ffmpeg_writes_it_and_ffprobe_reads_it_back(ingested):
    assert _read_tag(ingested) == REL_PATH


def test_it_survives_the_chapter_write_into_the_same_comment_block(ingested,
                                                                   tmp_path):
    chapters = tmp_path / "chapters.txt"
    chapters.write_text("CHAPTER01=00:00:00.000\nCHAPTER01NAME=First\n"
                        "CHAPTER02=00:00:01.000\nCHAPTER02NAME=Second\n")
    assert mutagentags.embed_chapters(str(ingested), str(chapters),
                                      "Title") == 0
    assert _read_tag(ingested) == REL_PATH


def test_it_survives_the_library_being_renamed(ingested, tmp_path):
    renamed = tmp_path / "1.flac"
    ingested.rename(renamed)
    assert _read_tag(renamed) == REL_PATH


def test_a_path_holding_an_equals_comes_back_whole(ingested):
    """Not split on the "=" that Vorbis comments separate on."""
    assert "mix=alt" in _read_tag(ingested)


def test_an_untagged_flac_reports_no_provenance(source):
    """Which is what makes the fallback to the check by NAME reachable at
    all - a wrong answer here would send a re-run down the resume path."""
    assert _read_tag(source) == ""
