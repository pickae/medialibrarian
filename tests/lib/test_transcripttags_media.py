"""Chapters and a transcript really written into an Opus and an m4a file, and
read back by ffprobe the way a player would find them."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from medialib.lib import chapters, mutagentags, transcripttags

pytestmark = pytest.mark.media

_ROWS = ["CHAPTER01=00:00:00.000", "CHAPTER01NAME=Opening",
         "CHAPTER02=00:00:04.000", "CHAPTER02NAME=Later"]
_SRT = ("1\n00:00:01,000 --> 00:00:02,500\nHello there\n\n"
        "2\n00:00:05,000 --> 00:00:07,000\nSecond line\n\n")


def _probe(path, *entries):
    done = subprocess.run(["ffprobe", "-v", "error", *entries, "-of", "json", str(path)],
                          capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


@pytest.fixture(params=[("opus", "libopus"), ("m4a", "aac")], ids=["opus", "m4a"])
def audio(request, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("no ffmpeg")
    suffix, codec = request.param
    path = tmp_path / ("book." + suffix)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=8",
                    "-metadata", "artist=Someone", "-c:a", codec, str(path)], check=True)
    (tmp_path / "book.srt").write_text(_SRT)
    return path


def test_chapters_replace_none_and_keep_the_tags(audio, tmp_path):
    assert chapters.write_chapters(str(audio), _ROWS, str(tmp_path)) == 0
    assert chapters.chapter_count(str(audio)) == 2
    tags = _probe(audio, "-show_entries", "format_tags:stream_tags")
    assert "Someone" in json.dumps(tags)


def test_a_transcript_goes_in_and_is_seen_there(audio, tmp_path):
    assert not transcripttags.has_transcript(str(audio))
    assert transcripttags.embed_transcript(str(audio), str(tmp_path / "book.srt"),
                                           str(tmp_path)) == 0
    assert transcripttags.has_transcript(str(audio))
    if audio.suffix == ".opus":
        assert mutagentags.has_tag(str(audio), "LYRICS")
        lyrics = _probe(audio, "-show_entries", "stream_tags=LYRICS")
        assert "[00:01.00]Hello there" in json.dumps(lyrics)
    else:
        done = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-map", "0:s:0",
                               "-f", "srt", "-"], capture_output=True, text=True, check=True)
        assert "Second line" in done.stdout


def test_chapters_survive_a_transcript_written_after_them(audio, tmp_path):
    chapters.write_chapters(str(audio), _ROWS, str(tmp_path))
    transcripttags.embed_transcript(str(audio), str(tmp_path / "book.srt"), str(tmp_path))
    assert chapters.chapter_count(str(audio)) == 2
