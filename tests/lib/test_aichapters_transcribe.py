"""The transcription phase handed to transcribe-audio: what it is given, and
from where it is started."""

from __future__ import annotations

import os

import pytest

from medialib import commands
from medialib.lib import aichapters

pytestmark = pytest.mark.fs


@pytest.fixture
def started(monkeypatch):
    seen: dict = {}

    class Done:
        returncode = 0

    def run(command, argv, script_dir="", **kwargs):
        seen.update(command=command, argv=argv, kwargs=kwargs)
        return Done()

    monkeypatch.setattr(commands, "run_command", run)
    return seen


def test_each_file_is_linked_under_its_relative_path(started, tmp_path):
    audio = tmp_path / "book.opus"
    audio.write_text("x")
    aichapters.transcribe([("a/b/book.opus", str(audio))], str(tmp_path / "stage"),
                          str(tmp_path / "out"))
    link = tmp_path / "stage" / "a" / "b" / "book.opus"
    assert os.readlink(link) == str(audio)
    assert started["command"] == "transcribe-audio"
    assert started["argv"] == ["-f", "srt", str(tmp_path / "stage"), str(tmp_path / "out")]


def test_it_is_started_where_the_package_can_be_imported(started, tmp_path, monkeypatch):
    """convert-audio has moved into its input folder by the time it gets here,
    and a child started there could not import a package that is not
    installed."""
    monkeypatch.chdir(tmp_path)
    aichapters.transcribe([], "stage", "out")
    assert started["kwargs"]["cwd"] == commands.package_root()
    assert started["argv"][2:] == [str(tmp_path / "stage"), str(tmp_path / "out")]
