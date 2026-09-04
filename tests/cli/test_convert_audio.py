"""The white box for medialib/cli/convert_audio.py.

Its five small helpers. The planning arithmetic beside them - the chunk plan and
the boundary nudging - belongs to `medialib/lib/segments.py` and is pinned with
it, so it is not repeated here.
"""

import pytest

from medialib.cli import convert_audio as ca

pytestmark = pytest.mark.pure


class TestResolveBitrate:
    """The forced-mono swap, which exists so a CHUNK encode derives bit-for-bit
    the same setting as a whole-file one."""

    def test_stereo_keeps_the_stereo_bitrate(self):
        assert ca.resolve_bitrate(46, mono=False) == 46

    def test_mono_at_the_default_swaps_in_the_mono_bitrate(self):
        assert ca.resolve_bitrate(46, mono=True) == 32

    @pytest.mark.parametrize("mono", [True, False])
    def test_an_explicit_bitrate_is_never_second_guessed(self, mono):
        """The swap is deliberately narrow: it fires only when the bitrate is
        still the built-in default."""
        assert ca.resolve_bitrate(64, mono=mono) == 64


class TestIsVideoFile:
    @pytest.mark.parametrize("name", ["a.mkv", "A.MKV", "a.Mp4",
                                      "/deep/path/to/a.webm"])
    def test_a_video_container_is_one_whatever_its_spelling(self, name):
        assert ca.is_video_file(name) is True

    @pytest.mark.parametrize("name", ["a.mp3", "a.opus", "noextension", ""])
    def test_everything_else_is_not(self, name):
        assert ca.is_video_file(name) is False


class TestAlwaysTranscodeFile:
    """The formats re-encoded however small they already are, because it is their
    CONTAINER that is unwanted in the output rather than their size."""

    @pytest.mark.parametrize("name", ["a.m4a", "A.M4A", "a.m4b", "a.mka"])
    def test_a_listed_container_is_always_transcoded(self, name):
        assert ca.always_transcode_file(name) is True

    @pytest.mark.parametrize("name", ["a.mp3", "a.opus", "a.flac"])
    def test_others_are_judged_on_their_bitrate_instead(self, name):
        assert ca.always_transcode_file(name) is False


class TestSourceAudioIsFinished:
    """Is this source's audio ALREADY what the pipeline produces, and small
    enough to keep? The question a video is asked before its soundtrack is
    re-encoded - encoding 46 kbps Opus into 46 kbps Opus changes nothing except
    to spend another lossy generation on it.

    The two probes are the only parts that would need ffprobe, so they are
    stubbed: what is under test is the RULE.
    """

    @pytest.fixture
    def probes(self, monkeypatch):
        answers = {"codec": "opus", "measured": 0}

        monkeypatch.setattr(ca, "source_audio_codec",
                            lambda src: answers["codec"])
        monkeypatch.setattr(ca, "estimated_audio_bitrate",
                            lambda src: answers["measured"])
        return answers

    def test_an_opus_stream_below_the_threshold_is_finished(self, probes):
        assert ca.source_audio_is_finished("x", 46000, 90000) is True

    def test_an_opus_stream_above_the_threshold_is_not(self, probes):
        assert ca.source_audio_is_finished("x", 128000, 90000) is False

    def test_the_threshold_itself_is_not_below_it(self, probes):
        assert ca.source_audio_is_finished("x", 90000, 90000) is False

    @pytest.mark.parametrize("stated", [0, ""])
    def test_an_unstated_bitrate_is_measured_and_a_small_one_counts(
            self, probes, stated):
        """A stated 0 means "nothing stated it", which Matroska and WebM do all
        the time - they carry no per-stream bitrate at all. Rather than guess
        either way, the stream is weighed and the measurement decides."""
        probes["measured"] = 46000
        assert ca.source_audio_is_finished("x", stated, 90000) is True

    def test_a_large_measurement_still_means_re_encode(self, probes):
        probes["measured"] = 200000
        assert ca.source_audio_is_finished("x", 0, 90000) is False

    def test_a_stream_that_cannot_be_weighed_at_all_is_re_encoded(self, probes):
        probes["measured"] = 0
        assert ca.source_audio_is_finished("x", 0, 90000) is False

    @pytest.mark.parametrize("codec", ["aac", "vorbis", "mp3", ""])
    def test_a_small_stream_of_another_codec_still_has_to_be_encoded(
            self, probes, codec):
        probes["codec"] = codec
        assert ca.source_audio_is_finished("x", 46000, 90000) is False


