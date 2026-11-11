"""convert-video's encodes, and the mux that puts the file together.

The video encode - whole, or one chunk of a chunked plan, or through the
upscaler - writes into the run's scratch; the audio is encoded track by
track beside it; and the mux brings both together with the source's
subtitles, attachments, chapters and metadata into the output. When the
mux fails, the finished video is still written out on its own, marked
" (video only)", because it is the expensive part and must not be lost to
the cheap ones.
"""

import os
import shutil

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_probes as probes
from medialib.lib import (
    formatting,
    pausecontrol,
    runlog,
    safety,
    segments,
    toolcapture,
    upscale,
)
from medialib.lib.runlog import log

# --- the encodes --------------------------------------------------------------

def run_quiet_encode(argv: list, label: str = "",
                     tool: str = "ffmpeg") -> int:
    """One video encode, with the p/r keys able to stop and continue it and its
    output kept rather than shown.

    Nothing the encode prints reaches the console while it runs: libsvtav1 alone
    writes its banner and resolved configuration once per encoder instance -
    once PER CHUNK of a chunked encode - and parallel chunks would interleave
    that over the status row. An encode that fails is replayed instead, with the
    lines that say why, and kept in `logs/`; one that succeeds is never heard
    from.

    Every video encode goes through here, whole file or chunk, software or NVENC,
    which is what makes one keypress reach all of them at once.
    """
    output = toolcapture.ToolOutput()
    status = pausecontrol.run_pausable(argv, output=output)
    if status != 0:
        toolcapture.report_failure(toolcapture.finished(
            argv, status, output, label=label, tool=tool))
    return status


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
    out = os.path.join(directory, "video.mkv")
    progress = os.path.join(directory, "prog.0000")
    if settings.upscale_size:
        return run_upscaled_encode(settings, source, args, progress, out, 0, -1,
                                   relative)
    argv = (["ffmpeg", "-nostdin", "-hide_banner",
             *toolcapture.ffmpeg_loglevel(), "-nostats", "-progress", progress,
             "-y"] + settings.decode_accel.split()
            + ["-i", source, "-an", "-sn", "-map", "0:v:0"] + args.split()
            + ["-f", "matroska", out])
    return run_quiet_encode(argv, relative)


def run_upscaled_encode(settings, source: str, args: str, progress: str,
                        out: str, start: int, end: int, label: str = "") -> int:
    """Frames <start> up to <end> (-1: to the last) of <source> upscaled and
    encoded into <out>.

    The upscaler reads the source itself, so there is no -ss and no trim here:
    it is handed frame numbers, and its crop and resize replace ffmpeg's filter
    chain. A failure removes what was written, because the ffmpeg at the end of
    the pipe finishes cleanly on whatever it was given, and a chunk that stopped
    a few frames short would otherwise pass for a whole one.
    """
    encode = (["ffmpeg", "-nostdin", "-hide_banner",
               *toolcapture.ffmpeg_loglevel(), "-nostats", "-progress", progress,
               "-y",
               "-f", "yuv4mpegpipe", "-i", "pipe:0"] + args.split()
              + ["-f", "matroska", out])
    width, height = settings.upscale_size.split("x")
    fps_num, _sep, fps_den = settings.upscale_fps.partition("/")
    values = {"source": source, "cache": settings.upscale_index,
              "fpsnum": fps_num, "fpsden": fps_den or "1",
              "start": str(start), "end": str(end),
              "crop": settings.upscale_crop, "matrix": settings.upscale_matrix,
              "range": settings.upscale_range, "engine": settings.upscale_engine,
              "width": width, "height": height}
    status = run_quiet_encode(upscale.pipeline_argv(
        settings.upscale_stack, settings.upscale_script, values, encode),
        label, tool="vspipe | ffmpeg")
    if status != 0:
        probes._remove(out)
    return status


