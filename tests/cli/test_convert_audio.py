"""The white box for medialib/cli/convert_audio.py.

Its small helpers, and the decisions -e makes: where a track's output lands,
which sources are already what a run produces, and what the run tells the reader
about the encoder it picked. The planning arithmetic beside them - the chunk plan
and the boundary nudging - belongs to `medialib/lib/segments.py` and is pinned
with it, so it is not repeated here; nor is the encoder choice itself, which is
`medialib/lib/xheaac.py` and has its own file.
"""

import itertools
import os

import pytest

from medialib.cli import convert_audio as ca
from medialib.lib import clioptions, enums, xheaac

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
        answers = {"codec": "opus", "profile": "", "measured": 0}

        monkeypatch.setattr(ca, "source_audio_codec",
                            lambda src: answers["codec"])
        monkeypatch.setattr(ca, "source_audio_profile",
                            lambda src: answers["profile"])
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


class TestTheMeasuredBitrate:
    """What the packets ffprobe lists really cost: their bytes over the time
    they cover, for the containers that state no bitrate at all."""

    def test_it_is_the_bits_the_packets_take_over_the_seconds_they_cover(
            self):
        # Packets of uneven sizes: 10000 bytes in two seconds.
        listed = ("duration_time=0.500000|size=2000|\n"
                  "duration_time=0.500000|size=3000\n"
                  "duration_time=1.000000|size=5000\n")
        assert ca._packet_bitrate(listed) == 40000

    def test_packets_that_cover_no_time_measure_nothing(self):
        """Not a division by zero, and not "free": 0 is "unknown"."""
        assert ca._packet_bitrate("duration_time=N/A|size=200|\n" * 3) == 0
        assert ca._packet_bitrate("") == 0

    def test_a_size_ffprobe_could_not_read_counts_as_nothing(self):
        listed = ("duration_time=0.500000|size=N/A|\n"
                  "duration_time=0.500000|size=4000\n")
        assert ca._packet_bitrate(listed) == 32000


class TestTheOutputCodec:
    """-o, and the four places the choice of codec has to reach. Every one of
    them used to spell `.opus` out."""

    def test_opus_is_the_default_and_the_first_of_the_list(self):
        assert ca.DEFAULT_CODEC == "opus" == enums.AUDIO_CODECS[0]

    def test_every_codec_the_page_offers_is_accepted(self):
        """Read off the page a reader sees. Written out by hand the two drift,
        and the page ends up advertising a codec -e refuses."""
        declaration = ca.spec("convert-audio")
        page = clioptions.page(declaration)
        line = next(line for line in page.splitlines()
                    if "Output encoding:" in line)
        offered = line.split("Output encoding:")[1].strip().rstrip(".")
        codecs = offered.split(" or ")
        assert len(codecs) > 1
        for codec in codecs:
            result = clioptions.parse(declaration, ["-e", codec, "in", "out"])
            assert result.values["outputCodec"] == codec

    def test_every_codec_has_a_spoken_name_for_the_messages(self):
        assert set(ca.CODEC_NAMES) == set(enums.AUDIO_CODECS)

    def test_the_page_spells_the_token_and_not_the_codecs_name(self):
        """`xHE-AAC` is what the codec is called and `xheaac` is what -o takes;
        the page is where a reader learns what to TYPE, so a hyphen here is a
        line the reader copies into a refusal."""
        assert "xhe-aac" not in ca.OPT_SPEC.lower()
        assert "xheaac" in ca.OPT_SPEC

    def test_the_page_names_the_encoder_the_refusal_points_at(self):
        """One name in both places, so a reader who installs what the page
        asked for does not then meet a refusal naming something else."""
        named = ca.OPT_SPEC.split("external encoder (")[1].split(")")[0]
        assert named == xheaac.ENCODER_SPEC == xheaac.EXHALE

    @pytest.mark.parametrize("codec,expected", [
        ("opus", "book/track.opus"),
        ("xheaac", "book/track.m4a"),
    ])
    def test_the_output_path_follows_the_codec(self, codec, expected):
        run = ca.Run(output_dir="out", codec=codec,
                     extension=enums.AUDIO_CODEC_EXTENSIONS[codec])
        assert run.output_path("book/track.mp3").replace(os.sep, "/") == (
            "out/" + expected)

    def test_the_source_extension_is_replaced_and_not_appended(self):
        run = ca.Run(output_dir="out", codec="xheaac", extension="m4a")
        assert run.output_path("a.b.mp3").endswith("a.b.m4a")

    def test_a_track_with_no_extension_still_gets_one(self):
        run = ca.Run(output_dir="out", codec="opus", extension="opus")
        assert run.output_path("track").endswith("track.opus")


class TestWhichSourcesAreAlreadyFinished:
    """The lift-out asks "is this stream already what I would produce". For
    xhe-aac that cannot be answered by the codec name alone."""

    @pytest.fixture
    def probes(self, monkeypatch):
        answers = {"codec": "aac", "profile": "xHE-AAC", "measured": 0}
        monkeypatch.setattr(ca, "source_audio_codec",
                            lambda src: answers["codec"])
        monkeypatch.setattr(ca, "source_audio_profile",
                            lambda src: answers["profile"])
        monkeypatch.setattr(ca, "estimated_audio_bitrate",
                            lambda src: answers["measured"])
        return answers

    def test_a_small_xhe_aac_stream_is_finished(self, probes):
        assert ca.source_audio_is_finished("x", 46000, 90000, "xheaac") is True

    def test_an_aac_lc_stream_is_not_however_small_it_is(self, probes):
        """The one mistake this must not make. ffprobe calls xHE-AAC `aac` - the
        same name AAC-LC, HE-AAC, HE-AACv2, LD and ELD all answer - so a
        codec-only test would stream-copy a 46 kbps AAC-LC podcast straight into
        an .m4a and call it xHE-AAC."""
        probes["profile"] = "LC"
        assert ca.source_audio_is_finished("x", 46000, 90000, "xheaac") is False

    @pytest.mark.parametrize("profile", ["HE-AAC", "HE-AACv2", "LD", "ELD", ""])
    def test_no_other_aac_profile_passes_either(self, probes, profile):
        probes["profile"] = profile
        assert ca.source_audio_is_finished("x", 46000, 90000, "xheaac") is False

    def test_an_opus_stream_is_not_finished_for_an_xhe_aac_run(self, probes):
        """It is small and it is already lossy, but it is not what this run
        produces, so re-encoding it is the point rather than the waste."""
        probes["codec"] = "opus"
        assert ca.source_audio_is_finished("x", 46000, 90000, "xheaac") is False

    def test_an_xhe_aac_stream_is_not_finished_for_an_opus_run(self, probes):
        assert ca.source_audio_is_finished("x", 46000, 90000, "opus") is False

    def test_the_profile_is_only_asked_where_the_codec_name_is_ambiguous(
            self, monkeypatch):
        """Opus is `opus` and nothing else, so its row asks for no profile - and
        the probe is a second ffprobe per file that nothing should pay for
        without a reason."""
        asked = []
        monkeypatch.setattr(ca, "source_audio_codec", lambda src: "opus")
        monkeypatch.setattr(ca, "source_audio_profile",
                            lambda src: asked.append(src) or "")
        assert ca.source_audio_is_finished("x", 46000, 90000, "opus") is True
        assert asked == []

    def test_every_codec_the_option_offers_can_be_asked_about(self):
        """A codec in the list with no row here is an unhandled KeyError at the
        first video the run reaches."""
        assert set(ca.FINISHED_STREAM) == set(enums.AUDIO_CODECS)


