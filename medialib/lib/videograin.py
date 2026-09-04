"""The film grain measurement and defaults.

"How grainy is this source" is asked for two quite different reasons - deciding how
much grain to synthesise into an encode, and judging how much the source's own
encoder had to spend on noise - and it is the same measurement either way, so it
lives here once rather than being answered twice.

The probe is the expensive part: moments spread across the file are decoded and the
TEMPORAL residual is graded - consecutive frames are subtracted (which cancels every
part of the picture that held still and leaves grain, which is fresh every frame,
plus whatever moved), high-passed, and the frame is scored in blocks, because no
filter can finish the job - a moving edge leaves high-frequency residue that looks
exactly like grain - so only the CALMEST tenth of the blocks is believed. The median
of the sample sigmas decides the level: a sample's reading tracks how BUSY its scene
is, so favouring the top of the range favours the fastest pans; sampling widely is
what guards against reading low.
"""

import concurrent.futures
import math
import shlex
import struct
import subprocess
import sys

__all__ = [
    "GRAIN_PROBE_SAMPLES",
    "GRAIN_PROBE_FRAMES",
    "GRAIN_PROBE_BLOCK",
    "GRAIN_PROBE_QUANTILE",
    "GRAIN_PROBE_BLACK",
    "GRAIN_PROBE_DEAD",
    "GRAIN_PROBE_MID",
    "GRAIN_PROBE_SLOPE",
    "grain_default_for",
    "grain_probe_sample",
    "grain_probe_level",
    "grain_level_for",
    "source_grain_for",
]

# Film grain synthesis DEFAULTS, per video profile: what that profile synthesises
# when nothing else says otherwise - a level in the 0-50 scale libsvtav1's
# film-grain takes, or "probe": measure each source and synthesise what it
# measured. A profile with no row here defaults to 0, which is the point for the
# x265 rows (no grain synthesis in this pipeline) and the NVENC rows (NVENC cannot
# synthesise grain at all).
_GRAIN_DEFAULTS_TABLE = (
    ("av1BluRay", "probe"),
    ("av1Grain", "probe"),
    ("av1Animation", "0"),
    ("av1ConstrainedGood", "probe"),
    ("av1ConstrainedBad", "probe"),
    ("av1ConstrainedBluRay", "probe"),
)

# Everything the grain probe compares its measurement against: the calibration.
#
# How much of the file is looked at. Grain varies enormously reel to reel, so it
# is the sample COUNT that makes the answer repeatable, not the length of any one
# sample.
GRAIN_PROBE_SAMPLES = 40
GRAIN_PROBE_FRAMES = 8

# The side, in pixels, of the blocks a frame is scored in: small enough that calm
# areas exist inside one, large enough for the average within one to mean
# something.
GRAIN_PROBE_BLOCK = 16

# WHICH block speaks for a frame. Not the average one - the average block holds
# motion and detail, which no residual can tell from grain. The tenth percentile
# is the calm end of the frame, and what is left in a calm block is grain.
GRAIN_PROBE_QUANTILE = 0.10

# The two floors that drop a block from the count entirely, in 16-bit code
# values. The first drops blocks with no picture in them - letterbox bars,
# crushed blacks - which are exactly zero and would otherwise BE the tenth
# percentile. The second drops blocks the source froze rather than coded (skip
# blocks, and the duplicated frames of a telecine), whose grain is identical
# between the two frames and so measures as none.
GRAIN_PROBE_BLACK = 1028
GRAIN_PROBE_DEAD = 2

# The measured sigma -> libsvtav1 level map: a source measuring GRAIN_PROBE_MID
# gets the middle of the 0-50 scale, and every DOUBLING of measured grain adds
# GRAIN_PROBE_SLOPE. Log-linear because grain reads in ratios rather than
# differences.
GRAIN_PROBE_MID = 0.34
GRAIN_PROBE_SLOPE = 9

# 0.886227 is sqrt(pi/2)/sqrt(2): the first factor turns a mean absolute
# deviation into the sigma of the grain behind it, the second undoes the pairing,
# since differencing two frames of independent grain measures sqrt(2) times one
# frame of it.
_SIGMA_SCALE = 0.886227


def grain_default_for(profile: str) -> str:
    """What ``profile`` synthesises when ``-g`` does not say otherwise.

    A level, or "probe" (see :func:`grain_probe_level`). A profile with no row
    defaults to 0, which is how the x265 and NVENC profiles opt out without a row
    each.
    """
    for name, level in _GRAIN_DEFAULTS_TABLE:
        if name == profile:
            return level
    return "0"


