"""The shared Opus target bitrates, one row per channel configuration.

The shell held it as a string, because bash cannot export an array into the
``bash -c`` workers that read it. Here it is a mapping, and the only copy.

Channel counts are strings, not numbers, because the shell compares them as text:
a track reported as "01" channels finds no row, and neither does one reported as
"2.0". Reading them as numbers here would answer where the shell declines to.
"""

__all__ = ["COLUMNS", "audio_bitrate", "audio_opus_layout"]

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
    because the shell prints nothing and succeeds for each of them alike.
    """
    row = _TABLE.get(channels)
    if row is None or column not in COLUMNS:
        return None
    value = row[COLUMNS.index(column)]
    return None if value == _NOT_SET else value


def audio_opus_layout(channels: str) -> str | None:
    """``audioOpusLayout``: the layout to hand the encoder, or None.

    The caller must also make sure the SOURCE carries no LFE - 3.0 for three
    channels, 4.0 or quad for four - or a 2.1 / 3.1 source's LFE would be
    silently remapped onto a regular speaker.
    """
    return _LAYOUTS.get(channels)
