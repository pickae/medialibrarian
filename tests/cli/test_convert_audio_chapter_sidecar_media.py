"""Tier D for a chapter file beside a convert-audio source
(``<name>.chapters.txt``, as generate-chapters -w leaves beside a recording
whose format has no place for chapters): the real encoder makes the output, and
ffprobe reads the chapters back out of it.
"""

import shutil
import subprocess

import pytest

from medialib.lib import chapters
from tests import blackbox

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]

ROWS = ["CHAPTER01=00:00:00.000", "CHAPTER01NAME=Opening",
        "CHAPTER02=00:00:20.000", "CHAPTER02NAME=Second speaker"]


def _noise(path, *extra):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=40:c=pink:r=48000", *extra, str(path)],
                   check=True, stdin=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sidecar")
    source, out = tmp / "in", tmp / "out"
    source.mkdir()
    _noise(source / "plain.aac", "-c:a", "aac", "-b:a", "192k")
    (source / "plain.chapters.txt").write_text("\n".join(ROWS) + "\n")
    # A source with chapters of its own, and a chapter file beside it too.
    meta = tmp / "own.ffmetadata"
    meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\n"
                    "END=40000\ntitle=Its own\n")
    _noise(source / "chaptered.flac", "-i", str(meta), "-map_chapters", "1")
    (source / "chaptered.chapters.txt").write_text("\n".join(ROWS) + "\n")
    _noise(source / "alone.aac", "-c:a", "aac", "-b:a", "192k")
    done = blackbox.run("convert-audio", source, out, cwd=tmp, timeout=900)
    return out, done


def test_the_run_exits_zero(converted):
    _out, done = converted
    assert done.returncode == 0, done.stderr


def test_the_chapter_file_goes_into_the_output(converted):
    out, done = converted
    assert chapters.carries_chapters(str(out / "plain.opus"), ROWS)
    assert "Chapters added from plain.chapters.txt: plain.aac" in done.stderr


def test_it_wins_over_the_sources_own_chapters(converted):
    out, _done = converted
    assert chapters.carries_chapters(str(out / "chaptered.opus"), ROWS)


def test_an_output_without_one_gets_none(converted):
    out, _done = converted
    assert chapters.chapter_count(str(out / "alone.opus")) == 0


def test_a_rerun_leaves_them_as_they_are(converted, tmp_path):
    out, _done = converted
    done = blackbox.run("convert-audio", out.parent / "in", out,
                        cwd=out.parent, timeout=900)
    assert done.returncode == 0, done.stderr
    assert "Chapters added" not in done.stderr
    assert chapters.carries_chapters(str(out / "plain.opus"), ROWS)