class TestSplittingAndTheCodec:
    """Both codecs split. What differs is what the pieces have to obey to join
    without a seam, which is what `Planner.join_rules` answers."""

    def _planner(self, **settings):
        base = {"codec": "opus", "input_dir": "in", "mono": False,
                "surround": False, "bitrate": ca.DEFAULT_BITRATE}
        base.update(settings)
        return ca.Planner(ca.Run(**base), jobs=4)

    def test_opus_joins_wherever_it_was_cut(self, monkeypatch):
        """No frame to land on and no priming to allow for, so the planner asks
        the encoder nothing at all."""
        monkeypatch.setattr(ca.xheaac, "encoder_timing",
                            lambda *a: pytest.fail("Opus asked the xhe-aac "
                                                   "encoder about its timing"))
        assert self._planner().join_rules("book.flac") == (0, 0, 0)

    def test_xhe_aac_reads_the_rules_off_the_encoder(self, monkeypatch):
        monkeypatch.setattr(ca, "source_sample_rate", lambda src: 44100)
        monkeypatch.setattr(ca, "source_channels", lambda src: 2)
        monkeypatch.setattr(ca.xheaac, "encoder_timing",
                            lambda preset, rate, channels: (2048, 2048))
        assert self._planner(codec="xheaac").join_rules("book.flac") == (
            44100, 2048, 2048)

    def test_the_rules_are_asked_for_the_settings_the_chunks_will_use(
            self, monkeypatch):
        """Not the source's own rate and not the run's nominal bitrate: the
        WAVE is resampled into the encoder's band and -m downmixes it, and the
        preset follows the channel count. An answer about anything else would
        be an answer about a file that is not being encoded."""
        asked = []
        monkeypatch.setattr(ca, "source_sample_rate", lambda src: 96000)
        monkeypatch.setattr(ca, "source_channels", lambda src: 6)
        monkeypatch.setattr(ca.xheaac, "encoder_timing",
                            lambda *a: asked.append(a) or (2048, 2048))
        self._planner(codec="xheaac", mono=True).join_rules("book.flac")
        preset, rate, channels = asked[0]
        assert rate == 48000
        assert channels == 1
        assert preset == xheaac.exhale_preset(ca.MONO_BITRATE, 1)


class TestTheChunkingDecision:
    """Which tracks a run cuts is `chunkdecision`'s to weigh; what is pinned
    here is what the run hands it and does with the answer."""

    def _run(self, tmp_path, sizes, **settings):
        for track, size in sizes.items():
            (tmp_path / track).write_bytes(b"\0" * size)
        base = {"input_dir": str(tmp_path), "tracks": list(sizes),
                "adaptive": False, "mono": False, "bitrate": ca.DEFAULT_BITRATE,
                "threshold": ca.THRESHOLD, "codec": "opus"}
        base.update(settings)
        return ca.Run(**base)

    def test_adaptive_mode_cuts_nothing_and_weighs_nothing(self, tmp_path,
                                                           monkeypatch):
        monkeypatch.setattr(ca.chunkdecision, "decide",
                            lambda *a: pytest.fail("adaptive mode weighed"))
        state = self._run(tmp_path, {"book.m4b": 10}, adaptive=True)
        assert ca._decide_chunking(state, 32) == frozenset()

    def test_the_tracks_are_weighed_by_extension_and_size(self, tmp_path,
                                                          monkeypatch):
        asked = []

        def decide(tracks, workers, *rest):
            asked.append((tracks, workers))
            return ca.chunkdecision.Decision(frozenset(), 0.0, 0.0)

        monkeypatch.setattr(ca.chunkdecision, "decide", decide)
        state = self._run(tmp_path, {"Book.M4B": 30, "a.mp3": 20})
        ca._decide_chunking(state, 8)
        assert asked == [([("m4b", 30), ("mp3", 20)], 8)]

    def test_what_it_chose_is_named_by_track_and_said(self, tmp_path,
                                                      monkeypatch, capsys):
        monkeypatch.setattr(
            ca.chunkdecision, "decide",
            lambda *a: ca.chunkdecision.Decision(frozenset({1}), 60.0, 600.0))
        state = self._run(tmp_path, {"a.m4a": 20, "book.m4b": 30})
        assert ca._decide_chunking(state, 32) == frozenset({"book.m4b"})
        said = capsys.readouterr().out
        assert "Splitting 1 long file(s)" in said
        assert "0:01:00" in said and "0:10:00" in said

    def test_the_probe_asks_about_the_track_at_that_position(self, tmp_path,
                                                             monkeypatch):
        probed = []
        monkeypatch.setattr(ca, "chunk_candidate_audio", probed.append)

        def decide(*args):
            args[-1](1)
            return ca.chunkdecision.Decision(frozenset(), 0.0, 0.0)

        monkeypatch.setattr(ca.chunkdecision, "decide", decide)
        ca._decide_chunking(self._run(tmp_path, {"a.m4a": 20, "b.m4b": 30}),
                            32)
        assert probed == [str(tmp_path / "b.m4b")]

    def test_a_run_that_cuts_nothing_says_nothing(self, tmp_path, monkeypatch,
                                                  capsys):
        monkeypatch.setattr(
            ca.chunkdecision, "decide",
            lambda *a: ca.chunkdecision.Decision(frozenset(), 60.0, 60.0))
        ca._decide_chunking(self._run(tmp_path, {"a.m4a": 20}), 32)
        assert capsys.readouterr().out == ""


class TestTheChunkCandidateProbe:
    def _probed(self, monkeypatch, document):
        monkeypatch.setattr(ca, "_probe", lambda argv: document)
        return ca.chunk_candidate_audio("book.m4b")

    def test_it_reads_decoder_channels_rate_and_length(self, monkeypatch):
        document = ('{"format": {"duration": "301309.7", "bit_rate": "96836"},'
                    ' "streams": [{"codec_type": "video"},'
                    ' {"codec_type": "audio", "codec_name": "AAC",'
                    ' "channels": 2, "bit_rate": "95124"}]}')
        assert self._probed(monkeypatch, document) == ("aac", 2, 95124,
                                                        301309.7)

    def test_a_stream_that_states_no_channels_is_read_as_stereo(
            self, monkeypatch):
        document = ('{"format": {"duration": "60"},'
                    ' "streams": [{"codec_type": "audio",'
                    ' "codec_name": "mp3"}]}')
        assert self._probed(monkeypatch, document) == ("mp3", 2, 0, 60.0)

    @pytest.mark.parametrize("document", [
        "", "not json", "[]",
        '{"format": {"duration": "60"}, "streams": []}',
        '{"format": {}, "streams": [{"codec_type": "audio"}]}',
    ])
    def test_a_file_it_cannot_measure_is_none(self, monkeypatch, document):
        assert self._probed(monkeypatch, document) is None


