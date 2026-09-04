"""The audio and video census rows.

One probe per file. Everything the audio report needs comes out of ONE ffprobe
call, and everything the video report needs out of that same call plus one
mediainfo call - both metadata reads, no decoding and no seeking, which is what
makes a census of a whole library on a spinning disk finish.

The suffix is a CLAIM and not a fact, because a census reads a tree nobody has
cleaned. Three claims would put a row in the wrong report and are refused: an
audio suffix over a file with no audio stream, an audio suffix over a file that
holds video (an .mka with a video track is a video), and a video suffix over a
file with no video stream. Cover art is not such a case - an embedded cover is a
video stream whose disposition is attached_pic, a picture and not a track, and it
is filtered out before any of that is judged.

The two jq programs are modelled here rather than shelled out to, and where jq's
own evaluation DIES or produces an empty stream this module raises: the shell
reads the filter's output with one ``read`` and swallows its status, so a filter
that says nothing leaves every field empty - which the gates below then read as
"no audio stream" or "no video track". Those are not the same thing as an
unreadable file, and the difference is a skip reason.
"""

import json
import math
import os
import subprocess

from medialib.lib import census, dolbyvision, videobitrate
from medialib.lib.enums import lower_extension_of, shell_lower

__all__ = [
    "census_probe_json",
    "census_audio_row",
    "census_video_row",
    "census_bitrate_adequacy",
    "census_container_name",
    "census_dynamic_range",
]

# jq's empty stream, which is not the same as its null: a `tonumber?` over a
# string that is not a number produces NO value, and a binding over no value
# makes the whole filter produce no output.
_EMPTY = object()


class _NoOutput(Exception):
    """The filter stated nothing - jq died on the arithmetic, or a binding was
    handed an empty stream. Either way stdout is empty and every field the shell
    reads out of it stays unset."""

def _alt(left, right):
    """jq's ``left // right``: the left unless it is null, false or empty."""
    if left is _EMPTY or left is None or left is False:
        return right
    return left


def _alt_lazy(*thunks):
    """jq's ``a // b // c`` with its LAZINESS, which is not a detail: an
    alternative jq never reaches is an alternative that cannot fail, and two of
    the spots this filter uses `//` in have a right-hand side that dies on a
    field the left-hand side made irrelevant. A stream that states its bitrate is
    never asked how many channels it has.

    The value of the last alternative reached is the answer, whatever it is -
    including null, false and the empty stream.
    """
    value = None
    for thunk in thunks:
        value = thunk()
        if value is _EMPTY or value is None or value is False:
            continue
        return value
    return value


def _bind(value):
    """``value as $name``: an empty stream here takes the whole filter with it."""
    if value is _EMPTY:
        raise _NoOutput()
    return value


def _num(value):
    """The filter's ``num($x)``: null for null and the empty string, the whole
    value as a number, or jq's EMPTY stream for a string ``tonumber?`` cannot
    read - never a numeric prefix."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return _EMPTY
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return _EMPTY


def _tostring(value):
    """jq's ``tostring``. A number keeps the spelling the document gave it, which
    is what json.loads already preserves for the integers and decimals ffprobe
    writes."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    return str(value)


def _mul(left, right):
    """jq's ``*``, whose (string, number) case REPEATS the string rather than
    failing - which is how a channels field of "two" becomes a very long string
    instead of an error, and takes the subtraction below down with it."""
    if isinstance(left, str) or isinstance(right, str):
        text, count = (left, right) if isinstance(left, str) else (right, left)
        if not isinstance(count, (int, float)) or isinstance(count, bool):
            raise _NoOutput()
        return None if count <= 0 else text * int(count)
    if isinstance(left, bool) or isinstance(right, bool) \
            or not isinstance(left, (int, float)) \
            or not isinstance(right, (int, float)):
        raise _NoOutput()
    return left * right


def _sub(left, right):
    """jq's ``-``, which refuses anything but two numbers."""
    for side in (left, right):
        if isinstance(side, bool) or not isinstance(side, (int, float)):
            raise _NoOutput()
    return left - right


