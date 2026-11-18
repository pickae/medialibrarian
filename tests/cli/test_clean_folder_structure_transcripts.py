"""A transcript beside a recording keeps the recording's name when the names
are cleaned: convert-audio finds it by that name alone."""

from __future__ import annotations

import pytest

from medialib.cli import clean_folder_structure as cfs
from medialib.lib import safety

pytestmark = pytest.mark.fs

_NAMES = ("20260101 The Show - Ep 1 Hello World",
          "20260102 The Show - Ep 2 Another_Thing",
          "20260103 The Show - Ep 3 Final.Part")


def _clean(folder):
    cfs.rename_siblings_until_stable(str(folder), "files", "", safety.SkipLog())
    return sorted(path.name for path in folder.iterdir())


def _stems(names, suffix):
    return sorted(name[:-len(suffix)] for name in names if name.endswith(suffix))


def test_every_transcript_follows_its_recording(tmp_path):
    for name in _NAMES:
        (tmp_path / (name + ".opus")).write_text("x")
        (tmp_path / (name + ".srt")).write_text("x")
    names = _clean(tmp_path)
    assert _stems(names, ".opus") == _stems(names, ".srt")
    # The cleaning still happened: the stems are not the ones they came with.
    assert _stems(names, ".opus") != sorted(_NAMES)


def test_one_with_a_language_code_keeps_the_code(tmp_path):
    for name in _NAMES:
        (tmp_path / (name + ".opus")).write_text("x")
    (tmp_path / (_NAMES[1] + ".en.vtt")).write_text("x")
    names = _clean(tmp_path)
    vtt = [name for name in names if name.endswith(".en.vtt")]
    assert len(vtt) == 1
    assert vtt[0][:-len(".en.vtt")] + ".opus" in names


def test_the_recording_and_not_its_cover_owns_the_transcript(tmp_path):
    for name in _NAMES:
        (tmp_path / (name + ".opus")).write_text("x")
        (tmp_path / (name + ".jpg")).write_text("x")
        (tmp_path / (name + ".srt")).write_text("x")
    names = _clean(tmp_path)
    assert _stems(names, ".opus") == _stems(names, ".srt")


def test_a_transcript_with_no_recording_is_cleaned_as_any_file(tmp_path):
    (tmp_path / "Lonely_Notes.srt").write_text("x")
    assert _clean(tmp_path) == ["Lonely Notes.srt"]
