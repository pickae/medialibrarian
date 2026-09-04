"""convert-audio: a tree of spoken-word audio ingested as Opus.

Video files are ingested too: their audio stream is extracted and converted like
any other input.

Two questions decide what happens to each file. Does it get an .opus of its own
at all - yes for every video, for the formats the output must not keep whatever
their size, and for anything above the bitrate threshold. And is that .opus
ENCODED or LIFTED OUT - lifted when the source's audio already IS what this
pipeline produces, an Opus below the threshold, which happens all the time inside
a video pulled off the web: encoding 46 kbps Opus into 46 kbps Opus changes
nothing except to spend another lossy generation on it.

A file longer than the split threshold is cut into one chunk per core, and the
chunks encode as independent queue jobs mixed in with every other file - so one
huge file does not pin a single core while the rest of the machine idles. The
cuts are nudged to the nearest quiet spot, so the seam between separately encoded
chunks is inaudible. Finding those quiet spots is the one expensive part of
planning, and it is windowed: each interior cut becomes a small silencedetect job
and they all run in one flat pool, so a lone three-hour file spreads its search
over its own cut windows instead of one core decoding the whole thing.
"""

import os
import re
import shutil
import subprocess
import sys
import time

from medialib import commands
from medialib.lib import (
    bitrates,
    chapters,
    clioptions,
    enums,
    ffmpegselect,
    formatting,
    imagemagick,
    imagesizes,
    ramscratch,
    runlog,
    safety,
    segments,
    statusline,
    thumbnails,
    tooldeps,
    workerpool,
)
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>
Video files are ingested as well: their audio stream is extracted and converted
like any other input.
Options:"""

CREDITS_LINE = "ingest spoken word audio files"

# The spec is DATA, and the page it renders is compared byte for byte against the
# recorded contract under tests/data/cliContract.
OPT_SPEC = """
h |  | Print this help page.
m |  | Force mono output.
                    Default false
a |  | Adaptive mode: decide channels and bitrate per file rather
                    than globally. The output keeps the source's own channel
                    count, and its target bitrate is the spoken-word (commentary)
                    figure listed for that channel count in the shared bitrate
                    table, so a mono source encodes mono at the lower rate, a
                    stereo one stays stereo, and a multi-channel one gets its own
                    rate. Cannot be combined with -m or -b, and disables long-file
                    splitting.
                    Default false
k |  | Keep temporary files.
                    Default false
c |  | Copy non transcoded files from <inputDir> into <outputDir>
b | <bitrate> | Bitrate of the output files
                    Default 46 kbps or 32 kbps for forced mono output
j | <jobs> | Run up to <jobs> encoder processes in parallel.
                    Default one per CPU thread
s | <seconds> | Split files longer than <seconds> into one chunk per logical
                    CPU core, which encode in parallel alongside the other files,
                    then transparently re-concatenate. 0 disables splitting.
                    Default 10000
