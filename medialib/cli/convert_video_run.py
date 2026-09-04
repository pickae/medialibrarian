"""convert-video's run: what it settles at startup, and what it does per file.

The decisions - the profile tables, the quality level, the chunk plan, the
argument string, the -t test - are `medialib/cli/convert_video.py`. This is the
run around them: the validation and hardware detection that happen once, the
per-file encode, and the closing report.

The order of the per-file work is the point of it. The video encode is by far the
most expensive thing here and lives in RAM until the mux writes it out, so it is
never lost to a failure of the cheap parts: if the audio, or the mux of the
subtitles, attachments, chapters and metadata, fails, the finished video is still
written to disk on its own and marked " (video only)".
"""

import os
import shutil
import subprocess
import sys
import time

from medialib import commands
from medialib.cli import convert_video as rules
from medialib.lib import (
    clioptions,
    codecs,
    ffmpegselect,
    formatting,
    hostos,
    pausecontrol,
    ramscratch,
    resolutions,
    runlog,
    safety,
    segments,
    statusline,
    tooldeps,
    videograin,
    workerpool,
)
from medialib.lib.runlog import log

# The sysfs PCI vendor id of the iGPU maker this script decodes with. Matching on
# it is what keeps a mixed Intel-plus-NVIDIA box from mistaking the NVIDIA node for
# the iGPU.
INTEL_PCI_VENDOR = "0x8086"


def _run(argv: list, capture: bool = False):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE if capture
                              else subprocess.DEVNULL)
    except OSError:
        class Failed:
            returncode = 127
            stderr = b""
        return Failed()


def _has_tool(name: str) -> bool:
    return shutil.which(name) is not None


# --- what this build and this machine can do ----------------------------------

def ffmpeg_takes_args(binary: str, args: str) -> bool:
    """The one-frame encode the build check is made of: true when the build runs
    it AND does not report a parameter it had to ignore.

    Only one of the two ways a build can be too old is loud. An option ffmpeg
    itself does not know fails the encode; but a key inside -svtav1-params that the
    linked SVT-AV1 does not know is logged as "Error parsing option" and then
    IGNORED. A build that applies half the tuning looks exactly like one that
    applies all of it, which is why this reads the encoder's complaints rather than
    comparing version numbers.
    """
    done = _run([binary, "-hide_banner", "-nostdin", "-loglevel", "warning",
                 "-f", "lavfi", "-i", "color=c=black:s=256x256:r=5:d=1"]
                + args.split() + ["-frames:v", "1", "-f", "null", "-"],
                capture=True)
    if done.returncode != 0:
        return False
    return b"error parsing option" not in done.stderr.lower()


def ffmpeg_takes_profile(binary: str, args: str) -> bool:
    """True when that build accepts every argument the selected profile asks for.

    The tuning info is the one argument allowed to be negotiated rather than
    demanded: a row asks for uhq, and a build that does not know the value refuses
    the encode over it - but the run does not need uhq, it FALLS BACK to hq, so
    judging a build on the un-demoted row rejects it for a reason that never
    applies to the encode it would actually run.
    """
    if ffmpeg_takes_args(binary, args):
        return True
    wanted = " -tune %s " % rules.NVENC_TUNE_WANTED
    if wanted not in " %s " % args:
        return False
    return ffmpeg_takes_args(binary, args.replace(
        wanted, " -tune %s " % rules.NVENC_TUNE_FALLBACK))


def nvenc_works(encoder: str) -> bool:
    """True when a one-frame test encode succeeds: GPU present, driver up, that
    codec's NVENC block available."""
    return _run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
                 "-i", "color=c=black:s=256x256:r=5:d=1", "-c:v", encoder,
                 "-pix_fmt", "p010le", "-f", "null", "-"]).returncode == 0


def nvenc_tune_works(encoder: str, tune: str) -> bool:
    """True when that encoder will really accept that tuning info.

    Asked by encoding a frame rather than by reading the option's documented
    range, because the answer depends on the SDK the build was compiled against,
    the driver and the GPU generation at once - and only an actual encode has all
    three in play.
    """
    return _run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
                 "-i", "color=c=black:s=256x256:r=5:d=1", "-c:v", encoder,
                 "-pix_fmt", "p010le", "-preset", "p7", "-tune", tune,
                 "-f", "null", "-"]).returncode == 0


def vaapi_works(node: str) -> bool:
    """True when a VA display can actually be opened on that node - the true gate
    for using it as a decode device.

    A functional probe rather than a guess from sysfs, so it reports what the
    machine can do right now. Only the DEVICE is exercised, not a particular
    filter, to avoid rejecting a working iGPU over an unrelated missing one.
    """
    return _run(["ffmpeg", "-hide_banner", "-v", "error", "-init_hw_device",
                 "vaapi=va:" + node, "-f", "lavfi",
                 "-i", "color=c=black:s=64x64:r=1:d=0.1", "-f", "null",
                 "-"]).returncode == 0


def videotoolbox_works() -> bool:
    """True when a VideoToolbox device can be opened - the macOS answer to the
    question :func:`vaapi_works` asks on Linux.

    Every Mac has the hardware, Intel and Apple Silicon alike, so what this is
    really testing is whether the ffmpeg on PATH was built with it: a
    Homebrew build is, and a static build fetched from elsewhere may not be.
    The probe is the same shape as the VAAPI one - open the device, decode
    nothing - so it rejects a build that cannot use the hardware without
    rejecting one that merely lacks some unrelated filter.
    """
    return _run(["ffmpeg", "-hide_banner", "-v", "error", "-init_hw_device",
                 "videotoolbox=vt", "-f", "lavfi",
                 "-i", "color=c=black:s=64x64:r=1:d=0.1", "-f", "null",
                 "-"]).returncode == 0


def intel_render_node() -> str:
    """The Intel render node to decode on, or "".

    The sysfs PCI vendor id is preferred. When the vendor files are absent (some
    virtualised hosts) it falls back to the first node a VA display can be opened
    on.
    """
    try:
        nodes = sorted(name for name in os.listdir("/dev/dri")
                       if name.startswith("renderD"))
    except OSError:
        return ""

    def vendor_of(name):
        try:
            with open("/sys/class/drm/%s/device/vendor" % name) as handle:
                return handle.read().strip()
        except OSError:
            return ""

    for name in nodes:
        node = "/dev/dri/" + name
        if vendor_of(name) == INTEL_PCI_VENDOR and vaapi_works(node):
            return node
    for name in nodes:
        node = "/dev/dri/" + name
        # sysfs present but not Intel: skip it.
        if vendor_of(name):
            continue
        if vaapi_works(node):
            return node
    return ""


