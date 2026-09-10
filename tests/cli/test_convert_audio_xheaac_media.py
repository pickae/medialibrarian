"""Tier D for `convert-audio -o xheaac`: the real external encoder, end to end.

Everything about this codec that can be checked without running it is checked in
tests/lib/test_xheaac.py and tests/cli/test_convert_audio.py - the back-end
choice, the refusals, the preset ladder, the output paths. What is left is the
part only the real tools can answer, and it is the part that matters most: that
what comes out is really xHE-AAC, that it is the whole recording, and that the
chapters and the cover survive a pipeline whose Opus path writes both of them
through mutagen and whose MP4 path cannot.

The one rule with teeth here is the LIFT-OUT guard. ffprobe calls xHE-AAC `aac`,
the same codec name that AAC-LC, HE-AAC, HE-AACv2, LD and ELD all answer, so the
"is this source already what I produce?" test has to read the profile beside it.
Get that wrong and a 40 kbps AAC-LC podcast is stream-copied into an .m4a and
filed as xHE-AAC - a silent, permanent mislabelling of a library, and the exact
failure `test_an_aac_lc_soundtrack_is_encoded_and_not_copied` exists to catch.

The fixtures are NOISE rather than tones, for the reason the Opus tier gives:
a sine wave costs an encoder almost nothing, so a re-encode and a copy would
produce nearly the same file and the assertions would prove very little.
"""

import shutil
import subprocess

import pytest

from tests import blackbox

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
    pytest.mark.skipif(shutil.which("exhale") is None,
                       reason="no xHE-AAC encoder: build exhale "
                              "(https://gitlab.com/ecodis/exhale)"),
]

# What ffprobe calls this codec and this profile. The codec name is shared with
# every other member of the AAC family, which is the whole point of checking
# both.
CODEC = "aac"
PROFILE = "xHE-AAC"


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


def _chapter_count(path):
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_chapters", "-of", "compact",
         "--", str(path)], capture_output=True, text=True)
    return len([line for line in done.stdout.splitlines()
                if line.startswith("chapter|")])


def _has_cover(path):
    """Whether the file carries an MP4 `covr` atom, read by the library that
    wrote it rather than by ffprobe - which reports an MP4 cover as a video
    stream and would not tell a cover from the picture of a video."""
    from mutagen.mp4 import MP4

    return bool(MP4(str(path)).get("covr"))


def _noise(path, seconds, codec_args):
    """<seconds> of pink noise in whatever the codec arguments ask for."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "anoisesrc=d=%d:c=pink:r=48000" % seconds,
         "-map", "0:a", "-ac", "2", *codec_args, str(path)],
        check=True, stdin=subprocess.DEVNULL)
    return path


def _video_with_audio(path, seconds, codec_args):
    """A container with a video stream, which is what sends a file down the
    lift-out branch at all."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5:d=%d" % seconds,
         "-f", "lavfi", "-i", "anoisesrc=d=%d:c=pink:r=48000" % seconds,
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         *codec_args, "-ac", "2", str(path)],
        check=True, stdin=subprocess.DEVNULL)
    return path


def _add_chapters(path, seconds):
    """Two chapters into a FLAC, as the OGM "CHAPTERnnn=" Vorbis-comment rows a
    FLAC really carries them in - which is what mutagen writes and what ffprobe
    reads back as chapters, so this is the shape a chaptered source has here.

    Written with mutagen because ffmpeg's FLAC muxer writes no chapters at all:
    not from an ffmetadata input, not with an explicit -map_chapters.
    """
    from mutagen.flac import FLAC

    half = seconds // 2
    tags = FLAC(str(path))
    tags["CHAPTER000"] = "00:00:00.000"
    tags["CHAPTER000NAME"] = "One"
    tags["CHAPTER001"] = "00:%02d:%02d.000" % (half // 60, half % 60)
    tags["CHAPTER001NAME"] = "Two"
    tags.save()
    return path


SECONDS = 20


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    """One run of the real command over four shapes, with -s set low enough
    that a codec which DID split would visibly split."""
    tmp = tmp_path_factory.mktemp("xheaac")
    source, out = tmp / "in", tmp / "out"
    source.mkdir()

    # A plain FLAC, well above the threshold, so it is encoded. Chapters go in
    # after it: MP4 has nowhere to keep a Vorbis-comment row, so carrying these
    # across is a chapter TRACK written by ffmpeg and not a tag.
    chaptered = _add_chapters(_noise(source / "chaptered.flac", SECONDS, []),
                              SECONDS)

    # A sidecar cover sharing the track's name, which has to end up inside the
    # .m4a as a `covr` atom.
    cover = source / "chaptered.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(cover)],
        check=True, stdin=subprocess.DEVNULL)

    # A video whose soundtrack is small AAC-LC. It must be ENCODED, not lifted
    # out: it shares its codec name with the output and nothing else.
    aac = _video_with_audio(source / "aaclc.mp4", SECONDS,
                            ["-c:a", "aac", "-b:a", "40k"])

    # A video whose soundtrack is loud Opus - encoded, like the Opus tier's.
    opus = _video_with_audio(source / "opus.mkv", SECONDS,
                             ["-c:a", "libopus", "-b:a", "128k"])

    done = blackbox.run("convert-audio", "-o", "xheaac", "-s", "5",
                        source, out, cwd=tmp, timeout=1800)
    return {"source": source, "out": out, "done": done,
            "made": {"chaptered": chaptered, "aac": aac, "opus": opus}}


