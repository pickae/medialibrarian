"""convert-video: a folder of videos re-encoded into a clean, uniform library.

Every source becomes one Matroska: the video re-encoded to a modern codec, the
audio to Opus, and the subtitles and attachments copied across. The input tree is
never touched.

Two things make this script what it is. The video encode dwarfs everything else
and lives in RAM until the final mux, so it is never thrown away when a cheap step
fails - a file whose audio or mux dies still gets its encode, marked
" (video only)". And parallelism is per FILE rather than across files: one video
at a time is cut into chunks that encode in parallel and re-join with a stream
copy, because the chunk count is already sized to fill the encoder.

How a file is encoded is chosen by NAME: a video profile and an audio profile each
name a bundle of ffmpeg arguments in the tables below, so adding or tuning a preset
is one row. What a row deliberately does not fix is decided per file or per run -
the quality level moves with the tier the file is ENCODED at, film grain is
measured per source, and the resolution ceiling, fast-decode level and Dolby Vision
mode all come from the run or the file.
"""

import os
import re
import subprocess

from medialib.lib import bitrates, clioptions, codecs, resolutions, videobitrate
from medialib.lib.runlog import log

USAGE_HEAD = r"""Usage:
    {program} [options] <inputDir> <outputDir>

    inputDir      directory holding the source videos to convert
    outputDir     directory the re-encoded library is written to
                  (created if it does not exist)

While it runs:
    p             Pause the video encoding: every encoder of the moment is stopped
                  where it is, so the CPU cores or the NVENC engines are free for
                  something else. Their memory is NOT released, and the pause lasts
                  only as long as this shell does. Audio keeps encoding.
    r             Resume: every paused encoder carries on where it stopped.
                  (Ctrl+C still ends the run, paused or not.)

Options:"""

OPT_SPEC = r"""
h |  | Print this help page.
j | <cores> | Logical-core count to size the per-file chunking for
                  (default: the number of CPU cores). Parallelism comes from
                  chunking one file at a time, not from encoding files in
                  parallel, so this scales the chunk count up or down.
p | <profile> | Video encoding profile to use (default: av1Grain). Selecting a
                  *Nvenc profile (hevcNvenc, av1Nvenc) encodes on the GPU.
a | <profile> | Audio encoding profile to use (default: opus).
b | <kbit/s> | Opus bitrate applied to every audio track, overriding the
                  per-channel table. Implies -a opusCustom when no audio profile
                  is given explicitly; required by, and only valid with, that
                  profile.
e | <count> | NVENC engine count to parallelise hardware encoding across,
                  overriding the built-in per-GPU guess. Only affects the *Nvenc
                  profiles.
q | <level> | Constant-quality level to encode at, overriding the one the
                  chosen video profile hardcodes: -crf for the software profiles,
                  -cq for the *Nvenc ones. Lower means better quality and a bigger
                  file, and the bitrate follows from it. Also turns OFF the
                  resolution bias, so the level given is the level used for every
                  file. Only valid for the constant-quality profiles - the
                  av1Constrained* rows target an average bitrate instead and have
                  no quality level to override.
r | <tier> | Resolution ceiling: scale every video down to at most this tier,
                  keeping its aspect ratio. A tier is named either by its line
                  count (720p ... 4320p) or by a marketing name (fullHD, 2K, 4K,
                  UltraHD, 8K, ...), in any case. A source already at or below the
                  tier is encoded at its own size - nothing is ever scaled UP.
                  Default: no ceiling, every source keeps its resolution.
f | <1\|2> | Fast-decode level to encode with, trading a little compression
                  for a cheaper decode on weak playback hardware (2 is cheaper to
                  decode than 1). Off by default, and only valid for the AV1
                  software profiles - x265 and the *Nvenc ones have no such setting.
g | <level\|0\|off> | How much film grain to synthesise, in libsvtav1's 0-50 scale.
                  Left out, every grain-synthesising profile MEASURES each source
                  and synthesises what it measured (av1Animation synthesises none).
                  A number replaces that outright for every file - it is capped by
                  nothing, so 35 means 35. A level of 0 asks for that same per-source
                  probe explicitly, on a profile that would not otherwise run it;
                  note that 0 therefore does NOT mean "none" - off is how none is
                  asked for.
                  Only valid for the AV1 software profiles. Note this is lossy: the
                  encoder denoises before encoding, so the source's real grain is
                  not in the output either way.
t | [percent] | Test each source before encoding it, and convert only the ones
                  with room to save. A file is measured against the bitrate its
                  codec, frame size, frame rate and grain make ADEQUATE: one below
                  that is already starved and is skipped (re-encoding it can only
                  make it worse), and one above it is converted only if the encode
                  this run would produce is still adequate after giving up
                  <percent> of the source's video bitrate. Default 50, i.e. convert
                  only what can be halved; a smaller number is a looser run (-t 30
                  converts anything that can save 30%), and -t 0 keeps only the
                  starved-source check. Audio is left out of the comparison.
                  Off by default: every source is converted.
"""

