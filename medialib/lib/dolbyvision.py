"""The Dolby Vision normalisation helpers.

The readers return their results in memory as a dict with the DV_* names (like
the bash RET_* pair) so a caller needs no subshell and no second probe; the
three pure gates judge the fields the readers settle; and the two pipelines -
profile 7 -> 8.1, and the video-stream copy that drops a false claim - run the
same ffmpeg and dovi_tool commands the bash copy spells, through a fakeable
``run`` so the white box can drive them with stubs.
"""

import json
import os
import re
import subprocess
import tempfile

from medialib.lib import dolbyvisionlevel

_INT = re.compile(r"^[0-9]+$")
_FPS = re.compile(r"^[0-9]+([.][0-9]+)?$")

# The NTSC rates mediainfo reports rounded ("23.976"), mapped back to the exact
# fraction mkvmerge's --default-duration takes - the rounding error alone
# drifts by ~0.1s over a feature-length film. Matched as a prefix, the way the
# bash case's "23.976*" glob matches, so "23.9765" lands where "23.976" does.
_NTSC = (
    ("23.976", "24000/1001fps"),
    ("29.97", "30000/1001fps"),
    ("47.952", "48000/1001fps"),
    ("59.94", "60000/1001fps"),
    ("119.88", "120000/1001fps"),
)

# The eight values the reader's jq program prints, in its order.
_FIELDS = ("HDR_Format_Profile", "HDR_Format_Settings", "HDR_Format",
           "transfer_characteristics", "FrameRate", "FrameRate_Num",
           "FrameRate_Den", "StreamSize")


def _field(value):
    """One field as the reader's ``(.X // "") | tostring`` prints it: null and
    false read as empty, a string as itself, a number as its digits (an integer
    or the decimal form the document carried - a port of jq's literal-preserving
    numbers, which is the domain mediainfo's fields live in), and anything else
    as its JSON."""
    if value is None or value is False:
        return ""
    if value is True:
        return "true"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


def _track_items(track):
    """What ``.media.track[]?`` iterates: the list's entries, a mapping's
    values; a string, number or null iterates to nothing (the ``?`` swallows
    the index error), so a document without a usable track list names no
    video."""
    if isinstance(track, list):
        return list(track)
    if isinstance(track, dict):
        return list(track.values())
    return []


def _first_video_track(data):
    """``[.media.track[]? | select(."@type" == "Video")][0] // {}``, with the
    jq error semantics the reader's ``2>/dev/null`` hides: a top level that is
    not an object, a non-object ``media``, or a track entry that is not an
    object or null (a string, a number, a boolean) errors the whole probe - the
    reader then reads every field as empty, even when an earlier entry already
    named the video. Everything else settles on the first video entry, or on no
    video at all, which the reader reads the same way."""
    if not isinstance(data, dict):
        return None
    media = data.get("media")
    if media is not None and not isinstance(media, dict):
        return None
    track = media.get("track") if isinstance(media, dict) else None
    video = None
    for item in _track_items(track):
        if item is not None and not isinstance(item, dict):
            return None
        if video is None and isinstance(item, dict) and item.get("@type") == "Video":
            video = item
    return video if video is not None else {}


def _fps_spec(fps, fps_num, fps_den):
    """The frame rate as an mkvmerge --default-duration value: the exact
    numerator/denominator mediainfo reports when it gives both (compared as
    strings, the way the bash test does - so a denominator of "00" stands and
    one of "0" does not), else the rounded decimal mapped back to its NTSC
    fraction, else the decimal as given, else nothing at all."""
    if _INT.match(fps_num) and _INT.match(fps_den) and fps_den != "0":
        return "%s/%sfps" % (fps_num, fps_den)
    if _FPS.match(fps):
        for prefix, spec in _NTSC:
            if fps.startswith(prefix):
                return spec
        return fps + "fps"
    return ""


