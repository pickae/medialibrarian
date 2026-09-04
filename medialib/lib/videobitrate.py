"""The "is this video worth re-encoding" model.

A re-encode is only worth its hours when the source has bits to spare: a file
that was never given enough of them cannot be improved by encoding it again,
only made worse, and a file that has plenty can be re-encoded far smaller at
the same quality. Telling the two apart is one anchor scaled along its five
axes - resolution, codec and its era, aspect ratio, frame rate and grain -
and the reading of a measured bitrate against the result.

Which codec a name means, and which tier a size is, are answered by
:mod:`medialib.lib.codecs` and :mod:`medialib.lib.resolutions` respectively (the
family/era and the tier ladder the model keys on); the grain level it is read
against is the one :mod:`medialib.lib.videograin` measures. The model itself is pure
arithmetic over its tables and needs no tool; only measuring a file
(``videoBitrateStats``) reaches for ffprobe.
"""

from __future__ import annotations

import json
import math
import re
import subprocess

from medialib.lib.codecs import (
    MODERN_ERA,
    MPEG2_ERA,
    MPEG4_ERA,
    era_of,
    family_of,
)
from medialib.lib.formatting import awk_number
from medialib.lib.resolutions import ceiling, tier_of

__all__ = [
    "adequate_bitrate_1080",
    "adequate_bitrate_pixels",
    "generous_bitrate_factor",
    "codec_tuning_table",
    "era_penalty_table",
    "hfr_reference_fps",
    "hfr_bitrate_exponent",
    "hfr_bitrate_factor_max",
    "grain_bitrate_knee",
    "grain_bitrate_full",
    "grain_bitrate_surcharge",
    "grain_synthesis_bitrate_gain",
    "hardware_bitrate_penalty",
    "bitrate_codec_tuning",
    "bitrate_tier_pixels",
    "bitrate_grain_heaviness",
    "source_bitrate_grain",
    "adequate_video_bitrate",
    "bitrate_verdict",
    "video_bitrate_stats",
    "stats_from_json",
]

# What a 1920x1080 16:9 H.264 source at up to 30 fps needs to be adequate, in
# kbit/s, and the pixel count that figure belongs to. Everything else is this
# figure scaled, so it is the one anchor the rest of the model multiplies.
adequate_bitrate_1080 = 5000
adequate_bitrate_pixels = 2073600

# Where "generous" starts: twice adequate is a whole adequate encode's worth of
# spare, which is the same reading a caller demanding a 50% saving applies.
generous_bitrate_factor = 2

# Per-family tuning, keyed on the family names of ``codecs``. Each row is
# (family, factor, exponent): what the family needs at the 1080p anchor,
# relative to H.264, and how its requirement scales with the pixel count
# (1 would be linearly, and every row is under it). The exponents descend with
# the codecs' age - a newer codec gains more on a big frame than on a small one
# - which is what grows its advantage over H.264 as the frame size grows.
#
# The pre-H.264 families (mpeg2, intra, mpeg4) carry H.264's own factor and
# exponent and are charged once for their era by the table below, instead of
# being given curves of their own.
codec_tuning_table = (
    ("mpeg2", "1.00", "0.80"),
    ("intra", "1.00", "0.80"),
    ("mpeg4", "1.00", "0.80"),
    ("vc1", "1.25", "0.85"),
    ("vp8", "1.30", "0.86"),
    ("h264", "1.00", "0.80"),
    ("vp9", "0.82", "0.78"),
    ("hevc", "0.78", "0.77"),
    ("av1", "0.60", "0.70"),
    ("vvc", "0.50", "0.66"),
)

# What each generation costs on top of everything else, keyed on the era names
# of ``codecs``. ONE flat number per era, applied last, rather than a curve:
# files that old are rare enough that the interaction of their age with
# resolution and grain is not worth guessing at, and a flat penalty is the one
# thing no other axis can quietly flatter away. An era this table does not
# name - including the unknown one - costs nothing extra.
era_penalty_table = {
    MPEG2_ERA: "2.00",
    MPEG4_ERA: "1.50",
    MODERN_ERA: "1.00",
}

# The frame rate the anchor was measured at, and how a higher one scales.
# 0.585 is log2(1.5) - exactly "twice the frames for half as much again" - and
# the factor is capped so a 240 fps clip cannot extrapolate the trend past
# where it was ever measured.
hfr_reference_fps = 30
hfr_bitrate_exponent = 0.585
hfr_bitrate_factor_max = 2.0

