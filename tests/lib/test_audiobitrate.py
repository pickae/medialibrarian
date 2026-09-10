"""The white box for medialib/lib/audiobitrate.py.

The anchor is not this module's own - it is the shared table convert-audio
encodes to - so what is pinned here is the two things it adds: that a codec's
requirement is that table's row scaled by what the codec costs against Opus, and
that a lossless stream is never starved.

The equivalences the factors encode are worth stating as cases, because they are
the whole content of the table: 120 kbit/s of stereo Opus is about 160 of AAC-LC
and about 192 of MP3, and 250 of 5.1 Opus is about the 448 an AC-3 film track is
carried at.
"""

import pytest

from medialib.lib import adequacy, bitrates
from medialib.lib import audiobitrate as ab

pytestmark = pytest.mark.pure


class TestTheAnchorIsTheSharedTable:
    def test_opus_asks_for_exactly_what_the_table_says(self):
        """Which is what makes "adequate" mean "as good as this repo would have
        encoded it" rather than a second opinion about the same file."""
        for channels in ("1", "2", "6", "8"):
            assert ab.adequate_audio_bitrate("opus", channels, ab.MUSIC) \
                == bitrates.audio_bitrate(channels, "normal")
            assert ab.adequate_audio_bitrate("opus", channels, ab.SPEECH) \
                == bitrates.audio_bitrate(channels, "comment")

    def test_more_channels_want_more(self):
        rates = [float(ab.adequate_audio_bitrate("opus", n, ab.MUSIC))
                 for n in range(1, 9)]
        assert rates == sorted(rates)

    def test_spoken_word_wants_less_than_music(self):
        assert float(ab.adequate_audio_bitrate("aac", 2, ab.SPEECH)) \
            < float(ab.adequate_audio_bitrate("aac", 2, ab.MUSIC))

    @pytest.mark.parametrize("channels", [0, 9, 16, "", "two", "2.5", -1])
    def test_a_channel_count_with_no_row_states_nothing(self, channels):
        """The table stops at 8, which is where ffmpeg's own Opus wrapper stops.
        Every figure here is that row scaled, so a guessed row would be a guessed
        verdict."""
        assert ab.adequate_audio_bitrate("opus", channels, ab.MUSIC) == ""

    def test_a_content_word_the_table_has_never_heard_of_is_read_as_music(self):
        assert ab.adequate_audio_bitrate("mp3", 2, "podcast") \
            == ab.adequate_audio_bitrate("mp3", 2, ab.MUSIC)


class TestTheCodecFactors:
    @pytest.mark.parametrize("dearer,cheaper", [
        ("aac", "opus"), ("vorbis", "opus"), ("mp3", "aac"),
        ("ac3", "mp3"), ("dts", "ac3"), ("wma", "eac3"),
    ])
    def test_the_ladder_runs_from_opus_upwards(self, dearer, cheaper):
        assert float(ab.adequate_audio_bitrate(dearer, 2, ab.MUSIC)) \
            > float(ab.adequate_audio_bitrate(cheaper, 2, ab.MUSIC))

    @pytest.mark.parametrize("codec,channels,content,bits,expected", [
        # the equivalences the table encodes, at the rates they are quoted at
        ("opus", 2, ab.MUSIC, 128000, adequacy.ADEQUATE),
        ("opus", 2, ab.MUSIC, 64000, adequacy.STARVED),
        ("aac", 2, ab.MUSIC, 160000, adequacy.ADEQUATE),
        ("aac", 2, ab.MUSIC, 128000, adequacy.STARVED),
        ("mp3", 2, ab.MUSIC, 192000, adequacy.ADEQUATE),
        ("mp3", 2, ab.MUSIC, 128000, adequacy.STARVED),
        ("ac3", 6, ab.MUSIC, 448000, adequacy.ADEQUATE),
        ("ac3", 6, ab.MUSIC, 384000, adequacy.STARVED),
        # a DTS soundtrack is carried at twice what it needs and more
        ("dts", 6, ab.MUSIC, 1509000, adequacy.GENEROUS),
    ])
    def test_the_verdicts_those_rates_come_out_at(self, codec, channels,
                                                  content, bits, expected):
        assert ab.audio_adequacy(codec, channels, bits, content) == expected

    def test_a_codec_nobody_could_identify_is_tuned_as_opus(self):
        """The anchor table's own codec, so an unidentified stream is judged by
        exactly the figures this repo would encode it at."""
        assert ab.adequate_audio_bitrate("", 2, ab.MUSIC) \
            == ab.adequate_audio_bitrate("opus", 2, ab.MUSIC)
        assert ab.audio_codec_tuning("").split() == ["1.00", "unknown",
                                                     "unknown"]


class TestTheLosslessCase:
    @pytest.mark.parametrize("codec", ["flac", "alac", "wavpack", "ape",
                                       "truehd", "pcm_s16le", "wav"])
    def test_a_lossless_stream_is_never_starved(self, codec):
        """It carries the recording exactly, at whatever the material costs - a
        quiet mono FLAC of a spoken chapter is a cheap recording, not a degraded
        one."""
        assert ab.kind_of(codec) == ab.LOSSLESS
        assert ab.audio_adequacy(codec, 1, 1000, ab.SPEECH) \
            == adequacy.ADEQUATE

    def test_and_a_full_sized_one_is_still_generous(self):
        assert ab.audio_adequacy("flac", 2, 900000, ab.MUSIC) \
            == adequacy.GENEROUS

    def test_but_a_lossy_stream_that_small_is_starved(self):
        assert ab.audio_adequacy("mp3", 1, 1000, ab.SPEECH) \
            == adequacy.STARVED


class TestWhatCannotBeJudged:
    @pytest.mark.parametrize("channels,bits", [
        (0, 128000), (2, 0), (2, ""), (2, "none"), (99, 128000)])
    def test_a_missing_figure_is_unknown_and_not_an_extreme(self, channels,
                                                            bits):
        assert ab.audio_adequacy("opus", channels, bits) == adequacy.UNKNOWN


class TestTheLookups:
    @pytest.mark.parametrize("name,family", [
        ("libopus", "opus"), ("Opus", "opus"), ("aac_latm", "aac"),
        ("EAC3", "eac3"), ("ec-3", "eac3"), ("dca", "dts"),
        ("mp3float", "mp3"), ("wmav2", "wma"), ("pcm_s24le", "pcm"),
    ])
    def test_every_spelling_reaches_its_family(self, name, family):
        assert ab.family_of(name) == family

    @pytest.mark.parametrize("name", ["", "speex", "not a codec"])
    def test_and_an_unlisted_one_stays_unknown(self, name):
        assert ab.family_of(name) == ab.UNKNOWN
        assert ab.kind_of(name) == ab.UNKNOWN