class TestTheSampleRateProbe:
    """Only the xhe-aac path reads it, because only its encoders have a band."""

    @pytest.mark.parametrize("raw,expected", [
        ("48000", 48000), ("44100", 44100), ("8000", 8000),
    ])
    def test_a_stated_rate_is_read(self, monkeypatch, raw, expected):
        monkeypatch.setattr(ca, "_probe", lambda argv: raw)
        assert ca.source_sample_rate("x") == expected

    @pytest.mark.parametrize("raw", ["", "N/A", "0.5", "-1", "abc"])
    def test_anything_unreadable_is_zero_rather_than_a_guess(self, monkeypatch,
                                                             raw):
        """0 is "unknown", which the band clamp reads as the top - a guess made
        in one place instead of two."""
        monkeypatch.setattr(ca, "_probe", lambda argv: raw)
        assert ca.source_sample_rate("x") == 0

    def test_only_the_first_line_is_taken(self, monkeypatch):
        monkeypatch.setattr(ca, "_probe", lambda argv: "48000\nx")
        assert ca.source_sample_rate("x") == 48000


class TestTheEncoderReport:
    """What an xHE-AAC run says it will encode at. Reached only from the
    xHE-AAC branch: an Opus run has nothing to add, ffmpeg encodes it and
    ffmpegselect already says which ffmpeg."""

    def _lines(self, capsys, **kwargs):
        settings = {"codec": "xheaac", "bitrate": 46, "mono": False,
                    "adaptive": False}
        settings.update(kwargs)
        ca._report_encoder(settings["codec"], settings["bitrate"],
                           settings["mono"], settings["adaptive"])
        return capsys.readouterr().out.splitlines()

    def test_the_chosen_encoder_is_named(self, capsys):
        lines = self._lines(capsys)
        assert "exhale" in lines[0] and "xHE-AAC" in lines[0]

    def test_the_rounding_onto_the_preset_ladder_is_shown(self, capsys):
        """exhale takes a preset about 12 kbps apart, not a bitrate, so -b 46
        really encodes at 48. Silent, that is a library nobody can account
        for."""
        lines = self._lines(capsys)
        assert "48 kbps stereo" in lines[1]
        assert "nearest preset to 46" in lines[1]

    def test_a_rate_that_lands_exactly_says_nothing_about_rounding(self, capsys):
        lines = self._lines(capsys, bitrate=96)
        assert "96 kbps stereo" in lines[1]
        assert "nearest preset" not in lines[1]

    def test_forced_mono_is_reported_as_mono_and_at_the_mono_rate(self, capsys):
        lines = self._lines(capsys, mono=True)
        assert "mono" in lines[1]
        assert "nearest preset to 32" in lines[1]

    def test_adaptive_mode_names_the_encoder_and_no_one_rate(self, capsys):
        """It decides per file, so there is no single figure to print - and
        printing the global one would be printing a number nothing uses."""
        assert len(self._lines(capsys, adaptive=True)) == 1

    def test_the_encoder_named_is_the_one_this_host_encodes_with(self, capsys):
        """Not a label written out beside the call: the same constant the
        preflight looks for and the page tells a reader to install."""
        assert xheaac.EXHALE in self._lines(capsys)[0]