OPT_LONG = ("h:help j:cores p:profile a:audio-profile b:audio-bitrate "
            "e:nvenc-engines q:quality r:max-resolution f:fast-decode "
            "g:grain t:test")

USAGE_TAIL = r"""

    Run with an unknown profile or tier name to have the valid names listed.

Dependencies:
    ffmpeg (with libsvtav1/libx265/libopus) and ffprobe. Hardware
    acceleration additionally uses
    nvidia-smi (NVENC engine count), a VAAPI-capable Intel iGPU, or on macOS
    VideoToolbox; all are optional and detected at runtime. Keeping Dolby Vision
    needs an ffmpeg whose
    libx265/libsvtav1 offer -dolbyvision (ffmpeg 7.1 and newer), which is also
    detected at runtime - an older build just converts without it. A dual-layer
    Dolby Vision profile 7 source is normalised to single-layer profile 8.1 before
    it is encoded, which additionally needs dovi_tool and mkvmerge (mkvtoolnix),
    both optional too: without them such a file is encoded as plain HDR10. The
    finished file's Dolby Vision level - the capability claim a player checks
    before it decodes anything - is corrected if it overstates the video, which
    needs python3 and is skipped with a warning without it.

    The AV1 profiles ask for psychovisual SVT-AV1 parameters, and the *Nvenc ones
    for NVENC's uhq tuning, that a distribution's ffmpeg is often too old for -
    and a too-old libsvtav1 DROPS an unknown parameter silently rather than
    failing. So a newer build installed beside the packaged one is looked for and
    preferred when it accepts more: PATH first, then $HOME/.local/bin,
    /opt/homebrew/bin, /usr/local/bin and /opt/ffmpeg/bin. Set ffmpegOverride to
    name one outright.
    The startup summary says which build was used and what it could not do."""

SVT_PSY_PARAMS = 'tune=0:enable-variance-boost=1:variance-boost-strength=2:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=1:enable-qm=1:qm-min=0:qm-max=15:keyint=10s:irefresh-type=2'

SVT_PSY_PARAMS_ANIMATION = 'tune=0:enable-variance-boost=1:variance-boost-strength=1:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=2:enable-qm=1:qm-min=8:qm-max=15:keyint=10s:irefresh-type=2'

VIDEO_PROFILES = """
x265BluRay|-c:v libx265 -crf 20 -preset slow -x265-params profile=main10
x265Fast|-c:v libx265 -crf 24 -preset medium -x265-params profile=main10
av1BluRay|-c:v libsvtav1 -crf 26 -preset 5 -svtav1-params tune=0:enable-variance-boost=1:variance-boost-strength=2:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=1:enable-qm=1:qm-min=0:qm-max=15:keyint=10s:irefresh-type=2
av1Grain|-c:v libsvtav1 -crf 30 -preset 6 -svtav1-params tune=0:enable-variance-boost=1:variance-boost-strength=2:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=1:enable-qm=1:qm-min=0:qm-max=15:keyint=10s:irefresh-type=2
av1Animation|-c:v libsvtav1 -crf 30 -preset 5 -svtav1-params tune=0:enable-variance-boost=1:variance-boost-strength=1:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=2:enable-qm=1:qm-min=8:qm-max=15:keyint=10s:irefresh-type=2
av1Fast|-c:v libsvtav1 -crf 30 -preset 8 -svtav1-params tune=0:enable-variance-boost=1:variance-boost-strength=2:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=1:enable-qm=1:qm-min=0:qm-max=15:keyint=10s:irefresh-type=2
av1Constrained|-c:v libsvtav1 -b:v 3000k -qmin 30 -preset 8 -svtav1-params tune=0:enable-variance-boost=1:variance-boost-strength=2:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=1:enable-qm=1:qm-min=0:qm-max=15:keyint=10s:irefresh-type=2
av1ConstrainedGood|-c:v libsvtav1 -b:v 1000k -qmin 28 -preset 5 -svtav1-params tune=0:enable-variance-boost=1:variance-boost-strength=2:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=1:enable-qm=1:qm-min=0:qm-max=15:keyint=10s:irefresh-type=2
av1ConstrainedBad|-c:v libsvtav1 -b:v 600k -qmin 35 -preset 4 -svtav1-params tune=0:enable-variance-boost=1:variance-boost-strength=2:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=1:enable-qm=1:qm-min=0:qm-max=15:keyint=10s:irefresh-type=2
av1ConstrainedBluRay|-c:v libsvtav1 -b:v 25000k -qmin 26 -preset 5 -svtav1-params tune=0:enable-variance-boost=1:variance-boost-strength=2:variance-octile=6:qp-scale-compress-strength=1:enable-dlf=2:sharpness=1:enable-qm=1:qm-min=0:qm-max=15:keyint=10s:irefresh-type=2
hevcNvenc|-c:v hevc_nvenc -preset p7 -tune uhq -rc vbr -cq 24 -b:v 0 -profile:v main10 -spatial-aq 1 -aq-strength 8 -temporal-aq 1 -b_ref_mode middle -g 240 -forced-idr 1 -split_encode_mode disabled
av1Nvenc|-c:v av1_nvenc -preset p7 -tune uhq -rc vbr -cq 28 -b:v 0 -spatial-aq 1 -aq-strength 8 -temporal-aq 1 -b_ref_mode middle -g 240 -forced-idr 1 -split_encode_mode disabled
"""