def test_the_fixtures_really_are_what_they_claim(converted):
    made = converted["made"]
    assert _property(made["aac"], "codec_name") == "aac"
    assert _property(made["aac"], "profile") != PROFILE, (
        "the AAC-LC fixture is already xHE-AAC, so it proves nothing")
    assert _chapter_count(made["chaptered"]) == 2


def test_the_run_exits_zero(converted):
    done = converted["done"]
    assert done.returncode == 0, done.stderr


def test_the_run_says_which_encoder_it_picked(converted):
    """Two back-ends can encode this codec, so a library has to be able to say
    which one made it."""
    assert "exhale" in converted["done"].stdout


class TestWhatCameOut:
    def test_every_track_is_an_m4a_and_nothing_is_an_opus(self, converted):
        out = converted["out"]
        assert sorted(path.name for path in out.rglob("*.m4a")) == [
            "aaclc.m4a", "chaptered.m4a", "opus.m4a"]
        assert list(out.rglob("*.opus")) == []

    @pytest.mark.parametrize("name", ["chaptered", "aaclc", "opus"])
    def test_and_really_holds_xhe_aac(self, converted, name):
        made = converted["out"] / (name + ".m4a")
        assert _property(made, "codec_name") == CODEC
        assert _property(made, "profile") == PROFILE

    @pytest.mark.parametrize("name", ["chaptered", "aaclc", "opus"])
    def test_and_is_the_whole_recording(self, converted, name):
        """Short of the full length means a pipe that was closed early, which is
        the failure a two-process encode invites."""
        assert _seconds(converted["out"] / (name + ".m4a")) >= SECONDS - 1


class TestTheLiftOutGuard:
    def test_an_aac_lc_soundtrack_is_encoded_and_not_copied(self, converted):
        """The rule this whole tier exists for. AAC-LC at 40 kbps is small
        enough to keep and shares the output's codec NAME, so a check that read
        the name alone would copy it through and file it as xHE-AAC.
        """
        made = converted["out"] / "aaclc.m4a"
        assert _property(made, "profile") == PROFILE
        assert _property(converted["made"]["aac"], "profile") != PROFILE


class TestTheMetadataMp4CannotKeepAsATag:
    def test_the_chapters_came_across_as_a_chapter_track(self, converted):
        """mutagen writes the Opus path's chapters as Vorbis comments, which MP4
        has no room for at all, so this half is ffmpeg's `-map_chapters`."""
        assert _chapter_count(converted["out"] / "chaptered.m4a") == 2

    def test_the_sidecar_cover_is_embedded_as_a_covr_atom(self, converted):
        assert _has_cover(converted["out"] / "chaptered.m4a")


class TestSplittingIsOff:
    def test_a_low_split_threshold_is_refused_out_loud(self, converted):
        """-s 5 over 20-second files would chunk every one of them for a codec
        that could be chunked. This one says so rather than ignoring the flag
        silently."""
        assert "keeps long files whole" in converted["done"].stdout

    def test_and_nothing_was_re_concatenated(self, converted):
        """The join is the step that would not be sample-exact, so its absence
        is the thing to assert."""
        assert "Re-concatenating" not in converted["done"].stdout