def encoder_takes_master_display(encoder: str) -> bool:
    """Whether this nvenc build has the HDR static-metadata options; an older one
    keeps colour signalling only."""
    try:
        done = subprocess.run(["ffmpeg", "-hide_banner", "-h",
                               "encoder=" + encoder],
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return b"-master_display" in done.stdout


def encoder_takes_dolby_vision(encoder: str) -> bool:
    """Whether the SELECTED encoder can accept a source's RPU and write it back.

    Asked of the encoder rather than assumed from an ffmpeg version: only one that
    exposes the option can do it (libx265 and libsvtav1 do; the NVENC encoders have
    no RPU support at all), and an ffmpeg too old to offer it then simply behaves
    as it did before rather than erroring on an unknown argument.
    """
    try:
        done = subprocess.run(["ffmpeg", "-hide_banner", "-h",
                               "encoder=" + encoder],
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return b"-dolbyvision" in done.stdout


# --- the Dolby Vision preparation ---------------------------------------------

def normalise_dolby_vision(relative: str, settings, output_dir: str) -> tuple:
    """Bring a dual-layer profile 7 source to single-layer 8.1, so its RPU can
    ride through the re-encode after all, and answer with the video-only file to
    encode INSTEAD of the source, plus the scratch holding it.

    Profile 7 carries its RPU in an enhancement layer no encoder here can
    re-encode; 8.1 is the single-layer form of the very same Dolby Vision, and
    reaching it needs no re-encode at all. Doing it here is what keeps a conversion
    from having to be preceded by an ingest: a profile 7 source that is not
    normalised first loses its dynamic metadata, and whether the user thought of
    that is not what should decide it.

    Every failure is a fallback to plain HDR10 - the source is never touched, and
    nothing here can fail the conversion of a file.
    """
    from medialib.lib import dolbyvision
    source = os.path.join(settings.input_dir, relative)

    missing = []
    if not _has_tool("dovi_tool"):
        missing.append("dovi_tool")
    if not _has_tool("mkvmerge"):
        missing.append("mkvmerge (mkvtoolnix)")
    if missing:
        log("Dolby Vision profile 7 cannot be normalised to 8.1 without %s - "
            "install it to keep Dolby Vision on dual-layer sources: %s"
            % (" and ".join(missing), relative))
        return "", ""

    fps = rules.video_frame_rate(source)
    if not fps:
        log("Dolby Vision profile 7 cannot be normalised to 8.1: ffprobe "
            "reports no usable frame rate, and the converted stream has to be "
            "given one: %s" % relative)
        return "", ""

    # The container's claim is not proof of an RPU: a file remuxed by a tool that
    # copied the configuration record while dropping the RPU still reports profile
    # 7 with nothing behind it. Asked first, from a few frames, because the
    # alternative is copying a whole 4K video stream out of the file to find out.
    if not dolbyvision.stream_has_rpu(source):
        log("Dolby Vision profile 7 is claimed by the container but the video "
            "carries no RPU, so there is nothing to normalise: %s" % relative)
        return "", ""

    # Two files exist at once - mkvmerge reads the converted stream while writing
    # the Matroska around it - and a video stream is most of a film, so the need is
    # twice the SOURCE's size: an upper bound rather than an estimate, which is the
    # direction a capacity check has to err in.
    try:
        need = str(os.path.getsize(source) * 2)
    except OSError:
        need = ""
    scratch, on_disk, status = ramscratch.ram_scratch_dir_for(
        need, "convertVideoDv", output_dir)
    if status != 0:
        log("Dolby Vision profile 7 cannot be normalised to 8.1: no scratch "
            "directory could be created: %s" % relative)
        return "", ""
    # Registered as well as handed back by the caller: the release is the ordinary
    # path, the registration is what covers an interrupt landing before it.
    ramscratch.add_exit_cleanup([scratch])
    log("Dolby Vision: normalising the dual-layer profile 7 source to "
        "single-layer profile 8.1 first, so the encode can carry its RPU (the "
        "video itself is not re-encoded): %s" % relative)
    if on_disk:
        log("Dolby Vision: too large for the RAM scratch, preparing it on disk "
            "beside the output instead: %s" % relative)

    hevc = os.path.join(scratch, "dv81.hevc")
    prepared = os.path.join(scratch, "dv81.mkv")
    if dolbyvision.convert_to_profile81(source, hevc, log=log) != 0:
        log("Dolby Vision: the profile 7 -> 8.1 conversion failed, encoding the "
            "source as plain HDR10 instead: %s" % relative)
        ramscratch.release_exit_cleanup([scratch])
        return "", ""

    # --default-duration is not decoration: the raw stream has no timing, and
    # without it mkvmerge falls back to 25 fps - which would misplace every chunk
    # boundary cut from the intermediate.
    done = _run(["mkvmerge", "--quiet", "-o", prepared, "--default-duration",
                 "0:%sfps" % fps, hevc], capture=True)
    # The converted stream has served its purpose the moment it is inside the
    # Matroska; free it before the encode starts rather than holding both.
    _remove(hevc)
    if done.returncode > 1:
        log("Dolby Vision: the profile 8.1 stream could not be muxed (%s), "
            "encoding the source as plain HDR10 instead: %s"
            % (done.stderr.decode("utf-8", "replace").strip() or "no output",
               relative))
        ramscratch.release_exit_cleanup([scratch])
        return "", ""

    # Verified before a frame is encoded from it, the same way the source was
    # classified. Anything else is thrown away and the source encoded as it is -
    # which still has its RPU, so nothing is lost that was not lost already.
    profile, enhancement = rules.dolby_vision_profile(prepared)
    reason = ""
    if not profile:
        reason = "the result signals no Dolby Vision at all"
    elif enhancement == "1":
        reason = "the result is still dual-layer (profile %s)" % profile
    if reason:
        log("Dolby Vision: %s, encoding the source as plain HDR10 instead: %s"
            % (reason, relative))
        ramscratch.release_exit_cleanup([scratch])
        return "", ""

    log("Dolby Vision: the source now reads as single-layer profile %s, and is "
        "encoded from that: %s" % (profile, relative))
    return prepared, scratch


def dolby_vision_probe(relative: str, settings) -> tuple:
    """True when this file can really be encoded with its RPU carried through,
    decided by actually running the encoder for one frame with the exact arguments
    the real encode would use.

    That covers every way it can fail - a profile ffmpeg cannot map onto the
    output codec, colour signalling it cannot classify, a build without the
    feature - in one check, and it costs a single frame. On failure the encoder's
    first complaint comes back so the caller can report why.
    """
    source = rules.video_source_for(relative, settings.input_dir,
                                    settings.dolby_vision_source)
    probing = rules.Settings(**dict(settings.__dict__, dolby_vision_mode="1"))
    args = rules.build_video_args(
        rules.profile_args(rules.VIDEO_PROFILES, settings.video_profile),
        source, probing)
    argv = (["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-nostats"] + settings.decode_accel.split()
            + ["-i", source, "-an", "-sn", "-map", "0:v:0", "-frames:v", "1"]
            + args.split() + ["-f", "null", "-"])
    done = _run(argv, capture=True)
    if done.returncode == 0:
        return True, ""
    for line in done.stderr.decode("utf-8", "replace").splitlines():
        if line.strip() and not svt_chatter(line):
            return False, line
    return False, ""


def dolby_vision_mode_for(relative: str, profile: str, enhancement: str,
                          settings) -> str:
    """The mode to encode this file with, as the "1" / "0" / "" value the argument
    builder takes, with the reason logged.

    The two cases that cannot work are recognised from the source and the encoder
    rather than attempted; anything else is settled by actually encoding a frame.
    Note the difference between "" and "0": empty leaves ffmpeg's auto default
    alone, which is right when there is no RPU to carry, while 0 actively switches
    DV off for a DV source we have decided against - otherwise auto would enable it
    by itself and fail an encode already known not to work.
    """
    if not profile:
        return ""
    if not settings.dv_encoder_support:
        log("Dolby Vision profile %s present, but %s cannot code an RPU - "
            "keeping HDR10 only: %s" % (profile, settings.encoder, relative))
        return ""
    if enhancement == "1":
        log("Dolby Vision profile %s is dual-layer, which cannot be re-encoded, "
            "and could not be normalised to single-layer profile 8.1 here (see "
            "above) - keeping HDR10 only: %s" % (profile, relative))
        return "0"
    worked, reason = dolby_vision_probe(relative, settings)
    if worked:
        log("Dolby Vision: carrying the profile %s RPU through the encode: %s"
            % (profile, relative))
        return "1"
    log("Dolby Vision profile %s cannot be encoded here - keeping HDR10 only "
        "(%s): %s" % (profile, reason or "no reason given", relative))
    return "0"


def svt_chatter(line: str) -> bool:
    """True for a line libsvtav1 wrote about itself rather than about a failure -
    its banner, the configuration it resolved and its warnings. Its errors are left
    alone: they are the only thing it says that a caller needs."""
    return line.startswith(("Svt[info]:", "Svt[warn]:"))


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- the encodes --------------------------------------------------------------

def run_quiet_encode(argv: list) -> int:
    """One video encode, with the encoder library's own chatter stripped and the
    p/r keys able to stop and continue it.

    libsvtav1 does not log through ffmpeg: it prints its banner, resolved
    configuration and warnings straight to stderr, ignoring -loglevel entirely,
    once per encoder instance - so a chunked software AV1 encode emits that block
    once PER CHUNK, all interleaved, burying the progress row. Only the library's
    info and warn lines are dropped; its errors, everything ffmpeg prints, and the
    exit status pass through untouched.

    Every video encode goes through here, whole file or chunk, software or NVENC,
    which is what makes one keypress reach all of them at once.
    """
    return pausecontrol.run_pausable(argv, keep=lambda line: not svt_chatter(line))


def encode_video_whole(relative: str, directory: str, settings) -> int:
    """The VIDEO of a whole file into a video-only Matroska intermediate.

    Audio, subtitles and attachments are handled separately, so this pass is pure
    video. Used for files left unchunked - a single chunk, or a clip too short to
    cut.
    """
    source = rules.video_source_for(relative, settings.input_dir,
                                    settings.dolby_vision_source)
    args = rules.build_video_args(
        rules.profile_args(rules.VIDEO_PROFILES, settings.video_profile),
        source, settings)
    argv = (["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-nostats", "-progress", os.path.join(directory, "prog.0000"),
             "-y"] + settings.decode_accel.split()
            + ["-i", source, "-an", "-sn", "-map", "0:v:0"] + args.split()
            + ["-f", "matroska", os.path.join(directory, "video.mkv")])
    return run_quiet_encode(argv)


def encode_video_chunk(settings, token: str) -> int:
    """One time-range of a source into a video-only Matroska chunk: a single
    parallel job.

    The same profile settings a whole-file encode uses, so every chunk is a
    keyframe-aligned, stream-copy-compatible piece.
    """
    safety.trap_worker_abort()
    ramscratch.adopt_ram_base(getattr(settings, "ram_base", ""))
    relative, index, _total, start, duration = token.split(rules.UNIT)

    source = rules.video_source_for(relative, settings.input_dir,
                                    settings.dolby_vision_source)
    directory = segments.chunk_dir_for(settings.chunk_root, relative)
    os.makedirs(directory, exist_ok=True)
    out = os.path.join(directory, "%04d.mkv" % int(index))

    args = rules.build_video_args(
        rules.profile_args(rules.VIDEO_PROFILES, settings.video_profile),
        source, settings)
    argv = (["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-nostats", "-progress",
             os.path.join(directory, "prog.%04d" % int(index)), "-y"]
            + settings.decode_accel.split()
            + ["-ss", start, "-t", duration, "-i", source, "-an", "-sn",
               "-map", "0:v:0"] + args.split() + ["-f", "matroska", out])
    return run_quiet_encode(argv)


def reconcat_video_only(relative: str, total: int, settings) -> int:
    """The chunks re-joined into the video-only intermediate.

    The concat demuxer streams them through verbatim; because every chunk opens
    with a keyframe and the audio is no longer part of them, the join is seamless.
    """
    directory = segments.chunk_dir_for(settings.chunk_root, relative)
    listing = os.path.join(directory, "concat.txt")
    with open(listing, "w") as handle:
        for index in range(total):
            handle.write("file '%s'\n"
                         % os.path.join(directory, "%04d.mkv" % index))

    # The status row is still live here - the video pass is not over until the
    # chunks are re-joined - so make an empty console row for this line.
    statusline.clear_status()
    log("Joining %d video chunks: %s" % (total, relative))
    return _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-nostats", "-y", "-safe", "0", "-f", "concat",
                 "-i", listing, "-map", "0:v", "-c", "copy", "-f", "matroska",
                 os.path.join(directory, "video.mkv")]).returncode


