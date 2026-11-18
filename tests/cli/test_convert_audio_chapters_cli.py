"""`convert-audio -t`: chapters for what has none, and the transcripts that go
into the outputs.

The encoders are the stubs the other convert-audio cases use, so an output is
an empty file and nothing can be written into it; what is checked is which
files reach whisper and the model, and that a run without a model is refused
before anything is encoded. ffprobe says a source whose name holds
"chaptered" has two chapters of its own.
"""

from __future__ import annotations

import os

import pytest

from tests.cli.test_convert_audio_cli import _stubbed
from tests.cli.test_generate_chapters_cli import _CURL, _PIPX

pytestmark = pytest.mark.stubbed

_FFPROBE = '''
last="${!#}"
for a in "$@"; do
  if [[ "$a" == "-show_chapters" ]]; then
    if [[ "$last" == *chaptered* ]]; then
      printf 'chapters.chapter.0.start_time="0.000000"\\n'
      printf 'chapters.chapter.1.start_time="60.000000"\\n'
    fi
    exit 0
  fi
done
for a in "$@"; do [[ "$a" == *duration* ]] && { echo 1200.000; exit 0; }; done
exit 0
'''


@pytest.fixture
def convert(sandbox, tmp_path):
    _stubbed(sandbox)
    sandbox.with_tool("ffprobe", _FFPROBE)
    sandbox.with_tool("pipx", _PIPX)
    sandbox.with_tool("curl", _CURL)
    sandbox.with_tool("nvidia-smi", "exit 0")
    for name in ("whisper", "model"):
        (tmp_path / (name + ".calls")).write_text("")
    sandbox.environment = dict(
        os.environ, WHISPERLOG=str(tmp_path / "whisper.calls"),
        MODELLOG=str(tmp_path / "model.calls"),
        chapterLlmUrl="http://model.test/v1", chapterLlmModel="stub")
    inputs = tmp_path / "in"
    inputs.mkdir()
    for name in ("plain.m4a", "chaptered.m4a"):
        (inputs / name).write_bytes(b"\0" * 1000)
    return sandbox


def _run(convert, tmp_path, *options, env=None, expect=0):
    done = convert.run("convert-audio", "-j", 1, *options, tmp_path / "in",
                       tmp_path / "out", env=env or convert.environment, timeout=600)
    assert done.returncode == expect, done.stdout + done.stderr
    return done.stderr


def test_only_an_output_whose_source_has_no_chapters_is_transcribed(convert, tmp_path):
    said = _run(convert, tmp_path, "-t")
    whisper = (tmp_path / "whisper.calls").read_text()
    assert "plain.opus" in whisper
    assert "chaptered" not in whisper
    assert "Chaptering 1 file(s)" in said
    # One outline and one second look, for the one file.
    assert len((tmp_path / "model.calls").read_text().splitlines()) == 2


def test_a_sidecar_is_the_transcript_and_nothing_is_transcribed(convert, tmp_path):
    (tmp_path / "in" / "plain.en.srt").write_text("".join(
        "%d\n00:%02d:%02d,000 --> 00:%02d:%02d,500\nwords\n\n"
        % (n, s // 60, s % 60, s // 60, s % 60) for n, s in enumerate(range(0, 1200, 10), 1)))
    _run(convert, tmp_path, "-t")
    assert (tmp_path / "whisper.calls").read_text() == ""
    assert (tmp_path / "model.calls").read_text() != ""


def test_without_t_neither_whisper_nor_the_model_is_asked(convert, tmp_path):
    _run(convert, tmp_path)
    assert (tmp_path / "whisper.calls").read_text() == ""
    assert (tmp_path / "model.calls").read_text() == ""


def test_t_without_a_model_is_refused_before_anything_is_encoded(convert, tmp_path):
    environment = dict(convert.environment)
    del environment["chapterLlmUrl"]
    said = _run(convert, tmp_path, "-t", env=environment, expect=1)
    assert "chapterLlmUrl is not set" in said
    assert not (tmp_path / "out" / "plain.opus").exists()


def test_an_output_too_short_for_chapters_is_not_transcribed(convert, tmp_path):
    """Two chapters of the shortest length do not fit in it, so the model would
    turn it down whatever was said; transcribing it would be paid for on
    every rerun."""
    convert.with_tool("ffprobe", _FFPROBE.replace("echo 1200.000", "echo 100.000"))
    _run(convert, tmp_path, "-t")
    assert (tmp_path / "whisper.calls").read_text() == ""
    assert (tmp_path / "model.calls").read_text() == ""