AUDIO_PROFILES = """
opus|-c:a libopus
opusCustom|-c:a libopus
passthrough|-c:a copy
"""

# VBV (HRD) settings merged into -x265-params when an x265 encode carries a Dolby
# Vision RPU: x265 refuses to code one without them. The ceiling is the HEVC level
# 5.1 High tier one UHD Blu-ray Dolby Vision itself uses - far above anything the
# CRF presets produce, so it satisfies the requirement without ever binding.
DOLBY_VISION_VBV = "vbv-maxrate=160000:vbv-bufsize=160000"

# Opus bitrate for a track whose channel count is in neither the shared table nor
# its stereo fallback.
DEFAULT_AUDIO_BITRATE = "120"

# The NVENC tuning the *Nvenc rows ask for, and what to use when the build cannot:
# uhq enables lookahead and the temporal filter by itself, and needs HEVC or AV1 on
# Turing+ AND an ffmpeg built against an SDK that exposes it.
NVENC_TUNE_WANTED = "uhq"
NVENC_TUNE_FALLBACK = "hq"

# What -t asks a conversion to save, as a share of the source's video bitrate.
DEFAULT_BITRATE_SAVING = "50"

# The chunking table is calibrated for this many threads and scaled to the
# machine's own count.
REFERENCE_CORES = 32

# How far the interlace measurement looks. Interlacing is a property of the SOURCE
# rather than of a scene, so the head of the file settles it, and the decode costs
# seconds against an encode measured in minutes.
INTERLACE_PROBE_FRAMES = "500"

# The same constant-quality level does not buy the same visible quality across the
# ladder: at 2160p a frame's detail is spread over four times the pixels of 1080p,
# so a couple of levels are invisible there and pay for themselves in size, while at
# SD every pixel is on show. A signed number to ADD, so positive is softer and
# smaller. "unknown" is deliberately 0 - a file whose size could not be read gets
# the level its profile asked for rather than a guess in either direction.
QUALITY_BIAS_TABLE = """
4320p    3
2160p    2
1440p    1
1080p    0
720p    -1
SD      -2
unknown  0
"""

# How many chunks one file of each tier is cut into on a REFERENCE_CORES machine.
# Driven by resolution rather than length: the smaller the frames, the less work a
# single encoder spreads across cores. A 2160p frame already saturates the CPU and
# is left whole. "unknown" gets the middle rung, which is safe in both directions.
CHUNK_COUNT_TABLE = """
4320p   1
2160p   1
1440p   2
1080p   3
720p    4
SD      6
unknown 3
"""

# The field separator the chunk queue joins on.
UNIT = "\x1f"

_ENCODER = re.compile(r"-c:v\s+(\S+)")
_LEVEL = re.compile(r"(-crf|-cq)\s+([0-9]+)")


def spec(program: str) -> "clioptions.Spec":
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars="j:CORES p:videoProfile a:audioProfile b:customAudioBitrate "
             "e:nvencEnginesOverride q:videoQuality f:videoFastDecode "
             "g:videoGrain r:maxVideoResolution",
        flags="optionalArg:t:^[0-9]+$",
        column=18,
        tail=USAGE_TAIL,
    )


# --- the profile tables -------------------------------------------------------

def profile_args(table: str, wanted: str) -> str:
    """``profileArgs``: a named profile's ffmpeg arguments.

    Raises :class:`UnknownProfile`, which carries the known names, rather than
    answering with nothing: a typo in -p or -a has to stop the run before a file
    is touched, and the message that stops it is the list.
    """
    for name, _sep, args in _rows(table):
        if name == wanted:
            return args
    raise UnknownProfile(wanted, [name for name, _sep, _args in _rows(table)])


class UnknownProfile(Exception):
    def __init__(self, wanted: str, known: list) -> None:
        self.wanted, self.known = wanted, known

    def text(self) -> str:
        return ('Unknown profile "%s". Known profiles:\n' % self.wanted
                + "".join("    %s\n" % name for name in self.known))


def _rows(table: str) -> list:
    return [line.partition("|") for line in table.split("\n") if line.strip()]


def encoder_of(args: str) -> str:
    """The video encoder an argument string names, e.g. libsvtav1 or
    hevc_nvenc - the *nvenc* suffix being what flags a hardware encode."""
    found = _ENCODER.search(args)
    return found.group(1) if found else ""


