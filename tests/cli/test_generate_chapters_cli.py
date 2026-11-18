"""`generate-chapters` end to end, with whisper and the model both stubbed.

whisper (behind pipx) writes a transcript of twenty minutes, one cue every ten
seconds; the model (behind curl) answers the outline with two chapters - the
first passage and the one ten minutes in - and every second look with the
line it was asked about. What is checked is the plumbing around that: which
inputs are transcribed, where transcripts and chapter files land, what a rerun
leaves alone, and that a missing model is refused before anything runs.
"""

from __future__ import annotations

import os

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

_PIPX = r"""
printf '%s\n' "$*" >> "$WHISPERLOG"
input="$3"; outDir=""; prev=""
for a in "$@"; do [[ "$prev" == "--output_dir" ]] && outDir="$a"; prev="$a"; done
base="${input##*/}"
python3 -c '
import sys
with open(sys.argv[1], "w") as out:
    for n, s in enumerate(range(0, 1200, 10), 1):
        out.write("%d\n00:%02d:%02d,000 --> 00:%02d:%02d,000\ntopic %d words\n\n"
                  % (n, s // 60, s % 60, (s + 9) // 60, (s + 9) % 60, s // 600))
' "$outDir/${base%.*}.srt"
"""

_CURL = r"""
python3 -c '
import json, re, sys
config = sys.stdin.read()
with open(re.search(r"data-binary = \"@(.*)\"", config).group(1)) as f:
    prompt = json.load(f)["messages"][1]["content"]
with open(sys.argv[1], "a") as log:
    log.write(prompt.split("\n", 1)[0] + "\n")
if "Find the line where" in prompt:
    at = re.search(r"placed to start \((\d+:\d\d:\d\d)\)", prompt).group(1)
    line = re.search(r"\[(L\d+) %s\]" % at, prompt).group(1)
    answer = {"start": line, "title": "Second half"}
else:
    ids = re.findall(r"\[(P\d+) (\d+:\d\d:\d\d)\]", prompt)
    later = [pid for pid, at in ids if at == "0:10:00"][0]
    answer = {"summary": "s", "chapters": [{"start": ids[0][0], "title": "First half"},
                                           {"start": later, "title": "Middle"}]}
print(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}))
print("200", end="")
' "$MODELLOG"
"""


@pytest.fixture
def chaptering(sandbox, tmp_path):
    whisper_log = tmp_path / "whisper.calls"
    model_log = tmp_path / "model.calls"
    whisper_log.write_text("")
    model_log.write_text("")
    sandbox.with_tool("pipx", _PIPX)
    sandbox.with_tool("curl", _CURL)
    sandbox.with_tool("nvidia-smi", "exit 0")
    sandbox.with_tool("ffmpeg", ': > "${!#}"')
    environment = dict(os.environ, WHISPERLOG=str(whisper_log),
                       MODELLOG=str(model_log),
                       chapterLlmUrl="http://model.test/v1", chapterLlmModel="stub")

    def run(*args, expect=0, env=None):
        done = sandbox.run("generate-chapters", *args, env=env or environment,
                           timeout=600)
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.whisper_log = whisper_log
    sandbox.model_log = model_log
    sandbox.environment = environment
    sandbox.chapter = run
    return sandbox


def _tree(tmp_path, *names):
    source = tmp_path / "in"
    for name in names:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).touch()
    return source, tmp_path / "out"


def test_a_tree_gets_transcripts_and_chapter_files(chaptering, tmp_path):
    source, out = _tree(tmp_path, "a/show.mp3", "b/talk.opus")
    chaptering.chapter(source, out)
    for stem in ("a/show", "b/talk"):
        assert (out / (stem + ".srt")).is_file()
        assert (out / (stem + ".chapters.txt")).read_text() == (
            "CHAPTER01=00:00:00.000\nCHAPTER01NAME=First half\n"
            "CHAPTER02=00:09:59.500\nCHAPTER02NAME=Second half\n")
    # One outline and one second look per file.
    assert len(chaptering.model_log.read_text().splitlines()) == 4


def test_whisper_is_handed_only_what_has_no_transcript(chaptering, tmp_path):
    source, out = _tree(tmp_path, "show.mp3", "other.mp3")
    (source / "other.vtt").write_text(
        "WEBVTT\n\n" + "".join("00:%02d:%02d.000 --> 00:%02d:%02d.000\nwords\n\n"
                               % (s // 60, s % 60, (s + 9) // 60, (s + 9) % 60)
                               for s in range(0, 1200, 10)))
    chaptering.chapter(source, out)
    calls = chaptering.whisper_log.read_text()
    assert "show.mp3" in calls and "other.mp3" not in calls
    assert not (out / "other.srt").exists()
    assert (out / "other.chapters.txt").is_file()


def test_a_rerun_leaves_finished_files_alone_unless_forced(chaptering, tmp_path):
    source, out = _tree(tmp_path, "show.mp3")
    chaptering.chapter(source, out)
    chaptering.whisper_log.write_text("")
    chaptering.model_log.write_text("")
    log = chaptering.chapter(source, out)
    assert "already present: show.mp3" in log
    assert chaptering.whisper_log.read_text() == ""
    assert chaptering.model_log.read_text() == ""
    chaptering.chapter("-f", source, out)
    assert chaptering.whisper_log.read_text() == ""
    assert chaptering.model_log.read_text() != ""


def test_no_model_is_refused_before_anything_runs(chaptering, tmp_path):
    source, out = _tree(tmp_path, "show.mp3")
    environment = dict(chaptering.environment)
    del environment["chapterLlmUrl"]
    log = chaptering.chapter(source, out, expect=1, env=environment)
    assert "chapterLlmUrl is not set" in log
    assert chaptering.whisper_log.read_text() == ""
    assert not out.exists()


def test_the_settings_reach_the_run_from_x(chaptering, tmp_path):
    source, out = _tree(tmp_path, "show.mp3")
    environment = dict(chaptering.environment)
    del environment["chapterLlmModel"]
    chaptering.chapter("-x", "chapterLlmModel=stub", source, out, env=environment)
    assert (out / "show.chapters.txt").is_file()


def test_a_short_recording_is_reported_and_fails_the_run(chaptering, tmp_path):
    source, out = _tree(tmp_path, "show.mp3")
    (out).mkdir()
    (out / "show.srt").write_text("1\n00:00:00,000 --> 00:00:05,000\nHi\n\n")
    log = chaptering.chapter(source, out, expect=1)
    assert "no chapters" in log
    assert not (out / "show.chapters.txt").exists()


def test_the_tree_is_mirrored_under_blackbox(chaptering, tmp_path):
    source, out = _tree(tmp_path, "deep/er/show.mp3")
    chaptering.chapter(source, out)
    assert "deep/er/show.chapters.txt" in blackbox.tree_of(out)