def _add(values):
    """jq's ``add`` over an array: null for an empty one, and the sum otherwise -
    which for a list of strings is a concatenation."""
    if not values:
        return None
    total = values[0]
    for value in values[1:]:
        if isinstance(total, str) or isinstance(value, str):
            if not (isinstance(total, str) and isinstance(value, str)):
                raise _NoOutput()
            total = total + value
        else:
            total = _add_numbers(total, value)
    return total


def _add_numbers(left, right):
    """jq's ``+`` over two values it will accept as numbers."""
    for side in (left, right):
        if isinstance(side, bool) or not isinstance(side, (int, float)):
            raise _NoOutput()
    return left + right


def _streams(document, kind):
    """``[ .streams[]? | select(.codec_type == <kind>) ]``. The ``[]?`` walks
    over a streams field that is not an array; a stream that is not an object
    makes the select itself fail, which is the whole filter."""
    streams = document.get("streams")
    if not isinstance(streams, list):
        return []
    kept = []
    for stream in streams:
        if not isinstance(stream, dict):
            raise _NoOutput()
        if stream.get("codec_type") == kind:
            kept.append(stream)
    return kept


def _pictures_removed(streams):
    """``select((.disposition.attached_pic // 0) == 0)`` - an embedded cover is a
    picture and not a track."""
    kept = []
    for stream in streams:
        disposition = stream.get("disposition")
        attached = None
        if isinstance(disposition, dict):
            attached = disposition.get("attached_pic")
        elif disposition is not None:
            raise _NoOutput()
        if _alt(attached, 0) == 0:
            kept.append(stream)
    return kept


def _bps(stream):
    """The value of the first ``bps``-prefixed tag, case-insensitively, or "".

    Matroska states no per-stream bitrate; mkvmerge writes a per-track "BPS" tag
    instead (and "BPS-eng" and friends, one per language), whose suffix spelling
    is not fixed - so it is matched on the prefix alone. The filter's own ``// ""``
    is why this never answers null.
    """
    tags = stream.get("tags") if isinstance(stream, dict) else None
    if isinstance(tags, dict):
        for key, value in tags.items():
            if str(key).lower().startswith("bps"):
                return _alt(value, "")
    elif tags is not None:
        raise _NoOutput()
    return ""


def _chapter_count(document):
    chapters = document.get("chapters")
    if chapters is None:
        return 0
    if not isinstance(chapters, list):
        raise _NoOutput()
    return len(chapters)


def _format_field(document, name):
    section = document.get("format")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise _NoOutput()
    return section.get(name)


def _field(stream, name):
    return stream.get(name) if isinstance(stream, dict) else None


# --- the probe ------------------------------------------------------------------