# --- the quality level --------------------------------------------------------

def video_quality_flag(args: str) -> str:
    """``videoQualityFlag``: the flag this argument string sets its constant
    quality with, or "" when it does not encode at a constant quality at all.

    The same knob under two names: -crf for the software encoders, -cq for the
    NVENC ones. The av1Constrained* rows have neither - they target an average
    bitrate, so there is no level for -q to override.
    """
    padded = " %s " % args
    if " -crf " in padded:
        return "-crf"
    if " -cq " in padded:
        return "-cq"
    return ""


def video_quality_max(args: str) -> int:
    """``videoQualityMax``: the top of that encoder's scale. SVT-AV1's CRF runs to
    63; x265's CRF and NVENC's CQ both stop at 51.

    Used both to reject an out-of-range -q up front and to clamp the resolution
    bias, so the two cannot disagree about what the encoder will accept.
    """
    return 63 if "libsvtav1" in args else 51


def quality_bias_for(width, height) -> int:
    """``qualityBiasFor``: how far to move a profile's level for a frame this size.

    A tier with no row biases by nothing, so a ladder that grows a rung cannot
    quietly start moving quality levels it has no tuning for.
    """
    tier = resolutions.tier_of(width, height)
    for row in QUALITY_BIAS_TABLE.split("\n"):
        fields = row.split()
        if len(fields) == 2 and fields[0] == tier:
            return int(fields[1])
    return 0


def quality_bias_spellings() -> str:
    """The table as "2160p +2, 1080p 0, ..." for the startup summary, generated
    from it so the run cannot describe a bias it will not apply."""
    parts = []
    for row in QUALITY_BIAS_TABLE.split("\n"):
        fields = row.split()
        if len(fields) != 2:
            continue
        bias = int(fields[1])
        # +2 / -1 say "moved" where a bare 2 would read as the level itself; 0 is
        # written plainly, because "+0" only looks like a decision that was made.
        parts.append("%s %s" % (fields[0], bias if bias == 0 else "%+d" % bias))
    return ", ".join(parts)


def apply_video_quality(args: str, width="", height="", given: bool = False,
                        quality: str = "") -> str:
    """``applyVideoQuality``: that argument string with its level settled.

    The -q value when one was given, or the profile's own level moved by the
    resolution bias when it was not. The two dimensions are the size the file is
    ENCODED at, not the size it arrived at: with -r in play those differ, and it is
    the encoded frame that decides whether a level is generous or mean. A profile
    with no quality flag comes back untouched.
    """
    flag = video_quality_flag(args)
    if not flag:
        return args
    if given:
        level = quality
    else:
        found = _LEVEL.search(args)
        # No number to move: leave the arguments exactly as the profile wrote
        # them rather than inventing a level for it.
        if not found:
            return args
        level = str(min(max(int(found.group(2)) + quality_bias_for(width, height),
                            0), video_quality_max(args)))
    return re.sub(r"%s [0-9]+" % re.escape(flag), "%s %s" % (flag, level), args,
                  count=1)


def apply_nvenc_tune(args: str, tune: str = NVENC_TUNE_WANTED) -> str:
    """``applyNvencTune``: that argument string with its -tune replaced by the
    tuning this build actually accepts.

    A no-op for a software profile, for a row with no -tune, and whenever the
    wanted tuning is the resolved one - so it is safe to apply to everything.
    """
    if " -tune " not in " %s " % args or tune == NVENC_TUNE_WANTED:
        return args
    return args.replace(" -tune %s " % NVENC_TUNE_WANTED, " -tune %s " % tune)


# --- the chunk plan -----------------------------------------------------------

def chunk_count_for(width, height, cores: int) -> int:
    """``chunkCountFor``: how many chunks a video of this pixel size is cut into on
    THIS machine - its tier's row, scaled linearly from the reference core count."""
    tier = resolutions.tier_of(width, height)
    base = None
    for row in CHUNK_COUNT_TABLE.split("\n"):
        fields = row.split()
        if len(fields) != 2:
            continue
        # The unknown rung doubles as the fallback for a tier this table has no
        # row for, so a ladder that grows one cannot quietly stop chunking here.
        if fields[0] == tier or (fields[0] == resolutions.UNKNOWN_TIER
                                 and base is None):
            base = int(fields[1])
    if base is None:
        return 1
    return max(1, int(base * cores / REFERENCE_CORES + 0.5))


def equal_boundaries(duration, count: int) -> list:
    """``equalBoundaries``: the count-1 interior cut points that split a file into
    equal chunks, and nothing at all for fewer than two.

    Equal-length because the chunks re-join with a stream copy and every encoder
    opens a chunk with a keyframe, so the seam is exact at any boundary: there is
    nothing to gain from nudging cuts to quiet moments, which only ever mattered
    for an audio seam.
    """
    from medialib.lib import formatting
    total = formatting.awk_number(duration)
    if count < 2:
        return []
    segment = total / count
    return ["%.3f" % (step * segment) for step in range(1, count)]