def encode_audio_all(relative: str, directory: str, settings) -> int:
    """Every audio track transcoded in parallel, one software process per track.

    There are only ever a handful, so this fans out one job each without regard to
    core count: they are cheap and, with hardware video decode and encode, run
    while the CPU is otherwise idle. A passthrough profile writes nothing here.
    Any track above stereo is downmixed to stereo.
    """
    import multiprocessing
    source = os.path.join(settings.input_dir, relative)
    audio_args = rules.profile_args(rules.AUDIO_PROFILES,
                                    settings.audio_profile)
    if "-c:a copy" in audio_args:
        return 0

    counts = rules._probe(["ffprobe", "-v", "error", "-select_streams", "a",
                           "-show_entries", "stream=channels",
                           "-of", "csv=p=0", source]).split()
    workers = []
    for index, channels in enumerate(counts):
        track_args, bitrate = rules.audio_track_args(channels, settings)
        out = os.path.join(directory, "audio%02d.mka" % index)
        process = multiprocessing.Process(
            target=_encode_audio_track,
            args=(source, str(index), bitrate, track_args, out))
        process.start()
        workers.append(process)

    status = 0
    for process in workers:
        process.join()
        if process.exitcode != 0:
            status = 1
    return status


def _encode_audio_track(source: str, index: str, bitrate: str, audio_args: str,
                        out: str) -> None:
    """ONE audio track to its own Matroska.

    Mapping a single audio stream carries its metadata, language and disposition
    across automatically, so the final mux keeps them.
    """
    safety.trap_worker_abort()
    argv = (["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-nostats", "-y", "-i", source, "-map", "0:a:" + index, "-vn",
             "-sn"] + audio_args.split()
            + ["-b:a", bitrate + "k", "-f", "matroska", out])
    raise SystemExit(_run(argv).returncode)


def restore_output_folder(path: str) -> bool:
    """Recreate the folder ``path`` is about to be written into, and say whether
    that was needed.

    The output tree is mirrored from the input once, at startup, so a sub-folder
    deleted while a file was encoding is missing by the time that file comes to be
    written. Recreating it costs nothing; not recreating it costs the encode.
    """
    parent = os.path.dirname(path)
    if not parent or os.path.isdir(parent):
        return False
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:
        return False
    log("Output folder gone, recreated to write into: " + parent)
    return True


def output_path_for(relative: str, settings, replacing: bool) -> str:
    """Where this file's finished encode is written - the normal output path, or a
    " (N)" sibling of it, with its folder made sure of.

    A name that was free when the run reached this file and is taken by the time
    the encode is done belongs to a file that appeared meanwhile, from something
    other than this conversion, so the encode goes NEXT to it. ``replacing`` marks
    the one case where an existing output is written over on purpose: an
    out-of-date file from an earlier run, which is what this conversion was started
    to replace.
    """
    output = os.path.join(settings.output_dir,
                          os.path.splitext(relative)[0] + ".mkv")
    restore_output_folder(output)
    if replacing or not os.path.exists(output):
        return output
    sibling = safety.unique_suffix_path(output)
    log('WARNING: %s: the output name was taken during the encode - writing '
        '"%s" instead.'
        % (relative, os.path.relpath(sibling, settings.output_dir)))
    return sibling


