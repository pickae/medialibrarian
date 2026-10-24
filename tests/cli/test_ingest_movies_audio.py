"""The opus-transcode decision, per lossless track.

What is under test is the RULE and the argv it builds, so the two probes and the
encoder are stubbed.

The source's channel layout is the whole point. libopus takes one layout per
channel count, and ffmpeg REMIXES anything else onto it - a 4.1 loses its LFE -
so a layout opus has is encoded as it is, one it lacks is padded out to 5.1 or
7.1 with silent channels, and one neither covers is kept lossless.
"""

import os

import pytest

from medialib.cli import ingest_movies as im

pytestmark = pytest.mark.fs

# A leading video track pins the audio-stream-index arithmetic.
TRACKS = [
    ("Video", "hevc", "null", "video"),
    ("Discrete", "A_DTS", "4", "audio"),          # quad -> quad
    ("Discrete LFE", "A_DTS", "4", "audio"),      # 3.1 -> padded to 5.1
    ("Three", "A_FLAC", "3", "audio"),            # 3.0 -> 3.0
    ("Three LFE", "A_FLAC", "3", "audio"),        # 2.1 -> padded to 5.1
    ("Five", "A_DTS", "5", "audio"),              # other counts: plain -ac
    ("Seven", "A_DTS", "7", "audio"),
    ("Compatibility", "A_AC3", "6", "audio"),     # lossy, untouched
    ("Unreadable", "A_FLAC", "4", "audio"),       # no layout -> kept lossless
]

LAYOUTS = ["quad", "3.1", "3.0", "2.1", "5.0", "6.1", "5.1(side)", ""]


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

    def test_every_lossless_track_with_a_readable_layout(self, run):
        _movie, _base, calls = run
        assert len(calls) == 6

    def test_an_opus_lands_for_each_and_for_no_other(self, run):
        _movie, base, _calls = run
        assert os.path.isfile(base + "_1.opus")      # quad
        assert os.path.isfile(base + "_2.opus")      # 3.1, padded
        assert os.path.isfile(base + "_3.opus")      # 3.0
        assert os.path.isfile(base + "_4.opus")      # 2.1, padded
        assert os.path.isfile(base + "_5.opus")      # 5 channels
        assert os.path.isfile(base + "_6.opus")      # 7 channels
        assert not os.path.exists(base + "_7.opus")  # lossy
        assert not os.path.exists(base + "_8.opus")  # layout unreadable

    def test_a_track_kept_for_its_layout_says_so(self, capsys, run):
        """capsys comes first so it is already capturing when the phase runs."""
        captured = capsys.readouterr()
        assert ("audio track 8 kept as it is, its 4-channel layout 'unknown' "
                "has no opus equivalent") in captured.out + captured.err


class TestTheEncoderCall:
    """The argv is the contract: the stream mapped, the per-channel bitrate, and
    the layout or channel count."""

    def test_a_quad_source_converts_to_quad(self, run):
        movie, base, calls = run
        assert calls[0] == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-i", movie,
            "-vn", "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "libopus",
            "-b:a", "185k", "-channel_layout", "quad", base + "_1.opus"]

    def test_a_three_channel_source_with_no_LFE_converts_to_3_0(self, run):
        movie, base, calls = run
        assert calls[2] == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-i", movie,
            "-vn", "-map", "0:a:2", "-map_metadata", "-1", "-c:a", "libopus",
            "-b:a", "150k", "-channel_layout", "3.0", base + "_3.opus"]

    def test_a_3_1_source_is_padded_to_5_1_at_the_six_channel_rate(self, run):
        """Front left, right and centre and the LFE keep their places; the two
        surrounds 5.1 adds are silent."""
        movie, base, calls = run
        assert calls[1] == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-i", movie,
            "-vn", "-map", "0:a:1", "-map_metadata", "-1", "-c:a", "libopus",
            "-b:a", "250k",
            "-af", "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE",
            base + "_2.opus"]

    def test_a_2_1_source_is_padded_to_5_1_with_a_silent_centre(self, run):
        movie, base, calls = run
        assert calls[3] == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-i", movie,
            "-vn", "-map", "0:a:3", "-map_metadata", "-1", "-c:a", "libopus",
            "-b:a", "250k",
            "-af", "pan=5.1|FL=FL|FR=FR|LFE=LFE",
            base + "_4.opus"]

    @pytest.mark.parametrize("index,stream,bitrate,channels,suffix", [
        (4, "0:a:4", "220k", "5", "_5.opus"),
        (5, "0:a:5", "285k", "7", "_6.opus"),
    ])
    def test_every_other_count_is_left_to_a_plain_channel_count(
            self, run, index, stream, bitrate, channels, suffix):
        """A layout opus has from five channels up needs only its count."""
        movie, base, calls = run
        assert calls[index] == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-i", movie,
            "-vn", "-map", stream, "-map_metadata", "-1", "-c:a", "libopus",
            "-b:a", bitrate, "-ac", channels, base + suffix]


