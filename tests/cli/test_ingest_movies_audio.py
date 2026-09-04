"""The opus-transcode decision, per lossless track.

What is under test is the RULE and the argv it builds, so the two probes and the
encoder are stubbed.

The 3- and 4-channel counts are the whole point. ffmpeg's own defaults for them
are layouts libopus refuses, so the layout has to be forced - and forcing it only
renames channels when the source carries no LFE, so a 2.1 or 3.1 source would
have its LFE remapped onto a regular speaker and is kept lossless instead.
"""

import os

import pytest

from medialib.cli import ingest_movies as im

pytestmark = pytest.mark.fs

# A leading video track pins the audio-stream-index arithmetic.
TRACKS = [
    ("Video", "hevc", "null", "video"),
    ("Discrete", "A_DTS", "4", "audio"),          # 4.0 -> quad, no LFE
    ("Discrete LFE", "A_DTS", "4", "audio"),      # 3.1 -> kept lossless
    ("Three", "A_FLAC", "3", "audio"),            # 3.0 -> 3.0
    ("Three LFE", "A_FLAC", "3", "audio"),        # 2.1 -> kept lossless
    ("Five", "A_DTS", "5", "audio"),              # other counts: plain -ac
    ("Seven", "A_DTS", "7", "audio"),
    ("Compatibility", "A_AC3", "6", "audio"),     # lossy, untouched
]

LAYOUTS = ["4.0", "3.1", "3.0", "2.1", "5.0", "6.1", "5.1(side)"]


@pytest.fixture
def run(tmp_path, monkeypatch):
    """The phase run over one film, with every tool stubbed: what comes back is
    the encoder calls it made, in order."""
    folder = tmp_path / "Film (2020)"
    folder.mkdir()
    movie = str(folder / "Film (2020).mkv")
    open(movie, "w").close()

    tracks = [im.Track(id=str(position), type=kind, codec=codec,
                       channels=channels, name=name, language="eng",
                       commentary="false")
              for position, (name, codec, channels, kind)
              in enumerate(TRACKS)]
    monkeypatch.setattr(im, "_identify", lambda path: tracks)

    def probe(argv):
        if "stream=channel_layout" in argv:
            stream = argv[argv.index("-select_streams") + 1]
            return LAYOUTS[int(stream.split(":")[1])] + "\n"
        if "format=duration" in argv:
            # The source is long; an output that does not exist yet is zero, so
            # every wanted track is encoded.
            return "7200\n" if os.path.isfile(argv[-1]) else ""
        return ""
    monkeypatch.setattr(im, "_probe", probe)

    calls = []

    def encode(argv):
        calls.append(argv)
        open(argv[-1], "w").close()
        return 0
    monkeypatch.setattr(im, "_run", encode)

    im.check_audio_tracks(movie, str(tmp_path))
    return movie, str(folder / "Film (2020)"), calls


class TestWhichTracksAreTranscoded:

    def test_exactly_the_LFE_free_lossless_tracks(self, run):
        _movie, _base, calls = run
        assert len(calls) == 4

    def test_an_opus_lands_for_each_and_for_no_other(self, run):
        _movie, base, _calls = run
        assert os.path.isfile(base + "_1.opus")      # 4.0
        assert not os.path.exists(base + "_2.opus")  # 3.1, an LFE to remap
        assert os.path.isfile(base + "_3.opus")      # 3.0
        assert not os.path.exists(base + "_4.opus")  # 2.1, an LFE to remap
        assert os.path.isfile(base + "_5.opus")      # 5 channels
        assert os.path.isfile(base + "_6.opus")      # 7 channels
        assert not os.path.exists(base + "_7.opus")  # lossy


class TestTheEncoderCall:
    """The argv is the contract: the stream mapped, the per-channel bitrate, and
    the layout or channel count."""

    def test_a_four_channel_source_with_no_LFE_converts_to_quad(self, run):
        movie, base, calls = run
        assert calls[0] == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-i", movie,
            "-vn", "-map", "0:a:0", "-c:a", "libopus", "-b:a", "185k",
            "-channel_layout", "quad", base + "_1.opus"]

    def test_a_three_channel_source_with_no_LFE_converts_to_3_0(self, run):
        movie, base, calls = run
        assert calls[1] == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-i", movie,
            "-vn", "-map", "0:a:2", "-c:a", "libopus", "-b:a", "150k",
            "-channel_layout", "3.0", base + "_3.opus"]

    @pytest.mark.parametrize("index,stream,bitrate,channels,suffix", [
        (2, "0:a:4", "220k", "5", "_5.opus"),
        (3, "0:a:5", "285k", "7", "_6.opus"),
    ])
    def test_every_other_count_is_left_to_a_plain_channel_count(
            self, run, index, stream, bitrate, channels, suffix):
        """The layout guard applies to 3 and 4 alone; the rest are fine with
        -ac."""
        movie, base, calls = run
        assert calls[index] == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-i", movie,
            "-vn", "-map", stream, "-c:a", "libopus", "-b:a", bitrate,
            "-ac", channels, base + suffix]


class TestResume:
    """A finished output is not made twice."""

    def test_a_complete_opus_is_left_alone(self, tmp_path, monkeypatch):
        folder = tmp_path / "Film (2020)"
        folder.mkdir()
        movie = str(folder / "Film (2020).mkv")
        open(movie, "w").close()
        base = str(folder / "Film (2020)")
        open(base + "_1.opus", "w").close()

        monkeypatch.setattr(im, "_identify", lambda path: [
            im.Track(id="0", type="video", codec="hevc", channels="null"),
            im.Track(id="1", type="audio", codec="A_DTS", channels="6")])
        # Both the source and the existing output report the same length, so the
        # output is complete.
        monkeypatch.setattr(im, "_probe", lambda argv: "7200\n")
        calls = []
        monkeypatch.setattr(im, "_run", lambda argv: calls.append(argv) or 0)

        im.check_audio_tracks(movie, str(tmp_path))
        assert calls == []

    def test_a_truncated_opus_is_encoded_again(self, tmp_path, monkeypatch):
        folder = tmp_path / "Film (2020)"
        folder.mkdir()
        movie = str(folder / "Film (2020).mkv")
        open(movie, "w").close()
        base = str(folder / "Film (2020)")
        open(base + "_1.opus", "w").close()

        monkeypatch.setattr(im, "_identify", lambda path: [
            im.Track(id="0", type="video", codec="hevc", channels="null"),
            im.Track(id="1", type="audio", codec="A_DTS", channels="6")])
        monkeypatch.setattr(im, "_probe", lambda argv:
                            "10\n" if argv[-1].endswith(".opus") else "7200\n")
        calls = []
        monkeypatch.setattr(im, "_run", lambda argv: calls.append(argv) or 0)

        im.check_audio_tracks(movie, str(tmp_path))
        assert len(calls) == 1


class TestTheBonusGuard:
    """Every per-movie phase opens with it: a featurette is not a film."""

    def test_a_bonus_folders_tracks_are_not_transcoded(self, tmp_path,
                                                       monkeypatch):
        folder = tmp_path / "Film (2020)" / "Trailers"
        folder.mkdir(parents=True)
        movie = str(folder / "A trailer.mkv")
        open(movie, "w").close()

        called = []
        monkeypatch.setattr(im, "_identify",
                            lambda path: called.append(path) or [])
        im.check_audio_tracks(movie, str(tmp_path))
        assert called == []