"""

OPT_VARS = "m:mono a:adaptive c:copy k:keep b:bitrate j:jobs s:splitThreshold"
OPT_COLUMN = 20
OPT_LONG = ("h:help m:mono a:adaptive k:keep c:copy-others b:bitrate j:jobs "
            "s:split-threshold")

DEFAULT_BITRATE = 46
MONO_BITRATE = 32
THRESHOLD = 90000
MONO_THRESHOLD = 50000
COVER_THRESHOLD = 500000
COVER_QUALITY = 75

# Files longer than this many seconds are cut into one chunk per logical core.
DEFAULT_SPLIT_THRESHOLD = 10000
SILENCE_NOISE = "-30dB"
SILENCE_MIN_DUR = "0.3"

# How much of an audio stream is weighed when nothing states its bitrate, and how
# long the weighing may take: one demux seek plus this many seconds of packet
# headers, no decoding at all.
BITRATE_PROBE_SECONDS = 60

# The separator inside a chunk job token. A track's path may contain anything
# else.
UNIT = "\x1f"


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
        credits=CREDITS_LINE,
    )


def _probe(argv: list) -> str:
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    return done.stdout.decode("utf-8", "surrogateescape").strip()


def resolve_bitrate(bitrate: int, mono: bool) -> int:
    """The Opus bitrate for a track, honouring the forced-mono default swap.

    Kept apart so the chunk encoder produces bit-for-bit the same settings as a
    whole-file encode.
    """
    if mono and bitrate == DEFAULT_BITRATE:
        return MONO_BITRATE
    return bitrate


def is_video_file(path: str) -> bool:
    """Whether a track's extension is a video container.

    Video inputs are ingested in every mode: only their audio stream is taken,
    and their video stream is never mistaken for cover art.
    """
    return enums.lower_extension_of(path) in enums.VIDEO_EXTENSIONS


def always_transcode_file(path: str) -> bool:
    """Whether this is a format the output must not keep whatever its bitrate
    says - unwanted as a format, not as a size."""
    return (enums.lower_extension_of(path)
            in enums.ALWAYS_TRANSCODE_EXTENSIONS)


def source_audio_codec(src: str) -> str:
    """The codec of a source's FIRST AUDIO stream, lower-cased, or nothing.

    That one stream is what the encode maps, and what a stream copy would lift
    out.
    """
    return _probe(["ffprobe", "-v", "quiet", "-select_streams", "a:0",
                   "-show_entries", "stream=codec_name",
                   "-of", "default=nk=1:nw=1", src]).split("\n")[0].lower()


def estimated_audio_bitrate(src: str) -> int:
    """The bitrate of the first audio stream, MEASURED rather than read - the
    bytes its packets really take over the time they really cover.

    Needed because the containers this matters most for state nothing: Matroska
    and WebM carry no per-stream bitrate field, and the BPS tags that stand in
    for one are written by mkvmerge but not by ffmpeg - so the very files this is
    about (a video pulled off the web, muxed by ffmpeg, with an Opus soundtrack)
    report nothing at all.

    Measured over a window from the MIDDLE of the file, for the same reason a
    voice sample is taken from there: the opening of a recording is titles and
    silence, which is not what the rest of it costs. Packets are only listed,
    never decoded, so this is a seek and a scan of headers.
    """
    duration = formatting.awk_number(
        _probe(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                "-of", "default=nk=1:nw=1", src]))
    start = duration / 2 - BITRATE_PROBE_SECONDS / 2
    if start < 0 or duration <= 0:
        start = 0
    listed = _probe(["ffprobe", "-v", "quiet", "-select_streams", "a:0",
                     "-show_entries", "packet=size,duration_time",
                     "-of", "compact=p=0",
                     "-read_intervals", "%d%%+%d" % (round(start),
                                                     BITRATE_PROBE_SECONDS),
                     src])
    total_bytes = 0.0
    total_seconds = 0.0
    for line in listed.split("\n"):
        for field in line.split("|"):
            key, _, value = field.partition("=")
            if key == "size":
                total_bytes += formatting.awk_number(value)
            elif key == "duration_time":
                total_seconds += formatting.awk_number(value)
    if total_seconds > 0:
        return int(total_bytes * 8 / total_seconds)
    return 0


def source_audio_bitrate(src: str) -> int:
    """The bitrate of a source's FIRST AUDIO stream, or 0 when nothing states it.

    The container's overall bitrate is NOT that figure: it is the sum of every
    stream, so a video's is dominated by its picture and says nothing about its
    soundtrack, and even a plain audio file's is inflated by cover art and muxing
    overhead.

    Not every container states a per-stream bitrate: MP4 does, Matroska does not -
    mkvmerge writes it as a per-track "BPS" tag instead. As a last resort the
    container bitrate is used, but ONLY for a source with no real video stream,
    where the two figures are the same number bar the cover. A video whose audio
    bitrate stays unknown therefore reports 0, which is read as "unknown" and not
    as "small".
    """
    import json

    raw = _probe(["ffprobe", "-loglevel", "0", "-print_format", "json",
                  "-show_format", "-show_streams", src])
    try:
        document = json.loads(raw or "{}")
    except ValueError:
        return 0
    # jq's `.streams[]?` yields nothing for anything that is not an object, and
    # the chain falls through to 0 - so a probe that answered a bare number, or a
    # list, is "nothing stated it" rather than an error.
    if not isinstance(document, dict):
        return 0

    streams = document.get("streams") or []
    audio = [stream for stream in streams
             if stream.get("codec_type") == "audio"]
    first = audio[0] if audio else {}
    videos = [stream for stream in streams
              if stream.get("codec_type") == "video"
              and not (stream.get("disposition") or {}).get("attached_pic", 0)]

    value = first.get("bit_rate")
    if value is None:
        # Matched case-insensitively on the "bps" prefix, because the suffix
        # spelling is not fixed.
        for key, tag in (first.get("tags") or {}).items():
            if key.lower().startswith("bps"):
                value = tag
                break
    if value is None and not videos:
        value = (document.get("format") or {}).get("bit_rate")
    if value is None:
        return 0
    return int(value) if re.fullmatch(r"[0-9]+", str(value)) else 0


def source_audio_is_finished(src: str, bitrate, limit) -> bool:
    """Whether a source's audio already IS what this pipeline produces, and small
    enough to be worth keeping as it is: Opus, below the re-encode threshold.

    The bitrate is the one the caller already probed; a 0 from that means
    "nothing stated it", which is not the same as "small", so it is MEASURED
    instead rather than assumed either way. That second probe only ever runs for
    a stream that is already Opus - the codec is the cheap question and is asked
    first - so nothing else in a run pays for it.

    What is deliberately NOT asked is the channel count: -m does not downmix a
    file under the threshold either, so a stereo stream small enough to keep is
    kept for exactly the same reason.
    """
    if source_audio_codec(src) != "opus":
        return False
    value = bitrate if re.fullmatch(r"[0-9]+", str(bitrate)) else 0
    if int(value) <= 0:
        value = estimated_audio_bitrate(src)
    if int(value) <= 0:
        return False
    return int(value) < int(limit)


def adaptive_channels(src: str) -> int:
    """The channel count of the source's first AUDIO stream.

    Probing the audio stream rather than stream 0 means a video file is judged by
    its soundtrack, so an extracted video audio track adapts exactly like a plain
    audio file. An unknown count is read as stereo.
    """
    raw = _probe(["ffprobe", "-v", "quiet", "-select_streams", "a:0",
                  "-show_entries", "stream=channels",
                  "-of", "default=nk=1:nw=1", src]).split("\n")[0]
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) < 1:
        return 2
    return int(raw)


def adaptive_bitrate(channels, default: int) -> int:
    """The Opus target for a channel count, from the shared table's COMMENTARY
    column - the spoken-word one, which is exactly what this script ingests.

    A channel count the table has no row for falls back to the stereo row, and an
    empty table to the script's own default.
    """
    value = (bitrates.audio_bitrate(str(channels), "comment")
             or bitrates.audio_bitrate("2", "comment"))
    return int(value) if value else default


def detect_window(token: str, noise: str, min_duration: str) -> None:
    """silencedetect ONE window of a file, writing the midpoints it found.

    The one piece of the planning that is an ffmpeg decode rather than a
    calculation, which is why the shared library has the arithmetic either side
    of it and not this.
    """
    src, window_start, window_length, mids_file = token.split(UNIT)
    try:
        done = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-copyts",
             "-ss", window_start, "-t", window_length, "-i", src,
             "-af", "silencedetect=noise=%s:d=%s" % (noise, min_duration),
             "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        text = done.stdout.decode("utf-8", "surrogateescape")
    except OSError:
        text = ""

    midpoints = []
    for line in text.split("\n"):
        fields = line.split()
        start = None
        for index, field in enumerate(fields):
            if field == "silence_start:" and index + 1 < len(fields):
                start = formatting.awk_number(fields[index + 1])
            elif field == "silence_end:" and index + 1 < len(fields) \
                    and start is not None:
                end = formatting.awk_number(fields[index + 1])
                midpoints.append("%.3f" % ((start + end) / 2))
                start = None
    try:
        with open(mids_file, "w") as handle:
            handle.write("".join(point + "\n" for point in midpoints))
    except OSError:
        pass


class Counters:
    """The shared counters, the per-file line, and the live status row.

    The same lock keeps the console coherent: the row is erased before a line is
    printed and re-pinned underneath it afterwards, and the row's own background
    refresher takes this very lock before it draws, so the two never write over
    each other.
    """

    def __init__(self, progress_file: str, duration_file: str, total: int,
                 run_start_epoch: int) -> None:
        self.progress_file = progress_file
        self.duration_file = duration_file
        self.total = total
        self.run_start_epoch = run_start_epoch

    def _read(self, path: str, default=0):
        try:
            with open(path) as handle:
                return handle.read().strip() or default
        except OSError:
            return default

    def report_progress(self, label: str) -> None:
        with open(self.progress_file + ".lock", "w") as lock, \
                runlog.take_lock(lock):
            try:
                current = int(self._read(self.progress_file, "0")) + 1
            except ValueError:
                current = 1
            with open(self.progress_file, "w") as handle:
                handle.write("%d\n" % current)
            statusline.clear_status()
            sys.stdout.write("%sConverting: %s\n" % (
                runlog.counted_prefix(current, self.total), label))
            sys.stdout.flush()
            statusline.repin_status(self.status_text)

    def tally_duration(self, add) -> None:
        """The audio-seconds actually encoded, serialised across the parallel
        jobs. Tallied as work happens rather than re-probed at the end, so a
        split file's chunk durations sum to exactly its own length - counted
        once, never doubled with the whole-file figure."""
        if not add:
            return
        with open(self.duration_file + ".lock", "w") as lock, \
                runlog.take_lock(lock):
            current = formatting.awk_number(self._read(self.duration_file, "0"))
            with open(self.duration_file, "w") as handle:
                handle.write("%.3f\n" % (current + formatting.awk_number(add)))

    def status_text(self) -> str:
        """ONE line saying how the whole run is doing, pinned at the bottom while
        the per-file lines scroll past above it.

        Deliberately no ETA: the remaining jobs are files of unknown length - a
        queue with two jobs left can hold ten seconds of work or ten hours - so
        extrapolating from the position in it would be inventing a number.
        """
        try:
            current = int(self._read(self.progress_file, "0"))
        except ValueError:
            current = 0
        with open(self.duration_file + ".lock", "w") as lock, \
                runlog.take_lock(lock):
            duration = formatting.awk_number(self._read(self.duration_file, "0"))

        elapsed = max(0, int(time.time()) - self.run_start_epoch)
        speed = formatting.fmt_ratio(
            "%.6f" % (duration / elapsed) if elapsed > 0 else "0")
        # Without flock the counter can lose increments, so the row says how much
        # work there is without claiming a position in it.
        position = ("%d/%d jobs" % (current, self.total)
                    if runlog.have_flock()
                    else "%d jobs" % self.total)
        return "  encoding %s: elapsed %s  encoded %s  %sx realtime" % (
            position, formatting.fmt_clock(elapsed),
            formatting.fmt_clock("%.3f" % duration), speed)


class Run:
    """One run's settings, and the work the queue jobs do."""

    # Declared, not defaulted: the settings dict supplies every one, so a name
    # it does not carry is still an AttributeError at the read.
    input_dir: str
    output_dir: str
    script_dir: str
    mono: bool
    adaptive: bool
    copy: bool
    keep: bool
    bitrate: int
    threshold: int
    split_threshold: int
    # A geometry, not None: DEFAULT_TIER is a row of the table by construction.
    cover_resolution: str
    ram_base: str
    tracks: list
    duration_file: str
    chunk_root: str
    plan_root: str
    counters: "Counters"
    # The four phase clocks, unset until the phase they time has begun.
    pre_start: float
    conv_start: float | None
    conv_end: float | None
    post_start: float | None
    post_end: float | None

    def __init__(self, **settings) -> None:
        self.__dict__.update(settings)

    # --- encoding -------------------------------------------------------------

    def encode(self, token: str) -> None:
        """One queue job: a whole file, or one time-range of a long one."""
        if UNIT in token:
            self.encode_chunk(token)
            return
        self.encode_whole(token)

    def encode_chunk(self, token: str) -> None:
        """One time-range straight to Opus, with no metadata: metadata is
        re-attached once, from the original, after re-concatenation."""
        relative, index, total, start, duration = token.split(UNIT)
        self.counters.report_progress(
            "%s [chunk %d/%s]" % (relative, int(index) + 1, total))

        directory = segments.chunk_dir_for(self.chunk_root, relative)
        os.makedirs(directory, exist_ok=True)
        out = os.path.join(directory, "%04d.opus" % int(index))
        bitrate = resolve_bitrate(self.bitrate, self.mono)

        argv = ["ffmpeg", "-nostdin", "-y", "-ss", start, "-t", duration,
                "-i", os.path.join(self.input_dir, relative),
                "-map", "0:a:0", "-map_metadata", "-1"]
        if self.mono:
            argv += ["-ac", "1"]
        argv += ["-c:a", "libopus", "-b:a", "%dk" % bitrate, out]
        subprocess.run(argv, stderr=subprocess.DEVNULL)

        # A chunk's slice of audio; the chunks of one file sum to its length.
        self.counters.tally_duration(duration)

    def encode_whole(self, relative: str) -> None:
        self.counters.report_progress(relative)

        source = os.path.join(self.input_dir, relative)
        opus = os.path.join(self.output_dir,
                            os.path.splitext(relative)[0] + ".opus")

        # Video sources are flagged so their audio stream is extracted, and their
        # video stream is not mistaken for cover art - in every mode. Adaptive
        # mode additionally derives this file's own channel and bitrate decision
        # from its first audio stream instead of the global -m/-b.
        video = is_video_file(relative)
        mono, bitrate, threshold = self.mono, self.bitrate, self.threshold
        if self.adaptive:
            channels = adaptive_channels(source)
            mono = channels <= 1
            bitrate = adaptive_bitrate(channels, self.bitrate)

        input_duration_raw = _probe(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", source])
        input_duration = round(formatting.awk_number(input_duration_raw))
        output_duration = round(formatting.awk_number(_probe(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", opus])))
        source_bitrate = source_audio_bitrate(source)

        if os.path.isfile(opus) and input_duration <= output_duration:
            self._reapply_sidecar_cover(relative, source, opus)
            return

        if mono:
            threshold = MONO_THRESHOLD
            # Only the DEFAULT bitrate is swapped - a provided -b is kept, and so
            # is the per-file target adaptive mode already looked up, which is the
            # whole point of that mode.
            if not self.adaptive and bitrate == DEFAULT_BITRATE:
                bitrate = MONO_BITRATE

        tmp_dir, status = ramscratch.ram_scratch_dir("convertAudio")
        if status != 0 or not tmp_dir:
            return
        try:
            self._produce(relative, source, opus, video, mono, bitrate,
                          threshold, source_bitrate, tmp_dir,
                          input_duration_raw)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _produce(self, relative: str, source: str, opus: str, video: bool,
                 mono: bool, bitrate: int, threshold: int, source_bitrate: int,
                 tmp_dir: str, input_duration_raw: str) -> None:
        # Only a video ever reaches the lift-out question: a PLAIN file whose
        # audio is already finished never got past the first one - it is its own
        # .opus already, and copying it verbatim is what -c is.
        lift_out = video and source_audio_is_finished(source, source_bitrate,
                                                      threshold)
        cover_target = ""

        if video or always_transcode_file(relative) \
                or source_bitrate >= threshold:
            # One ffmpeg pass writes the .opus either way, and everything after
            # it is the same for both. The codec is the only difference: a
            # straight encode, or "-c:a copy" remuxing the source's own Opus
            # stream sample for sample with no encoder in the path at all.
            if lift_out:
                codec = ["-c:a", "copy"]
            else:
                codec = ["-c:a", "libopus", "-b:a", "%dk" % bitrate]
                if mono:
                    codec = ["-ac", "1"] + codec
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-i", source, "-map", "0:a:0",
                 "-map_metadata", "0:s:0"] + codec + [opus],
                stderr=subprocess.DEVNULL)

            # libopus does not carry chapters through, so they are re-attached
            # from the source with mutagen.
            chapters.attach_chapters(source, opus, tmp_dir, self.script_dir)
            # Cover art often lives ONLY inside the source, so it is pulled from
            # there; a sidecar image sharing the track name overrides it below.
            # Skipped for a video, whose "video stream" is not cover art.
            if not video:
                thumbnails.extract_source_cover(source, tmp_dir)
            cover_target = opus
        elif self.copy:
            copied = os.path.join(self.output_dir, relative)
            os.makedirs(os.path.dirname(copied) or self.output_dir,
                        exist_ok=True)
            shutil.copyfile(source, copied)
            cover_target = copied

        if cover_target:
            thumbnails.apply_cover(relative, cover_target, COVER_THRESHOLD,
                                   COVER_QUALITY, self.cover_resolution,
                                   self.input_dir, self.output_dir, tmp_dir,
                                   opus, self.script_dir)
            # The source's modification time, set last: the encode and the
            # mutagen writes each bump it, so the final timestamp matches the
            # source rather than whichever edit happened to run last.
            _touch_from(source, cover_target)
            # Counted once, and only when output was actually produced: a skipped
            # low-bitrate no-op leaves no target and tallies nothing.
            self.counters.tally_duration(input_duration_raw)

        if self.keep:
            for name, suffix in (("chapters.ogm", ".chapters.txt"),
                                 ("tempCover.jpg", ".temp.jpg"),
                                 ("cover.jpg", ".cover.jpg")):
                kept = os.path.join(tmp_dir, name)
                if os.path.isfile(kept):
                    shutil.copyfile(kept, os.path.join(
                        self.output_dir,
                        os.path.splitext(relative)[0] + suffix))

    def _reapply_sidecar_cover(self, relative: str, source: str,
                               existing: str) -> None:
        """The encode was skipped because an up-to-date output exists. Even so,
        honour a sidecar image sharing the track's name: re-embed it over
        whatever the existing output carries, through the same path a fresh
        encode uses. A no-op when there is no sidecar."""
        stem = os.path.splitext(relative)[0]
        if not any(os.path.isfile(os.path.join(self.input_dir, stem + suffix))
                   for suffix in (".webp", ".jpg")):
            return
        tmp_dir, status = ramscratch.ram_scratch_dir("convertAudio")
        if status != 0 or not tmp_dir:
            return
        try:
            thumbnails.apply_cover(relative, None, COVER_THRESHOLD,
                                   COVER_QUALITY, self.cover_resolution,
                                   self.input_dir, self.output_dir, tmp_dir,
                                   existing, self.script_dir)
            # Re-embedding bumped the existing output's mtime; restore the
            # source's so the output keeps matching the input.
            _touch_from(source, existing)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def reconcat(self, token: str) -> None:
        """A track's Opus chunks joined into the final output, with every piece
        of metadata re-attached from the ORIGINAL file.

        The whole assembly is done on a RAM-backed staging copy and only the
        finished file is written out, once: mutagen rewrites the entire Opus file
        on every chapter and cover embed, so keeping those rewrites in RAM means
        the disk sees a single sequential write instead of three
        read-modify-write passes.
        """
        relative, _, total = token.partition("\t")
        source = os.path.join(self.input_dir, relative)
        opus = os.path.join(self.output_dir,
                            os.path.splitext(relative)[0] + ".opus")
        directory = segments.chunk_dir_for(self.chunk_root, relative)
        chunk_files = [os.path.join(directory, "%04d.opus" % index)
                       for index in range(int(total))]

        sys.stdout.write("Joining %s chunks: %s\n" % (total, relative))
        sys.stdout.flush()

        tmp_dir, status = ramscratch.ram_scratch_dir("convertAudio.rc")
        if status != 0 or not tmp_dir:
            return
        stage = os.path.join(tmp_dir, "out.opus")
        try:
            listing = os.path.join(tmp_dir, "concat.txt")
            with open(listing, "w") as handle:
                for chunk in chunk_files:
                    handle.write("file '%s'\n" % chunk)
            # The segments joined AND the source's stream tags carried over in
            # one pass, straight into the RAM staging file.
            subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-y", "-safe", "0", "-f", "concat", "-i", listing,
                 "-i", source, "-map", "0:a:0", "-map_metadata", "1:s:0",
                 "-c:a", "copy", stage], stderr=subprocess.DEVNULL)

            # The staged output carries all the audio now, so the chunk copies
            # are dead weight: the steps below read only the ORIGINAL source.
            # Freed here, before the slower mutagen writes, so a split file's
            # chunk memory is reclaimed as early as possible instead of lingering
            # through the whole tail. Guarded on a non-empty output, so a failed
            # join keeps its chunks for inspection.
            if not self.keep and os.path.getsize(stage) > 0:
                shutil.rmtree(directory, ignore_errors=True)

            chapters.attach_chapters(source, stage, tmp_dir, self.script_dir)
            # A video source's "video stream" is not cover art, so it is not
            # pulled in - a sidecar image still applies, the same rule a
            # whole-file encode follows.
            if not is_video_file(relative):
                thumbnails.extract_source_cover(source, tmp_dir)
            thumbnails.apply_cover(relative, stage, COVER_THRESHOLD,
                                   COVER_QUALITY, self.cover_resolution,
                                   self.input_dir, self.output_dir, tmp_dir,
                                   stage, self.script_dir)

            # The source's modification time, after the mutagen writes that each
            # bumped it, so the final timestamp matches the source.
            _touch_from(source, stage)
            os.makedirs(os.path.dirname(opus) or self.output_dir,
                        exist_ok=True)
            shutil.move(stage, opus)
            _touch_from(source, opus)
        except OSError:
            pass
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _touch_from(source: str, target: str) -> None:
    try:
        stamp = os.stat(source)
        os.utime(target, (stamp.st_atime, stamp.st_mtime))
    except OSError:
        pass