class TestTheXheAacEncodeCall:
    """The two-process pipe, with both processes stubbed. What is under test is
    the wiring and the housekeeping around it, not the encoders."""

    @pytest.fixture
    def spawned(self, monkeypatch):
        """Every Popen the encode starts, in order, with what it was given."""
        calls = []

        class _Process:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.kwargs = kwargs
                self.stdout = _Pipe() if kwargs.get("stdout") is not None \
                    else None
                self.waited = 0

            def wait(self):
                self.waited += 1
                return 0

            def kill(self):
                pass

        def popen(argv, **kwargs):
            process = _Process(list(argv), **kwargs)
            calls.append(process)
            return process

        monkeypatch.setattr(ca.subprocess, "Popen", popen)
        monkeypatch.setattr(ca, "source_sample_rate", lambda src: 48000)
        monkeypatch.setattr(ca, "source_channels", lambda src: 2)
        return calls

    def _run(self, **settings):
        base = {"codec": "xheaac", "extension": "m4a", "output_dir": "out",
                "surround": False}
        base.update(settings)
        return ca.Run(**base)

    def test_ffmpeg_decodes_and_the_encoder_reads_its_pipe(self, spawned,
                                                           tmp_path):
        out = tmp_path / "a.m4a"
        self._run()._encode_xheaac("a.flac", str(out), mono=False, bitrate=46)
        decode, encode = spawned
        assert decode.argv[0] == "ffmpeg"
        assert encode.argv[0] == "exhale"
        # The encoder's stdin IS the decoder's stdout, which is what makes this
        # a pipe rather than a temporary file.
        assert encode.kwargs["stdin"] is decode.stdout

    def test_the_preset_comes_from_the_bitrate_and_the_channels(self, spawned,
                                                                tmp_path):
        out = tmp_path / "a.m4a"
        self._run()._encode_xheaac("a.flac", str(out), mono=False, bitrate=46)
        _decode, encode = spawned
        assert encode.argv[1] == xheaac.exhale_preset(46, 2)

    def test_forced_mono_asks_for_the_mono_preset(self, spawned, tmp_path):
        """The preset is a rate for the OUTPUT, and -m makes that one channel
        whatever the source had - so a stereo source under -m must not be
        priced as stereo."""
        out = tmp_path / "a.m4a"
        self._run()._encode_xheaac("a.flac", str(out), mono=True, bitrate=32)
        _decode, encode = spawned
        assert encode.argv[1] == xheaac.exhale_preset(32, 1)

    def test_a_channel_count_already_probed_is_not_probed_again(self, spawned,
                                                                monkeypatch,
                                                                tmp_path):
        """Adaptive mode has already asked. A second ffprobe per file for an
        answer the run is holding is pure waste."""
        asked = []
        monkeypatch.setattr(ca, "source_channels",
                            lambda src: asked.append(src) or 2)
        out = tmp_path / "a.m4a"
        self._run()._encode_xheaac("a.flac", str(out), mono=False, bitrate=46,
                                   channels=6)
        assert asked == []
        _decode, encode = spawned
        assert encode.argv[1] == xheaac.exhale_preset(46, 2)

    def test_a_surround_source_is_decoded_to_stereo_by_default(self, spawned,
                                                                tmp_path):
        self._run()._encode_xheaac("a.flac", str(tmp_path / "a.m4a"),
                                   mono=False, bitrate=46, channels=6)
        decode, encode = spawned
        assert decode.argv[decode.argv.index("-ac") + 1] == "2"
        assert encode.argv[1] == xheaac.exhale_preset(46, 2)

    def test_and_kept_whole_with_surround(self, spawned, tmp_path):
        self._run(surround=True)._encode_xheaac(
            "a.flac", str(tmp_path / "a.m4a"), mono=False, bitrate=46,
            channels=6)
        decode, encode = spawned
        assert "-ac" not in decode.argv
        assert encode.argv[1] == xheaac.exhale_preset(46, 6)

    def test_a_stale_output_is_removed_before_the_encoder_sees_it(
            self, spawned, tmp_path):
        """exhale opens its output O_CREAT|O_EXCL and refuses a name that
        exists - there is no -y for it. An output from an interrupted run, or
        one too short to have passed the up-to-date check, would otherwise fail
        that file on every run for as long as the stale file survived."""
        out = tmp_path / "a.m4a"
        out.write_bytes(b"stale")
        self._run()._encode_xheaac("a.flac", str(out), mono=False, bitrate=46)
        assert not out.exists(), "exhale would have refused this name"
        assert len(spawned) == 2, "the encode did not run"

    def test_an_absent_output_is_not_an_error(self, spawned, tmp_path):
        out = tmp_path / "nested" / "a.m4a"
        self._run()._encode_xheaac("a.flac", str(out), mono=False, bitrate=46)
        assert len(spawned) == 2

    def test_both_halves_are_waited_for(self, spawned, tmp_path):
        """A decoder nobody reaps is a zombie per file, and a run that returned
        before its encoder finished would move on to the cover embed while the
        file was still being written."""
        out = tmp_path / "a.m4a"
        self._run()._encode_xheaac("a.flac", str(out), mono=False, bitrate=46)
        assert all(process.waited == 1 for process in spawned)

    def test_the_decoders_pipe_is_closed_in_this_process(self, spawned,
                                                         tmp_path):
        """Held open here the pipe has two readers, so a dead encoder never
        gives the decoder an EPIPE and the run hangs on it."""
        out = tmp_path / "a.m4a"
        self._run()._encode_xheaac("a.flac", str(out), mono=False, bitrate=46)
        decode, _encode = spawned
        assert decode.stdout.closed

    def test_an_encoder_that_cannot_start_leaves_no_process_behind(
            self, monkeypatch, tmp_path):
        """The preflight makes this unlikely, not impossible - a binary can go
        away between the check and the run - and a decoder left alive would
        block a worker for the length of the file."""
        started = []

        class _Decoder:
            def __init__(self):
                self.stdout = _Pipe()
                self.killed = False
                self.waited = 0

            def wait(self):
                self.waited += 1

            def kill(self):
                self.killed = True

        def popen(argv, **kwargs):
            if argv[0] == "ffmpeg":
                started.append(_Decoder())
                return started[-1]
            raise OSError("no exhale")

        monkeypatch.setattr(ca.subprocess, "Popen", popen)
        monkeypatch.setattr(ca, "source_sample_rate", lambda src: 48000)
        monkeypatch.setattr(ca, "source_channels", lambda src: 2)
        self._run()._encode_xheaac("a.flac", str(tmp_path / "a.m4a"),
                                   mono=False, bitrate=46)
        assert started[0].killed and started[0].waited == 1
        assert started[0].stdout.closed

    def test_a_decoder_that_cannot_start_is_not_a_dead_worker(self,
                                                              monkeypatch,
                                                              tmp_path):
        """An unguarded OSError here would take the whole worker PROCESS down,
        and with it every file queued behind this one."""
        def popen(argv, **kwargs):
            raise OSError("no ffmpeg")

        monkeypatch.setattr(ca.subprocess, "Popen", popen)
        monkeypatch.setattr(ca, "source_sample_rate", lambda src: 48000)
        monkeypatch.setattr(ca, "source_channels", lambda src: 2)
        self._run()._encode_xheaac("a.flac", str(tmp_path / "a.m4a"),
                                   mono=False, bitrate=46)


