"""The "is this soundtrack worth re-encoding" model.

A stream that was never given enough bits cannot be improved by encoding it
again. The anchor it is judged against is already in this repo -
:mod:`medialib.lib.bitrates`, the table ``convert-audio`` encodes to, one row per
channel configuration and one column per kind of content - so what this module
adds is the two things that table does not say: what a channel count needs in a
codec that is NOT Opus, and how a measured bitrate reads against the result.

The requirement is therefore "what this repo would encode that content at, in
that codec": adequate means as good as a careful encode of it would be, and
:data:`adequacy.GENEROUS_FACTOR` times that means there is a whole such encode's
worth of spare.

The channel count is the only axis. Sample rate and bit depth are deliberately
not axes: every lossy codec here resamples and requantises to whatever it needs,
so a 96 kHz 24-bit source and a 44.1 kHz 16-bit one of the same music want the
same bitrate out - the extra is thrown away by the encoder either way, not
carried.
"""

from __future__ import annotations

from medialib.lib import adequacy, bitrates
from medialib.lib.enums import shell_lower
from medialib.lib.formatting import awk_number

__all__ = [
    "MUSIC",
    "SPEECH",
    "CONTENT_COLUMNS",
    "LOSSY",
    "LOSSLESS",
    "UNKNOWN",
    "TABLE",
    "family_of",
    "kind_of",
    "audio_codec_tuning",
    "adequate_audio_bitrate",
    "audio_verdict",
    "audio_adequacy",
]

# The two kinds of content the shared table has a column for, under the names
# this model uses. MUSIC is the full-band case the "normal" column is for;
# SPEECH is the spoken-word one the "comment" column holds, which is what a
# commentary track, an audiobook and a podcast all are.
MUSIC = "music"
SPEECH = "speech"

# Which column of ``bitrates`` each reads. Named here rather than passed through,
# so a caller says what the content IS and not which column it wants.
CONTENT_COLUMNS = {
    MUSIC: "normal",
    SPEECH: "comment",
}

LOSSY = "lossy"
LOSSLESS = "lossless"
UNKNOWN = "unknown"

# One row per family: (family, kind, factor, aliases).
#
# The FACTOR is what the family needs relative to Opus for the same quality,
# which is what makes the shared Opus table usable as everyone's anchor. The
# figures are the published listening tests read as equivalences at the anchor's
# own rates: the table's 120 kbit/s of stereo Opus is about 160 of AAC-LC or
# Vorbis and about 192 of MP3 or AC-3, and its 250 kbit/s of 5.1 Opus is about
# the 448 an AC-3 film track is carried at. The broadcast codecs are the dearest
# of the lot - AC-3 was designed for a 1990s optical track and has none of the
# tools that came after it - and DTS dearest of all, which is why its own
# soundtracks are carried at three and six times these rates.
#
# The LOSSLESS families have no factor worth stating: they carry the recording
# exactly, at whatever the material costs, so nothing about their size says
# anything about their quality. They are listed so they can be RECOGNISED - the
# verdict for one is floored at adequate - and their factor is what a transparent
# lossy encode of the same material would take, which is the figure that makes
# "generous" mean what it means everywhere else.
#
# To recognise another codec, add its spelling to a row. Matched
# case-insensitively, because the names arrive from ffprobe, from mediainfo and
# from people.
TABLE = (
    ("opus", LOSSY, "1.00", ("opus", "libopus")),
    ("xheaac", LOSSY, "1.00", ("xhe-aac", "usac")),
    ("heaac", LOSSY, "1.10", ("he-aac", "heaacv2", "he-aacv2", "aac_he")),
    ("vorbis", LOSSY, "1.20", ("vorbis", "libvorbis")),
    ("aac", LOSSY, "1.30", ("aac", "aac_latm", "mp4a")),
    ("eac3", LOSSY, "1.35", ("eac3", "ec-3", "dd+")),
    ("wma", LOSSY, "1.55", ("wmav1", "wmav2", "wmapro", "wmalossless")),
    ("mp3", LOSSY, "1.60", ("mp3", "mp2", "mp1", "mp3float", "mp3adu")),
    ("ac3", LOSSY, "1.70", ("ac3", "ac-3", "dnet")),
    ("dts", LOSSY, "3.00", ("dts", "dca")),
    ("pcm", LOSSLESS, "1.00", ("pcm_s16le", "pcm_s16be", "pcm_s24le",
                               "pcm_s24be", "pcm_s32le", "pcm_f32le", "wav")),
    ("flac", LOSSLESS, "1.00", ("flac",)),
    ("alac", LOSSLESS, "1.00", ("alac",)),
    ("wavpack", LOSSLESS, "1.00", ("wavpack", "wv")),
    ("ape", LOSSLESS, "1.00", ("ape", "monkeys audio")),
    ("truehd", LOSSLESS, "1.00", ("truehd", "mlp")),
)