def mux_final(relative: str, directory: str, settings, output: str) -> int:
    """The final output: the encoded video, the separately encoded audio, and the
    source's subtitles, attachments, chapters and metadata, all stream-copied into
    one Matroska.

    Input 0 is the encoded video, input 1 the original (for the parts only it
    still has, and for passthrough audio), and any further inputs the encoded audio
    in order.
    """
    source = os.path.join(settings.input_dir, relative)
    audio_args = rules.profile_args(rules.AUDIO_PROFILES,
                                    settings.audio_profile)

    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-nostats", "-y", "-i", os.path.join(directory, "video.mkv"),
            "-i", source]
    maps = ["-map", "0:v"]
    if "-c:a copy" in audio_args:
        maps += ["-map", "1:a?"]
    else:
        next_input = 2
        for name in sorted(os.listdir(directory)):
            if not (name.startswith("audio") and name.endswith(".mka")):
                continue
            argv += ["-i", os.path.join(directory, name)]
            maps += ["-map", "%d:a" % next_input]
            next_input += 1
    maps += ["-map", "1:s?", "-map", "1:t?"]

    log("Muxing: " + relative)
    argv += maps + ["-map_metadata", "1", "-map_chapters", "1", "-c", "copy",
                    "-f", "matroska", output]
    status = _run(argv).returncode
    # A mux that failed on a folder that has just gone is retried, once, on the
    # folder put back: stream-copying the parts together again is minutes at
    # most, against the hours of encoding they took to produce.
    if status != 0 and restore_output_folder(output):
        status = _run(argv).returncode
    if status != 0:
        return status
    # The output gets the source's modification time, like the sibling scripts do.
    try:
        stamp = os.stat(source)
        os.utime(output, (stamp.st_atime, stamp.st_mtime))
    except OSError:
        pass
    return 0


def write_video_only(relative: str, directory: str, reason: str,
                     settings) -> bool:
    """The failsafe for a file whose VIDEO encoded fine but whose finishing steps
    did not.

    The video encode costs orders of magnitude more than everything else, so it is
    written to disk rather than thrown away with the RAM scratch when the cheap
    remaining work fails. Copied out verbatim - a plain copy, so nothing that
    already failed can make this step fail as well.
    """
    source = os.path.join(settings.input_dir, relative)
    output = rules.video_only_path_for(relative, settings.output_dir)
    log('WARNING: %s: %s - keeping the finished video encode as "%s" (no '
        "audio, subtitles, attachments, chapters or metadata)."
        % (relative, reason, os.path.relpath(output, settings.output_dir)))
    encoded = os.path.join(directory, "video.mkv")
    try:
        shutil.copy(encoded, output)
    except (OSError, shutil.Error):
        # The last thing standing between a deleted folder and hours of lost
        # encoding: put the folder back and copy once more.
        try:
            restore_output_folder(output)
            shutil.copy(encoded, output)
        except (OSError, shutil.Error):
            log("WARNING: the video-only failsafe could not be written, the "
                "video encode is lost: %s" % relative)
            return False
    try:
        stamp = os.stat(source)
        os.utime(output, (stamp.st_atime, stamp.st_mtime))
    except OSError:
        pass
    return True


# --- one file -----------------------------------------------------------------

