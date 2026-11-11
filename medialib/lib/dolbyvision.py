"""The Dolby Vision normalisation helpers.

The readers return their results in memory as a dict with the DV_* names so a
caller needs no second probe; the three pure gates judge the fields the
readers settle; and the two pipelines - profile 7 -> 8.1, and the
video-stream copy that drops a false claim - run the same ffmpeg and
dovi_tool commands, through a fakeable ``run`` (one tool) or ``pipe`` (two of
them joined) so the white box can drive them with stubs.
"""

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zlib

from medialib.lib import filmrecord, runlog, toolcapture

_INT = re.compile(r"^[0-9]+$")
_FPS = re.compile(r"^[0-9]+([.][0-9]+)?$")

# The NTSC rates mediainfo reports rounded ("23.976"), mapped back to the exact
# fraction mkvmerge's --default-duration takes - the rounding error alone
# drifts by ~0.1s over a feature-length film. Matched as a prefix, so
# "23.9765" lands where "23.976" does.
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
    or the decimal form the document carried - the domain mediainfo's fields
    live in), and anything else as its JSON."""
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
    strings - so a denominator of "00" stands and one of "0" does not), else
    the rounded decimal mapped back to its NTSC fraction, else the decimal as
    given, else nothing at all."""
    if _INT.match(fps_num) and _INT.match(fps_den) and fps_den != "0":
        return "%s/%sfps" % (fps_num, fps_den)
    if _FPS.match(fps):
        for prefix, spec in _NTSC:
            if fps.startswith(prefix):
                return spec
        return fps + "fps"
    return ""


def _subprocess_run(argv, **kwargs):
    """The real runner for the one-tool probes: subprocess.run."""
    return subprocess.run(list(argv), **kwargs)


def _captured(handle):
    """A capture file read back from the start, or nothing when the caller
    asked for no capture."""
    if handle is None:
        return b""
    try:
        handle.seek(0)
        return handle.read()
    except OSError:
        return b""


def _subprocess_pipe(first_argv, second_argv, second_stdout=None,
                     capture_stderr=False):
    """The real runner for the two pipelines: ``first | second`` joined by an
    OS pipe, so the second tool consumes the first one's output WHILE it is
    still being produced - a reader that starts only once the writer has
    finished blocks forever on the first pipeful, and both these streams are
    far past a pipe's 64 KiB capacity.

    Returns (first status, second status, first stderr, second stderr), the
    two byte strings empty unless capture_stderr asked for them - and captured
    into a temporary FILE rather than a second pipe, which would be the same
    trap one level down.
    """
    with contextlib.ExitStack() as stack:
        first_err = (stack.enter_context(tempfile.TemporaryFile())
                     if capture_stderr else None)
        second_err = (stack.enter_context(tempfile.TemporaryFile())
                      if capture_stderr else None)
        first = subprocess.Popen(list(first_argv), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=first_err)
        assert first.stdout is not None  # stdout=PIPE
        try:
            second = subprocess.Popen(list(second_argv), stdin=first.stdout,
                                      stdout=second_stdout, stderr=second_err)
        except BaseException:
            first.stdout.close()
            first.kill()
            first.wait()
            raise
        # Closed in THIS process once the second tool holds it, so the pipe
        # really has one reader: kept open here, the first tool would never
        # see the reader go away and the pipeline would not end.
        first.stdout.close()
        second.wait()
        first.wait()
        return (first.returncode, second.returncode,
                _captured(first_err), _captured(second_err))


def read_video_info(path, run=_subprocess_run):
    """dvReadVideoInfo: the DV state and frame rate of <path>'s video track,
    in one mediainfo pass, as the six DV_* values. A file mediainfo cannot
    read, one that prints nothing, and one whose JSON the probe cannot
    navigate all yield the empty values, which the gates below read as
    "nothing to convert".

    ``run`` stands in for the runner - (argv, **kwargs) to a result with
    returncode/stdout/stderr - so the white box can feed the reader its
    canned JSON.
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
    probe cannot clobber the caller's DV_* values; what it saw is the second
    return (DV_SEEN_PROFILE) so a rejection can say what the file actually
    claimed. Matched as a
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
    """The status of ``first | second``: the rightmost non-zero exit, zero
    when both pass."""
    return second if second else first


def stream_has_rpu(path, pipe=_subprocess_pipe, tmpdir=None):
    """dvStreamHasRpu: true when dovi_tool can actually FIND Dolby Vision RPU
    data in the video bitstream - the claim the container makes is checked
    against the 48 frames it can get cheaply, a fraction of a second even on a
    4.5 GB film. ``tmpdir`` is the directory the probe name is made under
    ($TMPDIR or /tmp when unset)."""
    probe_dir = tmpdir if tmpdir is not None else (os.environ.get("TMPDIR") or "/tmp")
    fd, probe = tempfile.mkstemp(prefix="dvRpuProbe.", dir=probe_dir)
    os.close(fd)
    try:
        first, second, _first_err, _second_err = pipe(
            ["ffmpeg", "-loglevel", "error", "-nostats", "-i", path,
             "-map", "0:v:0", "-c", "copy", "-frames:v", "48",
             "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
            ["dovi_tool", "extract-rpu", "-", "-o", probe])
        return _pipeline_status(first, second) == 0
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def _mkdirs_parent(path):
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
    """The captured bytes with their trailing newlines stripped, "<no
    output>" when there was none to capture."""
    text = b"".join(streams).decode("utf-8", "replace").rstrip("\n")
    return text if text else "<no output>"