class Planner:
    """The three-phase expansion of the track list into one flat job queue.

    A. classify every track with cheap probes: whole, skipped, or a split
       candidate.
    B. turn every candidate's interior cut into a small silencedetect WINDOW job,
       and run all of them - across all candidates - in one flat pool. A lone
       three-hour file thus spreads its search over its own cut windows instead of
       one core decoding the whole thing, and many-file runs still fill the cores
       from one pool.
    C. turn each candidate's gathered midpoints into chunk jobs, which is cheap.

    The per-track outputs are stitched back together in track order, so the
    queue's interleaved chunk/whole-file ordering matches a serial walk.
    """

    def __init__(self, state: Run, jobs: int) -> None:
        self.state = state
        self.jobs = jobs

    def classify(self, track: str) -> None:
        """ONE track, cheap probes only. A whole or skipped track is fully
        resolved here; a split candidate drops just its duration, and the
        expensive boundary search is deferred to phase B."""
        state = self.state
        base = segments.plan_file_for(state.plan_root, track)
        source = os.path.join(state.input_dir, track)

        if state.split_threshold <= 0:
            _write_jobs(base, [track])
            return

        duration = round(formatting.awk_number(_probe(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", source])))
        if duration <= state.split_threshold:
            _write_jobs(base, [track])
            return

        # Only files that would actually be RE-ENCODED gain from chunking. A
        # verbatim copy, a skipped low-bitrate file and a video whose Opus
        # soundtrack is only lifted out all stay whole - the last emphatically so,
        # since chunking it would mean encoding the very audio the lift-out exists
        # to preserve.
        threshold = MONO_THRESHOLD if state.mono else state.threshold
        source_bitrate = source_audio_bitrate(source)
        video = is_video_file(track)
        if video and source_audio_is_finished(source, source_bitrate,
                                              threshold):
            _write_jobs(base, [track])
            return
        if not video and not always_transcode_file(track) \
                and source_bitrate < threshold:
            _write_jobs(base, [track])
            return

        opus = os.path.join(state.output_dir,
                            os.path.splitext(track)[0] + ".opus")
        if os.path.isfile(opus):
            output_duration = round(formatting.awk_number(_probe(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=nk=1:nw=1", opus])))
            if duration <= output_duration:
                sys.stdout.write("Up to date, skipping: %s\n" % track)
                sys.stdout.flush()
                return

        with open(base + ".meta", "w") as handle:
            handle.write(str(duration))

    def window_jobs(self, tracks: list) -> tuple:
        """Phase B's flat queue of window probes, and the candidates it is for."""
        state = self.state
        candidates, queue = [], []
        for track in tracks:
            base = segments.plan_file_for(state.plan_root, track)
            if not os.path.isfile(base + ".meta"):
                continue
            with open(base + ".meta") as handle:
                duration = formatting.awk_number(handle.read())
            plan = segments.seg_plan(duration, self.jobs)
            fields = plan.split() if plan else []
            count = int(fields[0]) if plan else 0
            if count < 2:
                # A candidate is longer than the threshold so this should not
                # happen, but a file that would not split stays whole.
                _write_jobs(base, [track])
                _remove(base + ".meta")
                continue
            segment, window = float(fields[1]), float(fields[2])
            candidates.append(track)

            seek_source = self._seek_copy(track, base)
            # One probe window per interior cut, centred on the ideal boundary and
            # widened by the same half-segment nudge window, plus a 2s guard so a
            # silence sitting on a window edge is still seen whole.
            for index in range(1, count):
                ideal = index * segment
                start = max(0.0, ideal - window - 2)
                end = min(duration, ideal + window + 2)
                queue.append(UNIT.join(["%s" % seek_source, "%.6f" % start,
                                        "%.6f" % (end - start),
                                        "%s.mids.%d" % (base, index)]))
        return candidates, queue

    def _seek_copy(self, track: str, base: str) -> str:
        """A RAM-backed, seek-cheap copy of just this candidate's audio stream.

        Containers like m4b keep a per-sample index that ffmpeg parses ENTIRELY
        into RAM on every open, even to seek a two-second window - so the interior
        probes, running in parallel, would each load that whole index and spike
        memory to jobs x (full index) for one huge file. Stream-copying the audio
        once into Matroska gives a sparse cue index instead, so every window probe
        seeks into this one shared copy for almost no per-open RAM.
        """
        source = os.path.join(self.state.input_dir, track)
        copy = base + ".seek.mka"
        try:
            done = subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-y", "-i", source, "-map", "0:a:0", "-c:a", "copy", copy],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if done.returncode == 0 and os.path.getsize(copy) > 0:
                return copy
        except OSError:
            pass
        _remove(copy)
        return source

    def write_chunk_jobs_for(self, track: str) -> None:
        """One candidate's gathered midpoints turned into its chunk jobs.

        A track whose silence detection collapsed everything back to one piece is
        queued as a single whole-file job instead.
        """
        state = self.state
        base = segments.plan_file_for(state.plan_root, track)
        with open(base + ".meta") as handle:
            duration = handle.read().strip()

        midpoints = []
        directory = os.path.dirname(base)
        prefix = os.path.basename(base) + ".mids."
        try:
            for name in sorted(os.listdir(directory)):
                if name.startswith(prefix):
                    with open(os.path.join(directory, name)) as handle:
                        midpoints += [line for line in
                                      handle.read().split("\n") if line]
        except OSError:
            pass

        interior = segments.select_boundaries(midpoints, duration, self.jobs)
        bounds = ["0"] + list(interior) + [duration]
        total = len(bounds) - 1
        if total < 2:
            _write_jobs(base, [track])
            return

        tokens = []
        for index in range(total):
            start, end = bounds[index], bounds[index + 1]
            length = formatting.awk_number(end) - formatting.awk_number(start)
            tokens.append(UNIT.join([track, str(index), str(total), start,
                                     "%.3f" % length]))
        _write_jobs(base, tokens)
        with open(base + ".plans", "w") as handle:
            handle.write("%s\t%d\0" % (track, total))


def _write_jobs(base: str, tokens: list) -> None:
    with open(base + ".jobs", "w", encoding="utf-8",
              errors="surrogateescape") as handle:
        for token in tokens:
            handle.write(token + "\0")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _records(path: str) -> list:
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as handle:
            return [record for record in handle.read().split("\0") if record]
    except OSError:
        return []


def footer(state: Run) -> None:
    """The closing report: wall-clock split into three phases - pre-conversion
    (setup and planning), conversion (the parallel encode loop) and
    post-conversion (re-concatenating split files) - plus the audio-seconds
    actually encoded, tallied as the run went so chunks are never double-counted
    with their parent.

    Named at the top of the run and called both at the end of a finished one and
    on the way out of an interrupted one, so a Ctrl+C reports the same figures for
    the part that got done. Everything it reads therefore has to hold before the
    phase that fills it has run, which is what the defaults are for.
    """
    now = time.time()
    conv_start = state.conv_start if state.conv_start is not None else now
    conv_end = state.conv_end if state.conv_end is not None else conv_start
    post_start = state.post_start if state.post_start is not None else now
    post_end = state.post_end if state.post_end is not None else post_start

    pre_seconds = conv_start - state.pre_start
    conv_seconds = conv_end - conv_start
    post_seconds = post_end - post_start
    total_seconds = (state.post_end if state.post_end is not None
                     else now) - state.pre_start

    file_count = len(state.tracks)
    audio_seconds = 0.0
    if state.duration_file and os.path.isfile(state.duration_file):
        with open(state.duration_file) as handle:
            audio_seconds = formatting.awk_number(handle.read())

    print("")
    print("Stats")
    print("=====")
    for label, seconds in (("Pre-conversion", pre_seconds),
                           ("Conversion", conv_seconds),
                           ("Post-conversion", post_seconds),
                           ("Total time", total_seconds)):
        print("%-18s %.2f s (%s)"
              % (label + ":", seconds, formatting.fmt_hms("%.2f" % seconds)))
    print("Files:             %d" % file_count)
    print("Total duration:    %.0f s (%s)"
          % (audio_seconds, formatting.fmt_hms("%.2f" % audio_seconds)))
    if file_count > 0:
        print("Time per file:     %.2f s" % (total_seconds / file_count))
    if total_seconds > 0:
        print("Real-time speedup: %sx" % formatting.fmt_ratio(
            "%.6f" % (audio_seconds / total_seconds)))

    sys.stdout.flush()
    safety.report_safety_skips()


def _in_worker(state, method: str, item) -> None:
    """One job, in a worker PROCESS.

    The worker's interrupt handling is installed here rather than in the work,
    because at width 1 that same work runs in the RUN's own process - where the
    worker's handler would replace the run's. The run's scratch base is adopted
    for the same kind of reason: a worker that settled its own would make a
    second run directory beside the parent's and leave it behind.
    """
    safety.trap_worker_abort()
    ramscratch.adopt_ram_base(getattr(state, "ram_base", ""))
    getattr(state, method)(item)


def _run_pool(state, method: str, items: list, jobs: int) -> None:
    if jobs <= 1:
        for item in items:
            if safety.abort_requested():
                return
            getattr(state, method)(item)
        return

    workerpool.run(items, jobs, _in_worker, lambda item: (state, method, item))


def _scan_tracks(input_dir: str) -> list:
    """Every track, LARGEST FILE FIRST.

    Byte size is a cheap, probe-free proxy for encode time, and starting the long
    encodes at the very front means they finish near the beginning of the run
    instead of one huge file landing last and pinning a core while everything else
    has drained.
    """
    wanted = tuple("." + extension for extension in
                   tuple(enums.AUDIO_EXTENSIONS) + tuple(enums.VIDEO_EXTENSIONS))
    found = []
    for parent, _dirs, names in os.walk(input_dir):
        for name in names:
            if not name.endswith(wanted):
                continue
            path = os.path.join(parent, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            found.append((size, os.path.relpath(path, input_dir)))
    return [relative for _size, relative in
            sorted(found, key=lambda entry: -entry[0])]


def main(argv: list, program: str = "convert-audio",
         script_dir: str = "") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        sys.stderr.write(clioptions.usage_error_text(declaration,
                                                     error.message))
        return 1

    mono = "m" in result.given
    adaptive = "a" in result.given
    copy = "c" in result.given
    keep = "k" in result.given
    bitrate_set = bool(result.values["bitrate"])
    bitrate = int(result.values["bitrate"] or DEFAULT_BITRATE)
    jobs = int(result.values["jobs"] or runlog.cpu_count())
    split_threshold = int(result.values["splitThreshold"]
                          or DEFAULT_SPLIT_THRESHOLD)

    # Adaptive mode owns the channel and bitrate decision per file, so a global
    # -m or -b would contradict it. It also keeps files whole: the split queue
    # threads the global settings through its chunk jobs, whereas adaptive needs a
    # per-file decision.
    if adaptive:
        if mono or bitrate_set:
            sys.stderr.write("%s\n\nerror: -a (adaptive) cannot be combined "
                             "with -m or -b.\n\n%s\n"
                             % (declaration.credits,
                                clioptions.page(declaration)))
            return 1
        split_threshold = 0

    if clioptions.args_out_of_range(len(result.positionals), 2, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1
    input_dir, output_dir = result.positionals[0], result.positionals[1]

    if not os.path.isdir(input_dir):
        sys.stdout.write(clioptions.missing_dir_text(declaration, input_dir))
        return 1

    # Both made absolute, once: the run later chdirs into the input folder, so a
    # relative path from the command line would resolve against that directory
    # instead of the caller's and point at itself.
    input_dir = os.path.abspath(input_dir)
    output_dir = os.path.abspath(output_dir)

    # Opus is itself an input format here, so an output folder inside the input
    # would hand the next run its own encodes to encode again.
    if safety.require_separate_output(input_dir, output_dir):
        return 1

    script_dir = script_dir or commands.script_dir()

    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()
    if tooldeps.require_tools(program, ["ffmpeg", "ffprobe", "rsync",
                                        imagemagick.CONVERT_SPEC]):
        return 1
    if tooldeps.require_python_module(
            "mutagen", program,
            "writes the chapter marks and the cover art into the output"):
        return 1
    runlog.warn_uncounted_progress()
    _settle_mkvtoolnix()

    # Nothing to encode? Said before the output folder is built up and before
    # pretreatment renames anything. Video containers count as input too: their
    # audio stream is extracted and encoded like any other track.
    probe_what = ("audio files (%s) or video files to take the audio from (%s)"
                  % (enums.extension_list(list(enums.AUDIO_EXTENSIONS)),
                     enums.extension_list(list(enums.VIDEO_EXTENSIONS))))
    if not _holds_input(input_dir):
        return safety.fail_no_relevant_input(input_dir, probe_what)

    os.makedirs(output_dir, exist_ok=True)

    safety.init_safety_log()
    skips = safety.RunSkipLog()
    safety.init_abort_flag()
    safety.trap_run_abort()
    statusline.init_status_line()
    ramscratch.init_ram_base()

    try:
        return _convert(program, script_dir, input_dir, output_dir, probe_what,
                        skips, mono, adaptive, copy, keep, bitrate, jobs,
                        split_threshold)
    finally:
        statusline.stop_status_monitor()
        ramscratch.run_exit_cleanup()
        safety.release_abort_flag()


def _holds_input(input_dir: str) -> bool:
    """Case-insensitively, because extensions are only lower-cased later."""
    wanted = tuple("." + extension.lower() for extension in
                   tuple(enums.AUDIO_EXTENSIONS) + tuple(enums.VIDEO_EXTENSIONS))
    for _parent, _dirs, names in os.walk(input_dir):
        if any(name.lower().endswith(wanted) for name in names):
            return True
    return False


def _convert(program: str, script_dir: str, input_dir: str, output_dir: str,
             probe_what: str, skips, mono: bool, adaptive: bool, copy: bool,
             keep: bool, bitrate: int, jobs: int, split_threshold: int) -> int:
    pre_start = time.time()

    state = Run(
        input_dir=input_dir, output_dir=output_dir, script_dir=script_dir,
        mono=mono, adaptive=adaptive, copy=copy, keep=keep, bitrate=bitrate,
        threshold=THRESHOLD, split_threshold=split_threshold,
        # The table's default: a cover that rides along in every transcoded
        # track is glanced at in a track list, not studied, so the floor of the
        # table is the right end of it.
        cover_resolution=imagesizes.geometry(imagesizes.DEFAULT_TIER),
        ram_base=ramscratch.ram_base(), tracks=[], pre_start=pre_start,
        conv_start=None, conv_end=None,
        post_start=None, post_end=None, duration_file="", counters=None,
        chunk_root="", plan_root="",
    )
    safety.set_run_footer(lambda: footer(state))

    _pretreat_input(input_dir, skips)

    try:
        os.chdir(input_dir)
    except OSError:
        return 1

    for parent, _dirs, _names in os.walk(input_dir):
        os.makedirs(os.path.join(output_dir,
                                 os.path.relpath(parent, input_dir)),
                    exist_ok=True)

    # The image files, keeping their modification times so copied cover art
    # matches the source the way the audio outputs do.
    subprocess.run(["rsync", "-rmt", "--quiet", "--include=*/",
                    "--include", "*.jpg", "--include", "*.png",
                    "--include", "*.webp", "--include", "*.avif",
                    "--exclude=*", input_dir + "/", output_dir])

    state.tracks = _scan_tracks(input_dir)
    # Belt and braces: the pre-flight probe is case-INsensitive while this scan
    # is not, so a file whose extension could not be lower-cased would slip past
    # the probe and leave the queue empty here.
    if not state.tracks:
        return safety.fail_no_relevant_input(input_dir, probe_what)

    # With a backlog this large the cores stay saturated on the many small files
    # no matter what, so a handful of large ones each blocking one thread costs
    # nothing overall - while the extra silence detection and re-concatenation
    # chunking adds would just be wasted effort.
    if split_threshold > 0:
        skip_at = jobs * 50
        if len(state.tracks) >= skip_at:
            print("Many input files (%d >= %d threads x 50): skipping long-file "
                  "chunking." % (len(state.tracks), skip_at))
            split_threshold = 0
            state.split_threshold = 0

    chunk_root, chunk_status = ramscratch.ram_scratch_dir("convertAudio.chunks")
    plan_root, plan_status = ramscratch.ram_scratch_dir("convertAudio.plans")
    if chunk_status != 0 or plan_status != 0 or not chunk_root or not plan_root:
        sys.stderr.write("\nError: no scratch directory could be made for this "
                         "run.\nNothing was changed.\n")
        return 1
    state.chunk_root, state.plan_root = chunk_root, plan_root
    # -k asks for the chunks to be left behind, so only a run without it
    # registers them.
    if not keep:
        ramscratch.add_exit_cleanup([chunk_root])
    ramscratch.add_exit_cleanup([plan_root])

    jobs_queue, plans_queue = _build_queue(state, jobs)

    progress_file = os.path.join(plan_root, "progress")
    duration_file = os.path.join(plan_root, "duration")
    for path, value in ((progress_file, "0\n"), (duration_file, "0\n")):
        with open(path, "w") as handle:
            handle.write(value)
    state.duration_file = duration_file
    state.counters = Counters(progress_file, duration_file, len(jobs_queue),
                              int(pre_start))

    print("Converting %d job(s)..." % len(jobs_queue))
    state.conv_start = time.time()
    if jobs_queue:
        # The live row, refreshed underneath the per-file lines for as long as
        # the queue is draining. It shares the progress lock with the per-file
        # print, so the row and the scrolling lines never interleave mid-line.
        statusline.start_status_monitor(progress_file + ".lock",
                                        state.counters.status_text)
        _run_pool(state, "encode", jobs_queue, jobs)
        statusline.stop_status_monitor()
        safety.exit_if_aborted()
    state.conv_end = time.time()

    # The split files re-concatenated, only after the whole encode queue has
    # drained, so all of a file's chunks exist.
    state.post_start = time.time()
    if plans_queue:
        print("Re-concatenating %d split file(s)..." % len(plans_queue))
        _run_pool(state, "reconcat", plans_queue, jobs)
        safety.exit_if_aborted()
    state.post_end = time.time()
    print("Done.")

    safety.print_run_footer()

    # Twice, so a folder that only held empty folders collapses too. The input
    # folder itself stays: a folder the user named must still be there.
    for _pass in range(2):
        for parent, _dirs, _names in os.walk(input_dir, topdown=False):
            if parent != input_dir:
                try:
                    os.rmdir(parent)
                except OSError:
                    pass
    return 0


def _build_queue(state: Run, jobs: int) -> tuple:
    planner = Planner(state, jobs)
    _run_pool(planner, "classify", state.tracks, jobs)
    safety.exit_if_aborted()

    candidates, windows = planner.window_jobs(state.tracks)
    if windows:
        _run_pool(_Detector(), "detect", windows, jobs)
        safety.exit_if_aborted()
    # The shared seek copies have served every window probe; that RAM goes back
    # before phase C, which needs only the gathered midpoints.
    for track in candidates:
        _remove(segments.plan_file_for(state.plan_root, track) + ".seek.mka")

    for track in candidates:
        planner.write_chunk_jobs_for(track)

    jobs_queue, plans_queue = [], []
    for track in state.tracks:
        base = segments.plan_file_for(state.plan_root, track)
        jobs_queue += _records(base + ".jobs")
        plans_queue += _records(base + ".plans")
    return jobs_queue, plans_queue


class _Detector:
    """The window probes, as a poolable object like the other phases."""

    def detect(self, token: str) -> None:
        detect_window(token, SILENCE_NOISE, SILENCE_MIN_DUR)


def _pretreat_input(input_dir: str, skips) -> None:
    safety.lower_case_extensions(input_dir, skips)
    for parent, _dirs, names in os.walk(input_dir):
        for name in names:
            if name.endswith(".jpeg"):
                path = os.path.join(parent, name)
                safety.safe_rename(path, os.path.splitext(path)[0] + ".jpg",
                                   skips)


def _settle_mkvtoolnix() -> None:
    """Settled once and shared with the workers and with any wrapper's children,
    so the warning is said exactly once per run by whichever script settled
    first."""
    if os.environ.get("HAVE_MKVTOOLNIX") is not None:
        return
    present = all(tooldeps.tool_present(tool) for tool in
                  ("mkvmerge", "mkvpropedit", "mkvextract"))
    os.environ["HAVE_MKVTOOLNIX"] = "1" if present else ""
    if not present:
        log("WARNING: mkvtoolnix not found (apt install mkvtoolnix) - cover art "
            "embedded in Matroska sources")
        log("         will not be extracted; sidecar images and the other cover "
            "sources still work. The")
        log("         chapters are written by mutagen and do not need it - "
            "nothing else is lost.")


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
