"""`transcribe-audio` end to end: one input, one transcript, in a mirrored folder.

Everything interesting is in how the input is turned into that transcript, which
needs no real model and no real media:

  * an AUDIO file is handed to whisper as it is, with no extraction;
  * a VIDEO file has its FIRST audio track lifted out and that extract is what
    whisper is handed - the video stream and any later tracks are never seen;
  * a video with NO audio track is skipped with a warning rather than a transcript.

`nvidia-smi` reports no GPU, so the model choice takes the CPU path - a fixed model
and no probe - whatever host this runs on.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

# A source whose name carries "noaudio" has no first audio track, so the extract -
# the only 0:a:0 mapping here - fails. Everything else creates the output it was
# given, which is the last argument.
_FFMPEG = r"""
printf '%s\n' "$*" >> "$FFMPEGLOG"
inp=""; prev=""
for a in "$@"; do [[ "$prev" == "-i" ]] && inp="$a"; prev="$a"; done
case "$inp" in *noaudio*) exit 1 ;; esac
out="${!#}"
[[ "$out" == "-" ]] || : > "$out"
exit 0
"""

# Writes the transcript named after the input file - basename, original extension
# dropped - into the given output directory, exactly as the real one does.
_PIPX = r"""
printf '%s\n' "$*" >> "$WHISPERLOG"
input="$3"
outDir=""; fmt="txt"; prev=""
for a in "$@"; do
    case "$prev" in
        --output_dir)    outDir="$a" ;;
        --output_format) fmt="$a" ;;
    esac
    prev="$a"
done
base="${input##*/}"
printf 'stub transcript\n' > "$outDir/${base%.*}.$fmt"
exit 0
"""


@pytest.fixture
def transcribe(sandbox, tmp_path):
    for tool in ("xargs", "sed"):
        if shutil.which(tool) is None:
            pytest.fail("the host has no %s: the queue is drained through it"
                        % tool)
    whisper_log = tmp_path / "whisper.calls"
    ffmpeg_log = tmp_path / "ffmpeg.calls"
    whisper_log.write_text("")
    ffmpeg_log.write_text("")
    sandbox.with_tool("ffmpeg", _FFMPEG)
    sandbox.with_tool("pipx", _PIPX)
    # No GPU, so the answer does not depend on the host.
    sandbox.with_tool("nvidia-smi", "exit 0")

    environment = dict(os.environ, WHISPERLOG=str(whisper_log),
                       FFMPEGLOG=str(ffmpeg_log))

    def run(*args, expect=0):
        done = sandbox.run("transcribe-audio", *args, env=environment,
                           timeout=600)
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.whisper_log = whisper_log
    sandbox.ffmpeg_log = ffmpeg_log
    sandbox.transcribe = run
    return sandbox


class TestOneRunOverAMixedTree:
    @pytest.fixture
    def run(self, transcribe, tmp_path):
        source = tmp_path / "in"
        outputs = tmp_path / "out"
        for relative in ("a/track.mp3",        # audio: transcribed directly
                         "b/movie.mkv",        # video: its first track is used
                         "b/noaudio.mkv",      # video with no audio track
                         "c/UPPER.MP3"):       # an uppercase extension
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        log = transcribe.transcribe(source, outputs)
        return transcribe, source, outputs, log

    def test_an_audio_file_becomes_a_mirrored_transcript(self, run):
        _, _, outputs, _ = run
        assert (outputs / "a" / "track.txt").is_file()

    def test_an_uppercase_extension_is_still_recognised(self, run):
        """The scan is case-insensitive."""
        _, _, outputs, _ = run
        assert (outputs / "c" / "UPPER.txt").is_file()

    def test_a_video_becomes_one_the_same_way(self, run):
        _, _, outputs, _ = run
        assert (outputs / "b" / "movie.txt").is_file()

    def test_a_video_with_no_audio_track_is_skipped_with_a_warning(self, run):
        _, _, outputs, log = run
        assert not (outputs / "b" / "noaudio.txt").exists()
        assert "noaudio" in log, log

    def test_only_the_video_is_extracted_and_from_its_first_track(self, run):
        transcribe, _, _, _ = run
        asked = transcribe.ffmpeg_log.read_text()
        assert "-map 0:a:0" in asked, asked
        assert "track.mp3" not in asked, asked

    def test_the_audio_goes_to_whisper_directly_and_the_video_as_its_extract(
            self, run):
        transcribe, _, _, _ = run
        calls = transcribe.whisper_log.read_text()
        assert "a/track.mp3" in calls, calls
        assert "track.wav" in calls, calls

    @pytest.mark.parametrize("argument", ["--model", "--device cpu",
                                          "--output_format txt"])
    def test_whisper_is_given_what_it_needs(self, run, argument):
        transcribe, _, _, _ = run
        assert argument in transcribe.whisper_log.read_text()


class TestASecondPass:
    """Every input already has its transcript, so no whisper run is queued and the
    output tree is left exactly as it was."""

    def test_it_queues_nothing_and_changes_nothing(self, transcribe, tmp_path):
        source = tmp_path / "in"
        outputs = tmp_path / "out"
        (source / "a").mkdir(parents=True)
        (source / "a" / "track.mp3").touch()
        transcribe.transcribe(source, outputs)
        calls = len(transcribe.whisper_log.read_text().splitlines())
        before = blackbox.tree_of(outputs)
        transcribe.transcribe(source, outputs)
        assert len(transcribe.whisper_log.read_text().splitlines()) == calls
        assert blackbox.tree_of(outputs) == before


class TestTheTranscriptFormat:
    def test_it_is_honoured_end_to_end(self, transcribe, tmp_path):
        source = tmp_path / "in2"
        outputs = tmp_path / "out2"
        (source / "talk").mkdir(parents=True)
        (source / "talk" / "episode.opus").touch()
        transcribe.transcribe("-f", "srt", source, outputs)
        assert (outputs / "talk" / "episode.srt").is_file()
        assert not (outputs / "talk" / "episode.txt").exists()


class TestTheJobCount:
    def test_one_that_is_not_a_number_is_refused_up_front(self, transcribe,
                                                          tmp_path):
        """Rather than surfacing as a queue-driver error on the first worker."""
        source = tmp_path / "in3"
        (source / "talk").mkdir(parents=True)
        (source / "talk" / "episode.opus").touch()
        transcribe.transcribe("-j", "zero", source, tmp_path / "out3", expect=1)
        assert transcribe.whisper_log.read_text() == ""
