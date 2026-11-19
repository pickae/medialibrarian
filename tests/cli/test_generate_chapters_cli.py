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
    body = json.load(f)
prompt = body["messages"][1]["content"]
with open(sys.argv[1], "a") as log:
    log.write("%s | effort=%s asked=%s\n" % (prompt.split("\n", 1)[0],
              body.get("reasoning_effort"), "<<<\nName each speaker.\n>>>" in prompt))
floor = re.search(r"No chapter shorter than [^.]*[.]", prompt)
if floor:
    with open(sys.argv[1] + ".floor", "a") as seen:
        seen.write(floor.group(0) + "\n")
if "Find the line where" in prompt:
    at = re.search(r"placed to start \((\d+:\d\d:\d\d)\)", prompt).group(1)
    line = re.search(r"\[(L\d+) %s\]" % at, prompt).group(1)
    answer = {"start": line, "title": "Second half"}
else:
    ids = re.findall(r"\[(P\d+) (\d+:\d\d:\d\d)\]", prompt)
    later = [pid for pid, at in ids if at == "0:10:00"][0]
    answer = {"summary": "s", "chapters": [{"start": ids[0][0], "title": "First half"},
                                           {"start": later, "title": "Middle"}]}
print(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}],
                  "usage": {"completion_tokens": 25}}))
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
    log = chaptering.chapter(source, out)
    for stem in ("a/show", "b/talk"):
        assert (out / (stem + ".srt")).is_file()
        assert (out / (stem + ".chapters.txt")).read_text() == (
            "CHAPTER01=00:00:00.000\nCHAPTER01NAME=First half\n"
            "CHAPTER02=00:09:59.500\nCHAPTER02NAME=Second half\n")
    # One outline and one second look per file.
    assert len(chaptering.model_log.read_text().splitlines()) == 4
    assert log.count("model outline:") == 2
    assert "model refine:" in log
    assert "Model time over the run:" in log
    assert "100 token(s) written" in log


