"""The HDR10+ helpers: finding the dynamic metadata, taking it out of a source,
and writing it back into a re-encode.

No encoder here carries HDR10+ (SMPTE ST 2094-40) through an encode by
itself, so a conversion that keeps it does it around the encode instead: the
source's metadata is extracted to hdr10plus_tool's JSON before, and injected
into the finished HEVC stream after, which touches no pixel. The stream it is
injected into is raw Annex B, and a raw HEVC stream with B-frames has no
timestamps ffmpeg can mux, so it goes back into Matroska through mkvmerge.

Every command runs through a fakeable ``run`` (one tool) or ``pipe`` (two of
them joined), the same shape as :mod:`medialib.lib.dolbyvision`, so the white
box can drive them with stubs.
"""

import json
import os
import re
import subprocess

from medialib.lib import dolbyvision

# What ffprobe calls the side data an HDR10+ frame carries.
SIDE_DATA_TYPE = "SMPTE2094-40"

# How hdr10plus_tool reports a video and a JSON of different lengths. It is a
# warning, not a failure: the tool pads or trims the metadata to fit and exits
# 0, which would put every scene's metadata on the wrong frames.
_MISMATCH = re.compile(rb"mismatched lengths\W*video (\d+), HDR10\+ JSON (\d+)",
                       re.IGNORECASE)


def stream_has_hdr10plus(path, run=dolbyvision._subprocess_run):
    """True when the first frames of <path>'s video carry HDR10+ metadata.

    Asked of a few decoded frames rather than of the container, which records
    nothing about it: the metadata lives in the bitstream, one message per
    frame."""
    try:
        done = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-read_intervals", "%+#3", "-show_frames",
                    "-show_entries", "frame=side_data_list", "-of", "json",
                    path],
                   stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return SIDE_DATA_TYPE.encode() in (done.stdout or b"")


def extract_metadata(movie, out, log=print, pipe=dolbyvision._subprocess_pipe):
    """The HDR10+ metadata of <movie>'s video, as hdr10plus_tool's JSON, to
    <out>. The video track is piped straight into the tool, so no copy of it is
    ever written. Returns 0 when the JSON is there and non-empty; on a failure
    it leaves nothing behind, says why, and returns 1."""
    dolbyvision._mkdirs_parent(out)
    first, second, first_err, second_err = pipe(
        ["ffmpeg", "-loglevel", "error", "-nostats", "-i", movie,
         "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
         "-f", "hevc", "-"],
        ["hdr10plus_tool", "extract", "-o", out, "-"],
        second_stdout=subprocess.DEVNULL, capture_stderr=True)
    if dolbyvision._pipeline_status(first, second) == 0 \
            and metadata_frames(out) > 0:
        return 0
    dolbyvision._unlink(out)
    log("    reason: " + dolbyvision._reason(first_err, second_err))
    return 1


def metadata_frames(path):
    """How many frames the JSON at <path> describes, 0 when it cannot be
    read."""
    try:
        with open(path) as handle:
            scenes = json.load(handle).get("SceneInfo")
    except (OSError, ValueError, AttributeError):
        return 0
    return len(scenes) if isinstance(scenes, list) else 0


def metadata_windows(path):
    """The most processing windows any frame of the JSON at <path> uses. Past
    the first, a window is placed by coordinates in the frame the film was
    graded in, which a crop or a scale no longer matches."""
    try:
        with open(path) as handle:
            scenes = json.load(handle).get("SceneInfo")
    except (OSError, ValueError, AttributeError):
        return 0
    if not isinstance(scenes, list):
        return 0
    return max((scene.get("NumberOfWindows") or 0 for scene in scenes
                if isinstance(scene, dict)), default=0)


def inject_metadata(video, metadata, out, fps, scratch, log=print,
                    run=dolbyvision._subprocess_run):
    """<video>'s HEVC stream with the HDR10+ <metadata> interleaved into it, as
    a video-only Matroska at <out>. <fps> is the frame rate as ffprobe reports
    it ("24000/1001"), which mkvmerge is given because the raw stream carries
    no timing of its own.

    The two raw streams it passes through are written under <scratch> and
    removed as soon as the next step has read them. A video and a JSON of
    different lengths are refused rather than stretched to fit. Returns 0 when
    <out> is there and non-empty; on a failure it leaves nothing behind, says
    why, and returns 1."""
    raw = os.path.join(scratch, "hdr10plus.in.hevc")
    injected = os.path.join(scratch, "hdr10plus.out.hevc")
    pipe = subprocess.PIPE
    devnull = subprocess.DEVNULL

    def failed(reason):
        for path in (raw, injected, out):
            dolbyvision._unlink(path)
        log("    reason: " + reason)
        return 1

    def ran(argv):
        try:
            return run(argv, stdin=devnull, stdout=pipe, stderr=pipe)
        except OSError as error:
            class Failed:
                returncode = 127
                stdout = b""
                stderr = str(error).encode()
            return Failed()

    done = ran(["ffmpeg", "-loglevel", "error", "-nostats", "-y", "-i", video,
                "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", raw])
    if done.returncode != 0 or not dolbyvision._nonempty(raw):
        return failed(dolbyvision._reason(done.stderr))

    # hdr10plus_tool reads its input by name: it refuses a pipe.
    done = ran(["hdr10plus_tool", "inject", "-i", raw, "-j", metadata,
                "-o", injected])
    dolbyvision._unlink(raw)
    said = (done.stdout or b"") + (done.stderr or b"")
    mismatch = _MISMATCH.search(said)
    if mismatch:
        return failed("the encode has %s frames and the source's HDR10+ "
                      "metadata describes %s"
                      % (mismatch.group(1).decode(), mismatch.group(2).decode()))
    if done.returncode != 0 or not dolbyvision._nonempty(injected):
        return failed(dolbyvision._reason(said))

    done = ran(["mkvmerge", "--quiet", "-o", out, "--default-duration",
                "0:%sfps" % fps, injected])
    dolbyvision._unlink(injected)
    # mkvmerge exits 1 for warnings, with the file written.
    if done.returncode > 1 or not dolbyvision._nonempty(out):
        return failed(dolbyvision._reason(done.stdout, done.stderr))
    return 0