# The grain scale, read as a weight from 0 (ordinary picture) to 1 (noise
# dominates). Below the knee grain is detail the codecs handle in their stride;
# 25 - very grainy - sits a quarter of the way up, so a typical 35mm transfer
# is nudged rather than reclassified.
grain_bitrate_knee = 20
grain_bitrate_full = 40

# What that weight does. The surcharge is what grain costs a codec that has to
# CODE it (up to half as much again at full heaviness); the gain is what it
# saves an encode that synthesises it instead - the denoised picture is easier
# to code than the same film without grain ever was.
grain_bitrate_surcharge = 0.5
grain_synthesis_bitrate_gain = 0.25

# What a hardware encode costs in efficiency: applied to the OUTPUT side only,
# because a source's encoder is not knowable from the file.
hardware_bitrate_penalty = 1.15

# A grain level a caller has not measured: the word the shell's heaviness awk
# rejects with ``^[0-9]+(\.[0-9]+)?$`` and answers as none, so a missing probe
# cannot silently raise or lower a verdict.
_GRAIN_NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")


def bitrate_codec_tuning(codec: str) -> str:
    """``"<factor> <exponent> <eraPenalty> <family>"`` for a codec.

    The family and generation come from ``codecs``; the two tables above say
    what they are worth. A codec that list does not name - or none at all, from
    a source ffprobe could not read - is tuned as H.264 with no era penalty:
    H.264 is the ladder's reference point, so an unknown source is judged
    neither generously nor harshly, and is named as the unknown it is.
    """
    family = family_of(codec)
    era = era_of(codec)
    penalty = era_penalty_table.get(era, "1.00")
    row = next((r for r in codec_tuning_table if r[0] == family), None)
    if row is None:
        return f"1.00 0.80 {penalty} {family}"
    return f"{row[1]} {row[2]} {penalty} {family}"


def bitrate_tier_pixels(width: str, height: str) -> str:
    """The pixel count of the 16:9 frame that DEFINES this size's tier.

    1920x1080 for anything in the 1080p tier, whatever its real shape - that
    nominal size is what the sub-linear resolution scaling applies to, and the
    frame's real pixel count is then applied linearly on top (see
    :func:`adequate_video_bitrate`). The open-ended SD floor and a frame whose
    size could not be read have no nominal size of their own, so both fall back
    to the frame's own pixel count, which is 0 when the size is unreadable.
    """
    c = ceiling(tier_of(width, height))
    if c is not None:
        return str(c[0] * c[1])
    w = awk_number(width)
    h = awk_number(height)
    if w <= 0 or h <= 0:
        return "0"
    return str(int(w * h))


def bitrate_grain_heaviness(grain: str) -> str:
    """A measured grain level as a 0-1 weight, three decimals.

    A level that is not a plain non-negative number - no measurement was taken
    - counts as none. The level is read from the knee (ordinary picture) to the
    full level (noise dominates), clamped to the unit interval.
    """
    text = "" if grain is None else str(grain)
    if not _GRAIN_NUMBER.fullmatch(text) or grain_bitrate_full <= grain_bitrate_knee:
        return "0.000"
    level = float(text)
    heaviness = (level - grain_bitrate_knee) / (grain_bitrate_full - grain_bitrate_knee)
    if heaviness < 0:
        heaviness = 0
    if heaviness > 1:
        heaviness = 1
    return f"{heaviness:.3f}"


def source_bitrate_grain(file: str, label: str = "", probe_enabled: int = 1,
                         source_grain_for=None) -> str:
    """The grain level to judge a source at: the measurement when the probe is
    on, nothing at all when it is off.

    The measurement itself is not here: it belongs to ``videograin`` (it needs
    a caller's own duration and dimensions to lay its sample grid out), so this
    only calls it when a caller has handed it over - a caller that never sourced
    the probe library cannot have been asking for one - and leaves an unstated
    level to weigh as an ordinary picture, the way ``bitrateGrainHeaviness``
    reads one.
    """
    if probe_enabled and source_grain_for is not None:
        return source_grain_for(file, label or file)
    return ""