class _Pipe:
    """Stands in for a Popen's stdout, so a case can see that it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestTheOpusEncodeIsUnchanged:
    """The default path is what every existing library was made with, so the
    argv it produces is pinned rather than assumed."""

    def test_the_stereo_call_is_the_one_it_always_was(self, monkeypatch):
        seen = []
        monkeypatch.setattr(ca.subprocess, "run",
                            lambda argv, **kw: seen.append(list(argv)))
        monkeypatch.setattr(ca, "source_channels", lambda src: 2)
        run = ca.Run(codec="opus", extension="opus", output_dir="out",
                     adaptive=False, surround=False)
        run._encode("a.flac", "out/a.opus", mono=False, bitrate=46)
        assert seen == [["ffmpeg", "-nostdin", "-y", "-i", "a.flac",
                         "-map", "0:a:0", "-map_metadata", "0:s:0",
                         "-c:a", "libopus", "-b:a", "46k", "out/a.opus"]]

    def test_and_the_mono_call_puts_the_downmix_before_the_codec(self,
                                                                 monkeypatch):
        seen = []
        monkeypatch.setattr(ca.subprocess, "run",
                            lambda argv, **kw: seen.append(list(argv)))
        run = ca.Run(codec="opus", extension="opus", output_dir="out")
        run._encode("a.flac", "out/a.opus", mono=True, bitrate=32)
        assert seen == [["ffmpeg", "-nostdin", "-y", "-i", "a.flac",
                         "-map", "0:a:0", "-map_metadata", "0:s:0",
                         "-ac", "1", "-c:a", "libopus", "-b:a", "32k",
                         "out/a.opus"]]

    def test_the_lift_out_copies_and_never_names_an_encoder(self, monkeypatch):
        seen = []
        monkeypatch.setattr(ca.subprocess, "run",
                            lambda argv, **kw: seen.append(list(argv)))
        run = ca.Run(codec="opus", extension="opus", output_dir="out")
        run._remux("a.mkv", "out/a.opus")
        assert "-c:a" in seen[0] and seen[0][seen[0].index("-c:a") + 1] == "copy"
        assert "libopus" not in seen[0]



class TestAnOpusEncodeOfASurroundSource:
    """A surround source - the 5.1(side) an Atmos m4b decodes to - is folded
    down to stereo by default, for listening on a phone, and kept only with -u.
    Kept, it has to be re-labelled: libopus opens past stereo only on the
    Vorbis layout for the count, and wrote nothing at all for a 5.1(side). Both
    encode paths are pinned, because the chunks of a split book have to come
    out exactly as a whole file would for the stream-copy join to take them."""

    @pytest.fixture
    def seen(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ca.subprocess, "run",
                            lambda argv, **kw: calls.append(list(argv)))
        monkeypatch.setattr(ca, "source_channels", lambda src: 6)
        monkeypatch.setattr(ca, "source_channel_layout",
                            lambda src: "5.1(side)")
        return calls

    def _run(self, **settings):
        base = {"codec": "opus", "extension": "opus", "output_dir": "out",
                "adaptive": False, "mono": False, "surround": False,
                "bitrate": 46}
        base.update(settings)
        return ca.Run(**base)

    def _chunk_and_whole(self, run, monkeypatch, tmp_path):
        class _Counters:
            def report_progress(self, *args):
                pass

        # The chunk's own measurement is not what is under test; declining it
        # stops the job right after the encode.
        monkeypatch.setattr(ca.durationcheck, "verify", lambda *a: False)
        run.__dict__.update(input_dir="in", chunk_root=str(tmp_path),
                            counters=_Counters())
        run.encode_chunk(ca.UNIT.join(["a.m4b", "0", "4", "0", "600"]))
        run._encode("in/a.m4b", "out/a.opus", mono=False, bitrate=46)

    def test_by_default_a_six_channel_file_is_folded_down_to_stereo(
            self, seen):
        self._run()._encode("a.m4b", "out/a.opus", mono=False, bitrate=46)
        assert seen == [["ffmpeg", "-nostdin", "-y", "-i", "a.m4b",
                         "-map", "0:a:0", "-map_metadata", "0:s:0",
                         "-ac", "2", "-c:a", "libopus", "-b:a", "46k",
                         "out/a.opus"]]

    def test_a_stereo_source_is_not_given_a_downmix_it_does_not_need(
            self, seen, monkeypatch):
        monkeypatch.setattr(ca, "source_channels", lambda src: 2)
        self._run()._encode("a.m4b", "out/a.opus", mono=False, bitrate=46)
        assert seen[0][9:-1] == ["-c:a", "libopus", "-b:a", "46k"]

    def test_with_surround_it_keeps_its_count_at_a_surround_rate(self, seen):
        self._run(surround=True)._encode("a.m4b", "out/a.opus", mono=False,
                                         bitrate=46)
        assert seen == [["ffmpeg", "-nostdin", "-y", "-i", "a.m4b",
                         "-map", "0:a:0", "-map_metadata", "0:s:0",
                         "-ac", "6", "-c:a", "libopus", "-b:a", "106k",
                         "out/a.opus"]]

    @pytest.mark.parametrize("surround, codec", [
        (False, ["-ac", "2", "-c:a", "libopus", "-b:a", "46k"]),
        (True, ["-ac", "6", "-c:a", "libopus", "-b:a", "106k"])])
    def test_a_chunk_is_encoded_with_the_settings_of_a_whole_file(
            self, seen, monkeypatch, tmp_path, surround, codec):
        self._chunk_and_whole(self._run(surround=surround), monkeypatch,
                              tmp_path)
        chunk, whole = seen
        # The chunk encoder joins the source path itself, so it is spelled with
        # this platform's separator.
        assert chunk[:13] == ["ffmpeg", "-nostdin", "-y", "-ss", "0",
                              "-t", "600", "-i", os.path.join("in", "a.m4b"),
                              "-map", "0:a:0", "-map_metadata", "-1"]
        assert chunk[13:-1] == whole[9:-1] == codec

    def test_a_given_bitrate_is_kept_as_given(self, seen):
        self._run(surround=True)._encode("a.m4b", "out/a.opus", mono=False,
                                         bitrate=128)
        assert seen[0][9:-1] == ["-ac", "6", "-c:a", "libopus", "-b:a",
                                 "128k"]

    def test_adaptive_mode_keeps_the_rate_it_looked_up(self, seen):
        self._run(adaptive=True, surround=True)._encode(
            "a.m4b", "out/a.opus", mono=False, bitrate=150, channels=6)
        assert seen[0][9:-1] == ["-ac", "6", "-c:a", "libopus", "-b:a",
                                 "150k"]

    def test_forced_mono_never_asks_about_the_source(self, seen, monkeypatch):
        def probed(src):
            raise AssertionError("mono needs no channel count")

        monkeypatch.setattr(ca, "source_channels", probed)
        self._run(surround=True)._encode("a.m4b", "out/a.opus", mono=True,
                                         bitrate=32)
        assert seen[0][9:-1] == ["-ac", "1", "-c:a", "libopus", "-b:a", "32k"]


class TestOutputChannels:
    @pytest.mark.parametrize("channels, expected", [
        (0, 1), (1, 1), (2, 2), (6, 2), (8, 2)])
    def test_stereo_is_the_ceiling_by_default(self, channels, expected):
        assert ca.output_channels(channels, surround=False) == expected

    @pytest.mark.parametrize("channels, expected", [
        (1, 1), (2, 2), (6, 6), (8, 8), (12, 8)])
    def test_surround_keeps_the_count_up_to_seven_one(self, channels,
                                                      expected):
        assert ca.output_channels(channels, surround=True) == expected


class TestOpusChannelArgs:
    """The layout decision on its own, one row per case libopus treats
    differently."""

    @pytest.mark.parametrize("channels", [1, 2])
    def test_stereo_and_below_need_nothing(self, channels):
        assert ca.opus_channel_args(channels) == (channels, [])

    @pytest.mark.parametrize("channels", [5, 6, 7, 8])
    def test_five_to_eight_ask_for_their_own_count(self, channels):
        assert ca.opus_channel_args(channels) == (channels,
                                                  ["-ac", str(channels)])

    @pytest.mark.parametrize("channels, layout, named", [
        (3, "3.0", "3.0"), (4, "4.0", "quad"), (4, "quad", "quad"),
        (4, "quad(side)", "quad")])
    def test_three_and_four_without_an_lfe_are_named_outright(
            self, channels, layout, named):
        assert ca.opus_channel_args(channels, layout) == (
            channels, ["-channel_layout", named])

    @pytest.mark.parametrize("channels, layout", [
        (3, "2.1"), (4, "3.1"), (3, ""), (4, "")])
    def test_an_lfe_or_an_unknown_layout_is_downmixed_to_stereo(
            self, channels, layout):
        assert ca.opus_channel_args(channels, layout) == (2, ["-ac", "2"])

    def test_past_eight_the_source_is_folded_down_to_seven_one(self):
        assert ca.opus_channel_args(12) == (8, ["-ac", "8"])


class TestSurroundBitrate:
    @pytest.mark.parametrize("channels, expected", [
        (1, 46), (2, 46), (6, 106), (8, 142)])
    def test_the_default_grows_with_the_channel_count(self, channels,
                                                      expected):
        assert ca.surround_bitrate(ca.DEFAULT_BITRATE, channels) == expected

    def test_a_given_bitrate_is_never_second_guessed(self):
        assert ca.surround_bitrate(64, 6) == 64

    def test_an_empty_table_leaves_the_default_alone(self, monkeypatch):
        monkeypatch.setattr(ca.bitrates, "audio_bitrate",
                            lambda channels, column: None)
        assert ca.surround_bitrate(ca.DEFAULT_BITRATE, 6) == 46

def _stub_popen(monkeypatch):
    """Both halves of the pipe, stubbed, recording the argv of each spawn."""
    calls = []

    class _Process:
        def __init__(self, argv, **kwargs):
            self.argv = list(argv)
            self.stdout = _Pipe() if kwargs.get("stdout") is not None else None

        def wait(self):
            return 0

        def kill(self):
            pass

    def popen(argv, **kwargs):
        process = _Process(argv, **kwargs)
        calls.append(process.argv)
        return process

    monkeypatch.setattr(ca.subprocess, "Popen", popen)
    return calls


class TestABookTooLongForOnePipe:
    """The refusal that stands between a 15-minute encode and a book with five
    sixths of it missing.

    The encode goes over a WAVE, which states its length in 32 bits, and exhale
    reads exactly the count it is given. Nothing about that fails: the encoder
    closes a finished MP4 and exits 0. So the length is asked BEFORE the encoder
    is started, and the arithmetic is `xheaac.wave_seconds_ceiling`.
    """

    @pytest.fixture
    def spawned(self, monkeypatch):
        """Every Popen the encode would start - which, for a file over the
        ceiling, has to be none at all."""
        calls = _stub_popen(monkeypatch)
        monkeypatch.setattr(ca, "source_sample_rate", lambda src: 44100)
        monkeypatch.setattr(ca, "source_channels", lambda src: 1)
        return calls

    def _run(self, **settings):
        # A run that is chunking past the ceiling, as a default one is: the
        # refusal then only ever meets a file the planner could not cut.
        return ca.Run(**dict(dict(codec="xheaac", extension="m4a",
                                  output_dir="out", adaptive=False,
                                  surround=False, chunk_over_ceiling=True),
                             **settings))

    # 83:41:50 of mono at 44.1 kHz: the file this was written for.
    TOO_LONG = 301309.7

    def test_no_encoder_is_started_for_a_file_over_the_ceiling(self, spawned,
                                                               tmp_path):
        assert self._run()._encode_xheaac(
            "book.m4b", str(tmp_path / "book.m4a"), mono=True, bitrate=18,
            duration=self.TOO_LONG) is False
        assert spawned == []

    def test_and_the_run_is_told_what_to_do_instead(self, spawned, tmp_path,
                                                    capsys):
        self._run()._encode_xheaac("book.m4b", str(tmp_path / "book.m4a"),
                                   mono=True, bitrate=18,
                                   duration=self.TOO_LONG)
        said = capsys.readouterr().err
        assert "too long for exhale to encode whole" in said
        assert "83:41:50" in said and "book.m4b" in said
        assert "-e opus" in said

    def _way_out(self, spawned, tmp_path, capsys, **settings):
        self._run(**settings)._encode_xheaac(
            "book.m4b", str(tmp_path / "book.m4a"), mono=True, bitrate=18,
            duration=self.TOO_LONG)
        return capsys.readouterr().err

    def test_a_run_that_was_cutting_is_not_told_to_cut(self, spawned,
                                                       tmp_path, capsys):
        said = self._way_out(spawned, tmp_path, capsys)
        assert "-a" not in said.replace("-aac", "")

    def test_under_a_it_is_adaptive_mode_that_stopped_the_cut(
            self, spawned, tmp_path, capsys):
        said = self._way_out(spawned, tmp_path, capsys, adaptive=True,
                             chunk_over_ceiling=False)
        assert "drop -a" in said

    def test_a_book_inside_the_ceiling_is_encoded_as_it_always_was(
            self, spawned, tmp_path):
        assert self._run()._encode_xheaac(
            "book.m4b", str(tmp_path / "book.m4a"), mono=True, bitrate=18,
            duration=6 * 3600) is True
        assert [argv[0] for argv in spawned] == ["ffmpeg", "exhale"]

    def test_a_duration_nothing_could_probe_is_not_a_refusal(self, spawned,
                                                             tmp_path):
        """An unreadable header must not become "this cannot be encoded": what
        came out is measured against the source either way."""
        assert self._run()._encode_xheaac(
            "book.m4b", str(tmp_path / "book.m4a"), mono=True, bitrate=18,
            duration=0) is True
        assert [argv[0] for argv in spawned] == ["ffmpeg", "exhale"]

    def test_the_ceiling_is_read_at_the_rate_the_WAVE_is_written_at(
            self, monkeypatch, tmp_path):
        """Not the source's own rate. A 96 kHz source is resampled down to 48
        before it reaches the pipe, so it fits twice as much as its own rate
        would suggest - and refusing it on the source's rate would decline a
        book the encoder could have taken.
        """
        calls = _stub_popen(monkeypatch)
        monkeypatch.setattr(ca, "source_sample_rate", lambda src: 96000)
        monkeypatch.setattr(ca, "source_channels", lambda src: 1)
        # Over the ceiling for 96 kHz, inside it for the 48 kHz it is resampled
        # to.
        seconds = xheaac.wave_seconds_ceiling(48000, 1) - 60
        assert self._run()._encode_xheaac(
            "book.m4b", str(tmp_path / "book.m4a"), mono=True, bitrate=18,
            duration=seconds) is True
        assert [argv[0] for argv in calls] == ["ffmpeg", "exhale"]

    def test_the_downmix_doubles_what_fits(self, spawned, tmp_path,
                                           monkeypatch):
        """-m is a real downmix on the ffmpeg side, so a mono encode carries
        half the bytes a stereo one would and reaches twice as far."""
        monkeypatch.setattr(ca, "source_channels", lambda src: 2)
        seconds = xheaac.wave_seconds_ceiling(44100, 2) + 60
        run = self._run()
        assert run._encode_xheaac("book.m4b", str(tmp_path / "a.m4a"),
                                  mono=False, bitrate=36,
                                  duration=seconds) is False
        assert run._encode_xheaac("book.m4b", str(tmp_path / "b.m4a"),
                                  mono=True, bitrate=18,
                                  duration=seconds) is True


class TestTheQueueOrder:
    """What the encode queue is handed, and what each job is worth.

    The queue keeps its buffer from the biggest of what it holds to the
    smallest, and it is the producer that decides what a job is worth: a chunk
    is worth its own share of the file's bytes, not the book's. So a book cut
    into eight hands the queue eight jobs smaller than the file beside it, and
    that file leads instead of waiting behind all of them. Nothing here depends
    on the chunks staying adjacent, because the re-concatenation is a pass of
    its own after the whole queue has drained.
    """

    def _producer(self, tmp_path, sizes, chunked, seed=0):
        """The encode producer with the planning stubbed out: every candidate's
        jobs are written as the planner would write them, so what is under test
        is the size settle, the weighing and the handover alone."""
        import types

        inputs = tmp_path / "in"
        plans = tmp_path / "plans"
        inputs.mkdir()
        plans.mkdir()
        for track, size in sizes.items():
            (inputs / track).write_bytes(b"\0" * size)

        class _Planner:
            def __init__(self, state, jobs):
                pass

            def plan_all(self, tracks):
                for track in tracks:
                    total = chunked[track]
                    if total < 2:
                        # What the planner writes for a candidate it settled
                        # whole after all: one job, the file itself.
                        ca._write_jobs(
                            ca.segments.plan_file_for(str(plans), track),
                            [track])
                        continue
                    length = sizes[track] / float(total)
                    ca._write_jobs(
                        ca.segments.plan_file_for(str(plans), track),
                        [ca.UNIT.join([track, str(index), str(total), "0",
                                       "%.9f" % length])
                         for index in range(total)])

        state = types.SimpleNamespace(
            tracks=sorted(sizes, key=lambda track: -sizes[track]),
            input_dir=str(inputs), plan_root=str(plans),
            chunked=frozenset(chunked), codec="opus")
        preload, candidates = ca._settle_preload(state, _Planner(state, 1))
        total_file = str(tmp_path / "total")
        return (ca._encode_producer(state, _Planner(state, 1), preload,
                                    candidates, total_file, seed),
                total_file)

    def test_a_chunk_queues_at_its_own_size_and_not_the_books(self, tmp_path):
        """The book is five times the size of the whole file beside it, but cut
        into eight it is eight jobs SMALLER than that file - so the file leads
        instead of waiting behind all of them."""
        producer, _total_file = self._producer(
            tmp_path, {"book.m4b": 800, "track.m4a": 150}, {"book.m4b": 8})
        items = list(producer)
        names = [token.split(ca.UNIT)[0] for token, _size in items]
        assert names[0] == "track.m4a"
        assert names.count("book.m4b") == 8
        assert [size for _token, size in items[1:]] == [100.0] * 8

    def test_the_whole_queue_is_largest_first_when_nothing_is_chunked(
            self, tmp_path):
        """The size settle sends the long files off to be planned and they come
        back needing no chunking after all. They are still the biggest jobs in
        the run, so they lead it - the queue is ordered on what the planning
        settled, not on which side of the settle a file came from."""
        sizes = {"long%d.m4b" % n: 900 - n for n in range(4)}
        sizes.update({"short%d.m4a" % n: 300 - n for n in range(6)})
        producer, _total_file = self._producer(
            tmp_path, sizes, {track: 1 for track in sizes})
        order = [token for token, _size in producer]
        assert order == sorted(sizes, key=lambda track: -sizes[track])

    def test_the_seed_is_the_largest_of_what_needs_no_planning(self, tmp_path):
        """The seed carries the pool through the planning, so it is handed over
        before the plan is made and it is the LARGEST of the tracks the settle
        asked nothing of - the most encoding the pool can be given up front."""
        sizes = {"book.m4b": 900, "a.m4a": 300, "b.m4a": 200, "c.m4a": 100}
        producer, _total_file = self._producer(
            tmp_path, sizes, {"book.m4b": 1}, seed=2)
        order = [token for token, _size in producer]
        assert order == ["a.m4a", "b.m4a", "book.m4b", "c.m4a"]

    def test_the_chunks_of_one_file_are_still_all_there_and_in_order(
            self, tmp_path):
        """Interleaving is free, losing a piece is not: every index the planner
        wrote is handed over, and they keep their order among themselves so the
        run is reproducible."""
        producer, _total_file = self._producer(
            tmp_path, {"book.m4b": 400, "a.m4a": 100, "b.m4a": 100},
            {"book.m4b": 4})
        items = list(producer)
        indexes = [token.split(ca.UNIT)[1] for token, _size in items
                   if ca.UNIT in token]
        assert indexes == ["0", "1", "2", "3"]

    def test_the_preload_is_handed_over_largest_first(self, tmp_path):
        """The queue orders what it holds by the size it is told, so the
        producer owes it the scan's order - largest file first - and no more."""
        producer, _total_file = self._producer(
            tmp_path, {"small.m4a": 10, "big.m4a": 900, "mid.m4a": 100}, {})
        items = list(producer)
        assert [token for token, _size in items] == \
            ["big.m4a", "mid.m4a", "small.m4a"]

    def test_a_book_cut_into_pieces_no_smaller_leads_the_buffer(self,
                                                                tmp_path):
        """The order follows the sizes and nothing else: two chunks of a file
        ten times the size of its neighbours are still the two biggest jobs."""
        producer, _total_file = self._producer(
            tmp_path, {"book.m4b": 2000, "a.m4a": 100, "b.m4a": 100},
            {"book.m4b": 2})
        items = list(producer)
        names = [token.split(ca.UNIT)[0]
                 for token, _size in sorted(items, key=lambda item: -item[1])]
        assert names[:2] == ["book.m4b", "book.m4b"]

    def test_the_total_is_written_when_the_planning_is_done(self, tmp_path):
        """The queue's lines count without a denominator until the producer
        knows the queue's own length, which is when the plan is made - and it
        writes it then, before the first job the plan settled."""
        producer, total_file = self._producer(
            tmp_path, {"book.m4b": 800, "track.m4a": 150}, {"book.m4b": 8})
        items = list(producer)
        with open(total_file) as handle:
            assert handle.read() == "%d\n" % len(items)

    def test_a_queue_with_nothing_to_plan_is_counted_from_the_start(
            self, tmp_path):
        """With no candidates the length is known before the first job is
        handed over, so the first line the run prints may already carry it."""
        producer, total_file = self._producer(
            tmp_path, {"a.m4a": 100, "b.m4a": 200}, {})
        next(producer)
        with open(total_file) as handle:
            assert handle.read() == "2\n"


