"""What a convert-video source is: its geometry, frame rate, colour, HDR and
Dolby Vision signalling, how long it runs, and whether it is interlaced.

Each answer is one ffprobe or mediainfo call read straight into the shape
the decisions in `medialib/cli/convert_video.py` want. A probe that cannot
run answers empty rather than failing, and the decision that reads it
takes the conservative branch.
"""

import re
import subprocess

from medialib.lib import formatting

# How far the interlace measurement looks. Interlacing is a property of the SOURCE
# rather than of a scene, so the head of the file settles it, and the decode costs
# seconds against an encode measured in minutes.
INTERLACE_PROBE_FRAMES = "500"

# The share of idet's classified frames that may be interlaced before a source is
# not upscaled. Not zero: idet calls the odd fast pan or cross-fade on real film
# interlaced, and a telecined source sits at forty percent, nowhere near this.
UPSCALE_INTERLACE_TOLERANCE = 0.05


def _probe(argv: list) -> str:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    return done.stdout.decode("utf-8", "surrogateescape")


def video_dimensions(path: str) -> tuple:
    """``videoDimensions``: the first video stream's coded geometry as
    (width, height, field order, sample aspect), any field that could not be read
    left empty.

    The CODED size, not the display size, is what the encoder processes, so an
    anamorphic source is classified by its real pixel workload - and the pixel
    aspect that makes it anamorphic comes back here too, because a caller wants to
    WARN about it. The field order comes back in its place but is not what the
    interlace warning is made from: that is measured from the frames instead.
    """
    import json
    text = _probe(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries",
                   "stream=width,height,field_order,sample_aspect_ratio",
                   "-of", "json", path])
    try:
        streams = json.loads(text).get("streams") or [{}]
    except (ValueError, AttributeError):
        return "", "", "", ""
    first = streams[0] if isinstance(streams[0], dict) else {}
    return (_field(first, "width"), _field(first, "height"),
            _field(first, "field_order"), _field(first, "sample_aspect_ratio"))


def _field(document: dict, name: str) -> str:
    value = document.get(name)
    return "" if value is None else str(value)


def video_frame_rate(path: str) -> str:
    """``videoFrameRate``: the nominal frame rate as the exact fraction ffprobe
    reports, or "" when it cannot be read.

    The rate to REPLAY the frames at, which is the container's nominal one - the
    average is the frame count over the duration and drifts on a file whose end is
    short. Kept as a fraction: the NTSC rates are 24000/1001 and friends, and a
    rounded 23.976 drifts by about a tenth of a second over a feature.
    """
    text = _probe(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=r_frame_rate",
                   "-of", "default=noprint_wrappers=1:nokey=1", path])
    first = text.splitlines()[0] if text.splitlines() else ""
    return first if re.fullmatch(r"[1-9][0-9]*/[1-9][0-9]*", first) else ""


def video_color_args(path: str) -> str:
    """``videoColorArgs``: colour signalling copied from the source.

    Standalone options that work for every encoder and, for SDR and HDR alike,
    keep the output tagged as the input so players interpret the colours
    correctly. Unknown values are skipped.
    """
    wanted = (("color_primaries", "-color_primaries"),
              ("color_transfer", "-color_trc"),
              ("color_space", "-colorspace"),
              ("color_range", "-color_range"))
    out = ""
    for entry, flag in wanted:
        value = _probe(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=" + entry,
                        "-of", "default=nk=1:nw=1", path]).strip()
        if value not in ("", "unknown", "N/A"):
            out += " %s %s" % (flag, value)
    return out


def upscaled_color_args(color_args: str) -> str:
    """``upscaledColorArgs``: the colour signalling of an UPSCALED picture.

    The upscaler hands over its frames in the 709 matrix at limited range
    whatever the source was in, so those two are stated rather than copied - an
    SD source's 601 tag on an HD frame would be read as the wrong matrix. The
    primaries and the transfer describe the source's colours, which the
    upscaler does not touch, so they stay the source's; a source that named none
    gets 709, which is what a player assumes of an HD frame anyway, stated so it
    does not have to.
    """
    fields = color_args.split()
    stated = dict(zip(fields[::2], fields[1::2], strict=False))
    stated["-colorspace"] = "bt709"
    stated["-color_range"] = "tv"
    stated.setdefault("-color_primaries", "bt709")
    stated.setdefault("-color_trc", "bt709")
    return "".join(" %s %s" % (flag, stated[flag]) for flag in (
        "-color_primaries", "-color_trc", "-colorspace", "-color_range"))