def _encode_one(tmp_path, monkeypatch, channels, layout):
    """One lossless track of <channels> in <layout> through the phase: the
    encoder calls made, and the probes asked of the source."""
    folder = tmp_path / "Film (2020)"
    folder.mkdir()
    movie = str(folder / "Film (2020).mkv")
    open(movie, "w").close()
    monkeypatch.setattr(im, "_identify", lambda path: [
        im.Track(id="0", type="video", codec="hevc", channels="null"),
        im.Track(id="1", type="audio", codec="A_TRUEHD", channels=channels)])
    probed = []

    def probe(argv):
        probed.append(argv)
        if "stream=channel_layout" in argv:
            return layout + "\n"
        return "7200\n" if os.path.isfile(argv[-1]) else ""
    monkeypatch.setattr(im, "_probe", probe)
    calls = []

    def encode(argv):
        calls.append(argv)
        open(argv[-1], "w").close()
        return 0
    monkeypatch.setattr(im, "_run", encode)
    im.check_audio_tracks(movie, str(tmp_path))
    return calls, probed


def _tail(call):
    """An encoder call from the bitrate on: the rate and the layout options."""
    return call[call.index("-b:a") + 1:-1]


class TestEveryLayout:
    """Encoded as it is where opus has the layout, padded out to 5.1 / 7.1
    where it does not, kept lossless where neither fits."""

    @pytest.mark.parametrize("channels,layout,tail", [
        ("3", "3.0", ["150k", "-channel_layout", "3.0"]),
        ("4", "quad", ["185k", "-channel_layout", "quad"]),
        ("5", "5.0", ["220k", "-ac", "5"]),
        ("5", "5.0(side)", ["220k", "-ac", "5"]),
        ("6", "5.1", ["250k", "-ac", "6"]),
        ("6", "5.1(side)", ["250k", "-ac", "6"]),
        ("7", "6.1", ["285k", "-ac", "7"]),
        ("8", "7.1", ["320k", "-ac", "8"]),
    ])
    def test_a_layout_opus_has_is_encoded_as_it_is(
            self, tmp_path, monkeypatch, channels, layout, tail):
        calls, _probed = _encode_one(tmp_path, monkeypatch, channels, layout)
        assert [_tail(call) for call in calls] == [tail]

    @pytest.mark.parametrize("channels,layout,bitrate,pan", [
        ("3", "2.1", "250k", "pan=5.1|FL=FL|FR=FR|LFE=LFE"),
        ("3", "3.0(back)", "250k",
         "pan=5.1|FL=FL|FR=FR|BL=0.7071*BC|BR=0.7071*BC"),
        ("4", "3.1", "250k", "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE"),
        ("4", "4.0", "250k",
         "pan=5.1|FL=FL|FR=FR|FC=FC|BL=0.7071*BC|BR=0.7071*BC"),
        ("4", "quad(side)", "250k", "pan=5.1|FL=FL|FR=FR|BL=SL|BR=SR"),
        ("5", "4.1", "250k",
         "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=0.7071*BC|BR=0.7071*BC"),
        ("6", "6.0", "320k",
         "pan=7.1|FL=FL|FR=FR|FC=FC|BL=0.7071*BC|BR=0.7071*BC|SL=SL|SR=SR"),
        ("7", "6.1(back)", "320k",
         "pan=7.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=0.7071*BC|BR=0.7071*BC"
         "|SL=BL|SR=BR"),
        ("7", "7.0", "320k",
         "pan=7.1|FL=FL|FR=FR|FC=FC|BL=BL|BR=BR|SL=SL|SR=SR"),
    ])
    def test_a_layout_opus_lacks_is_padded_at_the_padded_rate(
            self, tmp_path, monkeypatch, channels, layout, bitrate, pan):
        """Every source channel keeps its place, the LFE included."""
        calls, _probed = _encode_one(tmp_path, monkeypatch, channels, layout)
        assert [_tail(call) for call in calls] == [[bitrate, "-af", pan]]

    @pytest.mark.parametrize("channels,layout", [
        ("4", ""), ("4", "4 channels"), ("3", ""),
        ("6", "hexagonal"), ("8", "7.1(wide)"), ("8", "octagonal"),
        ("6", "3.1.2"),
    ])
    def test_neither_is_kept_lossless_and_said_so(
            self, tmp_path, monkeypatch, capsys, channels, layout):
        calls, _probed = _encode_one(tmp_path, monkeypatch, channels, layout)
        assert calls == []
        captured = capsys.readouterr()
        assert "kept as it is" in captured.out + captured.err

    @pytest.mark.parametrize("layout", ["", "6 channels"])
    def test_an_unnamed_layout_from_five_up_takes_the_counts_default(
            self, tmp_path, monkeypatch, layout):
        """ffmpeg's default for the count is opus's own layout."""
        calls, _probed = _encode_one(tmp_path, monkeypatch, "6", layout)
        assert [_tail(call) for call in calls] == [["250k", "-ac", "6"]]

    @pytest.mark.parametrize("channels,bitrate", [("1", "100k"),
                                                  ("2", "120k")])
    def test_mono_and_stereo_are_not_probed(
            self, tmp_path, monkeypatch, channels, bitrate):
        calls, probed = _encode_one(tmp_path, monkeypatch, channels, "")
        assert [_tail(call) for call in calls] == [[bitrate, "-ac", channels]]
        assert not any("stream=channel_layout" in argv for argv in probed)


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
        monkeypatch.setattr(im, "_probe", lambda argv:
                            "5.1\n" if "stream=channel_layout" in argv
                            else "7200\n")
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
                            "5.1\n" if "stream=channel_layout" in argv
                            else "10\n" if argv[-1].endswith(".opus")
                            else "7200\n")
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