class TestTheMissingMkvtoolnixWarning:
    """Said only to a tree it costs something: convert-audio asks mkvtoolnix for
    the cover of a Matroska AUDIO source and for nothing else."""

    def test_a_tree_with_an_mka_is_warned(self, capsys):
        ca._warn_mkvtoolnix(["a.mp3", "sub/b.mka"])
        said = capsys.readouterr().err
        assert "mkvtoolnix not found" in said and "sidecar images" in said

    @pytest.mark.parametrize("tracks", [["a.mp3", "b.m4a"], ["film.mkv"]],
                             ids=["no Matroska", "a Matroska video"])
    def test_a_tree_without_one_is_not(self, capsys, tracks):
        """A .mkv is a video, whose picture is never taken as a cover."""
        ca._warn_mkvtoolnix(tracks)
        assert capsys.readouterr().err == ""


class TestTheChunksOfALongFile:
    """The pieces a split file is encoded as, which have to join back into
    exactly the file: no gap, no overlap, and nothing past either end."""

    @staticmethod
    def _ranges(tokens):
        fields = [token.split(ca.UNIT) for token in tokens]
        return [(float(start), float(start) + float(length))
                for _track, _index, _total, start, length in fields]

    def test_opus_pieces_tile_the_file_exactly(self):
        tokens = ca.chunk_tokens("a.m4b", [0.0, 87.5, 162.5, 237.5, 300.0],
                                 0, 0, 0)
        ranges = self._ranges(tokens)
        assert ranges[0][0] == 0.0
        assert ranges[-1][1] == pytest.approx(300.0)
        for (_start, end), (next_start, _end) in itertools.pairwise(ranges):
            assert end == pytest.approx(next_start)

    def test_each_piece_says_which_of_how_many_it_is(self):
        tokens = ca.chunk_tokens("a.m4b", [0.0, 100.0, 200.0, 300.0], 0, 0, 0)
        assert [token.split(ca.UNIT)[:3] for token in tokens] == [
            ["a.m4b", "0", "3"], ["a.m4b", "1", "3"], ["a.m4b", "2", "3"]]

    def test_xhe_aac_pieces_leave_the_priming_at_every_seam(self):
        """Every piece starts one priming late, the first included, and that
        gap is exactly what the decoder's priming frame fills; the inner cuts
        sit on whole frames, and the last piece still runs to the end."""
        rate, priming, frame = 44100, 1024, 2048
        offset = priming / rate
        ranges = self._ranges(ca.chunk_tokens(
            "a.m4b", [0.0, 87.51, 162.49, 237.5, 300.0], rate, priming, frame))
        assert ranges[0][0] == pytest.approx(offset)
        assert ranges[-1][1] == pytest.approx(300.0)
        for (_start, end), (next_start, _end) in itertools.pairwise(ranges):
            assert next_start - end == pytest.approx(offset)
            cut = end * rate
            assert cut == pytest.approx(round(cut / frame) * frame)

    def test_cuts_closer_than_the_priming_are_not_cut_at_all(self):
        """A piece of no length cannot be encoded, so the file stays whole
        rather than being joined with a hole in it."""
        assert ca.chunk_tokens("a.m4b", [0.0, 0.01, 300.0], 44100, 1024,
                               2048) == []