def convert_to_profile81(movie, out, log=runlog.log, pipe=_subprocess_pipe):
    """dvConvertToProfile81: the video track out of <movie> rewritten from
    profile 7 to profile 8.1 - the RPU rewritten, the enhancement layer
    dropped, no re-encode - to <out>. The extraction is piped straight into
    dovi_tool, so the dual-layer stream never needs a temp file of its own.
    Returns 0 when the converted stream is there and non-empty; on a failure
    it leaves nothing behind, says why, and returns 1, so the caller can fall
    back to keeping the original profile 7 video."""
    _mkdirs_parent(out)
    first, second, first_err, second_err = pipe(
        ["ffmpeg", "-loglevel", "error", "-nostats", "-i", movie,
         "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
         "-f", "hevc", "-"],
        ["dovi_tool", "-m", "2", "--drop-hdr10plus", "convert",
         "--discard", "-", "-o", out],
        second_stdout=subprocess.DEVNULL, capture_stderr=True)
    if _pipeline_status(first, second) == 0 and _nonempty(out):
        return 0
    _unlink(out)
    log("  WARNING: Dolby Vision conversion failed, keeping profile 7: " + movie)
    log("    reason: " + _reason(first_err, second_err))
    return 1


def extract_video_stream(movie, out, log=runlog.log, run=_subprocess_run):
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
    toolcapture.keep(getattr(proc, "args", ["ffmpeg"]), proc.returncode,
                     getattr(proc, "stdout", ""), label=movie,
                     skipped="the false Dolby Vision claim was left")
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
    # Every failure in the navigation below is the same answer: the empty
    # result.
    try:
        streams = data.get("streams") if isinstance(data, dict) else None
        stream: dict
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