def _row(name: str) -> tuple[str, str, str, tuple[str, ...]] | None:
    """The table row a name resolves to, or None - walked in order, matched
    case-insensitively against the family names and their aliases alike."""
    wanted = shell_lower(name)
    if not wanted:
        return None
    for family, kind, factor, aliases in TABLE:
        if wanted == shell_lower(family) or any(
                wanted == shell_lower(alias) for alias in aliases):
            return family, kind, factor, aliases
    return None


def family_of(name: str) -> str:
    """The family a codec belongs to, or :data:`UNKNOWN` for one not listed."""
    row = _row(name)
    return row[0] if row is not None else UNKNOWN


def kind_of(name: str) -> str:
    """Whether the codec throws information away - :data:`LOSSY`,
    :data:`LOSSLESS` or :data:`UNKNOWN`."""
    row = _row(name)
    return row[1] if row is not None else UNKNOWN


def audio_codec_tuning(codec: str) -> str:
    """``"<factor> <kind> <family>"`` for a codec.

    A codec the table does not name - or none at all, from a stream nothing could
    probe - is tuned as Opus and named as the unknown it is: Opus is the anchor
    table's own codec, so an unidentified stream is judged by exactly the figures
    this repo would encode it at, which is neither generous nor harsh.
    """
    row = _row(codec)
    if row is None:
        return f"1.00 {UNKNOWN} {UNKNOWN}"
    return f"{row[2]} {row[1]} {row[0]}"


def adequate_audio_bitrate(codec: str, channels: object,
                           content: str = MUSIC) -> str:
    """What a stream of that description needs to be adequate, in kbit/s.

    The shared table's row for the channel count, in the column the content
    calls for, times what the codec costs against Opus. Prints nothing when the
    channel count has no row - the table stops at 8 channels, which is where
    ffmpeg's own Opus wrapper stops - because every figure here is that row
    scaled and a guessed row would be a guessed verdict.
    """
    column = CONTENT_COLUMNS.get(content or MUSIC)
    if column is None:
        column = CONTENT_COLUMNS[MUSIC]
    # The count arrives from ffprobe as text and from a caller as an int; the
    # table's rows are keyed by the plain decimal spelling of each.
    count = awk_number(channels)
    if count <= 0 or count != int(count):
        return ""
    anchor = bitrates.audio_bitrate(str(int(count)), column)
    if anchor is None:
        return ""
    factor, _kind, _family = audio_codec_tuning(codec).split()
    return "%.0f" % (awk_number(anchor) * awk_number(factor))


def audio_verdict(kbit: object, adequate: object, codec: str = "") -> str:
    """What a bitrate IS for a requirement - the shared verdict, with the one
    thing peculiar to audio applied on top.

    That one thing is losslessness: such a stream carries the recording
    exactly, so "it was not given enough bits" cannot be true of it
    however small it is - a quiet mono FLAC of a spoken chapter is a cheap
    recording, not a degraded one. Its verdict is floored at adequate, which
    leaves the reading that still means something - whether re-encoding would
    save anything - saying what it says everywhere else.
    """
    name = adequacy.verdict(kbit, adequate)
    if kind_of(codec) == LOSSLESS:
        return adequacy.at_least_adequate(name)
    return name


def audio_adequacy(codec: str, channels: object, bits_per_second: object,
                   content: str = MUSIC) -> str:
    """The verdict on one stream, from what a probe already read out of it.

    Bits per second in, because that is what every probe in this repo states and
    what the census records; the table is in kbit/s, and the division happens
    here so no caller has to remember which unit it is holding.
    """
    adequate = adequate_audio_bitrate(codec, channels, content)
    if not adequate:
        return adequacy.UNKNOWN
    rate = awk_number(bits_per_second)
    if rate <= 0:
        return adequacy.UNKNOWN
    return audio_verdict("%.0f" % (rate / 1000), adequate, codec)