def encode_video_chunk(settings, token: str) -> int:
    """One time-range of a source into a video-only Matroska chunk: a single
    parallel job.

    The same profile settings a whole-file encode uses, so every chunk is a
    keyframe-aligned, stream-copy-compatible piece.
    """
    safety.trap_worker_abort()
    relative, index, total, start, duration = token.split(rules.UNIT)

    source = rules.video_source_for(relative, settings.input_dir,
                                    settings.dolby_vision_source)
    directory = segments.chunk_dir_for(settings.chunk_root, relative)
    os.makedirs(directory, exist_ok=True)
    out = os.path.join(directory, "%04d.mkv" % int(index))

    args = rules.build_video_args(
        rules.profile_args(rules.VIDEO_PROFILES, settings.video_profile),
        source, settings)
    progress = os.path.join(directory, "prog.%04d" % int(index))
    chunk_label = "%s (chunk %d of %s)" % (relative, int(index) + 1, total)
    if settings.upscale_size:
        # The same boundaries in frames: each chunk ends at the frame the next
        # one starts at, and the last one runs to the end of the stream. The end
        # is put back to the boundary as written, millisecond for millisecond,
        # because the next chunk's first frame is worked out from that text: a
        # sum a hair either side of a half frame would round the other way.
        first = upscale.frame_at(start, settings.upscale_fps)
        end = "%.3f" % (formatting.awk_number(start)
                        + formatting.awk_number(duration))
        last = (upscale.frame_at(end, settings.upscale_fps)
                if int(index) < int(total) - 1 else -1)
        return run_upscaled_encode(settings, source, args, progress, out,
                                   first, last, chunk_label)
    # The last chunk runs to the end of the stream rather than to the container's
    # duration, so a final frame past that rounded figure is not lost.
    if int(index) < int(total) - 1:
        args = rules.with_chunk_end(args, duration)
    argv = (["ffmpeg", "-nostdin", "-hide_banner",
             *toolcapture.ffmpeg_loglevel(), "-nostats", "-progress", progress,
             "-y"]
            + settings.decode_accel.split()
            + ["-ss", start, "-i", source, "-an", "-sn",
               "-map", "0:v:0"] + args.split() + ["-f", "matroska", out])
    return run_quiet_encode(argv, chunk_label)


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
    # chunks are re-joined - and the console erases it for this line.
    log("Joining %d video chunks: %s" % (total, relative))
    return toolcapture.run(
        ["ffmpeg", "-nostdin", "-hide_banner", *toolcapture.ffmpeg_loglevel(),
         "-nostats", "-y", "-safe", "0", "-f", "concat",
         "-i", listing, "-map", "0:v", "-c", "copy", "-f", "matroska",
         os.path.join(directory, "video.mkv")], relative).returncode


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
            args=(source, str(index), bitrate, track_args, out,
                  "%s (audio track %d)" % (relative, index + 1)))
        process.start()
        workers.append(process)

    status = 0
    for process in workers:
        process.join()
        if process.exitcode != 0:
            status = 1
    return status


def _encode_audio_track(source: str, index: str, bitrate: str, audio_args: str,
                        out: str, label: str = "") -> None:
    """ONE audio track to its own Matroska.

    Mapping a single audio stream carries its metadata, language and disposition
    across automatically, so the final mux keeps them.
    """
    safety.trap_worker_abort()
    argv = (["ffmpeg", "-nostdin", "-hide_banner",
             *toolcapture.ffmpeg_loglevel(), "-nostats", "-y", "-i", source,
             "-map", "0:a:" + index, "-vn", "-sn"] + audio_args.split()
            + ["-b:a", bitrate + "k", "-f", "matroska", out])
    raise SystemExit(toolcapture.run(argv, label).returncode)


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
    output = rules.output_path_of(relative, settings.input_dir,
                                  settings.output_dir)
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
    if settings.keep_tracks:
        return _mux_keeping_tracks(relative, directory, source, output)
    audio_args = rules.profile_args(rules.AUDIO_PROFILES,
                                    settings.audio_profile)

    argv = ["ffmpeg", "-nostdin", "-hide_banner",
            *toolcapture.ffmpeg_loglevel(), "-nostats", "-y",
            "-i", os.path.join(directory, "video.mkv"),
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
    muxed = toolcapture.run(argv, relative, report=False)
    # A mux that failed on a folder that has just gone is retried, once, on the
    # folder put back: stream-copying the parts together again is minutes at
    # most, against the hours of encoding they took to produce.
    if not muxed.ok and restore_output_folder(output):
        muxed = toolcapture.run(argv, relative, report=False)
    if not muxed.ok:
        toolcapture.report_failure(muxed)
        return muxed.returncode
    # The output gets the source's modification time, like the sibling scripts do.
    try:
        stamp = os.stat(source)
        os.utime(output, (stamp.st_atime, stamp.st_mtime))
    except OSError:
        pass
    return 0


def _mux_keeping_tracks(relative: str, directory: str, source: str,
                        output: str) -> int:
    """-k's final output: the encoded video, then every other track of the
    source exactly as it is there - audio, subtitles, attachments, chapters,
    tags - combined by mkvmerge, which keeps each track's name, language and
    flags where an ffmpeg remap would translate them.

    Only the video comes from the encode, and only the source's video is left
    out of it, so a Dolby Vision enhancement layer muxed as a track of its own
    goes with the picture it belonged to.
    """
    argv = ["mkvmerge", "--quiet", "-o", output,
            "--no-audio", "--no-subtitles", "--no-buttons", "--no-attachments",
            "--no-chapters", "--no-track-tags", "--no-global-tags",
            os.path.join(directory, "video.mkv"),
            "--no-video", source]
    def written(muxed) -> bool:
        # mkvmerge exits 1 for warnings, with the file written.
        return muxed.ok or (muxed.returncode == 1 and os.path.isfile(output))

    log("Muxing: " + relative)
    muxed = toolcapture.run(argv, relative, report=False)
    # Retried once on a folder that has just gone, as the ffmpeg mux is.
    if not written(muxed) and restore_output_folder(output):
        muxed = toolcapture.run(argv, relative, report=False)
    if not written(muxed):
        toolcapture.report_failure(muxed)
        return muxed.returncode or 1
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
    output = rules.video_only_path_for(relative, settings.output_dir,
                                     settings.input_dir)
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
            runlog.error("the video-only failsafe could not be written, the "
                         "video encode is lost: %s" % relative,
                         files=[relative, encoded, output])
            return False
    try:
        stamp = os.stat(source)
        os.utime(output, (stamp.st_atime, stamp.st_mtime))
    except OSError:
        pass
    return True
