"""The white box for medialib/cli/convert_audio.py.

Its small helpers, and the decisions -o makes: where a track's output lands,
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
        offered = ca.OPT_SPEC.split("Output codec: ")[1].split(".")[0]
        assert offered.split(" or ") == list(enums.AUDIO_CODECS)
        accepted = ca.OPT_CHECKS.split("enum:")[1].split(" |")[0]
        assert accepted.split("\\|") == list(enums.AUDIO_CODECS)

    def test_every_codec_has_a_spoken_name_for_the_messages(self):
        assert set(ca.CODEC_NAMES) == set(enums.AUDIO_CODECS)

    def test_the_page_names_only_encoders_worth_installing(self):
        """A gated back-end on the page sends a reader off to build the one
        tool that is then declined. The refusal names it instead, where there
        is room to say why."""
        named = ca.OPT_SPEC.split("external encoder (")[1].split(")")[0]
        assert named == xheaac.usable_tools()
        for backend in xheaac.BACKENDS:
            if not backend.usable:
                assert backend.tool not in named

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
    def test_xhe_aac_keeps_long_files_whole(self):
        """Chunks are encoded separately and concatenated, which needs a
        sample-exact join. The external encoders write finished MP4s with their
        own edit lists and preroll frames, so there is no such join to make."""
        assert "xheaac" in ca.NO_SPLIT_CODECS

    def test_opus_does_not(self):
        assert "opus" not in ca.NO_SPLIT_CODECS

    def test_every_no_split_codec_is_a_codec_that_exists(self):
        assert set(ca.NO_SPLIT_CODECS) <= set(enums.AUDIO_CODECS)


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
    """A run that picks one of two tools by itself has to say which, or a library
    encoded by the fallback cannot be told apart afterwards from one encoded by
    the preferred back-end."""

    def _lines(self, capsys, **kwargs):
        settings = {"backend": xheaac.backend_named(xheaac.EXHALE),
                    "codec": "xheaac", "bitrate": 46, "mono": False,
                    "adaptive": False}
        settings.update(kwargs)
        ca._report_encoder(settings["backend"], settings["codec"],
                           settings["bitrate"], settings["mono"],
                           settings["adaptive"])
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

    def test_an_opus_run_reports_nothing_at_all(self, capsys):
        """There is no back-end to name: ffmpeg encodes it, and ffmpegselect
        already says which ffmpeg."""
        assert self._lines(capsys, backend=None) == []


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
                "backend": xheaac.backend_named(xheaac.EXHALE)}
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
        run = ca.Run(codec="opus", extension="opus", output_dir="out",
                     backend=None)
        run._encode("a.flac", "out/a.opus", mono=False, bitrate=46)
        assert seen == [["ffmpeg", "-nostdin", "-y", "-i", "a.flac",
                         "-map", "0:a:0", "-map_metadata", "0:s:0",
                         "-c:a", "libopus", "-b:a", "46k", "out/a.opus"]]

    def test_and_the_mono_call_puts_the_downmix_before_the_codec(self,
                                                                 monkeypatch):
        seen = []
        monkeypatch.setattr(ca.subprocess, "run",
                            lambda argv, **kw: seen.append(list(argv)))
        run = ca.Run(codec="opus", extension="opus", output_dir="out",
                     backend=None)
        run._encode("a.flac", "out/a.opus", mono=True, bitrate=32)
        assert seen == [["ffmpeg", "-nostdin", "-y", "-i", "a.flac",
                         "-map", "0:a:0", "-map_metadata", "0:s:0",
                         "-ac", "1", "-c:a", "libopus", "-b:a", "32k",
                         "out/a.opus"]]

    def test_the_lift_out_copies_and_never_names_an_encoder(self, monkeypatch):
        seen = []
        monkeypatch.setattr(ca.subprocess, "run",
                            lambda argv, **kw: seen.append(list(argv)))
        run = ca.Run(codec="opus", extension="opus", output_dir="out",
                     backend=None)
        run._remux("a.mkv", "out/a.opus")
        assert "-c:a" in seen[0] and seen[0][seen[0].index("-c:a") + 1] == "copy"
        assert "libopus" not in seen[0]