class TestSeekCopies:
    """The copies the window probes seek into: made a few at a time before any
    probe is queued, and only for the formats that cannot be seeked as they
    are."""

    def _planner(self, tmp_path, durations, monkeypatch, fails=()):
        """A planner over candidates of <durations>, with the copies and the pool
        they run in replaced by a record of what was asked."""
        plans = tmp_path / "plans"
        plans.mkdir()
        for track, duration in durations.items():
            base = ca.segments.plan_file_for(str(plans), track)
            with open(base + ".meta", "w") as handle:
                handle.write(str(duration))
        planner = ca.Planner(ca.Run(input_dir="in", plan_root=str(plans)),
                             jobs=8)
        pools = []

        def pool(state, method, items, jobs):
            pools.append((method, list(items), jobs))
            for item in items:
                if item not in fails:
                    open(ca.segments.plan_file_for(str(plans), item)
                         + ".seek.mka", "w").close()

        monkeypatch.setattr(ca, "_run_pool", pool)
        return planner, pools, plans

    @staticmethod
    def _sources(queue):
        return {token.split(ca.UNIT)[0] for token in queue}

    def test_the_copies_run_in_one_pool_of_a_few(self, tmp_path, monkeypatch):
        """All of them in one pool, and never wider than SEEK_COPY_JOBS however
        many cores the window probes after them get."""
        tracks = {"a.m4b": 40000, "b.m4b": 30000, "c.mp3": 20000}
        planner, pools, _plans = self._planner(tmp_path, tracks, monkeypatch)
        candidates, _queue = planner.window_jobs(list(tracks))
        assert candidates == list(tracks)
        assert pools == [("seek_copy", ["a.m4b", "b.m4b", "c.mp3"],
                          ca.segments.SEEK_COPY_JOBS)]

    def test_a_narrow_run_copies_no_wider_than_itself(self, tmp_path,
                                                      monkeypatch):
        planner, pools, _plans = self._planner(tmp_path, {"a.m4b": 40000},
                                               monkeypatch)
        planner.jobs = 2
        planner.window_jobs(["a.m4b"])
        assert pools[0][2] == 2

    def test_the_probes_seek_into_the_copy(self, tmp_path, monkeypatch):
        planner, _pools, plans = self._planner(tmp_path, {"a.m4b": 40000},
                                               monkeypatch)
        _candidates, queue = planner.window_jobs(["a.m4b"])
        assert len(queue) == 7
        assert self._sources(queue) == {
            ca.segments.plan_file_for(str(plans), "a.m4b") + ".seek.mka"}

    @pytest.mark.parametrize("track", ["a.ogg", "a.opus", "a.ogx"])
    def test_a_seek_cheap_format_is_probed_as_it_is(self, tmp_path,
                                                    monkeypatch, track):
        """No copy, and so no pool at all when nothing needs one - even with a
        copy an aborted run left behind, which may be of another file by
        now."""
        planner, pools, plans = self._planner(tmp_path, {track: 40000},
                                              monkeypatch)
        open(ca.segments.plan_file_for(str(plans), track) + ".seek.mka",
             "w").close()
        _candidates, queue = planner.window_jobs([track])
        assert pools == []
        assert self._sources(queue) == {os.path.join("in", track)}

    def test_a_failed_copy_is_probed_in_the_source(self, tmp_path,
                                                   monkeypatch):
        """Slower, never wrong: the book is still planned."""
        planner, _pools, _plans = self._planner(
            tmp_path, {"a.m4b": 40000, "b.m4b": 30000}, monkeypatch,
            fails=("b.m4b",))
        _candidates, queue = planner.window_jobs(["a.m4b", "b.m4b"])
        assert os.path.join("in", "b.m4b") in self._sources(queue)
        assert os.path.join("in", "a.m4b") not in self._sources(queue)

    @pytest.mark.parametrize("track, cheap", [
        ("A.OGG", True), ("a.opus", True), ("a.ogx", True), ("a.flac", False),
        ("a.mka", False), ("a.m4b", False), ("a.m4a", False), ("a.mp3", False), ("a.aac", False),
        ("a.mkv", False), ("a.mp4", False)])
    def test_which_formats_seek_cheaply(self, track, cheap):
        assert ca.seek_cheap_file(track) is cheap