def census_probe_json(path):
    """ffprobe's whole answer about a file as JSON text, or "" for a file it
    cannot read. Format, streams and chapters in one call."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", "-show_chapters", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
    except (OSError, ValueError):
        return ""
    # `|| true`: the status is discarded, so a probe that printed something and
    # then failed is read for what it printed.
    return proc.stdout.decode("utf-8", "replace")


def _document(text):
    """The probe's answer as a document, or None when it is not one - which the
    filter reads as no output at all rather than as an unreadable file."""
    try:
        data = json.loads(text)
    except ValueError as unreadable:
        raise _NoOutput() from unreadable
    if not isinstance(data, dict):
        raise _NoOutput()
    return data


# --- the audio row --------------------------------------------------------------

_AUDIO_FIELDS = 7


def _audio_filter(text):
    """The audio row's jq program: seven fields, or _NoOutput when the filter
    states nothing."""
    document = _document(text)
    audio = _streams(document, "audio")
    video = _pictures_removed(_streams(document, "video"))
    first = _bind(audio[0] if audio else {})
    return [
        _tostring(_alt(_format_field(document, "duration"), "")),
        _tostring(_alt_lazy(lambda: _format_field(document, "bit_rate"),
                            lambda: _field(first, "bit_rate"),
                            lambda: _bps(first))),
        _tostring(_alt(_field(first, "channels"), "")),
        _tostring(_alt(_field(first, "codec_name"), "")),
        _tostring(_chapter_count(document)),
        _tostring(len(audio)),
        _tostring(len(video)),
    ]


def census_audio_row(path, separator=None):
    """The audio report's row for <path> - path, size, duration, bitrate,
    channels, codec, chapters - or ``(None, reason)``.

    The bitrate is the CONTAINER's overall one rather than the audio stream's: it
    is the number actually stated for every format this list holds, and over a
    spoken-word file it differs from the stream's by the cover art alone.
    """
    if separator is None:
        separator = os.environ.get("CENSUS_SEP", census.DEFAULT_SEPARATOR)
    text = census_probe_json(path)
    if not text:
        return None, "ffprobe could not read it (not audio, or truncated)"
    try:
        fields = _audio_filter(text)
    except _NoOutput:
        fields = [""] * _AUDIO_FIELDS
    duration, bitrate, channels, codec, chapters, audio_streams, \
        video_streams = fields

    if (audio_streams or "0") == "0":
        return None, "its suffix says audio but it holds no audio stream"
    if video_streams and video_streams != "0":
        return None, "its suffix says audio but it holds a video track"

    row = census.join([path, census.file_size(path),
                       census.to_seconds(duration), census.to_int(bitrate),
                       census.to_int(channels), codec,
                       census.to_chapters(chapters)], separator)
    return row, None


# --- the video row --------------------------------------------------------------

_VIDEO_FIELDS = 18


def _video_filter(text):
    """The video row's jq program: eighteen fields, or _NoOutput.

    The estimate is the one piece of arithmetic in it. A container that states no
    video bitrate - which is most of a Matroska library - gets one worked out from
    the overall rate less the audio, at 98%; and the spots where jq's own
    arithmetic dies on a field it cannot read are spots where the whole filter
    states nothing.
    """
    document = _document(text)
    video = _pictures_removed(_streams(document, "video"))
    audio = _streams(document, "audio")
    subtitle = _streams(document, "subtitle")
    v0 = _bind(video[0] if video else {})
    a0 = _bind(audio[0] if audio else {})

    stated_video = _bind(_alt_lazy(lambda: _num(_field(v0, "bit_rate")),
                                   lambda: _num(_bps(v0))))

    audio_bits = []
    for stream in audio:
        value = _alt_lazy(lambda s=stream: _num(_field(s, "bit_rate")),
                          lambda s=stream: _num(_bps(s)),
                          lambda s=stream: _mul(
                              _alt(_field(s, "channels"), 2), 64000))
        if value is _EMPTY:
            raise _NoOutput()
        audio_bits.append(value)
    audio_total = _alt(_add(audio_bits), 0)

    def computed():
        """``if (num(.format.duration) // 0) > 0 and num(.format.size) != null
        then num(.format.size) * 8 / num(.format.duration) else null end``.

        The `and` short-circuits, so a duration that cannot be read settles the
        condition as false and the size is never looked at. A duration that CAN
        and a size that cannot is the other way round: the comparison is made
        against an empty stream, which makes the whole `if` produce nothing - and
        a binding over nothing takes the filter with it.
        """
        duration = _alt(_num(_format_field(document, "duration")), 0)
        if not _gt(duration, 0):
            return None
        size = _num(_format_field(document, "size"))
        if size is _EMPTY:
            raise _NoOutput()
        if size is None:
            return None
        return size * 8 / duration

    total_bits = _bind(_alt_lazy(
        lambda: _num(_format_field(document, "bit_rate")), computed))

    if stated_video is not None or total_bits is None:
        estimated = None
    else:
        estimated = _mul(_sub(total_bits, audio_total), 0.98)

    return [
        _tostring(_alt(_format_field(document, "duration"), "")),
        _tostring(_alt_lazy(lambda: _field(v0, "bit_rate"),
                            lambda: _bps(v0))),
        (_tostring(math.floor(estimated))
         if _alt(estimated, 0) is not None and _gt(_alt(estimated, 0), 0)
         else ""),
        _tostring(_alt(_field(v0, "width"), "")),
        _tostring(_alt(_field(v0, "height"), "")),
        _tostring(_alt(_field(v0, "avg_frame_rate"), "")),
        _tostring(_alt(_field(v0, "r_frame_rate"), "")),
        _tostring(_alt(_field(v0, "codec_name"), "")),
        _tostring(_alt(_format_field(document, "format_name"), "")),
        _tostring(_alt(_field(v0, "color_transfer"), "")),
        _tostring(_dovi_records(v0)),
        _tostring(len(audio)),
        _tostring(_alt(_field(a0, "channels"), "")),
        _tostring(_alt(_field(a0, "codec_name"), "")),
        _tostring(_alt_lazy(lambda: _field(a0, "bit_rate"),
                            lambda: _bps(a0))),
        _tostring(len(subtitle)),
        _tostring(_chapter_count(document)),
        _tostring(len(video)),
    ]


def _gt(left, right):
    """jq's ``>``, which orders across types rather than refusing - but the only
    comparison this filter makes is a number against 0."""
    if isinstance(left, bool) or not isinstance(left, (int, float)):
        return False
    return left > right


def _dovi_records(stream):
    """How many "DOVI configuration record" side-data entries the stream has."""
    side_data = _field(stream, "side_data_list")
    if side_data is None:
        return 0
    if not isinstance(side_data, list):
        raise _NoOutput()
    count = 0
    for entry in side_data:
        if not isinstance(entry, dict):
            raise _NoOutput()
        if entry.get("side_data_type") == "DOVI configuration record":
            count += 1
    return count


def census_video_row(path, separator=None):
    """The video report's row for <path>, or ``(None, reason)``.

    "First audio track" is a:0 - the first audio stream in container order, the
    one a player picks when nothing else decides. The subtitle count is of
    EMBEDDED tracks; a sidecar beside the film is a separate file. The resolution
    is the CODED size, and the frame rate the AVERAGE one, because the container's
    nominal rate is doubled for anything field-coded and would report a 25fps
    interlaced broadcast as 50fps.
    """
    if separator is None:
        separator = os.environ.get("CENSUS_SEP", census.DEFAULT_SEPARATOR)
    text = census_probe_json(path)
    if not text:
        return None, "ffprobe could not read it (not video, or truncated)"
    try:
        fields = _video_filter(text)
    except _NoOutput:
        fields = [""] * _VIDEO_FIELDS
    (duration, video_bitrate, estimated, width, height, avg_rate, base_rate,
     video_codec, format_name, transfer, dv_records, audio_tracks, a_channels,
     a_codec, a_bitrate, subtitle_tracks, chapters, video_streams) = fields

    if (video_streams or "0") == "0":
        return None, "its suffix says video but it holds no video track"

    duration = census.to_seconds(duration)
    video_bitrate = census.to_int(video_bitrate)
    estimated = census.to_int(estimated)
    width = census.to_int(width)
    height = census.to_int(height)
    audio_tracks = census.to_int(audio_tracks)
    a_channels = census.to_int(a_channels)
    a_bitrate = census.to_int(a_bitrate)
    subtitle_tracks = census.to_int(subtitle_tracks)
    chapters = census.to_chapters(chapters)

    resolution = "%sx%s" % (width, height) if width and height else ""
    avg_rate = census.to_frame_rate(avg_rate)
    base_rate = census.to_frame_rate(base_rate)
    frame_rate = avg_rate or base_rate

    row = census.join(
        [path, census.file_size(path), duration, video_bitrate,
         census_bitrate_adequacy(path, video_codec, width, height, frame_rate,
                                 video_bitrate or estimated),
         resolution, frame_rate, video_codec,
         census_container_name(format_name, lower_extension_of(path)),
         census_dynamic_range(path, transfer, dv_records or "0"),
         audio_tracks, a_channels, a_codec, a_bitrate, subtitle_tracks,
         chapters], separator)
    return row, None


# --- the one compound column ----------------------------------------------------


def census_bitrate_adequacy(path, codec, width, height, fps, bits):
    """What that video stream's bitrate IS for what the file is - "starved",
    "adequate", "generous", or "unknown" when it cannot be judged.

    The one judgement in this census rather than a reading, made against the model
    in ``videobitrate`` - the same model, tables and boundaries convertVideo's -t
    decides a single file with, so a library counted here as starved is the set of
    files that test refuses to re-encode. Grain is the one axis a census cannot
    afford, because measuring it is a decode per file; it is asked for through
    ``source_bitrate_grain`` with the probe switched off, which is the model's
    most generous reading and never calls a file starved that a grain-aware run
    would not.
    """
    if not width or not height or not str(bits).isdigit():
        return "unknown"
    grain = videobitrate.source_bitrate_grain(path, probe_enabled=0)
    adequate = videobitrate.adequate_video_bitrate(codec, width, height,
                                                   fps or "0", grain)
    if not adequate:
        return "unknown"
    return videobitrate.bitrate_verdict(str(int(bits) // 1000), adequate)


def census_container_name(format_name, extension):
    """What the file really is, as one word: "matroska", "mp4", "webm", "avi",
    "mpegts", ...

    Taken from ffprobe's format_name rather than from the suffix, because the
    suffix is the claim this column exists to check. format_name is a DEMUXER
    name though, and one demuxer serves a family, which it reports as a
    comma-separated list - so a suffix that is one of the family's members is the
    answer, and otherwise the family is named by the member everyone calls it by.
    """
    formats = shell_lower(format_name)
    extension = shell_lower(extension)
    if not formats:
        return extension
    # Both sides padded with the separator, so a whole entry has to match: "mp4"
    # must not match inside "mp4a".
    if extension and ("," + extension + ",") in ("," + formats + ","):
        return extension
    if "matroska" in formats:
        return "matroska"
    if formats.startswith("mov,mp4"):
        return "mp4"
    return formats.split(",")[0]


def census_dynamic_range(path, transfer, dv_records="0"):
    """The file's dynamic range as one of a small enum - SDR, HLG, HDR10, HDR10+,
    DolbyVision, DolbyVision+HDR10, DolbyVision+HLG.

    Read through ``dolbyvision``, which is where this repo already keeps the two
    judgements involved: does the container CLAIM Dolby Vision, and is the
    BITSTREAM really high dynamic range. Whether an RPU is really there is NOT
    checked - that costs a pass over the whole video stream, which would turn a
    census of a library into a re-read of it. A census records what the files say
    about themselves.

    Without mediainfo the same enum is derived from the single ffprobe pass: the
    colour transfer function, and whether a DOVI configuration record is
    attached. That misses HDR10+ and reports it as HDR10, which is what such a
    file also is.
    """
    transfer = shell_lower(transfer)
    dv = False
    hdr = False
    hdr10plus = False
    hlg = False

    if os.environ.get("CENSUS_HAVE_MEDIAINFO", ""):
        info = dolbyvision.read_video_info(path)
        dv = dolbyvision.claims_dolby_vision(info["PROFILE"], info["SETTINGS"])
        hdr = dolbyvision.is_hdr(info["TRANSFER"], info["HDR"])
        formats = shell_lower(info["HDR"])
        media_transfer = shell_lower(info["TRANSFER"])
        if "2094" in formats or "hdr10+" in formats:
            hdr10plus = True
        if "hlg" in media_transfer + formats:
            hlg = True
    else:
        if str(dv_records).isdigit() and int(dv_records) > 0:
            dv = True
        if "2084" in transfer or "pq" in transfer:
            hdr = True
        if "b67" in transfer or "hlg" in transfer:
            hlg = True
            hdr = True

    if dv:
        if hlg:
            return "DolbyVision+HLG"
        if hdr:
            return "DolbyVision+HDR10"
        return "DolbyVision"
    if hdr10plus:
        return "HDR10+"
    if hlg:
        return "HLG"
    if hdr:
        return "HDR10"
    return "SDR"