class Run:
    """The run's mutable state: what the closing report is read out of."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.start = time.time()
        self.converted = 0
        self.skipped = 0
        self.failed = 0
        self.frames = 0
        self.video_seconds = 0.0

    def convert_file(self, relative: str) -> int:
        """ONE video: resume-skip an up-to-date output, ask -t whether it is worth
        converting, then produce the result in three overlapping parts and mux
        them.

        VIDEO is cut into chunks - one per NVENC engine for a hardware encode, or
        resolution-driven for a software one - and encoded in parallel. AUDIO is
        transcoded in software alongside it, because with hardware video work the
        CPU is otherwise idle and even for a software encode the few tracks are
        cheap. Then the MUX.
        """
        settings = self.settings
        source = os.path.join(settings.input_dir, relative)
        output = os.path.join(settings.output_dir,
                              os.path.splitext(relative)[0] + ".mkv")

        # A pause holds the NEXT file off too: between two files this script
        # probes the source, and those probes are ffmpeg runs of their own that
        # would otherwise start up on a machine whose CPU was just handed back.
        pausecontrol.wait_while_paused()

        duration = _media_duration(source)
        # An output already here is the out-of-date result of an earlier run, and
        # replacing it is the whole point of converting this file again. Remembered
        # now, because after the encode a file at that path is no longer
        # distinguishable from one that turned up while the encode ran.
        replacing = os.path.isfile(output)
        if replacing:
            out_duration = _media_duration(output)
            if duration > 0 and out_duration >= duration:
                log("Up to date, skipping: " + relative)
                self.skipped += 1
                return 0

        directory = segments.chunk_dir_for(settings.chunk_root, relative)
        os.makedirs(directory, exist_ok=True)

        # The source's coded size, and the size it will actually be ENCODED at -
        # the same unless -r caps this one. The encoded size is what the chunk
        # planning goes by: a 2160p source scaled down to 1080p does a 1080p
        # file's worth of encoding work.
        width, height, _order, sar = rules.video_dimensions(source)
        enc_width, enc_height = resolutions.capped(width, height,
                                                   settings.max_resolution)
        size_text = self._size_text(width, height, enc_width, enc_height)

        # Film grain: measure THIS source, so a clean file is not given grain it
        # never had and a grainy one gets as much as it actually has.
        source_grain = ""
        if settings.grain_probe_wanted:
            settings.grain_level = videograin.grain_level_for(
                source, relative, _media_duration, _dimensions_line,
                runlog.jobs_per_core, settings.decode_accel)
            source_grain = settings.grain_level

        if settings.test_source_bitrate:
            if not source_grain:
                source_grain = videograin.source_grain_for(
                    source, relative, _media_duration, _dimensions_line,
                    runlog.jobs_per_core, settings.decode_accel)
            if not rules.conversion_worthwhile(relative, width, height,
                                               enc_width, enc_height,
                                               source_grain, settings):
                shutil.rmtree(directory, ignore_errors=True)
                self.skipped += 1
                return 0

        rules.warn_source_geometry(
            relative, rules.interlace_verdict(source, settings.decode_accel),
            sar)

        scratch = self._settle_dolby_vision(relative, source)

        # A hardware encode uses one chunk per NVENC engine so the GPU's engines
        # all stay busy; a software encode uses the resolution-driven count that
        # fills the CPU cores.
        count = (settings.nvenc_engines if settings.hardware_encode
                 else rules.chunk_count_for(enc_width, enc_height,
                                            settings.cores))

        # The audio starts NOW so it runs alongside the video pass: with hardware
        # video decode and encode the CPU is otherwise idle, and even for a
        # software encode the few tracks are cheap, so it overlaps for free
        # instead of running afterwards.
        import multiprocessing
        audio = multiprocessing.Process(
            target=_audio_worker, args=(relative, directory, settings))
        audio.start()

        self._encode(relative, directory, duration, count, size_text)
        frames, micros = rules.sum_encode_progress(directory)

        # The intermediate has been read for the last time - everything left reads
        # the ORIGINAL. Handed back per file because it is a whole film's video
        # stream: a run over a folder of profile 7 films would otherwise hold one
        # per file until the run ended.
        if scratch:
            ramscratch.release_exit_cleanup([scratch])
            settings.dolby_vision_source = ""

        # Video is done: stop the row and leave it behind on its own console
        # line, then wait for the audio before muxing.
        statusline.stop_status_monitor()
        audio.join()
        if safety.abort_requested():
            return safety.INTERRUPTED_EXIT_STATUS

        if self.finish(relative, directory, duration, audio.exitcode or 0,
                       replacing=replacing) != 0:
            return 1

        # Only a file that came out complete counts towards the stats: a skipped
        # or failed one did work that produced nothing, and averaging it in would
        # report a throughput the finished library does not reflect.
        self.converted += 1
        self.frames += frames
        self.video_seconds += micros / 1000000
        return 0

    def _size_text(self, width, height, enc_width, enc_height) -> str:
        if not width and not height:
            return "size unknown"
        text = "%sx%s" % (width or "?", height or "?")
        if (enc_width, enc_height) != (width, height):
            text += " -> %sx%s" % (enc_width, enc_height)
        return text

    def _settle_dolby_vision(self, relative: str, source: str) -> str:
        """The per-file Dolby Vision decision, which every chunk then inherits.

        A dual-layer profile 7 source is the one flavour that can be MADE
        encodable, by converting it to single-layer 8.1 first - done here, and not
        asked of the user as a separate ingest pass. Only worth it for an encoder
        that can code an RPU at all: with NVENC the result would be dropped again
        by the encode this is preparing for.
        """
        settings = self.settings
        settings.dolby_vision_source = ""
        profile, enhancement = rules.dolby_vision_profile(source)
        scratch = ""
        if enhancement == "1" and settings.dv_encoder_support:
            prepared, scratch = normalise_dolby_vision(relative, settings,
                                                       settings.output_dir)
            if prepared:
                settings.dolby_vision_source = prepared
                # Re-classified rather than assumed, so the mode is decided from
                # the file that will actually be encoded.
                profile, enhancement = rules.dolby_vision_profile(prepared)
        settings.dolby_vision_mode = dolby_vision_mode_for(
            relative, profile, enhancement, settings)
        self.source_dv_profile = profile

        # The conversion was preparation for an encode that carries the RPU.
        # Where that turns out to be impossible anyway, the intermediate buys
        # nothing: its base layer is the same HDR10 the source already has.
        if scratch and settings.dolby_vision_mode != "1":
            ramscratch.release_exit_cleanup([scratch])
            settings.dolby_vision_source = ""
            return ""
        return scratch

    def _encode(self, relative: str, directory: str, duration: float,
                count: int, size_text: str) -> int:
        """The video pass, whole or chunked, with the status row pinned under it.

        A failing pass is deliberately not fatal: the completeness check is the
        single arbiter of whether there is a usable video, so a broken encode skips
        that ONE file instead of aborting the whole run.
        """
        settings = self.settings
        started = int(time.time())
        paused_at_start = pausecontrol.paused_seconds(started)
        statusline.start_status_monitor(
            "", lambda: rules.video_status_text(
                directory, duration, relative, started, paused_at_start,
                cols=statusline.state.cols, paused=pausecontrol.pause_requested(),
                paused_now=pausecontrol.paused_seconds(int(time.time())),
                now=int(time.time())))

        whole = int(duration) < 2 or count <= 1
        if not whole:
            # Never make sub-second chunks.
            count = min(count, int(duration))
            interior = rules.equal_boundaries(duration, count)
            bounds = ["0"] + interior + ["%.3f" % duration]
            total = len(bounds) - 1
            whole = total < 2

        statusline.clear_status()
        if whole:
            log("Encoding whole %s (%s)..." % (relative, size_text))
            encode_video_whole(relative, directory, settings)
            return 0

        log("Chunking %s (%s) into %d parts..." % (relative, size_text, count))
        tokens = []
        for index in range(total):
            start, end = bounds[index], bounds[index + 1]
            span = "%.3f" % (formatting.awk_number(end)
                             - formatting.awk_number(start))
            tokens.append(rules.UNIT.join(
                [relative, str(index), str(total), start, span]))
        # -P is the CHUNK count, not the core count: the count is already sized to
        # fill the encoder, so this is the one place video parallelism lives.
        _run_chunk_pool(settings, tokens, count)
        # An interrupt must not fall through into the re-concatenation of a
        # half-encoded chunk set.
        if safety.abort_requested():
            return safety.INTERRUPTED_EXIT_STATUS
        reconcat_video_only(relative, total, settings)
        return 0

    def finish(self, relative: str, directory: str, duration: float,
               audio_status: int, replacing: bool = False) -> int:
        """Everything after both encodes: keep or discard the video, mux the final
        file, and confirm what came out.

        From here on the finished video is never discarded because something cheap
        failed - unless the VIDEO itself is incomplete, any later failure still
        writes the encode out on its own rather than letting hours of encoding die
        with the RAM scratch. Where the encode is written is settled here too, and
        only now: the output folder and the output name are both things the encode
        can have outlived.
        """
        from medialib.lib import dolbyvision
        settings = self.settings

        if not rules.video_intermediate_complete(directory, duration):
            log("Video encoding failed (no complete encoded video to keep), "
                "skipping: " + relative)
            shutil.rmtree(directory, ignore_errors=True)
            return 1

        if audio_status != 0:
            write_video_only(relative, directory, "audio encoding failed",
                             settings)
            shutil.rmtree(directory, ignore_errors=True)
            return 1

        output = output_path_for(relative, settings, replacing)
        if mux_final(relative, directory, settings, output) != 0:
            # A half-written output would only confuse the next run's resume
            # check.
            _remove(output)
            write_video_only(relative, directory,
                             "the final mux failed (audio, subtitles, "
                             "attachments, chapters or metadata)", settings)
            shutil.rmtree(directory, ignore_errors=True)
            return 1

        # This run produced the complete file, so a failsafe left by an earlier
        # run for the same source is now just a duplicate of its video stream.
        _remove(rules.video_only_path_for(relative, settings.output_dir))

        # The encoder embedded the RPU and ffmpeg derives the container's record
        # from that, but both the chunk re-join and the mux are stream copies, so
        # the cheap check that the output still SIGNALS it is worth making.
        if settings.dolby_vision_mode == "1":
            out_profile, _el = rules.dolby_vision_profile(output)
            if out_profile:
                log("Dolby Vision preserved: profile %s -> profile %s: %s"
                    % (self.source_dv_profile, out_profile, relative))
            else:
                log("WARNING: Dolby Vision was encoded but the output does not "
                    "signal it: " + relative)

        # Whatever produced the record, the LEVEL in it has to describe the video
        # that is actually here: a level that overstates the file is refused by a
        # player outright, which looks like a broken file rather than metadata.
        dolbyvision.normalise_config_level(output, relative,
                                           script_dir=settings.script_dir,
                                           log=log)
        shutil.rmtree(directory, ignore_errors=True)
        return 0

    def footer(self) -> None:
        """The same figures the live row showed, aggregated over every file.

        Only the files that came out COMPLETE feed the duration, frame and
        throughput figures: a resume-skipped file did no work and a failed one did
        work that produced nothing. Time spent PAUSED is taken out of the
        wall-clock every figure divides by, because during it there was nothing
        encoding to describe.
        """
        total = time.time() - self.start
        paused = pausecontrol.paused_seconds(int(time.time()))
        encoding = max(0.0, total - paused)

        out = sys.stderr
        out.write("\nStats\n=====\n")
        out.write("Total time:        %.2f s (%s)\n"
                  % (total, formatting.fmt_hms("%.2f" % total)))
        if paused > 0:
            out.write("Of that, paused:   %d s (%s) - left out of the figures "
                      "below\n" % (paused, formatting.fmt_hms(str(paused))))
        out.write("Files converted:   %d\n" % self.converted)
        out.write("Files skipped:     %d\n" % self.skipped)
        out.write("Files failed:      %d\n" % self.failed)
        out.write("Total duration:    %.0f s (%s)\n"
                  % (self.video_seconds,
                     formatting.fmt_hms("%.3f" % self.video_seconds)))
        out.write("Frames encoded:    %d\n" % self.frames)
        if self.converted > 0:
            out.write("Time per file:     %.2f s\n"
                      % (encoding / self.converted))
        if encoding > 0:
            out.write("Average fps:       %.1f\n" % (self.frames / encoding))
            out.write("Real-time speedup: %sx\n" % formatting.fmt_ratio(
                "%.6f" % (self.video_seconds / encoding)))
        out.flush()


def _audio_worker(relative: str, directory: str, settings) -> None:
    """The audio pass, in a process of its own so it overlaps the video.

    Its own interrupt handling, for the reason the shell installs one too: an
    asynchronous child of a non-interactive shell inherits SIGINT ignored, so
    without a handler it would survive Ctrl+C outright.
    """
    safety.trap_worker_abort()
    ramscratch.adopt_ram_base(getattr(settings, "ram_base", ""))
    raise SystemExit(encode_audio_all(relative, directory, settings))


def _dimensions_line(path: str) -> str:
    """The geometry as ``videoDimensions`` PRINTS it, which is what the grain
    probe reads: it splits the line and takes the first two fields."""
    return " ".join(rules.video_dimensions(path))


def _media_duration(path: str) -> float:
    return formatting.awk_number(rules._probe([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1", path]).strip() or 0)


def _in_worker(settings, token: str) -> None:
    encode_video_chunk(settings, token)


def _run_chunk_pool(settings, tokens: list, width: int) -> None:
    if width <= 1:
        for token in tokens:
            if safety.abort_requested():
                return
            encode_video_chunk(settings, token)
        return

    workerpool.run(tokens, width, _in_worker, lambda token: (settings, token))


# --- the run ------------------------------------------------------------------

def main(argv: list, program: str = "convert-video",
         script_dir: str = "") -> int:
    declaration = rules.spec(program)
    # -t is the one option with a side effect beyond its assignment: the flag
    # itself switches the test on, and the percentage it may carry is a second
    # value. getopts cannot express an optional argument, so the flag is declared
    # bare and the word after it is claimed only when it is a number - which is
    # what this hook is handed.
    saving = [rules.DEFAULT_BITRATE_SAVING]

    def on_option(letter: str, value: str) -> None:
        if letter == "t" and value:
            saving[0] = value

    try:
        result = clioptions.parse(declaration, argv, on_opt=on_option)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        sys.stderr.write(clioptions.usage_error_text(declaration,
                                                     error.message))
        return 1

    if clioptions.args_out_of_range(len(result.positionals), 2, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    script_dir = script_dir or commands.script_dir()

    settings = rules.Settings(
        script_dir=script_dir,
        input_dir=result.positionals[0],
        output_dir=result.positionals[1],
        cores=int(result.values["CORES"] or runlog.cpu_count()),
        video_profile=result.values["videoProfile"] or "av1Grain",
        audio_profile=result.values["audioProfile"] or "opus",
        custom_audio_bitrate=result.values["customAudioBitrate"] or "",
        quality=result.values["videoQuality"] or "",
        quality_given="q" in result.given,
        max_resolution=result.values["maxVideoResolution"] or "",
        fast_decode=result.values["videoFastDecode"] or "",
        test_source_bitrate="t" in result.given,
        required_saving=saving[0])
    grain = result.values["videoGrain"] or ""
    engines_override = result.values["nvencEnginesOverride"] or ""

    # A -b bitrate on its own says everything opusCustom does, so let it select
    # that profile rather than rejecting the run over an -a the user never typed.
    # Only the untouched default is promoted - an explicit `-a opus -b 46` still
    # contradicts itself and is caught below.
    if ("a" not in result.given and settings.custom_audio_bitrate
            and settings.audio_profile == "opus"):
        settings.audio_profile = "opusCustom"

    try:
        video_args = rules.profile_args(rules.VIDEO_PROFILES,
                                        settings.video_profile)
        rules.profile_args(rules.AUDIO_PROFILES, settings.audio_profile)
    except rules.UnknownProfile as unknown:
        sys.stderr.write(unknown.text())
        return 1

    refusal = _validate(settings, video_args, grain, engines_override)
    if refusal:
        sys.stderr.write(clioptions.usage_error_text(declaration, refusal))
        return 1

    if not os.path.isdir(settings.input_dir):
        sys.stdout.write(clioptions.missing_dir_text(declaration,
                                                     settings.input_dir))
        return 1
    # Before the output folder is created: a re-encode written inside the input is
    # a video this script would find and re-encode again on the next run.
    if safety.require_separate_output(settings.input_dir, settings.output_dir):
        return 1

    # This script asks ffmpeg for things a distribution's package is often too old
    # to do, so the shared ladder is given a probe that answers "can this build do
    # what THIS run's profile asks".
    ffmpegselect.select_ffmpeg(
        lambda binary: ffmpeg_takes_profile(binary, video_args))
    if tooldeps.require_tools(program, ["ffmpeg", "ffprobe"]):
        return 1
    os.makedirs(settings.output_dir, exist_ok=True)
    ramscratch.init_ram_base()

    # The name patterns are deliberately the exact ones the conversion loop uses,
    # so the probe and the loop can never disagree about what counts.
    if not _sources(settings.input_dir):
        return safety.fail_no_relevant_input(settings.input_dir,
                                             "videos (.mkv / .mp4)")

    settings.grain_level, settings.grain_probe_wanted = _settle_grain(
        settings, grain, video_args)
    _detect_hardware(settings, video_args, engines_override)
    _summarise(settings, video_args, grain)

    return _run_all(settings)


def _validate(settings, video_args: str, grain: str,
              engines_override: str) -> str:
    """Every refusal that can be decided before a file is touched.

    All of them would otherwise fail per chunk, deep inside a parallel worker.
    """
    encoder = rules.encoder_of(rules.apply_video_quality(video_args))
    if settings.audio_profile == "opusCustom" \
            and not settings.custom_audio_bitrate:
        return ("The opusCustom audio profile requires an audio bitrate: "
                "-b <kbit/s>.")
    if settings.custom_audio_bitrate and settings.audio_profile != "opusCustom":
        return ("The -b audio bitrate only applies to the opusCustom profile "
                "(got -a %s)." % settings.audio_profile)
    if settings.custom_audio_bitrate and not _positive(
            settings.custom_audio_bitrate):
        return ('The -b audio bitrate must be a positive integer in kbit/s '
                '(got "%s").' % settings.custom_audio_bitrate)

    if settings.quality:
        if not rules.video_quality_flag(video_args):
            return ("The -q quality level only applies to the "
                    "constant-quality profiles; -p %s targets an average "
                    "bitrate instead." % settings.video_profile)
        top = rules.video_quality_max(video_args)
        if not settings.quality.isdigit() or int(settings.quality) > top:
            return ('The -q quality level must be an integer between 0 and %d '
                    'for -p %s (got "%s").'
                    % (top, settings.video_profile, settings.quality))

    if settings.max_resolution:
        named = resolutions.named(settings.max_resolution)
        if not named or not resolutions.ceiling(named):
            return ('Cannot scale to resolution tier "%s". Valid -r tiers: %s.'
                    % (settings.max_resolution, resolutions.spellings()))
        settings.max_resolution = named

    saving = str(settings.required_saving)
    if not (saving.isdigit() and len(saving) <= 2):
        return ('The -t saving must be a whole percentage between 0 and 99 '
                '(got "%s").' % saving)

    if engines_override and "nvenc" not in encoder:
        return ("The -e NVENC engine count only applies to the *Nvenc video "
                "profiles (got -p %s)." % settings.video_profile)
    if engines_override and not _positive(engines_override):
        return ('The -e NVENC engine count must be a positive integer '
                '(got "%s").' % engines_override)

    if settings.fast_decode and encoder != "libsvtav1":
        return ("The -f fast-decode level only applies to the AV1 software "
                "profiles (got -p %s, which encodes with %s)."
                % (settings.video_profile, encoder))
    if settings.fast_decode and settings.fast_decode not in ("1", "2"):
        return ('The -f fast-decode level must be 1 or 2 (got "%s").'
                % settings.fast_decode)
    if grain and encoder != "libsvtav1":
        return ("The -g film grain level only applies to the AV1 software "
                "profiles (got -p %s, which encodes with %s)."
                % (settings.video_profile, encoder))
    if grain and grain != "off" and not (grain.isdigit()
                                         and 0 <= int(grain) <= 50):
        return ('The -g film grain level must be off, or an integer between 0 '
                'and 50 - where 0 asks for the per-source probe (got "%s").'
                % grain)
    settings.encoder = encoder
    return ""


def _positive(value: str) -> bool:
    return value.isdigit() and int(value) > 0


def _settle_grain(settings, grain: str, video_args: str) -> tuple:
    """The grain decision for the whole run.

    No -g leaves the profile's own default in force, an explicit level is what
    every file gets, "off" is none, and 0 is what asks for the per-file probe. The
    probe is skipped for the encoders that could not act on the answer; an explicit
    -g 0 is NOT skipped on any AV1 profile, whatever that profile's own default
    says, because asking for the probe is asking to be told what the source has.
    """
    software_av1 = settings.encoder == "libsvtav1"
    if grain == "":
        default = videograin.grain_default_for(settings.video_profile)
        if default != "probe":
            return default, False
        return "0", software_av1
    if grain == "0":
        return "0", software_av1
    if grain == "off":
        return "0", False
    return grain, False


def _sources(root: str) -> list:
    found = []
    for parent, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            if name.endswith("mkv") or name.endswith("mp4"):
                found.append(os.path.relpath(os.path.join(parent, name), root))
    return found


def _detect_hardware(settings, video_args: str, engines_override: str) -> None:
    """Whether hardware is used, decided ONCE by actually exercising ffmpeg rather
    than trusting a capability list: only a functional test reflects what this box
    can really do right now - drivers loaded, device reachable, engine present.

    Two independent things: NVENC encoding on a dedicated NVIDIA GPU, only when a
    *Nvenc profile is selected, and VAAPI decoding on an Intel iGPU, used whenever
    available for BOTH paths because hardware decode is a pure time win with no
    quality tradeoff.
    """
    settings.decode_accel, said = _decode_accel()
    log(said)

    if "nvenc" not in settings.encoder:
        settings.hardware_encode = False
        settings.nvenc_engines = 1
        log("Software encode: %s on the CPU." % settings.encoder)
    else:
        settings.hardware_encode = True
        if not nvenc_works(settings.encoder):
            log("The %s profile needs a working %s encoder, which this machine "
                "does not provide." % (settings.video_profile,
                                       settings.encoder))
            raise SystemExit(1)
        if not nvenc_tune_works(settings.encoder, rules.NVENC_TUNE_WANTED):
            settings.nvenc_tune = rules.NVENC_TUNE_FALLBACK
        if engines_override:
            settings.nvenc_engines = int(engines_override)
            log("Hardware encode: %s on NVENC across %d engine(s) (from -e)."
                % (settings.encoder, settings.nvenc_engines))
        else:
            name = _gpu_name()
            settings.nvenc_engines = rules.nvenc_engines_for(name)
            if not name:
                log("WARNING: nvidia-smi is unavailable - the NVENC engine "
                    "count is a blind guess of %d." % settings.nvenc_engines)
                log("         Use -e <count> to size the parallelism for your "
                    "card.")
            log('Hardware encode: %s on NVENC across %d engine(s) (guessed for '
                '"%s"; override with -e).'
                % (settings.encoder, settings.nvenc_engines,
                   name or "unknown GPU"))
        if settings.nvenc_tune == rules.NVENC_TUNE_WANTED:
            log("NVENC tuning: -tune %s, which enables lookahead and the "
                "temporal filter by itself." % settings.nvenc_tune)
        else:
            log("NVENC tuning: -tune %s - this ffmpeg/driver/GPU will not take "
                "-tune %s, so the profile falls back to it."
                % (settings.nvenc_tune, rules.NVENC_TUNE_WANTED))
        settings.nvenc_master_display = encoder_takes_master_display(
            settings.encoder)

    settings.dv_encoder_support = encoder_takes_dolby_vision(settings.encoder)


def _decode_accel() -> tuple[str, str]:
    """The hardware DECODE flags this run uses, and the line that says so.

    A ladder of one rung per platform's own interface, each behind a probe
    that opens the device for real:

      * VAAPI on an Intel iGPU, which is the Linux case and the original one;
      * VideoToolbox on macOS, where there is no ``/dev/dri`` to walk and the
        iGPU is reached through the framework instead - and where it is worth
        reaching for on every Mac, since Apple Silicon has no discrete GPU to
        fall back on;
      * nothing, and the decoding happens on the CPU.

    Hardware decode is used wherever it is found and for both encode paths,
    because it is a pure time win with no quality tradeoff - the frames come
    back to system memory either way.

    The macOS rung is asked only on macOS: the probe costs an ffmpeg start,
    and no other platform has the framework to find.
    """
    node = intel_render_node()
    if node:
        return ("-hwaccel vaapi -hwaccel_device " + node,
                "Hardware decode: Intel iGPU via VAAPI (%s)." % node)
    if hostos.is_macos() and videotoolbox_works():
        return ("-hwaccel videotoolbox",
                "Hardware decode: the GPU via VideoToolbox.")
    return ("", "Hardware decode: no usable iGPU found, decoding in software.")


def _gpu_name() -> str:
    try:
        done = subprocess.run(["nvidia-smi", "--query-gpu=name",
                               "--format=csv,noheader"],
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    lines = done.stdout.decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else ""


def _summarise(settings, video_args: str, grain: str) -> None:
    """Restate the decisions taken, so a run is self-explanatory.

    Stated unconditionally where the other scripts only speak up when a choice
    overruled PATH: a build that silently drops the psychovisual keys produces a
    perfectly good encode that is simply not the one that was asked for, and the
    alternative to this line is finding that out from the file.
    """
    ffmpegselect.report_ffmpeg_selection(always=True)
    if ffmpegselect.selected_full() != 1:
        log("WARNING: this ffmpeg does not accept every argument of -p %s - "
            "either it is too old for some of them, or the hardware to test "
            "them on is missing. Encoding anyway: an encoder parameter it does "
            "not recognise is dropped, so the result may not be the encode the "
            "profile describes." % settings.video_profile)

    settled = rules.apply_nvenc_tune(
        rules.apply_video_quality(video_args, given=settings.quality_given,
                                  quality=settings.quality),
        settings.nvenc_tune)
    log("Video profile: %s -> %s" % (settings.video_profile, settled))

    ceiling = (resolutions.ceiling(settings.max_resolution)
               if settings.max_resolution else None)
    # The validation refused a tier with no ceiling long before this, so the
    # guard is here to keep that true rather than because it can fail today.
    if ceiling:
        log("Resolution: capped at %s (%dx%d), aspect ratio kept; a smaller "
            "source keeps its own size."
            % (settings.max_resolution, ceiling[0], ceiling[1]))
    else:
        log("Resolution: unchanged, every source is encoded at its own size "
            "(-r caps it).")

    # Where the quality level comes from, stated because the bias is otherwise
    # invisible: the profile row above shows the unbiased level, not the one a
    # 2160p file will get.
    if not rules.video_quality_flag(settled):
        log("Video quality: none to set - %s targets an average bitrate, not a "
            "quality level." % settings.video_profile)
    elif settings.quality_given:
        log("Video quality: %s %s for every file (-q), so no resolution bias "
            "is applied." % (rules.video_quality_flag(settled),
                             settings.quality))
    else:
        log("Video quality: the %s profile default above, biased per file by "
            "the tier it is ENCODED at (%s; override with -q)."
            % (settings.video_profile, rules.quality_bias_spellings()))

    _summarise_grain(settings, grain)

    if settings.fast_decode:
        log("Fast decode: level %s, trading some compression for a cheaper "
            "decode." % settings.fast_decode)
    else:
        log("Fast decode: off (the encoder default; -f 1 or -f 2 asks for it).")

    if settings.test_source_bitrate:
        log("Bitrate test: on (-t) - each source is measured first, and only a "
            "video that is not already starved AND whose %s encode would still "
            "be adequate on %s%% less bitrate is converted. Everything else is "
            "skipped with its figures."
            % (codecs.encoder_codec(settings.encoder),
               settings.required_saving))
    else:
        log("Bitrate test: off, every source is converted (-t converts only "
            "the ones with room to save).")

    if settings.dv_encoder_support:
        log("Dolby Vision: kept where the source carries a single-layer RPU "
            "(%s can code one)." % settings.encoder)
        if _has_tool("dovi_tool") and _has_tool("mkvmerge"):
            log("Dolby Vision: a dual-layer profile 7 source is normalised to "
                "single-layer profile 8.1 first (no video re-encode), so it "
                "keeps its DV without being ingested first.")
        else:
            log("Dolby Vision: a dual-layer profile 7 source is encoded as "
                "plain HDR10 - normalising it to single-layer profile 8.1 "
                "needs dovi_tool and mkvmerge, and one of them is missing.")
    else:
        log("Dolby Vision: dropped, %s cannot code an RPU (HDR10 signalling is "
            "still preserved)." % settings.encoder)

    if settings.audio_profile == "passthrough":
        log("Audio profile: passthrough (source audio copied through, not "
            "re-encoded)")
    elif settings.custom_audio_bitrate:
        log("Audio profile: %s (%s kbit/s Opus applied to every track, "
            "surround downmixed to stereo)"
            % (settings.audio_profile, settings.custom_audio_bitrate))
    else:
        log("Audio profile: %s (per-channel Opus bitrate from the shared "
            "table, surround downmixed to stereo)" % settings.audio_profile)
    log("Audio parallelism: one software process per audio track, run "
        "alongside the video encode.")

    if settings.hardware_encode:
        log("Video parallelism: hardware - %d NVENC engine(s), so %d chunk(s) "
            "per file (files run one at a time), with split encode disabled so "
            "the engines are not also striping single frames."
            % (settings.nvenc_engines, settings.nvenc_engines))
        # Said up front so an allocation failure reads as a memory ceiling
        # rather than a broken profile.
        log("Note: %d encode session(s) run at once, so GPU memory is the "
            "ceiling on 4K sources - a driver allocation failure there means "
            "lowering -e, not a broken profile." % settings.nvenc_engines)
    else:
        log("Video parallelism: software - resolution-driven chunks per file, "
            "scaled to %d core(s) (files run one at a time)." % settings.cores)


def _summarise_grain(settings, grain: str) -> None:
    if settings.encoder != "libsvtav1":
        log("Film grain: not available with %s, none synthesised."
            % settings.encoder)
    elif settings.grain_probe_wanted and grain:
        log("Film grain: -g 0 - measured per file and synthesised as measured "
            "(lossy: the source grain is denoised away and re-generated at "
            "playback).")
    elif settings.grain_probe_wanted:
        log("Film grain: measured per file and synthesised as measured, the %s "
            "default (-g pins one level for every file, -g off none). Lossy: "
            "the source grain is denoised away and re-generated at playback."
            % settings.video_profile)
    elif int(settings.grain_level or 0) > 0 and grain:
        log("Film grain: level %s for every file (-g), regardless of source or "
            "profile (lossy: the source grain is denoised away and re-generated "
            "at playback)." % settings.grain_level)
    elif int(settings.grain_level or 0) > 0:
        log("Film grain: level %s for every file, the %s default (-g pins "
            "another level, -g 0 measures each source, -g off none). Lossy: the "
            "source grain is denoised away and re-generated at playback."
            % (settings.grain_level, settings.video_profile))
    elif not grain:
        log("Film grain: none - the %s default is 0 (-g 0 measures each source "
            "instead)." % settings.video_profile)
    else:
        log("Film grain: off (-g off), none synthesised.")


def _run_all(settings) -> int:
    """The walk: one file at a time, because the chunk count already saturates the
    encoder and encoding files in parallel would oversubscribe it."""
    statusline.init_status_line()

    # The pause state lives in the RAM scratch because the encoders a keypress has
    # to reach are in other processes. Armed only when nothing armed it already: a
    # wrapper that has pauses of its own owns that state, so one keypress holds
    # every layer of a run.
    if not os.environ.get("PAUSE_DIR"):
        directory, status = ramscratch.ram_scratch_dir("convertVideo.pause")
        if status == 0:
            pausecontrol.init(directory)
            ramscratch.add_exit_cleanup([directory])
    pausecontrol.start_pause_keys()
    if pausecontrol.pause_keys_active():
        log('Pause: press "p" to pause the video encoding (the CPU/GPU is '
            "freed, the encoders keep their memory and the audio keeps going) "
            'and "r" to carry on. Only for as long as this shell runs.')

    chunk_root, status = ramscratch.ram_scratch_dir("convertVideo.chunks")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([chunk_root])
    settings.chunk_root = chunk_root
    settings.ram_base = ramscratch.ram_base()

    run = Run(settings)
    safety.set_run_footer(run.footer)
    safety.init_abort_flag()
    safety.trap_run_abort()

    # The input tree's sub-folder layout, mirrored under the output.
    for parent, dirs, _names in os.walk(settings.input_dir):
        dirs.sort()
        for name in dirs:
            os.makedirs(os.path.join(
                settings.output_dir,
                os.path.relpath(os.path.join(parent, name),
                                settings.input_dir)), exist_ok=True)

    try:
        for relative in _sources(settings.input_dir):
            # An interrupt stops the walk instead of counting every remaining
            # file as a failure. The report still goes out, covering the files
            # that did convert.
            if safety.abort_requested():
                sys.stderr.write("\nInterrupted - stopping.\n")
                safety.print_run_footer()
                return safety.INTERRUPTED_EXIT_STATUS
            # One file that cannot be finished must not cost the rest of the run:
            # it has already said what went wrong, and kept its video encode
            # where it could.
            if run.convert_file(relative) != 0:
                run.failed += 1
    finally:
        statusline.stop_status_monitor()
        pausecontrol.stop_pause_keys()
        pausecontrol.kill_pausable_jobs()
        ramscratch.run_exit_cleanup()

    safety.print_run_footer()
    return 1 if run.failed else 0


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, commands.program_name(__spec__.name),
                commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
