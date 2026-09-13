"""Tests for medialib.lib.xheaac - whether a host can encode xHE-AAC at all,
and what a bitrate becomes on exhale's preset ladder.

Nothing here runs an encoder. What is under test is the two decisions the module
makes BEFORE one is reached: what a run is told when the host has none, and
which preset a -b lands on - both pure, and both deciding the shape of a whole
library.
"""

import pytest

from medialib.lib import enums, tooldeps, xheaac

pytestmark = pytest.mark.pure


def having(*tools):
    """A presence test for a host that has exactly <tools> and nothing else.

    The real one reads PATH, so every case below states the host it is asking
    about rather than the one it happens to be running on.
    """
    wanted = set(tools)
    return lambda spec: bool(set(spec.split("|")) & wanted)


class TestTheEncoder:
    """One encoder, named once: the spec a preflight asks PATH for, the name a
    refusal prints, and the word the help page tells a reader to install are the
    same constant."""

    def test_the_tool_spec_is_the_binary_a_preflight_asks_for(self):
        assert xheaac.ENCODER_SPEC == xheaac.EXHALE == "exhale"

    def test_the_binary_has_a_line_in_the_shared_notes_table(self):
        """A refusal reads its role and its install hint from there, so a
        binary missing from it would refuse with an empty sentence."""
        note = tooldeps.tool_note(xheaac.EXHALE)
        role, hint = note.split("|", 1)
        assert role and hint

    def test_a_host_with_the_binary_can_encode(self):
        assert xheaac.encoder_present(having("exhale")) is True

    def test_a_host_without_it_cannot(self):
        assert xheaac.encoder_present(having()) is False

    def test_the_presence_test_is_read_at_the_call_and_not_at_import(self):
        """A default argument is bound once, at import, so a signature default
        would freeze this to whatever the function was then."""
        calls = []
        xheaac.encoder_present(lambda spec: calls.append(spec) or True)
        assert calls == [xheaac.EXHALE]


class TestRefusing:
    def test_a_host_that_can_encode_is_not_refused(self):
        buffer = _Buffer()
        assert xheaac.require_encoder("x", present=having("exhale"),
                                      file=buffer) == 0
        assert buffer.text == ""

    def test_a_host_without_it_is_told_what_to_install_and_from_where(self):
        """The source rather than an apt line: no distribution packages it, so
        a hint naming a package would send the reader to a command that fails.
        """
        buffer = _Buffer()
        assert xheaac.require_encoder("convert-audio (-o xheaac)",
                                      present=having(), file=buffer) == 1
        assert xheaac.EXHALE in buffer.text
        assert "ecodis/exhale" in buffer.text

    def test_the_refusal_names_the_command_that_was_asked_for(self):
        buffer = _Buffer()
        xheaac.require_encoder("convert-audio (-o xheaac)", present=having(),
                               file=buffer)
        assert "convert-audio (-o xheaac)" in buffer.text

    def test_the_refusal_offers_opus_as_the_way_out(self):
        buffer = _Buffer()
        xheaac.require_encoder("x", present=having(), file=buffer)
        assert "-o opus" in buffer.text
        assert "Nothing was changed." in buffer.text

    def test_the_preflight_can_be_skipped_like_every_other_one(self):
        buffer = _Buffer()
        assert xheaac.require_encoder("x", present=having(),
                                      skip_preflight=True, file=buffer) == 0
        assert buffer.text == ""


class _Buffer:
    """A file the refusal can be written to, so a case reads the message rather
    than the exit status alone."""

    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> None:
        self.text += text