def nvenc_engines_for(gpu_name: str) -> int:
    """``nvencEnginesFor``: a best-effort count of independent NVENC engines.

    The real count needs the Video Codec SDK, so it is guessed from the model and
    can always be overridden with -e. An extra parallel session on a one-engine
    card time-slices; it is not an error.
    """
    return 3 if "5090" in (gpu_name or "") else 2


# --- the video argument string ------------------------------------------------
# Every profile is turned into a full output-argument string here rather than
# baked into the table, so one place guarantees 10-bit output and correct
# colour/HDR handling for every encoder, software or hardware.

def pix_fmt_for(args: str) -> str:
    """``pixFmtFor``: the 10-bit pixel format the encoder wants. 10-bit for every
    source, SDR or HDR, because it avoids banding at no meaningful cost."""
    if any(kind in args for kind in ("nvenc", "vaapi", "qsv")):
        return "p010le"
    return "yuv420p10le"


def merge_param(args: str, flag: str, fragment: str) -> str:
    """``mergeParam``: prepend a fragment to an existing colon-delimited
    "<flag> <value>" option, or append the flag when it is not there.

    Prepending rather than appending is what lets a caller inject a key the row
    also carries: duplicate keys in -svtav1-params have no defined precedence.
    """
    if flag + " " in args:
        return args.replace(flag + " ", "%s %s:" % (flag, fragment), 1)
    return "%s %s %s" % (args, flag, fragment)


def dolby_vision_args(args: str, mode: str) -> str:
    """``dolbyVisionArgs``: the encoder's Dolby Vision switch.

    ``mode`` is "1" to carry the source's RPU through, "0" to strip it explicitly,
    or "" when the source is not Dolby Vision at all and ffmpeg's own "auto"
    default is right. 0 is not the same as empty: auto would turn DV on by itself
    for a DV source and fail an encode already known not to work.

    Only added for an encoder that has the switch, so hardware arguments come back
    untouched. libx265 additionally refuses to code an RPU without VBV settings.
    """
    if not mode:
        return args
    encoder = encoder_of(args)
    if any(kind in encoder for kind in ("nvenc", "vaapi", "qsv")):
        return args
    if mode == "1":
        if encoder == "libx265":
            args = merge_param(args, "-x265-params", DOLBY_VISION_VBV)
        return args + " -dolbyvision 1"
    return args + " -dolbyvision 0"


def downscale_args(width, height, ceiling: str) -> str:
    """``downscaleArgs``: the filter that scales a source down to the -r ceiling,
    or nothing when it needs none.

    The size is computed here as fixed numbers rather than left to a min(iw,1920)
    expression, so every chunk of one file encodes to the same size.
    """
    if not ceiling:
        return ""
    capped_width, capped_height = resolutions.capped(width, height, ceiling)
    if (capped_width, capped_height) == (width, height):
        return ""
    return " -vf scale=%s:%s" % (capped_width, capped_height)


def video_only_path_for(relative: str, output_dir: str) -> str:
    """``videoOnlyPathFor``: where the failsafe copy of a finished video encode
    lands - the normal output path with a marker before the extension.

    Marked so the incomplete file is obvious, is never mistaken for a finished
    one, and leaves the real output path free for a later rerun.
    """
    return "%s/%s (video only).mkv" % (output_dir, os.path.splitext(relative)[0])


def video_source_for(relative: str, input_dir: str,
                     prepared: str = "") -> str:
    """``videoSourceFor``: the file this conversion's VIDEO is read from - the
    source, or the Dolby Vision profile 8.1 intermediate prepared for it.

    Only the video pass goes through here: the audio, subtitles, attachments,
    chapters and metadata are always taken from the ORIGINAL, which is the only
    file that still has them.
    """
    return prepared or os.path.join(input_dir, relative)


def warn_source_geometry(relative: str, verdict: str, sar: str, log=log) -> None:
    """``warnSourceGeometry``: report an interlaced or anamorphic source, once,
    and change nothing about the encode.

    Deliberately a warning and not a filter: deinterlacing picks a field order, a
    cadence and an output frame rate, un-squeezing picks a target pixel grid, and
    each is a judgement about what the source IS that some material defeats and
    nothing can undo afterwards.
    """
    if verdict not in ("", "unknown", "N/A", "progressive"):
        log("WARNING: %s is interlaced (measured from its own frames) - "
            "encoding it as-is, without deinterlacing." % relative)
    # 0:1 is ffprobe for "no pixel aspect recorded", which is not the same claim
    # as a non-square one and is not worth a warning.
    if sar not in ("", "unknown", "N/A", "0:1", "1:1"):
        log("WARNING: %s has non-square pixels (sample aspect ratio %s) - "
            "encoding it as-is, at its coded size." % (relative, sar))