def _filter_graph(cols: int, rows: int) -> str:
    """The probe's filter graph: luma block average over residual block average.

    The parts that are not self-evident:
      format=gray16le    luma only, at a common scale - 16-bit normalises 8- and
                         10-bit sources onto ONE scale, so one calibration fits
                         both bit depths.
      avgblur then a grain-extracting temporal difference, high-passed as
      |x - blur(x)| - averaging BEFORE differencing is what makes COARSE grain
                         count for more than fine speckle.
      scale flags=area   the block average, and the only reason this is
                         affordable: what leaves ffmpeg is a few thousand numbers
                         per frame rather than millions of pixels.
    """
    return (
        "[0:v:0]format=gray16le,split=2[m][d];"
        "[m]scale=w={c}:h={r}:flags=area[lum];"
        "[d]avgblur=sizeX=1:sizeY=1,tblend=all_mode=grainextract,split=2[x1][x2];"
        "[x2]gblur=sigma=3:steps=3[xb];"
        "[x1][xb]blend=all_mode=difference,scale=w={c}:h={r}:flags=area[res];"
        "[lum][res]vstack=inputs=2[out]"
    ).format(c=cols, r=rows)


def grain_probe_sample(input: str, t: str, cols: int, rows: int,
                       decode_accel_args: str = "") -> str:
    """The grain sigma of ONE moment of the source, on an 8-bit scale.

    Returns "" when the moment could not be measured - a fade to black, a seek
    past the end, an undecodable file - which callers treat as a SKIPPED sample
    rather than as a failure.

    ``decode_accel_args`` is the hardware decode flags the run settled on, empty
    when there is none. It is word-split, so it is plain flag words with no
    quoting of its own.

    What is graded is what the decode EMITTED, and the decode's status is not
    consulted: what was printed stands even when the seek ended badly, and a
    decode that fails outright prints nothing, which is exactly the skipped
    sample the caller wants.
    """
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
            *shlex.split(decode_accel_args),
            "-ss", t, "-i", input,
            "-filter_complex", _filter_graph(cols, rows),
            "-map", "[out]", "-frames:v", str(GRAIN_PROBE_FRAMES),
            "-an", "-sn", "-dn",
            "-f", "rawvideo", "-pix_fmt", "gray16le", "-"]
    try:
        ran = subprocess.run(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    # Each frame of the stacked map is the luma block average over the residual
    # block average: 16-bit little-endian, luma plane first.
    raw = ran.stdout
    values = struct.unpack("<{n}H".format(n=len(raw) // 2), raw[:len(raw) - len(raw) % 2])
    blocks = cols * rows
    frames = len(values) // (blocks * 2)
    hist: dict[int, int] = {}
    total = 0
    for f in range(frames):
        base = f * blocks * 2
        for b in range(blocks):
            luma = values[base + b]
            residual = values[base + blocks + b]
            if luma > GRAIN_PROBE_BLACK and residual > GRAIN_PROBE_DEAD:
                hist[residual] = hist.get(residual, 0) + 1
                total += 1
    # Too few usable blocks to have an opinion.
    if total < 200:
        return ""
    # The quantile comes out of a HISTOGRAM rather than a sort: the values are
    # 16-bit integers, so counting them is exact and costs one pass.
    want = total * GRAIN_PROBE_QUANTILE
    acc = 0
    for i in range(65536):
        acc += hist.get(i, 0)
        if acc >= want:
            return "{:.6f}".format(i / 257.0 * _SIGMA_SCALE)
    return ""


def grain_probe_level(input: str, media_duration, video_dimensions,
                      jobs_per_core, decode_accel_args: str = "",
                      samples: int = GRAIN_PROBE_SAMPLES) -> str:
    """How much grain this source has, as the libsvtav1 film-grain level it
    justifies (0 = none), with the median sigma.

    ``samples`` moments spread across the file are measured and the MEDIAN of
    them decides, so neither a grain-free title card nor one busy explosion can
    speak for a whole film. The callers' own probes are taken as functions, the
    functions: ``media_duration`` answers in seconds, ``video_dimensions`` with
    the coded size first, and ``jobs_per_core`` settles how many samples decode
    at once.

    Returns the line the bash function prints - ``<level> <sigma>`` newline
    apart, or just ``0`` when nothing could be measured - without the trailing
    newline, which is the caller's to add (a ``read`` of the bash output only
    succeeds on a newline-terminated last line).
    """
    dur = media_duration(input)
    dims = video_dimensions(input).split()
    w = int(dims[0]) if dims and dims[0].isdigit() else 0
    h = int(dims[1]) if len(dims) > 1 and dims[1].isdigit() else 0
    cols = w // GRAIN_PROBE_BLOCK
    rows = h // GRAIN_PROBE_BLOCK
    # A frame too small to hold a usable block grid, or a source with no duration
    # to spread samples over, is not a measurement failure worth reporting on -
    # there is simply nothing to measure.
    if cols < 4 or rows < 4 or not dur:
        return "0"
    duration = float(dur)
    # The samples stop short of both ends: the first and last few percent of a
    # film are logos, fades and credits, which are nobody's idea of representative.
    times = ["{:.3f}".format(duration * (0.04 + 0.92 * i / (samples - 1)))
             for i in range(samples)]
    workers = jobs_per_core(3)
    sigmas = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        def one(t):
            try:
                return grain_probe_sample(input, t, cols, rows,
                                          decode_accel_args)
            except Exception:
                # A worker that dies mid-sample is a SKIPPED sample, the way a
                # bash -c worker that aborts contributes nothing to the list.
                return ""
        for sigma in pool.map(one, times):
            # A sample that measures nothing is SKIPPED, not fatal: the rest
            # still decide.
            if sigma:
                sigmas.append(sigma)
    if not sigmas:
        return "0"
    values = sorted(float(s) for s in sigmas)
    count = len(values)
    if count % 2:
        median = values[count // 2]
    else:
        median = (values[count // 2 - 1] + values[count // 2]) / 2
    if median <= 0:
        return "0"
    level = 20 + GRAIN_PROBE_SLOPE * math.log(median / GRAIN_PROBE_MID) / math.log(2)
    # Rounded UP rather than to nearest: where the reading is uncertain the
    # cheaper mistake is synthesising slightly too much grain, since what is
    # denoised away at encode time cannot be recovered afterwards. (The truncating
    # int() is bash's ``int(level)``: for a negative level that is the rung above,
    # which the floor below then answers for.)
    level = int(level) if level == int(level) else int(level) + 1
    if level < 0:
        level = 0
    if level > 50:
        level = 50
    return "{} {:.4f}".format(level, median)


def grain_level_for(input: str, label, media_duration, video_dimensions,
                    jobs_per_core, decode_accel_args: str = "") -> str:
    """The grain level to encode this file at, the reasoning logged.

    Asking for the probe (``-g 0``) is asking to be told what the SOURCE has, so
    the measurement stands as measured and nothing caps it. An unmeasurable
    source synthesises none. Returns the level and logs one line to stderr, the
    way the bash function does.
    """
    label = label or input
    line = grain_probe_level(input, media_duration, video_dimensions,
                             jobs_per_core, decode_accel_args)
    level, _, sigma = line.partition(" ")
    if not sigma:
        sys.stderr.write(
            "Film grain: could not measure the source, synthesising none: "
            "{}\n".format(label))
    else:
        sys.stderr.write(
            "Film grain: source measured {} sigma -> synthesising {}: "
            "{}\n".format(sigma, int(level), label))
    return level


def source_grain_for(input: str, label, media_duration, video_dimensions,
                     jobs_per_core, decode_accel_args: str = "") -> str:
    """The source's measured grain level for a caller that is JUDGING the source
    - the bitrate test - rather than synthesising anything from it.

    The same measurement as everywhere else, reported in that test's own words
    because nothing is being synthesised here: it is being used to judge how much
    the source's own codec had to spend on noise. An unmeasurable source counts
    as clean, which is the assumption that changes a verdict least.
    """
    label = label or input
    line = grain_probe_level(input, media_duration, video_dimensions,
                             jobs_per_core, decode_accel_args)
    level, _, sigma = line.partition(" ")
    if not sigma:
        sys.stderr.write(
            "Bitrate test: could not measure the source grain, judging it as "
            "clean: {}\n".format(label))
        return "0"
    sys.stderr.write(
        "Bitrate test: source grain measured {} sigma -> level {}: "
        "{}\n".format(sigma, int(level), label))
    return level