class TestExhalesPresetLadder:
    """exhale takes a preset, not a bitrate, so a -b has to land on a rung. Which
    rung decides the size of every file in a library."""

    def test_the_default_bitrate_lands_just_above_itself(self):
        # 46 kbps stereo: preset b is 48, preset a is 36. Nearest wins, and
        # there is no reading of "46" that means 36.
        assert xheaac.exhale_preset(46, 2) == "b"
        assert xheaac.preset_bitrate("b", 2) == 48

    def test_the_forced_mono_bitrate_stays_on_the_esbr_ladder(self):
        # 32 kbps mono is preset `1` EXACTLY (64 stereo, halved) and eSBR
        # preset `c` only within 2 - and the exact one is the wrong answer: at
        # half the rate plain frequency-domain coding wants, eSBR is what makes
        # it listenable. The family is chosen before the rung for this case.
        assert xheaac.exhale_preset(32, 1) == "c"
        assert xheaac.preset_bitrate("c", 1) == 30

    def test_the_esbr_family_wins_the_whole_overlap(self):
        for bitrate in range(36, 109):
            assert xheaac.exhale_preset(bitrate, 2).isalpha()

    def test_above_the_esbr_ceiling_the_plain_family_is_the_only_one_left(self):
        assert xheaac.exhale_preset(128, 2) == "5"
        assert xheaac.preset_bitrate("5", 2) == 128

    def test_a_rate_below_the_floor_gets_the_floor(self):
        # exhale warns about this preset itself rather than refusing, and there
        # is nothing lower to offer.
        assert xheaac.exhale_preset(6, 1) == "a"
        assert xheaac.preset_bitrate("a", 1) == 18

    def test_mono_is_half_of_the_quoted_stereo_figure(self):
        """exhale quotes its ladder for 2 channels and states its own floor as
        "18 kbit/s mono, 36 kbit/s stereo", so the halving has to round to 18
        and not to 17."""
        assert xheaac.preset_bitrate("a", 2) == 36
        assert xheaac.preset_bitrate("a", 1) == 18

    def test_a_preset_always_answers_a_rate_and_a_non_preset_never_does(self):
        for character, _stereo in xheaac._presets():
            assert xheaac.preset_bitrate(character, 2) > 0
        for character in ("h", "z", "10", "", "A"):
            assert xheaac.preset_bitrate(character, 2) == 0

    def test_every_bitrate_a_run_could_ask_for_gets_a_real_preset(self):
        """A preset the ladder does not hold would be passed to exhale as an
        argument it refuses, one file at a time, with stderr silenced."""
        ladder = {character for character, _rate in xheaac._presets()}
        for channels in (1, 2, 6):
            for bitrate in range(1, 401):
                assert xheaac.exhale_preset(bitrate, channels) in ladder

    def test_a_higher_channel_count_asks_for_a_lower_rung(self):
        """The figures are per-channel underneath, so the same target spread
        over six channels cannot buy the same rung it buys over two."""
        assert (xheaac.preset_bitrate("g", 6) > xheaac.preset_bitrate("g", 2)
                > xheaac.preset_bitrate("g", 1))


class TestTheSampleRateBand:
    """exhale accepts 32-48 kHz and says to use nothing else, so what reaches it
    is settled here rather than by the encoder refusing a file at a time."""

    @pytest.mark.parametrize("rate", [32000, 44100, 48000])
    def test_a_rate_already_in_the_band_is_left_exactly_alone(self, rate):
        # Resampling 44.1 to 48 would be a real interpolation for no reason.
        assert xheaac.input_sample_rate(rate) == rate

    @pytest.mark.parametrize("rate", [8000, 16000, 22050, 31999])
    def test_a_rate_below_the_band_comes_up_to_the_floor(self, rate):
        assert xheaac.input_sample_rate(rate) == 32000

    @pytest.mark.parametrize("rate", [64000, 88200, 96000, 192000])
    def test_a_rate_above_the_band_comes_down_to_the_ceiling(self, rate):
        assert xheaac.input_sample_rate(rate) == 48000

    @pytest.mark.parametrize("rate", [0, -1])
    def test_an_unknown_rate_is_read_as_the_top_of_the_band(self, rate):
        """A probe that answered nothing is not a slow source: 48 kHz is what a
        modern one almost always is."""
        assert xheaac.input_sample_rate(rate) == 48000

    def test_a_rate_between_two_rungs_takes_the_nearer(self):
        assert xheaac.input_sample_rate(37000) == 32000
        assert xheaac.input_sample_rate(46000) == 44100