def adequate_video_bitrate(codec: str, width: str, height: str, fps: str,
                           grain: str, synth: str = "0",
                           hw: str = "0") -> str:
    """What a video stream of that description needs to be adequate, in kbit/s.

    The anchor scaled along every axis. Prints nothing when the frame size is
    unknown, since every axis is a multiple of it and a guessed size would be a
    guessed verdict. ``synth`` says the grain is carried as a parameter set
    rather than coded - the output side of a grain-synthesising profile - so the
    encoder keeps its full codec advantage and is judged below the grainless
    requirement instead of above it; ``hw`` adds the hardware-encode penalty,
    likewise an output-side concern. A pre-H.264 stream goes through all of it
    as though it were H.264 and is multiplied by its era penalty once, at the
    end.
    """
    factor, exponent, era_penalty, _family = bitrate_codec_tuning(codec).split()
    tier_pixels = bitrate_tier_pixels(width, height)
    heaviness = bitrate_grain_heaviness(grain)

    base = awk_number(adequate_bitrate_1080)
    ref = awk_number(adequate_bitrate_pixels)
    f = awk_number(factor)
    p = awk_number(exponent)
    era = awk_number(era_penalty)
    tp = awk_number(tier_pixels)
    w = awk_number(width)
    h = awk_number(height)
    fpstv = awk_number(fps)
    fps_ref = awk_number(hfr_reference_fps)
    fps_exp = awk_number(hfr_bitrate_exponent)
    fps_max = awk_number(hfr_bitrate_factor_max)
    hv = awk_number(heaviness)
    sy = awk_number(synth)
    surcharge = awk_number(grain_bitrate_surcharge)
    gain = awk_number(grain_synthesis_bitrate_gain)
    hwv = awk_number(hw)
    hw_penalty = awk_number(hardware_bitrate_penalty)

    if tp <= 0 or w <= 0 or h <= 0:
        return ""
    if sy == 1:
        # Denoised before it is encoded: the codec keeps its full advantage, the
        # frame still scales the way its codec does, and what is left to code is
        # easier than the same picture with grain in it.
        codec_factor = f
        pixel_exponent = p
        grain_factor = 1 - gain * hv
    else:
        # Coded grain is noise, so it flattens the difference between codecs and
        # straightens the resolution curve towards "bitrate follows pixels", on
        # top of costing bits of its own.
        codec_factor = f + (1 - f) * hv
        pixel_exponent = p + (1 - p) * hv
        grain_factor = 1 + surcharge * hv
    tier_factor = (tp / ref) ** pixel_exponent
    aspect_factor = (w * h) / tp
    fps_factor = 1.0
    if fpstv > fps_ref:
        fps_factor = (fpstv / fps_ref) ** fps_exp
        if fps_factor > fps_max:
            fps_factor = fps_max
    hw_factor = hw_penalty if hwv == 1 else 1.0
    era_factor = era if era > 0 else 1.0
    total = (base * codec_factor * tier_factor * aspect_factor * grain_factor
             * fps_factor * hw_factor * era_factor)
    return f"{total:.0f}"


def bitrate_verdict(kbit: str, adequate: str) -> str:
    """What a bitrate IS for a requirement: starved below it, generous at
    ``generous_bitrate_factor`` times it or above, adequate in between, and
    unknown when either figure is missing.

    This is the verdict on the source alone; whether a conversion follows is a
    second question, asked against the output's requirement.
    """
    b = awk_number(kbit)
    a = awk_number(adequate)
    gen = awk_number(generous_bitrate_factor)
    if a <= 0 or b <= 0:
        return "unknown"
    if b < a:
        return "starved"
    if b >= a * gen:
        return "generous"
    return "adequate"


def _num(x) -> float | None:
    """A value as the stats filter's ``num`` reads it: the whole string as a
    number, or nothing - never a numeric PREFIX, and never a non-finite one.
    """
    if x is None:
        return None
    try:
        value = float(str(x))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _num_or_raise(x) -> float:
    """The same reading that raises instead of answering nothing: the filter's
    spots where jq itself would die on the arithmetic (a channels that is a
    string it cannot coerce, a frame rate whose numerator is not a number) are
    spots where the WHOLE filter fails and states nothing, and that is what an
    exception out of :func:`stats_from_json` means.
    """
    value = _num(x)
    if value is None:
        raise ValueError(f"not a number: {x!r}")
    return value