# --- what the source is -------------------------------------------------------

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


def interlace_verdict(path: str, decode_accel: str = "") -> str:
    """``interlaceVerdict``: whether this source is interlaced, MEASURED from its
    own frames rather than read from the container's field-order flag, which this
    library's files carry wrong on progressive material.

    ffmpeg's idet filter classifies each frame, and the verdict is the MAJORITY of
    the DETERMINED ones - flat fades and title cards, which idet cannot tell apart
    either way, do not vote. A tie, or a sample it could not classify at all, is
    unknown; a source that cannot be decoded is unknown too rather than an error,
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
        return "unknown"

    counts = re.findall(r"Multi frame detection: TFF:\s*(\d+)\s+BFF:\s*(\d+)"
                        r"\s+Progressive:\s*(\d+)", text)
    if not counts:
        return "unknown"
    tff, bff, progressive = (int(value) for value in counts[-1])
    if tff + bff > progressive:
        return "interlaced"
    if progressive > tff + bff:
        return "progressive"
    return "unknown"


def build_video_args(base: str, path: str, settings) -> str:
    """``buildVideoArgs``: the COMPLETE video output-argument string for a file.

    The profile's own arguments, plus the 10-bit pixel format, the colour
    signalling, and - for an HDR source that carries it - the mastering-display and
    content-light metadata merged in the way each encoder wants it. Plus the Dolby
    Vision switch for the file, the settled quality level, the NVENC tuning this
    build accepts and, for the AV1 software encoders, the file's grain level and
    the -f fast-decode level.

    Every consumer assembles its arguments through here, so this is the one place
    any of those per-run or per-file decisions is applied.
    """
    # The source's coded size, read ONCE and used for both decisions that depend
    # on it: the -r downscale filter, and the resolution bias on the quality
    # level. Reading it once is what keeps the bias describing the frame the scale
    # filter actually produces rather than the one that arrived.
    width, height, _order, _sar = video_dimensions(path)
    enc_width, enc_height = resolutions.capped(width, height,
                                               settings.max_resolution)

    base = apply_video_quality(base, enc_width, enc_height,
                               settings.quality_given, settings.quality)
    base = apply_nvenc_tune(base, settings.nvenc_tune)
    encoder = encoder_of(base)

    # The two libsvtav1 settings the profile row deliberately leaves out, merged
    # in here rather than written into the row so the row cannot carry a stale
    # value that would duplicate the injected key.
    if encoder == "libsvtav1":
        extra = ""
        # film-grain-accompanies-denoise: asking for synthesis without the denoise
        # would store the source's grain AND re-generate more on top of it.
        if int(settings.grain_level or 0) > 0:
            extra += "film-grain=%s:film-grain-denoise=1:" % settings.grain_level
        if settings.fast_decode:
            extra += "fast-decode=%s:" % settings.fast_decode
        if extra:
            base = merge_param(base, "-svtav1-params", extra.rstrip(":"))

    color_args = video_color_args(path)
    metadata = hdr_master_display(path)
    if metadata:
        display, _space, light = metadata.rpartition(" ")
        if encoder == "libx265":
            # hdr-opt is x265's PQ-SPECIFIC block-level optimisation: it reasons
            # about where the PQ curve puts its steps, so it belongs to a
            # smpte2084 source and not to an HLG one carrying the same static
            # metadata. The metadata itself goes on either way.
            fragment = ("repeat-headers=1:master-display=%s:max-cll=%s"
                        % (display, light))
            if " -color_trc smpte2084" in color_args:
                fragment = "hdr-opt=1:" + fragment
            base = merge_param(base, "-x265-params", fragment)
        elif encoder == "libsvtav1":
            base = merge_param(base, "-svtav1-params",
                               "mastering-display=%s:content-light=%s"
                               % (display, light))
        elif "nvenc" in encoder and settings.nvenc_master_display:
            base += " -master_display %s -max_cll %s" % (display, light)

    base = dolby_vision_args(base, settings.dolby_vision_mode)
    return "%s -pix_fmt %s%s%s" % (base, pix_fmt_for(base), color_args,
                                   downscale_args(width, height,
                                                  settings.max_resolution))


def video_intermediate_complete(directory: str, source_duration) -> bool:
    """``videoIntermediateComplete``: is the video-only intermediate a COMPLETE
    encode of the source?

    A missing, empty or short video.mkv means the video pass itself failed - a
    chunk that died leaves a short re-join - and half a video is not worth keeping.
    The one-second tolerance absorbs the rounding between a container's duration
    and the sum of the chunks.
    """
    from medialib.lib import formatting
    path = os.path.join(directory, "video.mkv")
    try:
        if os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    out = formatting.awk_number(_probe([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1", path]).strip() or 0)
    return out > 0 and out >= formatting.awk_number(source_duration) - 1


def sum_encode_progress(directory: str) -> tuple:
    """``sumEncodeProgress``: how far this file's video encode has got, as
    (frames, microseconds).

    Every parallel job writes an ffmpeg -progress file, each key re-appended on
    every update, so the LAST occurrence per file is that chunk's position and the
    per-file totals sum to the file's.
    """
    frames = micros = 0
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.startswith("prog."))
    except OSError:
        return 0, 0
    for name in names:
        one_frames = one_micros = 0
        try:
            with open(os.path.join(directory, name)) as handle:
                for line in handle:
                    key, _sep, value = line.strip().partition("=")
                    if key == "frame":
                        one_frames = _as_int(value)
                    elif key == "out_time_us":
                        one_micros = _as_int(value)
        except OSError:
            continue
        frames += one_frames
        micros += one_micros
    return frames, micros


def _as_int(value: str) -> int:
    """awk's numeric read: a field it cannot parse is 0."""
    try:
        return int(float(value))
    except ValueError:
        return 0