class TestTheCalls:
    """The two argument lists, which are what the pipe is made of."""

    def test_the_decode_writes_wave_on_stdout(self):
        argv = xheaac.wav_argv("a.mp3", 48000, mono=False)
        assert argv[0] == "ffmpeg"
        assert argv[-5:] == ["-c:a", "pcm_s16le", "-f", "wav", "-"]

    def test_the_decode_takes_only_the_first_audio_stream(self):
        """A video source's picture must not reach the encoder, and neither must
        a second language track."""
        argv = xheaac.wav_argv("a.mkv", 48000, mono=False)
        assert argv[argv.index("-map") + 1] == "0:a:0"

    def test_the_decode_drops_the_source_metadata(self):
        # The chapters and the cover are re-attached from the original after the
        # encode, so carrying them through the WAVE would be pointless.
        assert "-map_metadata" in xheaac.wav_argv("a.mp3", 48000, mono=False)

    def test_forced_mono_is_the_decoder_s_job_not_the_encoder_s(self):
        """The encoders have no downmix: they encode the channels they are
        given, so -m has to be `-ac 1` here exactly as it is for Opus."""
        assert "-ac" in xheaac.wav_argv("a.mp3", 48000, mono=True)
        assert "-ac" not in xheaac.wav_argv("a.mp3", 48000, mono=False)

    def test_the_rate_reaches_the_decoder(self):
        argv = xheaac.wav_argv("a.mp3", 44100, mono=False)
        assert argv[argv.index("-ar") + 1] == "44100"

    def test_a_range_seeks_short_of_the_mark_and_cuts_in_the_filter_graph(self):
        """The seek is rough on purpose: on an m4b it lands late by about a
        thousand samples, reliably, and a chunk that begins a thousand samples
        after it was asked to is a hole in the joined book. So it seeks EARLY
        and the exact cut is a timestamp range in the filter graph.
        """
        argv = xheaac.wav_argv("a.mp3", 48000, False, start="10",
                               duration="5")
        assert argv.index("-ss") < argv.index("-i")
        assert argv[argv.index("-ss") + 1] == "%.9f" % (10 - xheaac.SEEK_LEAD)
        assert "-t" not in argv
        trim = argv[argv.index("-af") + 1]
        assert trim.startswith("atrim=start=10.000000000:end=15.000000000")

    def test_the_range_is_named_in_the_source_s_own_timestamps(self):
        """`atrim` cuts by timestamp, and without `-copyts` the decoder
        renumbers every seek from zero - so the range would name a moment in a
        file that does not exist."""
        argv = xheaac.wav_argv("a.mp3", 48000, False, start="10",
                               duration="5")
        assert "-copyts" in argv
        assert argv.index("-copyts") < argv.index("-i")

    def test_the_cut_pieces_are_put_back_on_a_timeline_of_their_own(self):
        """`-copyts` would otherwise write a WAVE that claims to begin ten
        seconds in, and the encoder would be handed ten seconds of nothing."""
        trim = xheaac.wav_argv("a.mp3", 48000, False, "10", "5")
        assert "asetpts=PTS-STARTPTS" in trim[trim.index("-af") + 1]

    def test_a_seek_never_goes_before_the_start_of_the_file(self):
        argv = xheaac.wav_argv("a.mp3", 48000, False, start="1", duration="5")
        assert argv[argv.index("-ss") + 1] == "%.9f" % 0.0

    def test_the_whole_file_decode_has_no_range_at_all(self):
        argv = xheaac.wav_argv("a.mp3", 48000, False)
        assert "-ss" not in argv and "-af" not in argv and "-copyts" not in argv

    def test_exhale_is_given_two_arguments_so_it_reads_its_stdin(self):
        """Three arguments is exhale's FILE form: it would then read the WAVE
        from a path and treat the output name as the input."""
        assert xheaac.exhale_argv("b", "out.m4a") == ["exhale", "b", "out.m4a"]


class TestAgreementWithTheCodecList:
    def test_xhe_aac_is_a_codec_convert_audio_offers(self):
        """This module is reached by `-o xheaac` and by nothing else, so a
        rename in the list without one here leaves it unreachable."""
        assert "xheaac" in enums.AUDIO_CODECS

    def test_it_is_not_the_default(self):
        """Opus stays the default: this codec needs a tool that is not
        installed anywhere by default."""
        assert enums.AUDIO_CODECS[0] == "opus"

    def test_it_is_written_as_an_mp4(self):
        assert enums.AUDIO_CODEC_EXTENSIONS["xheaac"] == "m4a"


