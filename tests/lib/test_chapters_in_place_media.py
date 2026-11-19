"""Tier D for chapters.write_chapters on the containers generate-chapters -w
writes into: each one encoded for real, given chapters in place, and read back
by ffprobe - with its own tags still there, and a container that cannot hold
chapters left exactly as it was and said to be one.
"""

import shutil
import subprocess

import pytest

from medialib.lib import chapters

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None,
                       reason="tier D needs a real ffmpeg"),
]

ROWS = ["CHAPTER01=00:00:00.000", "CHAPTER01NAME=Opening = intro",
        "CHAPTER02=00:00:03.000", "CHAPTER02NAME=Second – ü"]

_CODECS = {"mp3": [], "ogg": ["-c:a", "libvorbis"], "m4a": ["-c:a", "aac"],
           "opus": ["-c:a", "libopus"], "flac": [], "mka": [],
           "aac": ["-c:a", "aac"], "wav": []}


def _encode(tmp_path, extension):
    target = tmp_path / ("source." + extension)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=d=6", *_CODECS[extension], "-metadata", "artist=Kept",
                    str(target)], check=True)
    return target


def _artist(path):
    return subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries",
         "format_tags=artist:stream_tags=artist", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.split()


@pytest.mark.parametrize("extension", ["mp3", "ogg", "m4a", "opus", "flac", "mka"])
def test_the_chapters_go_in_and_the_tags_stay(tmp_path, extension):
    if extension == "mka" and shutil.which("mkvpropedit") is None:
        pytest.skip("Matroska is written by mkvpropedit")
    target = _encode(tmp_path, extension)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert not chapters.carries_chapters(str(target), ROWS)
    assert chapters.write_chapters(str(target), ROWS, str(scratch)) == 0
    assert chapters.carries_chapters(str(target), ROWS)
    assert "Kept" in _artist(target)


@pytest.mark.parametrize("extension", ["aac", "wav"])
def test_a_container_without_chapters_is_left_as_it_was(tmp_path, extension):
    target = _encode(tmp_path, extension)
    before = target.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert chapters.write_chapters(str(target), ROWS,
                                   str(scratch)) == chapters.CANNOT_HOLD
    assert target.read_bytes() == before
    assert list(scratch.glob("generated.%s" % extension)) == []