def _subprocess_run(argv, **kwargs):
    """The real runner: subprocess.run, with one extension the pipelines
    need - a bytes value for stdin, the captured stdout of the previous tool
    in a pipe, which subprocess would not take."""
    stdin = kwargs.get("stdin")
    if isinstance(stdin, (bytes, bytearray)):
        read_fd, write_fd = os.pipe()
        data = bytes(stdin)
        while data:
            data = data[os.write(write_fd, data):]
        os.close(write_fd)
        kwargs["stdin"] = read_fd
        try:
            return subprocess.run(list(argv), **kwargs)
        finally:
            os.close(read_fd)
    return subprocess.run(list(argv), **kwargs)


def read_video_info(path, run=_subprocess_run):
    """dvReadVideoInfo: the DV state and frame rate of <path>'s video track,
    in one mediainfo pass, as the six DV_* values. A file mediainfo cannot
    read, one that prints nothing, and one whose JSON the probe cannot
    navigate all yield the empty values, which the gates below read as
    "nothing to convert".

    ``run`` stands in for the runner - (argv, **kwargs) to a result with
    returncode/stdout/stderr, where stdin may be a bytes value (the pipe
    from the previous tool), a file, or DEVNULL - so the white box can feed
    the reader its canned JSON.
    """
    empty = {"PROFILE": "", "SETTINGS": "", "HDR": "", "TRANSFER": "",
             "FPS_SPEC": "", "STREAM_SIZE": ""}
    proc = run(["mediainfo", "--Output=JSON", path],
               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return empty
    try:
        text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return empty
    text = text.rstrip("\n")
    if not text:
        return empty
    try:
        data = json.loads(text)
    except ValueError:
        return empty
    video = _first_video_track(data)
    if video is None:
        return empty
    values = [_field(video.get(name)) for name in _FIELDS]
    profile, settings, hdr, transfer, fps, fps_num, fps_den, size = values
    # mediainfo omits StreamSize for some muxes; an unusable value must read
    # as "unknown", never as zero, which a capacity check would take for
    # "fits".
    if not _INT.match(size):
        size = ""
    return {"PROFILE": profile, "SETTINGS": settings, "HDR": hdr,
            "TRANSFER": transfer,
            "FPS_SPEC": _fps_spec(fps, fps_num, fps_den),
            "STREAM_SIZE": size}


def is_profile7(profile, settings):
    """dvIsProfile7: true only for a real dual-layer Dolby Vision profile 7 -
    the gate for the one thing that can be converted. Profile 5 and the
    already single-layer profile 8.x are rejected up front, whatever the
    settings field claims, so neither a mislabelled file nor a second run can
    ever reach the converter."""
    profile = profile.lower()
    settings = settings.upper()
    if profile.startswith(("dvhe.05", "dvh1.05", "dvav.05",
                           "dvhe.08", "dvh1.08", "hev1.08")):
        return False
    if profile.startswith("dvhe.07"):
        return True
    return "BL+EL+RPU" in settings


def claims_dolby_vision(profile, settings):
    """dvClaimsDolbyVision: true when the container claims Dolby Vision at
    all, whatever profile it names - the wider gate that decides whose
    bitstream gets probed for an RPU, so a false profile 5 or 8 claim is
    caught too. Any of the codec-string spellings a mux can report counts,
    and the settings field is enough on its own: only Dolby Vision ever puts
    an RPU in there."""
    profile = profile.lower()
    settings = settings.upper()
    if profile.startswith(("dvhe.", "dvh1.", "dvav.", "dva1.", "hev1.",
                           "hvc1.")):
        return True
    return "RPU" in settings


def is_hdr(transfer, formats):
    """dvIsHdr: true when the bitstream really is HDR. The transfer function
    decides it - PQ and HLG are the HDR ones, everything else is SDR whatever
    the container claims; HDR_Format is only a fallback for a file that names
    a format without a transfer function, with the Dolby Vision entry cut out
    first: that entry is exactly the claim under suspicion, and what may be
    left is matched by name rather than by "anything at all remains",
    because the field can carry Dolby Vision's own version and layer details
    along, and none of that makes a file HDR."""
    transfer = transfer.lower()
    if any(token in transfer for token in ("pq", "2084", "hlg", "2100")):
        return True
    formats = formats.lower().replace("dolby vision", "")
    return any(token in formats
               for token in ("2086", "2094", "hdr10", "hlg", "pq"))


def is_profile8(path, run=_subprocess_run):
    """dvIsProfile8: true when <path> reports Dolby Vision profile 8 - the
    check a finished remux passes before anything on disk is touched. The
    probe runs in a subshell on the bash side so it cannot clobber the
    caller's DV_* values; what it saw is the second return (DV_SEEN_PROFILE)
    so a rejection can say what the file actually claimed. Matched as a
    substring, not a prefix: mediainfo renders one field per HDR format
    joined with " / ", and nothing guarantees Dolby Vision is the first
    entry."""
    seen = read_video_info(path, run=run)["PROFILE"]
    return re.search(r"(dvhe|dvh1|hev1)\.08", seen.lower()) is not None, seen


def is_dolby_vision_free(path, source_hdr, run=_subprocess_run):
    """dvIsDolbyVisionFree: true when <path> claims no Dolby Vision any more
    AND is still exactly as HDR as the source was (source_hdr, "1" when the
    source really was HDR). Both halves have to hold before a stripped copy
    may replace anything: the first is the whole point of the strip, the
    second is what catches it taking real HDR10 metadata along. The second
    return is the (profile, settings, HDR-as-"1"/"0") triple the probe saw,
    so a rejection can name the half that failed."""
    info = read_video_info(path, run=run)
    hdr = "1" if is_hdr(info["TRANSFER"], info["HDR"]) else "0"
    seen = (info["PROFILE"], info["SETTINGS"], hdr)
    free = info["PROFILE"] == "" and "RPU" not in info["SETTINGS"].upper()
    return free and hdr == source_hdr, seen


def _pipeline_status(first, second):
    """The status of ``first | second`` under the callers' pipefail: the
    rightmost non-zero exit, zero when both pass."""
    return second if second else first


def stream_has_rpu(path, run=_subprocess_run, tmpdir=None):
    """dvStreamHasRpu: true when dovi_tool can actually FIND Dolby Vision RPU
    data in the video bitstream - the claim the container makes is checked
    against the 48 frames it can get cheaply, a fraction of a second even on a
    4.5 GB film. ``tmpdir`` is the ${TMPDIR:-/tmp} the probe name is made
    under."""
    probe_dir = tmpdir if tmpdir is not None else (os.environ.get("TMPDIR") or "/tmp")
    fd, probe = tempfile.mkstemp(prefix="dvRpuProbe.", dir=probe_dir)
    os.close(fd)
    try:
        first = run(["ffmpeg", "-loglevel", "error", "-nostats", "-i", path,
                     "-map", "0:v:0", "-c", "copy", "-frames:v", "48",
                     "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        second = run(["dovi_tool", "extract-rpu", "-", "-o", probe],
                     stdin=first.stdout)
        return _pipeline_status(first.returncode, second.returncode) == 0
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def _mkdirs_parent(path):
    """The module's ``mkdir -p "$(dirname "$out")"``."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def _nonempty(path):
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _reason(*streams):
    """What the shell's ``err=$( { ...; } 2>&1 )`` hands back: the captured
    bytes with their trailing newlines stripped, "<no output>" when there was
    none to capture."""
    text = b"".join(streams).decode("utf-8", "replace").rstrip("\n")
    return text if text else "<no output>"


def convert_to_profile81(movie, out, log=print, run=_subprocess_run):
    """dvConvertToProfile81: the video track out of <movie> rewritten from
    profile 7 to profile 8.1 - the RPU rewritten, the enhancement layer
    dropped, no re-encode - to <out>. The extraction is piped straight into
    dovi_tool, so the dual-layer stream never needs a temp file of its own.
    Returns 0 when the converted stream is there and non-empty; on a failure
    it leaves nothing behind, says why, and returns 1, so the caller can fall
    back to keeping the original profile 7 video."""
    _mkdirs_parent(out)
    first = run(["ffmpeg", "-loglevel", "error", "-nostats", "-i", movie,
                 "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
                 "-f", "hevc", "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
    second = run(["dovi_tool", "-m", "2", "--drop-hdr10plus", "convert",
                  "--discard", "-", "-o", out],
                 stdin=first.stdout, stdout=subprocess.DEVNULL,
                 stderr=subprocess.PIPE)
    if _pipeline_status(first.returncode, second.returncode) == 0 \
            and _nonempty(out):
        return 0
    _unlink(out)
    log("  WARNING: Dolby Vision conversion failed, keeping profile 7: " + movie)
    log("    reason: " + _reason(first.stderr, second.stderr))
    return 1


def extract_video_stream(movie, out, log=print, run=_subprocess_run):
    """dvExtractVideoStream: the video track out of <movie> copied, with no
    conversion of any kind, to <out> - the counterpart of
    convert_to_profile81 for a file whose Dolby Vision claim is false. There
    is no dovi_tool and in particular no --drop-hdr10plus: there is no RPU to
    collide with HDR10+ here. Returns 0 when the stream is there and
    non-empty; on a failure it leaves nothing behind, says why, and returns
    1, so the caller can fall back to keeping the file as it is."""
    _mkdirs_parent(out)
    proc = run(["ffmpeg", "-loglevel", "error", "-nostats", "-i", movie,
                "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", "-y", out],
               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
               stderr=subprocess.STDOUT)
    if proc.returncode == 0 and _nonempty(out):
        return 0
    _unlink(out)
    log("  WARNING: could not copy the video stream out, leaving the false "
        "Dolby Vision claim: " + movie)
    log("    reason: " + _reason(proc.stdout))
    return 1


# ---------------------------------------------------------------------------
# The Dolby Vision LEVEL a file declares about itself
# ---------------------------------------------------------------------------

# Table 6 of the Dolby Vision ISOBMFF spec, as (luma pixel rate, level). Each
# level is named by the resolution and frame rate it just covers, and a stream
# belongs to the lowest level it fits inside; the thresholds are written as the
# multiplication they are, so the resolution each stands for is readable.
_CONFIG_LEVELS = (
    (1280 * 720 * 24, 1),      #  720p24
    (1280 * 720 * 30, 2),      #  720p30
    (1920 * 1080 * 24, 3),     # 1080p24
    (1920 * 1080 * 30, 4),     # 1080p30
    (1920 * 1080 * 60, 5),     # 1080p60
    (3840 * 2160 * 24, 6),     #   4K24
    (3840 * 2160 * 30, 7),     #   4K30
    (3840 * 2160 * 48, 8),     #   4K48
    (3840 * 2160 * 60, 9),     #   4K60
    (3840 * 2160 * 120, 10),   #  4K120
)

_CONFIG_RECORD = "DOVI configuration record"


def expected_config_level(width, height, fps_num, fps_den):
    """dvExpectedConfigLevel: the lowest Dolby Vision level whose luma pixel
    rate covers this video, or None when the geometry or frame rate is
    unusable - which the caller reads as "no opinion" rather than as level 1.

    Above the table's last pixel rate the level is 11 or higher, and which one
    depends on the bitrate tier rather than on the geometry; nothing here needs
    them told apart, since such a stream is never one this lowers.
    """
    numbers = []
    for value in (width, height, fps_num, fps_den):
        text = "" if value is None else str(value)
        if not _INT.match(text):
            return None
        numbers.append(int(text))
    if not all(number > 0 for number in numbers):
        return None
    width, height, fps_num, fps_den = numbers
    rate = width * height * fps_num // fps_den
    for ceiling, level in _CONFIG_LEVELS:
        if rate <= ceiling:
            return level
    return 11


def _config_record(stream):
    """The one DOVI configuration record of a stream's side data that carries
    an RPU, as jq's ``[...][0] // {}`` settles it: the first match, or an empty
    mapping. A claim with nothing behind it is stream_has_rpu's subject, and
    there is no point tidying the level on a record that should not be there at
    all. Raises the way jq errors out on side data it cannot navigate."""
    side_data = stream.get("side_data_list")
    if side_data is None:
        return {}
    if not isinstance(side_data, (list, dict)):
        raise ValueError("side_data_list is not iterable")
    entries = side_data if isinstance(side_data, list) else list(side_data.values())
    for entry in entries:
        # jq reads a field of null as null, and errors on a field of anything
        # else that is not an object - so a null entry is merely filtered out.
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise ValueError("side data entry is not an object")
        if entry.get("side_data_type") != _CONFIG_RECORD:
            continue
        present = entry.get("rpu_present_flag")
        if present is None or present is False:
            present = 0
        if present == 1 and not isinstance(present, bool):
            return entry
    return {}


def read_config_level(path, run=_subprocess_run):
    """dvReadConfigLevel: what <path>'s Dolby Vision configuration record
    declares, and what its video actually needs, in ONE ffprobe pass:

      PROFILE   dv_profile from the record ("" when there is no record)
      LEVEL     dv_level as declared       ("" when there is no record)
      EXPECTED  what expected_config_level makes of the video ("" if unknown)

    ffprobe rather than mediainfo, because the record's own fields are wanted
    as numbers - mediainfo renders them into the "dvhe.08.10" codec string,
    which would have to be parsed back apart.
    """
    empty = {"PROFILE": "", "LEVEL": "", "EXPECTED": ""}
    proc = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                "stream=width,height,r_frame_rate:stream_side_data="
                "side_data_type,dv_profile,dv_level,rpu_present_flag",
                "-of", "json", path],
               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return empty
    try:
        text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return empty
    if not text.rstrip("\n"):
        return empty
    try:
        data = json.loads(text)
    except ValueError:
        return empty
    # Everything below models one jq program, whose every failure is the same
    # answer: jq prints nothing, the shell's one `read` gets nothing, and all
    # five fields stay empty.
    try:
        streams = data.get("streams") if isinstance(data, dict) else None
        if streams is None:
            stream = {}
        elif isinstance(streams, list):
            stream = streams[0] if streams else {}
        else:
            raise ValueError("streams is not an array")
        if stream is None:
            stream = {}
        if not isinstance(stream, dict):
            raise ValueError("the first stream is not an object")
        record = _config_record(stream)
        width = _field(stream.get("width"))
        height = _field(stream.get("height"))
        rate = _field(stream.get("r_frame_rate"))
        profile = _field(record.get("dv_profile"))
        level = _field(record.get("dv_level"))
    except (ValueError, AttributeError, TypeError):
        return empty
    if not _INT.match(level):
        return empty
    # r_frame_rate is a fraction ("24000/1001"); a bare integer is its own.
    fps_num, sep, fps_den = rate.partition("/")
    expected = expected_config_level(width, height, fps_num,
                                     fps_den if sep else "1")
    return {"PROFILE": profile, "LEVEL": level,
            "EXPECTED": "" if expected is None else str(expected)}


def normalise_config_level(path, report_as=None, script_dir="", log=print,
                           run=_subprocess_run):
    """dvNormaliseConfigLevel: correct <path>'s Dolby Vision level if it
    overstates what the video needs. <report_as> is the name to use in the log
    when the path on disk is not the one the caller talks to the user about;
    it defaults to <path>.

    Only ever DOWNWARDS: an overstated level is what breaks playback, an
    understated one does not, and raising one cannot be justified from the
    container alone. A file with no Dolby Vision, no usable geometry, or a
    level that is already right or lower is left untouched and silently - this
    runs on every file the two scripts produce, so the quiet path has to stay
    quiet. Returns non-zero only when a correction was attempted and failed,
    which is a warning rather than a reason to reject the file.

    The write is verified by re-probing rather than by trusting the corrector's
    answer, because what matters is what the file now says about itself.
    """
    name = report_as or path
    settled = read_config_level(path, run=run)
    declared, wanted = settled["LEVEL"], settled["EXPECTED"]
    if not declared or not wanted:
        return 0
    if int(declared) <= int(wanted):
        return 0
    if dolbyvisionlevel.correct_level(path, wanted) != 0:
        log("  WARNING: could not correct the overstated Dolby Vision level "
            "(" + declared + ", should be " + wanted + "): " + name)
        return 1
    now = read_config_level(path, run=run)["LEVEL"]
    if now != wanted:
        log("  WARNING: the Dolby Vision level still reads " +
            (now or "nothing") + " after correcting it to " + wanted +
            ": " + name)
        return 1
    log("  Corrected the Dolby Vision level, which overstated what the video "
        "needs and")
    log("    which players check before they decode anything: " + declared +
        " -> " + wanted + ": " + name)
    return 0
