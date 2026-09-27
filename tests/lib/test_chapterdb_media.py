"""Tier D for medialib.lib.chapterdb: chapters written into a real Matroska.

tests/lib/test_chapterdb.py settles which set a film is given. What only the
real tools can answer is the writing - that mkvpropedit takes the chapter file
as it is made, that it REPLACES what the film had rather than adding a second
edition beside it, and that ffprobe reads the result back the way the lookup
reads a film before it.
"""

import shutil
import subprocess

import pytest

from medialib.lib import chapterdb

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(any(shutil.which(tool) is None for tool in
                           ("ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit")),
                       reason="tier D needs ffmpeg, ffprobe and mkvtoolnix"),
]

SECONDS = 20


@pytest.fixture
def film(tmp_path):
    """A twenty-second mkv with three numbered chapters, the way a rip that
    kept the disc's marks but not its names arrives."""
    raw = tmp_path / "raw.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anoisesrc=d=%d:c=pink:r=48000" % SECONDS,
         "-c:a", "libopus", "-b:a", "32k", str(raw)],
        check=True, stdin=subprocess.DEVNULL)
    marks = tmp_path / "marks.txt"
    marks.write_text("CHAPTER01=00:00:00.000\nCHAPTER01NAME=Chapter 01\n"
                     "CHAPTER02=00:00:07.000\nCHAPTER02NAME=Chapter 02\n"
                     "CHAPTER03=00:00:14.000\nCHAPTER03NAME=Chapter 03\n",
                     encoding="utf-8")
    movie = tmp_path / "Nordwind (1998).mkv"
    subprocess.run(["mkvmerge", "--quiet", "-o", str(movie), "--chapters",
                    str(marks), str(raw)], check=True,
                   stdin=subprocess.DEVNULL)
    return str(movie)


def test_the_film_is_read_as_numbered(film):
    existing = chapterdb.existing_chapters(film)
    assert existing.seconds == pytest.approx(SECONDS, abs=0.5)
    assert existing.kind == "numbered"


def test_named_chapters_replace_the_numbered_ones(film):
    chosen = chapterdb.ChapterSet(
        "1", "Nordwind", "Blu-Ray", "eng", 5, SECONDS,
        ((0.0, "Opening"), (5.0, 'The "Harbour" & Pier'), (12.5, "Storm"),
         (16.0, "Landfall")))
    assert chapterdb.write_chapters(film, chosen)
    after = chapterdb.existing_chapters(film)
    assert after.names == ("Opening", 'The "Harbour" & Pier', "Storm",
                           "Landfall")
    assert after.kind == "named"
