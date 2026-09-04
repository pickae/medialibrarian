"""Tier D for what convertAudio does with a video whose soundtrack is already
Opus.

The rule cannot be checked with a stub, because it is about the SAMPLES that
come out: a video whose audio stream is already what this pipeline produces -
Opus, below the re-encode threshold - has that stream LIFTED OUT into the .opus,
sample for sample. Encoding 46 kbps Opus into 46 kbps Opus changes nothing
except to spend another lossy generation on it, and a library assembled from the
web is full of exactly this file.

"Lifted out" is asserted the only way it honestly can be: the audio PACKETS of
the output are bit-for-bit the packets of the source's stream, which no
re-encode however good can produce. It is deliberately not the DECODED audio
that is compared - Matroska and Ogg state an Opus stream's pre-skip differently,
so a perfect remux still decodes a few milliseconds longer at the front, and
comparing samples would report a re-encode that never happened.

The fixtures are NOISE rather than tones on purpose: a sine wave costs an Opus
encoder almost nothing, so two different bitrates would produce nearly the same
file and the "was this re-encoded?" assertions would prove very little.
"""

import hashlib
import shutil
import subprocess

import pytest

from tests import blackbox

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]



def _has_libopus():
    done = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                          capture_output=True, text=True)
    return "libopus" in done.stdout


pytestmark.append(pytest.mark.skipif(
    shutil.which("ffmpeg") is not None and not _has_libopus(),
    reason="this ffmpeg build has no libopus"))


def _packet_md5(path):
    """The compressed audio packets of a file: what a stream copy keeps exactly
    and an encoder cannot reproduce."""
    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "data", "-"],
        capture_output=True, stdin=subprocess.DEVNULL)
    return hashlib.md5(done.stdout).hexdigest()


def _property(path, entry):
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a:0", "-show_entries",
         "stream=" + entry, "-of", "default=nk=1:nw=1", "--", str(path)],
        capture_output=True, text=True)
    return done.stdout.splitlines()[0] if done.stdout.strip() else ""


def _seconds(path):
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", "--", str(path)],
        capture_output=True, text=True)
    return float(done.stdout.strip() or 0)


def _video_with_audio(path, codec_args):
    """A container with a video stream in it, which is all this decision looks
    at: 30 seconds of noise under a tiny picture."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5:d=30",
         "-f", "lavfi", "-i", "anoisesrc=d=30:c=pink:r=48000",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         *codec_args, "-ac", "1", str(path)],
        check=True, stdin=subprocess.DEVNULL)
    return path


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    """One run of the real script over the three shapes, at the readLibrary
    settings - mono at 36 kbps, comfortably below every fixture, so any
    re-encode would be plainly audible in the checksum."""
    tmp = tmp_path_factory.mktemp("liftout")
    source, out = tmp / "in", tmp / "out"
    source.mkdir()

    finished = _video_with_audio(source / "finished.mkv",
                                 ["-c:a", "libopus", "-b:a", "46k"])
    # the same, but well above the threshold: that one has to be encoded down
    loud = _video_with_audio(source / "loud.mkv",
                             ["-c:a", "libopus", "-b:a", "128k"])
    # and one whose soundtrack is small but is NOT Opus
    aac = _video_with_audio(source / "aac.mp4", ["-c:a", "aac", "-b:a", "40k"])

    before = {"finished": _packet_md5(finished), "loud": _packet_md5(loud)}
    # The real tools, so no stub PATH and no sandbox: this tier is about what
    # ffmpeg actually produced. `blackbox.run` is only the launcher.
    done = blackbox.run("convert-audio", "-m", "-b", "36", source, out,
                        cwd=source.parent, timeout=900)
    return source, out, before, done, {"finished": finished, "loud": loud,
                                       "aac": aac}


def test_the_fixtures_really_are_what_they_claim(converted):
    _source, _out, _before, _done, made = converted
    assert _property(made["finished"], "codec_name") == "opus"
    assert _property(made["aac"], "codec_name") == "aac"


def test_the_run_exits_zero(converted):
    _source, _out, _before, done, _made = converted
    assert done.returncode == 0, done.stderr


class TestAFinishedOpusSoundtrack:
    def test_it_produced_an_opus(self, converted):
        _source, out, _before, _done, _made = converted
        assert (out / "finished.opus").is_file()
        assert _property(out / "finished.opus", "codec_name") == "opus"

    def test_holding_exactly_the_sources_packets(self, converted):
        """Lifted out, not re-encoded."""
        _source, out, before, _done, _made = converted
        assert _packet_md5(out / "finished.opus") == before["finished"]


class TestASoundtrackAboveTheThreshold:
    def test_it_was_really_re_encoded(self, converted):
        _source, out, before, _done, _made = converted
        assert (out / "loud.opus").is_file()
        assert _property(out / "loud.opus", "codec_name") == "opus"
        assert _packet_md5(out / "loud.opus") != before["loud"]

    def test_and_came_out_much_smaller_than_its_source(self, converted):
        _source, out, _before, _done, made = converted
        assert (out / "loud.opus").stat().st_size < \
            made["loud"].stat().st_size / 2


class TestASoundtrackThatIsSmallButNotOpus:
    """AAC has to be encoded whatever its size, because AAC is not what this
    library keeps."""

    def test_it_came_out_as_opus_and_not_a_copied_aac_stream(self, converted):
        _source, out, _before, _done, _made = converted
        assert (out / "aac.opus").is_file()
        assert _property(out / "aac.opus", "codec_name") == "opus"

    def test_and_is_the_whole_thirty_seconds(self, converted):
        _source, out, _before, _done, _made = converted
        assert 29 < _seconds(out / "aac.opus") < 31