class TestTheSilencesAWindowProbeFound:
    """The midpoints read out of what silencedetect prints, which is one line
    for where a silence starts and ANOTHER for where it ends."""

    _TWO = (
        "Input #0, wav, from 'book.m4a':\n"
        "[Parsed_silencedetect_0 @ 0x7d8a90002d80] silence_start: 1.999909\n"
        "[Parsed_silencedetect_0 @ 0x7d8a90002d80] silence_end: 3.500113"
        " | silence_duration: 1.500204\n"
        "size=N/A time=00:00:09.00 bitrate=N/A speed= 812x\n"
        "[Parsed_silencedetect_0 @ 0x7d8a90002d80] silence_start: 7.25\n"
        "[Parsed_silencedetect_0 @ 0x7d8a90002d80] silence_end: 8.75"
        " | silence_duration: 1.5\n")

    def test_each_start_and_end_pair_is_one_midpoint(self):
        assert ca._silence_midpoints(self._TWO) == pytest.approx(
            [2.750011, 8.0])

    def test_a_silence_still_running_at_the_window_end_gives_none(self):
        """ffmpeg prints no end for a silence the window cut off, so there is
        nothing to take the middle of."""
        text = ("[Parsed_silencedetect_0 @ 0x55] silence_start: 12.5\n"
                "size=N/A time=00:00:14.00 bitrate=N/A speed= 900x\n")
        assert ca._silence_midpoints(text) == []

    def test_no_silence_at_all_gives_none(self):
        assert ca._silence_midpoints("") == []