# --- the bitrate test (-t) ----------------------------------------------------
# The MODEL - what a video stream needs to look adequate, and what a measured
# bitrate says about it - is medialib/lib/videobitrate.py. What is here is the
# DECISION this run takes with the model's answers, which depends on the run's own
# state: the codec its profile encodes to, whether it synthesises grain, whether it
# encodes on NVENC, how far -r lowers the frame, and how much -t insists on saving.
#
# Three questions, and a file has to get past all of them: is the source at least
# adequate for what it is (a starved one cannot be improved by encoding it again,
# only degraded a generation further); what would this run's output need to be
# adequate in turn; and does that still fit in the source's bitrate less the saving.
#
# The audio is deliberately no part of it: it is re-encoded on its own terms and is
# a rounding error against a film's video bitrate.

def conversion_worthwhile(relative: str, width, height, enc_width, enc_height,
                          grain, settings, log=log) -> bool:
    """``conversionWorthwhile``: the -t decision for ONE file.

    Each answer is reported, so a skipped file says what it was measured at and
    what it was measured against. A source whose bitrate or size could not be read
    is CONVERTED, with a warning: the test exists to avoid pointless work, and
    refusing a file over a missing measurement would lose a conversion that may
    well be worth doing.
    """
    from medialib.lib import formatting
    stats = videobitrate.video_bitrate_stats(
        os.path.join(settings.input_dir, relative)).split()
    codec, fps, kbps, origin = (stats + ["", "", "", ""])[:4]

    if not kbps or not width or not height:
        log("Bitrate test: could not measure the source video bitrate, "
            "converting anyway: %s" % relative)
        return True

    in_tier = resolutions.tier_of(width, height)
    in_adequate = videobitrate.adequate_video_bitrate(
        codec, width, height, fps, grain, "0", "0")
    verdict = videobitrate.bitrate_verdict(kbps, in_adequate)
    if verdict == "unknown":
        log("Bitrate test: could not judge the source (%s %s at %s kbit/s), "
            "converting anyway: %s"
            % (codec or "unknown codec", in_tier, kbps, relative))
        return True
    if verdict == "starved":
        log("Bitrate test: %s %s at %s kbit/s (%s) is already starved - about "
            "%s kbit/s would be adequate, so re-encoding can only make it "
            "worse. Skipping: %s"
            % (codec or "unknown codec", in_tier, kbps, origin, in_adequate,
               relative))
        return False

    # The output side: the profile's codec at the size this file is ENCODED at
    # (which -r may have lowered), at the same frame rate, with grain synthesised
    # whenever this run synthesises any.
    out_codec = codecs.encoder_codec(settings.encoder)
    out_synth = "1" if int(settings.grain_level or 0) > 0 else "0"
    out_tier = resolutions.tier_of(enc_width, enc_height)
    out_adequate = videobitrate.adequate_video_bitrate(
        out_codec, enc_width, enc_height, fps, grain, out_synth,
        "1" if settings.hardware_encode else "0")
    if not out_adequate:
        log("Bitrate test: could not judge the output size, converting "
            "anyway: %s" % relative)
        return True

    # What is left of the source's bitrate once the demanded saving is taken off
    # it: the budget the output encode has to be adequate within.
    budget = "%.0f" % (formatting.awk_number(kbps)
                       * (100 - int(settings.required_saving)) / 100)
    if formatting.awk_number(out_adequate) > formatting.awk_number(budget):
        log("Bitrate test: %s %s at %s kbit/s (%s) is %s for what it is, but "
            "%s %s needs about %s kbit/s and only %s would be left after "
            "saving %s%%. Skipping: %s"
            % (codec or "unknown codec", in_tier, kbps, origin, verdict,
               out_codec or "the output codec", out_tier, out_adequate, budget,
               settings.required_saving, relative))
        return False

    log("Bitrate test: %s %s at %s kbit/s (%s) is %s - adequate is about %s "
        "kbit/s and %s %s needs about %s, so %s%% can be saved and it stays "
        "adequate. Converting: %s"
        % (codec or "unknown codec", in_tier, kbps, origin, verdict,
           in_adequate, out_codec or "the output codec", out_tier, out_adequate,
           settings.required_saving, relative))
    return True


