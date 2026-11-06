"""Every command hands its RAM run directory back on a refusal, not only at the
end of a finished run.

The ffmpeg pin (``ffmpegselect.select_ffmpeg``) makes the run directory as soon
as the build it settles on is not the one PATH already finds, which is long
before most commands reach their own ``init_ram_base`` - and a refusal in
between, a missing tool most often, left a ``<program>.run.*`` folder in
``/dev/shm`` that nothing ever removed. The stubbed suite never pins, because
its ffmpeg IS the one on PATH, so each case here pins the way the real ladder
does and then refuses at the very next check.

Asked in-process and straight after ``main`` returns: the workspace fixture
runs the exit cleanup itself at teardown, so a leak inside this process would
otherwise be tidied away before anyone looked for it.
"""

import os

import pytest

from medialib.cli import concat_audio
from medialib.cli import convert_audio
from medialib.cli import convert_comics
from medialib.cli import ingest_movies_run
from medialib.cli import ingest_music
from medialib.cli import read_library
from medialib.cli import transcribe_audio
from medialib.cli import ytdlp
from medialib.lib import ffmpegselect, ramscratch, tooldeps

pytestmark = pytest.mark.fs


def _left_behind() -> list:
    """The run directories still standing under every base a command can land
    in: the shared ``ramScratchBase`` and the per-command knobs, which the
    workspace fixture points at one directory of their own."""
    bases = {os.environ["ramScratchBase"], os.environ["comicsRamBase"]}
    return sorted(entry for base in bases for entry in os.listdir(base)
                  if ".run." in entry)


@pytest.fixture
def pinned_then_refused(monkeypatch):
    """The pin makes the run directory, and the tool preflight then refuses.

    Answers a check that says the run directory really was made, so a case
    cannot pass by never reaching the pin at all.
    """
    pins = []

    def pin(*_args, **_kwargs):
        ramscratch.init_ram_base()
        pins.append(ramscratch.ram_base())
        return 0

    monkeypatch.setattr(ffmpegselect, "select_ffmpeg", pin)
    monkeypatch.setattr(ffmpegselect, "report_ffmpeg_selection",
                        lambda *a, **k: 0)
    monkeypatch.setattr(tooldeps, "require_tools", lambda *a, **k: 1)

    def pinned() -> bool:
        return bool(pins) and ".run." in os.path.basename(pins[0])

    return pinned


def _folders(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    return str(source), str(tmp_path / "out")


@pytest.mark.parametrize("command", [
    concat_audio, convert_audio, ingest_music, read_library, transcribe_audio,
], ids=lambda module: module.__name__.rsplit(".", 1)[1])
def test_a_refusal_after_the_pin_leaves_no_run_directory(
        command, tmp_path, pinned_then_refused):
    source, output = _folders(tmp_path)
    assert command.main([source, output]) == 1
    assert pinned_then_refused()
    assert _left_behind() == []


@pytest.mark.parametrize("phase", [[], ["-s"], ["-a"]],
                         ids=["full ingest", "subtitles only",
                              "commentary only"])
def test_an_ingest_movies_refusal_after_the_pin_leaves_no_run_directory(
        phase, tmp_path, pinned_then_refused):
    films = tmp_path / "Films"
    films.mkdir()
    assert ingest_movies_run.main(phase + [str(films)]) == 1
    assert pinned_then_refused()
    assert _left_behind() == []


def test_a_ytdlp_refusal_after_the_pin_leaves_no_run_directory(
        tmp_path, monkeypatch, pinned_then_refused):
    table = tmp_path / "feeds.tsv"
    table.write_text("#!profile rssAudio\n")
    monkeypatch.setattr(ytdlp, "_resolve_ytdlp", lambda *a: ["yt-dlp"])
    assert ytdlp.main(["-t", str(table), str(tmp_path / "out"),
                       str(tmp_path / "archive.txt")]) == 1
    assert pinned_then_refused()
    assert _left_behind() == []


def test_convert_comics_hands_back_the_run_directory_when_no_counters_dir(
        tmp_path, monkeypatch):
    """Its run directory comes from ``init_ram_base`` itself, and the first
    scratch directory under it failing is a refusal of its own."""
    monkeypatch.setattr(ramscratch, "ram_scratch_dir",
                        lambda *a, **k: ("", 1))
    source, output = _folders(tmp_path)
    assert convert_comics.main([source, output]) == 1
    assert _left_behind() == []