class TestExhalesOwnSampleRateGuards:
    """exhale's non-eSBR presets REFUSE a sample rate above a per-preset bound,
    and the chooser has to stay clear of the ones it would trip.

    Transcribed from exhale's own source (src/app/exhaleApp.cpp), because it is
    a refusal rather than a clamp: preset 0 at 44.1 kHz exits with "Input sample
    rate must be <=32 kHz for preset mode 0" and writes nothing. Silently, with
    stderr closed, that is one empty output per file.
    """

    @staticmethod
    def _refused(preset, rate):
        if preset.isalpha():
            # An eSBR preset bypasses the check entirely.
            return False
        mode = int(preset)
        limit = 32100 + mode * 12000 + (mode >> 2) * 3900
        # Modes 0 and 1 at exactly 48 kHz are the one exception: exhale
        # downsamples to 32 kHz itself rather than refusing.
        return rate > limit and (mode > 1 or rate != 48000)

    def test_the_chooser_never_asks_for_a_combination_exhale_refuses(self):
        for channels in (1, 2, 6):
            for bitrate in range(1, 401):
                preset = xheaac.exhale_preset(bitrate, channels)
                for rate in xheaac.SAMPLE_RATES:
                    assert not self._refused(preset, rate), (
                        "channels=%d -b %d gives preset %s, which exhale "
                        "refuses at %d Hz" % (channels, bitrate, preset, rate))

    def test_the_low_plain_presets_are_unreachable(self):
        """Presets 0-3 are the ones with the tight sample-rate bounds, and the
        eSBR ladder covers every rate that would reach them - so the guard
        above holds by construction rather than by luck."""
        reachable = {xheaac.exhale_preset(bitrate, channels)
                     for channels in (1, 2, 6)
                     for bitrate in range(1, 401)}
        assert reachable & set("0123") == set()

    def test_and_the_guard_would_catch_it_if_they_became_reachable(self):
        """A test that can never fail proves nothing: preset 0 at 44.1 kHz
        really is a combination the helper above rejects."""
        assert self._refused("0", 44100) is True
        assert self._refused("a", 44100) is False


class TestHowLongAWaveCanBe:
    """The one limit of the pipe, and the reason an 83-hour audiobook came out
    of it at thirteen and a half hours with a zero exit status.

    RIFF states a chunk's length in 32 bits, and exhale reads exactly the count
    the header declares. Over a pipe ffmpeg has no length to state and writes the
    field full of ones, which is that same 4 GiB rather than "read to the end" -
    so the ceiling is reached whether or not a file is in between.
    """

    def test_the_ceiling_is_the_largest_a_32_bit_length_can_say(self):
        assert xheaac.WAVE_DATA_CEILING == 0xFFFFFFFF

    def test_it_is_the_byte_count_divided_by_the_bytes_a_second_costs(self):
        assert xheaac.wave_seconds_ceiling(48000, 2) == pytest.approx(
            xheaac.WAVE_DATA_CEILING / (48000 * 2 * xheaac.SAMPLE_BYTES))

    def test_the_sample_width_is_the_one_the_decode_actually_writes(self):
        """The arithmetic and the ffmpeg call have to agree, or the ceiling
        describes a WAVE nobody writes: `pcm_s16le` is two bytes."""
        argv = xheaac.wav_argv("in.m4b", 44100, True)
        assert "pcm_s16le" in argv
        assert xheaac.SAMPLE_BYTES == 2

    def test_mono_at_44_1_kHz_runs_out_after_about_thirteen_and_a_half_hours(
            self):
        hours = xheaac.wave_seconds_ceiling(44100, 1) / 3600
        assert 13.5 < hours < 13.6

    def test_more_channels_reach_the_ceiling_proportionally_sooner(self):
        assert xheaac.wave_seconds_ceiling(48000, 1) == pytest.approx(
            2 * xheaac.wave_seconds_ceiling(48000, 2))

    def test_an_ordinary_book_fits(self):
        assert xheaac.fits_in_one_wave(6 * 3600, 44100, 1)

    def test_the_book_that_did_not_does_not(self):
        """83:41:50 of mono at 44.1 kHz - the file this check was written for,
        which encoded its first 13:31:36 and reported success."""
        assert not xheaac.fits_in_one_wave(301309.7, 44100, 1)

    def test_the_ceiling_itself_is_the_last_length_that_fits(self):
        ceiling = xheaac.wave_seconds_ceiling(44100, 1)
        assert xheaac.fits_in_one_wave(ceiling, 44100, 1)
        assert not xheaac.fits_in_one_wave(ceiling + 1, 44100, 1)

    def test_a_length_nothing_could_probe_is_not_a_refusal(self):
        """An unreadable header must not become "this file cannot be encoded":
        the finished output is measured against the source either way."""
        assert xheaac.fits_in_one_wave(0, 44100, 1)
        assert xheaac.fits_in_one_wave(-1, 44100, 1)

    def test_every_rate_in_the_band_has_a_ceiling_a_long_book_can_exceed(self):
        """Not a limit only the exotic rates have: the shortest ceiling in the
        band is under seven hours, which an audiobook reaches routinely."""
        for rate in xheaac.SAMPLE_RATES:
            assert xheaac.wave_seconds_ceiling(rate, 2) < 24 * 3600