# --- the run's own settings ---------------------------------------------------

class Settings:
    """What this run decided once, at startup, and every file is encoded with.

    The shell exports each of these, because the parallel chunk workers build
    their own argument strings in their own shells and have to agree with the rest
    of the file. Here they travel as one object, which is what a worker is handed.
    """

    def __init__(self, **values) -> None:
        self.input_dir = ""
        self.output_dir = ""
        self.cores = 1
        self.video_profile = "av1Grain"
        self.audio_profile = "opus"
        self.custom_audio_bitrate = ""
        self.encoder = ""
        self.quality = ""
        self.quality_given = False
        self.max_resolution = ""
        self.fast_decode = ""
        self.grain_level = "0"
        self.grain_probe_wanted = False
        self.required_saving = DEFAULT_BITRATE_SAVING
        self.test_source_bitrate = False
        self.hardware_encode = False
        self.nvenc_engines = 1
        self.nvenc_tune = NVENC_TUNE_WANTED
        self.nvenc_master_display = False
        self.decode_accel = ""
        self.dv_encoder_support = False
        # Per file, not per run: the Dolby Vision decision this file was given,
        # and the profile 8.1 intermediate prepared for it, if any.
        self.dolby_vision_mode = ""
        self.dolby_vision_source = ""
        self.__dict__.update(values)


# --- the live status row ------------------------------------------------------

def video_status_text(directory: str, total_seconds, label: str, start: int,
                      paused_at_start: int = 0, cols: int = 80,
                      margin: int = 1, paused: bool = False,
                      paused_now: int = 0, now: int = 0) -> str:
    """``videoStatusText``: ONE line for a file's video pass, in place of ffmpeg's
    per-process banners - one set per parallel chunk.

    It reports how far through the file is, the wall-clock spent and left, and the
    two figures that say how FAST it is going: frames per second, and the
    real-time speed-up. Both are aggregates across the parallel chunks, which is
    what makes them comparable between a chunked and an unchunked file.

    A PAUSED file keeps the very same row with the clock standing still: the
    seconds a run spends paused are subtracted, so everything the row says goes on
    describing how fast this file is being ENCODED rather than how long somebody
    left it stopped.

    The file NAME is what gives way when the row does not fit: the numbers are the
    point of it, so they are measured first and the name gets the columns left.
    """
    from medialib.lib import formatting, statusline
    # The two spellings are the same WIDTH, so pausing does not shift the rest of
    # the row sideways.
    prefix = "  paused   " if paused else "  encoding "
    minimum_label = 12

    frames, micros = sum_encode_progress(directory)
    elapsed = max(0, now - start - max(0, paused_now - paused_at_start))
    total = formatting.awk_number(total_seconds)

    percent = 0.0 if total <= 0 else min(micros / (total * 1000000) * 100, 100)
    if percent > 0.5:
        eta = formatting.fmt_clock("%d" % (elapsed * (100 - percent) / percent))
    else:
        eta = "--:--"
    fps = 0.0 if elapsed <= 0 else frames / elapsed
    speed = formatting.fmt_ratio(
        "%.6f" % (micros / 1000000 / elapsed) if elapsed > 0 else "0")

    fields = (": %.1f%%  elapsed %s  ETA %s  %.1f fps  %sx realtime"
              % (percent, formatting.fmt_clock(elapsed), eta, fps, speed))
    # What is left of the row once the prefix, the reserved end column and the
    # fields have had theirs. The prefix is MEASURED rather than counted: written
    # out as a literal and as its own width, the two would drift apart the first
    # time it was reworded.
    budget = max(minimum_label, cols - margin - len(prefix) - len(fields))
    return prefix + statusline.shorten_path(str(budget), label) + fields


def audio_track_args(channels: str, settings) -> tuple:
    """What ONE audio track is encoded with: (extra arguments, bitrate).

    Anything above stereo is downmixed, because libopus refuses a 5.1(side)
    layout - what AC3 surround commonly decodes to - outright, and is then priced
    as the stereo track it has become. A count that could not be read is left as
    it is rather than guessed at, and falls back to the stereo bitrate the way the
    lookup already does.
    """
    audio_args = profile_args(AUDIO_PROFILES, settings.audio_profile)
    if channels.isdigit() and int(channels) > 2:
        audio_args = "-ac 2 " + audio_args
        channels = "2"
    if settings.custom_audio_bitrate:
        return audio_args, settings.custom_audio_bitrate
    bitrate = (bitrates.audio_bitrate(channels, "normal")
               or bitrates.audio_bitrate("2", "normal")
               or DEFAULT_AUDIO_BITRATE)
    return audio_args, bitrate
