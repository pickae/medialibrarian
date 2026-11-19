"""The shared Opus target bitrates, one row per channel configuration.

It is a mapping.

Channel counts are strings, not numbers, and matched as text: a track reported
as "01" channels finds no row, and neither does one reported as "2.0". Reading
them as numbers would answer where a string lookup declines.

The layout an Opus encode carries for a channel count is settled here as well:
ffmpeg's libopus wrapper takes one layout per count, and every command that
encodes Opus past stereo needs that same decision.
"""

__all__ = ["COLUMNS", "WIDEST_LAYOUT", "audio_bitrate", "audio_opus_layout",
           "opus_channel_args"]

# A column with "-" in it means "leave this alone", not "zero".
_NOT_SET = "-"

COLUMNS = ("normal", "comment")

# channels -> (normal, comment), in kbit/s. 8 channels is the practical ceiling:
# ffmpeg's libopus wrapper rejects layouts above 7.1, so a row beyond 8 could
# never encode.
_TABLE = {
    "1": ("100", "55"),
    "2": ("120", "65"),
    "3": ("150", "85"),
    "4": ("185", "110"),
    "5": ("220", "130"),
    "6": ("250", "150"),
    "7": ("285", "175"),
    "8": ("320", "200"),
}

# The explicit channel layout ffmpeg's libopus wrapper accepts for a channel
# count. 3 and 4 are the only counts that need one: `-ac` alone lands them on
# ffmpeg's defaults for those counts - 2.1 and 4.0 - neither of which the encoder
# accepts, while every other count's default is exactly the layout it wants.
_LAYOUTS = {
    "3": "3.0",     # front left, front right, front centre
    "4": "quad",    # front left, front right, back left, back right
}


def audio_bitrate(channels: str, column: str) -> str | None:
    """The target bitrate for ``channels``, or None where there is none.

    None covers all three ways of having no answer - a channel count with no row,
    a column that is not one of :data:`COLUMNS`, and a row whose column is unset -
    because the callers read all three alike: as no answer, not as an error.
    """
    row = _TABLE.get(channels)
    if row is None or column not in COLUMNS:
        return None
    value = row[COLUMNS.index(column)]
    return None if value == _NOT_SET else value


def audio_opus_layout(channels: str) -> str | None:
    """The layout to hand the encoder, or None.

    The caller must also make sure the SOURCE carries no LFE - 3.0 for three
    channels, 4.0 or quad for four - or a 2.1 / 3.1 source's LFE would be
    silently remapped onto a regular speaker.
    """
    return _LAYOUTS.get(channels)


# The widest layout the table has a row for, and the widest ffmpeg's libopus
# wrapper encodes at all: 7.1. A source wider than it has no layout to be kept
# at, and is folded down to it.
WIDEST_LAYOUT = 8


def opus_channel_args(channels: int, layout: str = "") -> tuple:
    """(channels, argv): how many channels an Opus encode of this source comes
    out with, and the ffmpeg options that get it there. <layout> is the
    source's own, and is read only for three and four channels.

    ffmpeg's libopus wrapper encodes past stereo only in the Vorbis channel
    orders - one fixed layout per count, 5.1 for six channels - and refuses to
    open on anything else, with nothing written at all. That is not a rare
    corner: the 5.1(side) that EAC3 (and so every Atmos audiobook) decodes to is
    the SIDE variant, and so are 5.0(side), 6.1(back) and 7.1(wide); none is
    what the encoder wants, and without this each of them was left unconverted
    with the reason thrown away on a silenced stderr.

    From five channels up `-ac` with the source's own count is the whole fix:
    ffmpeg's default layout for 5 to 8 channels is exactly the Vorbis one, so
    asking for the count re-labels side speakers as back ones (a 5.1(side)
    comes out 5.1, every channel at full level) and leaves a layout that was
    already right alone. Three and four are the exception the shared bitrate
    table already knows: their defaults, 2.1 and 4.0, are refused too, so the
    layout is named outright - but only for a source with no LFE, since naming
    3.0 over a 2.1 would play its LFE out of a full-range speaker. A 2.1 or 3.1,
    or one whose layout the probe cannot say, is downmixed to stereo instead.
    Past eight channels Opus has no layout to offer at all, so the source is
    folded down to 7.1 - a book encoded narrower than its source rather than
    not encoded.

    Stereo and below need nothing, and get nothing: their call is the one every
    existing library was made with.
    """
    if channels <= 2:
        return channels, []
    if channels > WIDEST_LAYOUT:
        return WIDEST_LAYOUT, ["-ac", str(WIDEST_LAYOUT)]
    named = audio_opus_layout(str(channels))
    if named is None:
        return channels, ["-ac", str(channels)]
    if (channels, layout) in ((3, "3.0"), (4, "4.0"), (4, "quad"),
                              (4, "quad(side)")):
        return channels, ["-channel_layout", named]
    return 2, ["-ac", "2"]