def color_setparams(color_args: str) -> str:
    """``colorSetparams``: the same signalling as a filter that stamps it onto
    every frame.

    For frames that arrive with none, the way y4m out of the upscaler does: an
    ffmpeg that takes its colour description from the frames writes primaries
    and transfer from them even when the output options say otherwise, so on
    their own the options would come out as "unknown"."""
    names = {"-color_primaries": "color_primaries", "-color_trc": "color_trc",
             "-colorspace": "colorspace", "-color_range": "range"}
    fields = color_args.split()
    stated = dict(zip(fields[::2], fields[1::2], strict=False))
    return "setparams=" + ":".join("%s=%s" % (names[flag], stated[flag])
                                   for flag in names if flag in stated)


def video_color_space(path: str) -> tuple:
    """The first video stream's (colour space, range, transfer) as ffprobe names
    them, any it does not state left empty."""
    import json
    text = _probe(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=color_space,color_range,color_transfer",
                   "-of", "json", path])
    try:
        streams = json.loads(text).get("streams") or [{}]
    except (ValueError, AttributeError):
        return "", "", ""
    first = streams[0] if isinstance(streams[0], dict) else {}
    values = [_field(first, name)
              for name in ("color_space", "color_range", "color_transfer")]
    return tuple("" if value in ("unknown", "N/A") else value
                 for value in values)


def hdr_master_display(path: str) -> str:
    """``hdrMasterDisplay``: "<master-display> <maxCLL,maxFALL>" for a source that
    carries mastering-display metadata, and nothing otherwise.

    What decides it is whether the metadata IS THERE, not which HDR flavour the
    file is: PQ carries it as a matter of course and HLG needs none, but BT.2100
    permits an HLG stream to carry it and some graders write it. Copying whatever
    is present costs nothing on the files that have none.
    """
    import json
    transfer = _probe(["ffprobe", "-v", "error", "-select_streams", "v:0",
                       "-show_entries", "stream=color_transfer",
                       "-of", "default=nk=1:nw=1", path]).strip()
    if transfer not in ("smpte2084", "arib-std-b67"):
        return ""
    text = _probe(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-read_intervals", "%+#1", "-show_frames",
                   "-show_entries", "frame=side_data_list", "-of", "json",
                   path])
    try:
        frames = json.loads(text).get("frames") or []
        side_data = (frames[0] if frames else {}).get("side_data_list") or []
    except (ValueError, AttributeError, IndexError):
        return ""

    display = light = None
    for entry in side_data:
        if not isinstance(entry, dict):
            continue
        if entry.get("side_data_type") == "Mastering display metadata":
            display = display or entry
        elif entry.get("side_data_type") == "Content light level metadata":
            light = light or entry
    if display is None:
        return ""

    # The ffprobe side-data fraction NUMERATORS map straight onto the
    # G()B()R()WP()L() form the encoders expect - chroma coords /50000,
    # luminance /10000.
    def numerator(name):
        return str(display.get(name, "")).split("/")[0]

    master = ("G(%s,%s)B(%s,%s)R(%s,%s)WP(%s,%s)L(%s,%s)" % (
        numerator("green_x"), numerator("green_y"),
        numerator("blue_x"), numerator("blue_y"),
        numerator("red_x"), numerator("red_y"),
        numerator("white_point_x"), numerator("white_point_y"),
        numerator("max_luminance"), numerator("min_luminance")))
    if light is None:
        return master + " 0,0"
    return "%s %s,%s" % (master, light.get("max_content"),
                         light.get("max_average"))


def dolby_vision_profile(path: str) -> tuple:
    """``dolbyVisionProfile``: (profile, enhancement-layer flag) for a file whose
    video carries a Dolby Vision configuration record with an RPU, and ("", "")
    otherwise - so an empty profile means "not Dolby Vision".

    Used both to classify a source and to verify a finished output still signals
    it.
    """
    import json
    text = _probe(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries",
                   "stream_side_data=side_data_type,dv_profile,"
                   "rpu_present_flag,el_present_flag", "-of", "json", path])
    try:
        streams = json.loads(text).get("streams") or []
        side_data = (streams[0] if streams else {}).get("side_data_list") or []
    except (ValueError, AttributeError, IndexError):
        return "", ""
    for entry in side_data:
        if not isinstance(entry, dict):
            continue
        if entry.get("side_data_type") != "DOVI configuration record":
            continue
        if entry.get("rpu_present_flag", 0) != 1:
            continue
        return str(entry.get("dv_profile")), str(entry.get("el_present_flag", 0))
    return "", ""