def test_a_hosted_model_chapters_each_file_as_it_is_transcribed(chaptering, tmp_path):
    source, out = _tree(tmp_path, "a/show.mp3", "b/talk.opus", "c/ready.mp3")
    (out / "c").mkdir(parents=True)
    (out / "c/ready.srt").write_text("".join(
        "%d\n00:%02d:%02d,000 --> 00:%02d:%02d,000\ntopic %d words\n\n"
        % (n, s // 60, s % 60, (s + 9) // 60, (s + 9) % 60, s // 600)
        for n, s in enumerate(range(0, 1200, 10), 1)))
    environment = dict(chaptering.environment, chapterLlmUrl="https://8.8.8.8/v1")
    log = chaptering.chapter(source, out, env=environment)
    assert "hosted" in log
    for stem in ("a/show", "b/talk", "c/ready"):
        assert (out / (stem + ".chapters.txt")).read_text() == (
            "CHAPTER01=00:00:00.000\nCHAPTER01NAME=First half\n"
            "CHAPTER02=00:09:59.500\nCHAPTER02NAME=Second half\n")
    assert len(chaptering.model_log.read_text().splitlines()) == 6


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


def test_instructions_go_into_every_request_and_raise_the_effort(chaptering, tmp_path):
    source, out = _tree(tmp_path, "show.mp3")
    log = chaptering.chapter("-P", "Name each speaker.", source, out)
    assert "reasoning at medium effort" in log
    requests = chaptering.model_log.read_text().splitlines()
    assert len(requests) == 2
    assert all(line.endswith("effort=medium asked=True") for line in requests)


def test_without_them_the_settings_are_sent_as_they_are(chaptering, tmp_path):
    source, out = _tree(tmp_path, "show.mp3")
    chaptering.chapter(source, out)
    assert all(line.endswith("effort=None asked=False")
               for line in chaptering.model_log.read_text().splitlines())


def test_a_fraction_of_a_minute_is_taken_as_the_shortest_chapter(chaptering, tmp_path):
    source, out = _tree(tmp_path, "show.mp3")
    chaptering.chapter("-m", "0.5", source, out)
    assert (out / "show.chapters.txt").is_file()
    floor = chaptering.model_log.with_name(chaptering.model_log.name + ".floor")
    assert "No chapter shorter than 30 seconds." in floor.read_text().splitlines()


def test_a_shortest_chapter_of_zero_is_refused(chaptering, tmp_path):
    source, out = _tree(tmp_path, "show.mp3")
    log = chaptering.chapter("-m", "0", source, out, expect=1)
    assert "a number above 0" in log


# -w writes into Matroska inputs through mkvpropedit, stubbed to keep the rows it
# was handed beside the input; the ffprobe stub reads them back from there as
# chapters, the way a real probe would read the file.
_MKVPROPEDIT = r"""
printf '%s\n' "$*" >> "$MKVLOG"
cp "$4" "$2.written"
"""

_FFPROBE = r"""
case "$*" in *-show_chapters*) ;; *) exit 0 ;; esac
rows="${!#}.written"
[[ -f "$rows" ]] || exit 0
python3 -c '
import sys
starts, names = [], []
for line in open(sys.argv[1]):
    key, _, value = line.strip().partition("=")
    (names if key.endswith("NAME") else starts).append(value)
for n, (start, name) in enumerate(zip(starts, names)):
    h, m, s = start.split(":")
    print("chapters.chapter.%d.start_time=\"%f\"" % (n, int(h) * 3600 + int(m) * 60 + float(s)))
    print("chapters.chapter.%d.tags.title=\"%s\"" % (n, name))
' "$rows"
"""


@pytest.fixture
def writing(chaptering, tmp_path):
    mkv_log = tmp_path / "mkvpropedit.calls"
    mkv_log.write_text("")
    chaptering.with_tool("mkvpropedit", _MKVPROPEDIT)
    chaptering.with_tool("ffprobe", _FFPROBE)
    chaptering.environment = dict(chaptering.environment, MKVLOG=str(mkv_log))
    chaptering.mkv_log = mkv_log
    return chaptering


def test_w_writes_the_chapters_into_the_input(writing, tmp_path):
    source, out = _tree(tmp_path, "a/show.mka")
    log = writing.chapter("-w", source, out, env=writing.environment)
    assert "chapters written into: a/show.mka" in log
    assert (source / "a/show.mka.written").read_text() == (
        out / "a/show.chapters.txt").read_text()


def test_without_w_the_input_is_not_touched(writing, tmp_path):
    source, out = _tree(tmp_path, "show.mka")
    writing.chapter(source, out, env=writing.environment)
    assert (out / "show.chapters.txt").is_file()
    assert writing.mkv_log.read_text() == ""


def test_w_over_finished_chapter_files_needs_no_model(writing, tmp_path):
    source, out = _tree(tmp_path, "show.mka")
    writing.chapter(source, out, env=writing.environment)
    writing.model_log.write_text("")
    environment = dict(writing.environment)
    del environment["chapterLlmUrl"]
    log = writing.chapter("-w", source, out, env=environment)
    assert "chapters written into: show.mka" in log
    assert writing.model_log.read_text() == ""
    # Written once: a rerun finds them there and leaves the file alone.
    writing.mkv_log.write_text("")
    log = writing.chapter("-w", source, out, env=environment)
    assert "chapters already in: show.mka" in log
    assert writing.mkv_log.read_text() == ""


def test_an_input_that_will_not_take_them_fails_the_run(writing, tmp_path):
    source, out = _tree(tmp_path, "show.mka")
    writing.with_tool("mkvpropedit", "exit 2")
    log = writing.chapter("-w", source, out, expect=1, env=writing.environment)
    assert "could not be written into it, so it is left as it was" in log


def test_w_puts_the_chapters_beside_what_has_no_place_for_them(writing, tmp_path):
    """A raw AAC comes out of the copy whole but holding no chapters: it gets
    the chapter file beside it, and the run goes on to the rest and succeeds."""
    writing.with_tool("ffmpeg", 'echo copy > "${!#}"')
    source, out = _tree(tmp_path, "a/show.aac", "b/talk.mka")
    (source / "a/show.aac").write_text("original")
    log = writing.chapter("-w", source, out, env=writing.environment)
    assert "chapters written beside: a/show.aac" in log
    assert (source / "a/show.chapters.txt").read_text() == (
        out / "a/show.chapters.txt").read_text()
    assert (source / "a/show.aac").read_text() == "original"
    assert "chapters written into: b/talk.mka" in log
    log = writing.chapter("-w", source, out, env=writing.environment)
    assert "chapters already beside: a/show.aac" in log


def test_w_with_the_input_as_the_output_folder(writing, tmp_path):
    """The chapter file is then beside every input already, which neither
    stops a file that can hold them from taking them nor needs copying."""
    writing.with_tool("ffmpeg", 'echo copy > "${!#}"')
    source, _out = _tree(tmp_path, "show.aac", "talk.mka")
    log = writing.chapter("-w", source, source, env=writing.environment)
    assert "chapters written beside: show.aac" in log
    assert "chapters written into: talk.mka" in log