def normalise_config_level(path, report_as=None, script_dir="", log=runlog.log,
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
    corrected = correct_level(path, wanted)
    filmrecord.forget(path)
    if corrected != 0:
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


# --- writing the level into the record --------------------------------------
#
# Mechanism only - which level a file SHOULD declare is
# :func:`expected_config_level`'s decision.
#
# The level is six bits of the dvcC/dvvC block addition mapping, a container
# element attached to the video track and nothing to do with the HEVC
# bitstream. Correcting it is therefore a two-byte edit at a fixed offset
# near the front of the file, not a remux: instant whatever the file's size,
# and it cannot touch a single frame of video.
#
# Two bytes still cannot simply be written, which is why this is a Python
# helper and not four lines of dd: some muxers put a CRC-32 on the master
# element holding the record (ffmpeg does, mkvmerge does not), and a patch
# that left it stale would leave a knowingly corrupt checksum behind. So the
# walk down to the record keeps the master elements it passed through, and
# every CRC-32 among them is recomputed afterwards, innermost first - an
# outer element's CRC covers the inner one's, so it has to be settled last.
#
# :func:`correct_level` answers 0 on success and 1 when the file holds no
# single configuration record to patch - which the caller reads as "leave
# this file alone", not as a hard error.


# The record lives in the Tracks element, which every muxer writes before the
# first frame; reading a megabyte reaches it without pulling in a 20 GB film.
HEADER_BYTES = 1 << 20

MASTER_IDS = {
    b"\x18\x53\x80\x67",  # Segment
    b"\x16\x54\xae\x6b",  # Tracks
    b"\xae",              # TrackEntry
    b"\xe0",              # Video
    b"\x41\xe4",          # BlockAdditionMapping
}
BLOCK_ADD_ID_EXTRA_DATA = b"\x41\xed"
CRC32_ID = b"\xbf"


def read_id(buf, pos):
    b0 = buf[pos]
    for n, mask in ((1, 0x80), (2, 0x40), (3, 0x20), (4, 0x10)):
        if b0 & mask:
            return bytes(buf[pos:pos + n]), pos + n
    raise ValueError(f"not an EBML element id at {pos}")


def read_size(buf, pos):
    b0 = buf[pos]
    for n in range(1, 9):
        if b0 & (0x80 >> (n - 1)):
            val = b0 & (0xFF >> n)
            for i in range(1, n):
                val = (val << 8) | buf[pos + i]
            unknown = val == (1 << (7 * n)) - 1
            return (None if unknown else val), pos + n
    raise ValueError(f"not an EBML size at {pos}")


def find_config_record(buf):
    """(payloadStart, enclosingMasters) for the one dvcC/dvvC payload in the
    header, or None when there is not exactly one to be sure about."""
    found = []

    def walk(start, end, path):
        pos = start
        while pos < end:
            try:
                eid, p = read_id(buf, pos)
                size, p = read_size(buf, p)
            except (ValueError, IndexError):
                return                      # a truncated or unexpected element
            if size is None:                # unknown length: descend, no bound
                size = end - p
            cend = min(p + size, end)
            if eid == BLOCK_ADD_ID_EXTRA_DATA:
                # A DOVIDecoderConfigurationRecord opens with dv_version_major=1,
                # dv_version_minor=0. Other block addition types exist and must
                # not be mistaken for one.
                if cend - p >= 5 and buf[p:p + 2] == b"\x01\x00":
                    found.append((p, list(path)))
            elif eid in MASTER_IDS:
                path.append((p, cend))
                walk(p, cend, path)
                path.pop()
            pos = cend

    walk(0, len(buf), [])
    return found[0] if len(found) == 1 else None


def set_level(path, new_level):
    with open(path, "rb") as fh:
        buf = bytearray(fh.read(HEADER_BYTES))
    hit = find_config_record(buf)
    if hit is None:
        return False
    payloadStart, masters = hit

    # Bytes 2..3 pack dv_profile (7 bits), dv_level (6), then the rpu / el / bl
    # present flags; only the middle field changes.
    word = (buf[payloadStart + 2] << 8) | buf[payloadStart + 3]
    word = (word & ~(0x3F << 3)) | ((new_level & 0x3F) << 3)
    buf[payloadStart + 2] = word >> 8
    buf[payloadStart + 3] = word & 0xFF

    # Every edit is settled in the buffer first and only then written back, and
    # written back as the six or ten bytes that actually changed rather than as
    # the whole megabyte that was read. This is somebody's film: the smaller the
    # write, the less a crash halfway through it can cost.
    edits = [(payloadStart + 2, bytes(buf[payloadStart + 2:payloadStart + 4]))]

    for contentStart, contentEnd in reversed(masters):
        if buf[contentStart:contentStart + 1] != CRC32_ID:
            continue                        # this muxer wrote no CRC here
        size, valueAt = read_size(buf, contentStart + 1)
        if size != 4:
            continue
        crc = zlib.crc32(bytes(buf[valueAt + 4:contentEnd])) & 0xFFFFFFFF
        buf[valueAt:valueAt + 4] = crc.to_bytes(4, "little")
        edits.append((valueAt, bytes(buf[valueAt:valueAt + 4])))

    with open(path, "r+b") as fh:
        for offset, data in edits:
            fh.seek(offset)
            fh.write(data)
        fh.flush()
    return True


def correct_level(path, level) -> int:
    """Patch <path>'s record to <level>: 0 done, 1 nothing to patch or unreadable.

    Silent, and a status rather than an exception, because the caller runs this
    over every file it produces and reads the answer as advice - the level is
    re-probed afterwards either way.
    """
    try:
        return 0 if set_level(path, int(level)) else 1
    except (OSError, ValueError):
        return 1


def set_level_main(argv):
    """``python -m medialib.lib.dolbyvision FILE LEVEL``: write LEVEL into
    FILE's record by hand, refusing a level Dolby Vision does not have."""
    if len(argv) != 3:
        print("usage: python -m medialib.lib.dolbyvision FILE LEVEL",
              file=sys.stderr)
        return 2
    try:
        level = int(argv[2])
    except ValueError:
        print(f"not a Dolby Vision level: {argv[2]}", file=sys.stderr)
        return 2
    if not 1 <= level <= 63:
        print(f"level out of range: {level}", file=sys.stderr)
        return 2
    try:
        if not set_level(argv[1], level):
            print("no single Dolby Vision configuration record found", file=sys.stderr)
            return 1
    except OSError as exc:
        print(f"{argv[1]}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(set_level_main(sys.argv))