def interlace_counts(path: str, decode_accel: str = "") -> tuple | None:
    """``interlaceCounts``: how ffmpeg's idet filter classified the head of this
    source, as (top field first, bottom field first, progressive) frame counts,
    or None when it could not be measured.

    MEASURED from the frames rather than read from the container's field-order
    flag, which this library's files carry wrong on progressive material. Flat
    fades and title cards, which idet cannot tell apart either way, are in none
    of the three. A source that cannot be decoded is None rather than an error,
    because the encode that follows is what reports a broken file.
    """
    argv = ["ffmpeg", "-nostdin", "-hide_banner"] + decode_accel.split()
    argv += ["-i", path, "-frames:v", INTERLACE_PROBE_FRAMES, "-an",
             "-filter:v", "idet", "-f", "null", "-"]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
        text = done.stderr.decode("utf-8", "surrogateescape")
    except OSError:
        return None

    counts = re.findall(r"Multi frame detection: TFF:\s*(\d+)\s+BFF:\s*(\d+)"
                        r"\s+Progressive:\s*(\d+)", text)
    if not counts:
        return None
    tff, bff, progressive = (int(value) for value in counts[-1])
    return tff, bff, progressive


def interlace_verdict_of(counts: tuple | None) -> str:
    """The verdict the counts make: the MAJORITY of the frames idet could
    classify. A tie, or nothing it could classify at all, is unknown."""
    if not counts:
        return "unknown"
    tff, bff, progressive = counts
    if tff + bff > progressive:
        return "interlaced"
    if progressive > tff + bff:
        return "progressive"
    return "unknown"


def interlace_verdict(path: str, decode_accel: str = "") -> str:
    """``interlaceVerdict``: whether this source is interlaced, measured from its
    own frames - :func:`interlace_counts` read by :func:`interlace_verdict_of`."""
    return interlace_verdict_of(interlace_counts(path, decode_accel))


def upscale_blocked_by_fields(counts: tuple | None) -> str:
    """Why the fields of this source rule an upscale out, or "" when they do not.

    A much stricter question than the warning's majority. A telecined film is
    progressive in three frames out of five and woven from two films' fields in
    the other two - a majority verdict calls it progressive, and the network would
    then enlarge the combing in two frames of every five into the picture. So an
    upscale wants the frames idet could classify to be progressive nearly without
    exception, and a source it could not classify at all is not taken on trust.
    """
    if not counts:
        return "its fields could not be measured"
    tff, bff, progressive = counts
    classified = tff + bff + progressive
    if classified == 0:
        return "its fields could not be measured"
    woven = (tff + bff) / classified
    if woven > UPSCALE_INTERLACE_TOLERANCE:
        return ("%.0f%% of its measured frames are interlaced or telecined"
                % (woven * 100))
    return ""


def _dimensions_line(path: str) -> str:
    """The geometry as ``videoDimensions`` PRINTS it, which is what the grain
    probe reads: it splits the line and takes the first two fields."""
    return " ".join(video_dimensions(path))


def _media_duration(path: str) -> float:
    return formatting.awk_number(_probe([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1", path]).strip() or 0)


def _video_duration(path: str, container: float) -> float:
    """How long the source's PICTURE runs, which the video-only intermediate is
    measured against.

    Not the container's length: that is its longest stream, and an audio track
    running seconds past the last frame - a dub mastered from another cut - is
    common, while the video intermediate can only ever be as long as the picture.
    Matroska keeps the figure in a DURATION tag, MP4 as the stream's duration;
    a source stating neither, or one past the container, falls back to the
    container's.
    """
    lines = _probe([
        "ffprobe", "-v", "error", "-select_streams", "V:0",
        "-show_entries", "stream=duration:stream_tags=DURATION",
        "-of", "default=nk=1:nw=1", path]).split()
    for line in lines:
        parts = line.split(":")
        if not all(formatting.awk_looks_numeric(part) for part in parts):
            continue
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + formatting.awk_number(part)
        if 0 < seconds <= container:
            return seconds
    return container