class TestAdaptiveBitrate:
    """Adaptive mode reads its per-file target out of the shared table's
    COMMENTARY column - the spoken-word one, which is what this script
    ingests."""

    @pytest.mark.parametrize("channels,expected", [
        (1, 55), (2, 65), (6, 150), (8, 200),
    ])
    def test_a_channel_count_gets_its_own_row(self, channels, expected):
        assert ca.adaptive_bitrate(channels, 46) == expected

    def test_a_count_with_no_row_falls_back_to_stereo(self, channels=99):
        assert ca.adaptive_bitrate(channels, 46) == 65

    def test_an_empty_table_falls_back_to_the_script_default(self, monkeypatch):
        monkeypatch.setattr(ca.bitrates, "audio_bitrate",
                            lambda channels, column: None)
        assert ca.adaptive_bitrate(2, 46) == 46


class TestSourceAudioBitrate:
    """Only the FIRST AUDIO stream, because that is the one stream the encode
    maps - the container's overall bitrate is the sum of every stream."""

    def _probe(self, monkeypatch, document):
        import json
        monkeypatch.setattr(ca, "_probe", lambda argv: json.dumps(document))

    def test_the_streams_own_field_wins(self, monkeypatch):
        self._probe(monkeypatch, {"streams": [
            {"codec_type": "audio", "bit_rate": "128000"}],
            "format": {"bit_rate": "999999"}})
        assert ca.source_audio_bitrate("x") == 128000

    def test_a_matroska_BPS_tag_stands_in_for_it(self, monkeypatch):
        """Matroska states no per-stream bitrate; mkvmerge writes a per-track
        tag instead, whose suffix spelling is not fixed."""
        self._probe(monkeypatch, {"streams": [
            {"codec_type": "audio", "tags": {"BPS-eng": "64000"}}]})
        assert ca.source_audio_bitrate("x") == 64000

    def test_the_container_bitrate_is_a_last_resort_for_audio_only_files(
            self, monkeypatch):
        self._probe(monkeypatch, {"streams": [{"codec_type": "audio"}],
                                  "format": {"bit_rate": "70000"}})
        assert ca.source_audio_bitrate("x") == 70000

    def test_but_never_for_a_video(self, monkeypatch):
        """A video's overall bitrate is dominated by its picture and says nothing
        about its soundtrack, so it reports 0 - "unknown", not "small"."""
        self._probe(monkeypatch, {
            "streams": [{"codec_type": "audio"}, {"codec_type": "video"}],
            "format": {"bit_rate": "4000000"}})
        assert ca.source_audio_bitrate("x") == 0

    def test_an_attached_cover_is_not_a_video_stream(self, monkeypatch):
        self._probe(monkeypatch, {
            "streams": [{"codec_type": "audio"},
                        {"codec_type": "video",
                         "disposition": {"attached_pic": 1}}],
            "format": {"bit_rate": "70000"}})
        assert ca.source_audio_bitrate("x") == 70000

    @pytest.mark.parametrize("raw", ["123.4", "[]", "null", "", "not json"])
    def test_a_probe_that_answered_no_object_states_nothing(self, monkeypatch,
                                                            raw):
        """jq's `.streams[]?` yields nothing for anything that is not an object
        and the chain falls through to 0, so the port answers 0 rather than
        raising."""
        monkeypatch.setattr(ca, "_probe", lambda argv: raw)
        assert ca.source_audio_bitrate("x") == 0
