"""The white box for medialib/cli/convert_audio.py.

Its small helpers, and the decisions -e makes: where a track's output lands,
which sources are already what a run produces, and what the run tells the reader
about the encoder it picked. The planning arithmetic beside them - the chunk plan
and the boundary nudging - belongs to `medialib/lib/segments.py` and is pinned
with it, so it is not repeated here; nor is the encoder choice itself, which is
`medialib/lib/xheaac.py` and has its own file.
"""

import os

import pytest

from medialib.cli import convert_audio as ca
from medialib.lib import enums, xheaac

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


class TestTheOutputCodec:
    """-o, and the four places the choice of codec has to reach. Every one of
    them used to spell `.opus` out."""

    def test_opus_is_the_default_and_the_first_of_the_list(self):
        assert ca.DEFAULT_CODEC == "opus" == enums.AUDIO_CODECS[0]

    def test_the_page_offers_exactly_the_codecs_the_check_accepts(self):
        """Both are generated from the one list. Written out by hand they drift,
        and the page ends up advertising a codec -o refuses."""
        offered = ca.OPT_SPEC.split("Output encoding: ")[1].split(".")[0]
        assert offered.split(" or ") == list(enums.AUDIO_CODECS)
        accepted = ca.OPT_CHECKS.split("enum:")[1].split(" |")[0]
        assert accepted.split("\\|") == list(enums.AUDIO_CODECS)

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
                "bitrate": ca.DEFAULT_BITRATE}
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
        base = {"codec": "xheaac", "extension": "m4a", "output_dir": "out"}
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
        run = ca.Run(codec="opus", extension="opus", output_dir="out")
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

    def _run(self):
        return ca.Run(codec="xheaac", extension="m4a", output_dir="out")

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
        assert "-o opus" in said

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

    def _producer(self, tmp_path, sizes, chunked, threshold, seed=0):
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
            split_threshold=threshold, codec="opus")
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
            tmp_path, {"book.m4b": 800, "track.m4a": 150}, {"book.m4b": 8},
            0.01)
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
            tmp_path, sizes, {track: 1 for track in sizes}, 0.02)
        order = [token for token, _size in producer]
        assert order == sorted(sizes, key=lambda track: -sizes[track])

    def test_the_seed_is_the_largest_of_what_needs_no_planning(self, tmp_path):
        """The seed carries the pool through the planning, so it is handed over
        before the plan is made and it is the LARGEST of the tracks the settle
        asked nothing of - the most encoding the pool can be given up front."""
        sizes = {"book.m4b": 900, "a.m4a": 300, "b.m4a": 200, "c.m4a": 100}
        producer, _total_file = self._producer(
            tmp_path, sizes, {"book.m4b": 1}, 0.02, seed=2)
        order = [token for token, _size in producer]
        assert order == ["a.m4a", "b.m4a", "book.m4b", "c.m4a"]

    def test_the_chunks_of_one_file_are_still_all_there_and_in_order(
            self, tmp_path):
        """Interleaving is free, losing a piece is not: every index the planner
        wrote is handed over, and they keep their order among themselves so the
        run is reproducible."""
        producer, _total_file = self._producer(
            tmp_path, {"book.m4b": 400, "a.m4a": 100, "b.m4a": 100},
            {"book.m4b": 4}, 0.01)
        items = list(producer)
        indexes = [token.split(ca.UNIT)[1] for token, _size in items
                   if ca.UNIT in token]
        assert indexes == ["0", "1", "2", "3"]

    def test_the_preload_is_handed_over_largest_first(self, tmp_path):
        """The queue orders what it holds by the size it is told, so the
        producer owes it the scan's order - largest file first - and no more."""
        producer, _total_file = self._producer(
            tmp_path, {"small.m4a": 10, "big.m4a": 900, "mid.m4a": 100}, {},
            0.03)
        items = list(producer)
        assert [token for token, _size in items] == \
            ["big.m4a", "mid.m4a", "small.m4a"]

    def test_a_book_cut_into_pieces_no_smaller_leads_the_buffer(self,
                                                                tmp_path):
        """The order follows the sizes and nothing else: two chunks of a file
        ten times the size of its neighbours are still the two biggest jobs."""
        producer, _total_file = self._producer(
            tmp_path, {"book.m4b": 2000, "a.m4a": 100, "b.m4a": 100},
            {"book.m4b": 2}, 0.01)
        items = list(producer)
        names = [token.split(ca.UNIT)[0]
                 for token, _size in sorted(items, key=lambda item: -item[1])]
        assert names[:2] == ["book.m4b", "book.m4b"]

    def test_the_total_is_written_when_the_planning_is_done(self, tmp_path):
        """The queue's lines count without a denominator until the producer
        knows the queue's own length, which is when the plan is made - and it
        writes it then, before the first job the plan settled."""
        producer, total_file = self._producer(
            tmp_path, {"book.m4b": 800, "track.m4a": 150}, {"book.m4b": 8},
            0.01)
        items = list(producer)
        with open(total_file) as handle:
            assert handle.read() == "%d\n" % len(items)

    def test_a_queue_with_nothing_to_plan_is_counted_from_the_start(
            self, tmp_path):
        """With no candidates the length is known before the first job is
        handed over, so the first line the run prints may already carry it."""
        producer, total_file = self._producer(
            tmp_path, {"a.m4a": 100, "b.m4a": 200}, {}, 0.03)
        next(producer)
        with open(total_file) as handle:
            assert handle.read() == "2\n"