def _bps(stream: dict) -> object:
    """The value of the first ``bps``-prefixed tag (case-insensitively), or None.

    Matroska states no per-stream bitrate; mkvmerge writes a per-track "BPS" tag
    (and "BPS-eng" and friends, one per language) instead, whose suffix spelling
    is not fixed, so it is matched on the prefix alone. The first such tag's
    value is the answer even when it is null: jq's ``[0]`` takes the first
    match, and the ``// null`` behind it only covers the no-match case.
    """
    tags = stream.get("tags") or {}
    for key, value in tags.items():
        if str(key).lower().startswith("bps"):
            return value
    return None


def _frame_rate(r) -> float | None:
    """A frame-rate field as a number, the way the filter's ``rate`` reads it.

    "num/den" divided when the denominator is positive; anything else, the
    field read as a number, or nothing. A division jq cannot make - a
    denominator it parsed as positive but a numerator it could not - raises,
    the way jq's arithmetic error kills the whole filter.
    """
    if not isinstance(r, str):
        return None
    parts = r.split("/")
    denominator = _num(parts[1]) if len(parts) == 2 else None
    if denominator is not None and denominator > 0:
        return _num_or_raise(parts[0]) / denominator
    return _num(r)


def _number_text(value: float) -> str:
    """A number the way the filter's ``tostring`` prints it: a whole number as
    an integer, anything else at its shortest round-trip form.
    """
    if value == int(value):
        return str(int(value))
    return repr(value)


def stats_from_json(data: dict) -> str:
    """The four fields ``"<codec> <fps> <kbit/s> <origin>"`` the stats filter
    reads out of a ffprobe ``-show_streams -show_format`` document, space
    joined, each empty when it could not be read.

    The bitrate is the VIDEO stream's, never the container's, and getting it
    takes three tries: a per-stream ``bit_rate`` (MP4 states one, and it is the
    figure wanted and is used as-is); a per-track "BPS" tag (Matroska); or the
    container's own bitrate with the audio tracks subtracted - an estimate,
    labelled as one, that is the video's own to within a few percent on the
    files that need it.
    """
    # jq indexes null's .streams as null, which the // [] settles to no
    # streams - the filter completes with every field empty. Every other
    # non-object top level is a hard index error, which fails the filter and
    # states nothing.
    if data is None:
        data = {}
    elif not isinstance(data, dict):
        raise ValueError("the document's top level is not an object")

    streams = data.get("streams") or []
    video = None
    for stream in streams:
        if stream.get("codec_type") == "video":
            attached = (stream.get("disposition") or {}).get("attached_pic")
            if (attached if attached is not None else 0) == 0:
                video = stream
                break
    video = video or {}
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = data.get("format") or {}

    direct = _num(video.get("bit_rate"))
    if direct is None:
        direct = _num(_bps(video))

    audio = 0.0
    for stream in audio_streams:
        value = _num(stream.get("bit_rate"))
        if value is None:
            value = _num(_bps(stream))
        if value is None:
            channels = stream.get("channels")
            value = _num_or_raise(2 if channels is None else channels) * 64000
        audio += value

    total = _num(fmt.get("bit_rate"))
    if total is None:
        duration = _num(fmt.get("duration"))
        size = _num(fmt.get("size"))
        if duration is not None and duration > 0 and size is not None:
            total = size * 8 / duration

    if direct is not None:
        rate, origin = direct, "stated"
    elif total is not None:
        rate, origin = (total - audio) * 0.98, "estimated"
    else:
        rate, origin = None, ""

    fps = _frame_rate(video.get("avg_frame_rate"))
    if fps is None:
        fps = _frame_rate(video.get("r_frame_rate"))
    fps_text = "" if fps is None else _number_text(fps)

    if rate is not None and rate > 0:
        kbit_text = str(math.floor(rate / 1000))
    else:
        kbit_text = ""

    codec_name = video.get("codec_name")
    return " ".join(["" if codec_name is None else str(codec_name), fps_text,
                     kbit_text, origin])


def video_bitrate_stats(input: str) -> str:
    """``"<codec> <fps> <kbit/s> <where the bitrate came from>"`` for the
    source's first video stream, each field empty when it could not be read.

    Runs ffprobe for the document and hands it to :func:`stats_from_json`.
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of",
             "json", input],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    out = proc.stdout.decode(errors="replace")
    if not out.strip():
        return ""
    try:
        data = json.loads(out)
        return stats_from_json(data)
    except (ValueError, AttributeError, TypeError, KeyError):
        # The document is not JSON, or it reaches a spot the filter cannot
        # read - a streams field that is not a list of objects, a stream that
        # is not one - the way jq's own error fails the whole filter and
        # states nothing: an unreadable file.
        return ""