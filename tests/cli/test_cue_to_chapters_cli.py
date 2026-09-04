"""`cue-to-chapters` as a process: a cue sheet in, an OGM chapter file out.

The command drives no media tool, so these runs need no stubs and no audio - the
fixtures are text. Two groups: three sheets written here, which pin the
conversion itself, and three anonymised real-world sheets that tripped the
converter up and are compared byte for byte against a committed answer.
"""

from __future__ import annotations

import pytest

from tests import blackbox

pytestmark = pytest.mark.fs

_FIXTURES = blackbox.DATA / "cue"


def _convert(sandbox, name: str, cue_text: str) -> str:
    cue = sandbox.work / (name + ".cue")
    out = sandbox.work / (name + ".ch")
    # A real cue file ends with a newline, and the reader would otherwise drop
    # the last line.
    cue.write_text(cue_text.strip("\n") + "\n", encoding="utf-8")
    done = sandbox.run("cue-to-chapters", cue, out)
    assert done.returncode == 0, done.stderr
    assert out.exists()
    return out.read_text(encoding="utf-8").rstrip("\n")


class TestTheConversion:
    def test_three_tracks_at_zero_two_thirty_and_seven_thirty_four(
            self, sandbox):
        assert _convert(sandbox, "three_tracks", """
FILE "album.mp3" MP3
  TRACK 01 AUDIO
    TITLE "Intro"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Chapter One"
    INDEX 01 02:30:00
  TRACK 03 AUDIO
    TITLE "Outro"
    INDEX 01 07:34:00
""") == (
            "CHAPTER01=00:00:00.000\n"
            "CHAPTER01NAME=Intro\n"
            "CHAPTER02=00:02:30.000\n"
            "CHAPTER02NAME=Chapter One\n"
            "CHAPTER03=00:07:34.000\n"
            "CHAPTER03NAME=Outro")

    def test_the_header_title_is_ignored_and_only_the_tracks_count(
            self, sandbox):
        assert _convert(sandbox, "single_track", """
TITLE "Whole Album Header"
FILE "book.opus" WAVE
  TRACK 01 AUDIO
    TITLE "Only Chapter"
    INDEX 01 00:00:00
""") == (
            "CHAPTER01=00:00:00.000\n"
            "CHAPTER01NAME=Only Chapter")

    def test_the_third_field_is_frames_at_75_a_second_not_hundredths(
            self, sandbox):
        """The case that actually pins the conversion: 9 frames is 120 ms, 65 is
        867 ms and 74 - the largest legal value - is 987 ms. A sheet using only
        ":00" reads the same either way."""
        assert _convert(sandbox, "frames", """
FILE "disc.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second"
    INDEX 01 00:53:09
  TRACK 03 AUDIO
    TITLE "Third"
    INDEX 01 01:45:65
  TRACK 04 AUDIO
    TITLE "Fourth"
    INDEX 01 02:30:74
""") == (
            "CHAPTER01=00:00:00.000\n"
            "CHAPTER01NAME=First\n"
            "CHAPTER02=00:00:53.120\n"
            "CHAPTER02NAME=Second\n"
            "CHAPTER03=00:01:45.867\n"
            "CHAPTER03NAME=Third\n"
            "CHAPTER04=00:02:30.987\n"
            "CHAPTER04NAME=Fourth")


class TestTheRealWorldSheets:
    """Anonymised copies of sheets that tripped the converter up. The track split
    is theirs; performer, album, catalogue, disc id, ISRC and file name are
    placeholders, so the structure is in the repo and the release is not.

    A committed answer changes only when the behaviour is meant to: regenerate
    with `cue-to-chapters fixtures/cue/<name>.cue fixtures/cue/<name>.chapters`
    and read the diff, because that diff IS the change.
    """

    NAMES = ("many_tracks_single_file", "mixed_mode_data_and_audio",
             "separate_files_per_track")

    @pytest.mark.parametrize("name", NAMES)
    def test_the_committed_answer_is_reproduced_byte_for_byte(
            self, sandbox, name):
        cue = _FIXTURES / (name + ".cue")
        expected = _FIXTURES / (name + ".chapters")
        assert cue.is_file() and expected.is_file()

        out = sandbox.work / (name + ".chapters")
        done = sandbox.run("cue-to-chapters", cue, out)
        assert done.returncode == 0, done.stderr
        assert out.read_bytes() == expected.read_bytes()

    def test_a_sheet_of_one_file_per_track_yields_no_chapters_at_all(self):
        """It describes per-track files rather than chapters of one join, so the
        sheet is ignored rather than turned into a chapter each."""
        text = (_FIXTURES / "separate_files_per_track.chapters").read_text()
        assert text == ""

    def test_a_data_track_is_skipped_rather_than_made_chapter_one(self):
        text = (_FIXTURES / "mixed_mode_data_and_audio.chapters").read_text()
        assert len([line for line in text.splitlines()
                    if line.startswith("CHAPTER") and "NAME" not in line]) == 5
        assert "data track" not in text.lower()
