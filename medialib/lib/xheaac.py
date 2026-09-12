"""The xHE-AAC encoder: whether this host has it, and how to ask it.

xHE-AAC (MPEG-D USAC, ISO/IEC 23003-3) is the one output codec here that ffmpeg
cannot produce. It ships a DECODER for it and nothing else - there is no
`exhale` wrapper in ffmpeg, not even in master - so unlike every other encode in
this library, this one is an external binary reached over a pipe rather than a
`-c:a` argument. That is the whole reason this module exists: the question "can
this host encode xhe-aac at all" has to be asked and answered before a run
starts, the way :mod:`medialib.lib.imagemagick` asks whether an ImageMagick
build has the delegate for a format.

**exhale** (Christian R. Helmrich, project ecodis) is that binary: a small,
self-contained program that reads WAVE on stdin and writes a finished MP4. It
implements the frequency-domain path only - its own README says the lower-rate
tools, ACELP and TCX and MPEG Surround with unified stereo, "won't be
integrated" - and it takes a PRESET rather than a bitrate, whose two ladders are
the table at the bottom of this module.

Ittiam's libxaac implements the whole USAC toolbox including those speech
coders, and is deliberately not used: its encoder takes exactly two bitrates and
silently replaces every other with 96 kbps, which is above the top of the range
a spoken-word library works in, while exhale's eSBR ladder reaches 36 kbit/s
stereo and 18 mono. The fuller encoder cannot do this pipeline's job, so this
module knows about one encoder rather than carrying a preference order between
them.
"""

from __future__ import annotations

import sys

from medialib.lib import tooldeps

__all__ = [
    "EXHALE",
    "ENCODER_SPEC",
    "require_encoder",
    "encoder_present",
    "exhale_preset",
    "preset_bitrate",
    "exhale_argv",
    "wav_argv",
    "SAMPLE_RATES",
    "input_sample_rate",
]

EXHALE = "exhale"

# The binary as a tooldeps spec, for a CALLER that wants to ask the ordinary
# preflight "is there an xHE-AAC encoder here" in the same list as its ffmpeg
# and its rsync. `require_encoder` below deliberately does not use it - see its
# docstring.
ENCODER_SPEC = EXHALE


def encoder_present(present=None) -> bool:
    """Whether this host can encode xHE-AAC.

    ``present`` is the presence test, which is how a case asks about a host it
    is not running on. Defaulted to None and resolved HERE rather than written
    as ``present=tooldeps.tool_present`` in the signature: a default argument is
    bound once, at import, so the signature form would freeze this to whatever
    the function was then and quietly ignore a caller that replaced it.
    """
    present = present or tooldeps.tool_present
    return bool(present(EXHALE))


def require_encoder(what: str, *, present=None,
                    skip_preflight: bool = False, file=None) -> int:
    """Refuse before any work starts when this host cannot encode xhe-aac, and
    return 0 silently when it can.

    The message is written here rather than handed to
    ``tooldeps.require_tools`` because no distribution packages exhale: the hint
    has to name the source to build and the way out of the run (``-o opus``),
    which the shared preflight has no room for.

    ``skip_preflight`` is the same escape hatch the tool preflight and the
    ImageMagick delegate check honour, for a host whose tools this cannot see.
    """
    if file is None:
        file = sys.stderr
    if skip_preflight:
        return 0
    if encoder_present(present):
        return 0

    role, hint = tooldeps.tool_note(EXHALE).split("|", 1)
    file.write("\n".join([
        "",
        "Cannot run %s: it needs an xHE-AAC encoder, and this machine has "
        "none." % what,
        "",
        "  %s  %s" % (EXHALE, role),
        "      %s" % hint,
        "",
        "Install it (or put it on PATH) and run again, or encode Opus instead "
        "(-o opus).",
        "Nothing was changed.",
    ]) + "\n")
    return 1


# --- exhale's presets ---------------------------------------------------------
# exhale takes a PRESET, not a bitrate: one character, whose nominal rate is
# fixed arithmetic in its own usage text. Two families, and what separates them
# is eSBR rather than a rate:
#
#   0-9   no eSBR,   16 * n + 48 kbit/s
#   a-g   with eSBR, 12 * n + 36 kbit/s
#
# Both numbers are for STEREO, and mono is half - which is why the floor of the
# whole range is exhale's own "18 kbit/s mono, 36 kbit/s stereo".
_PLAIN_BASE, _PLAIN_STEP, _PLAIN_COUNT = 48, 16, 10
_SBR_BASE, _SBR_STEP, _SBR_COUNT = 36, 12, 7

# The channel count exhale's own figures are quoted for.
_QUOTED_CHANNELS = 2


def _plain_presets() -> list[tuple[str, int]]:
    """The non-eSBR family, `0`-`9`, each with its nominal STEREO rate."""
    return [(str(index), _PLAIN_BASE + _PLAIN_STEP * index)
            for index in range(_PLAIN_COUNT)]


def _sbr_presets() -> list[tuple[str, int]]:
    """The eSBR family, `a`-`g`, each with its nominal STEREO rate."""
    return [(chr(ord("a") + index), _SBR_BASE + _SBR_STEP * index)
            for index in range(_SBR_COUNT)]


def _presets() -> list[tuple[str, int]]:
    """Both families, for reading a preset character back."""
    return _plain_presets() + _sbr_presets()


def _scaled(stereo_kbps: int, channels: int) -> int:
    """A stereo figure at another channel count, per channel and rounded.

    Rounded to the nearest rather than truncated, so an odd stereo figure at
    mono answers the half exhale itself quotes.
    """
    channels = max(1, channels)
    return (stereo_kbps * channels * 2 + _QUOTED_CHANNELS) // (
        _QUOTED_CHANNELS * 2)


def preset_bitrate(preset: str, channels: int = _QUOTED_CHANNELS) -> int:
    """What a preset really encodes at for this channel count, or 0 for a
    character that is not a preset.

    The run prints this beside the requested rate, so exhale's coarse ladder
    rounding a -b is something a reader SEES rather than something that happens
    silently.
    """
    for character, stereo in _presets():
        if character == preset:
            return _scaled(stereo, channels)
    return 0


def exhale_preset(bitrate: int, channels: int = _QUOTED_CHANNELS) -> str:
    """The preset this run encodes at: the nearest rung of the eSBR ladder,
    unless the target is above where that ladder reaches.

    The FAMILY is chosen before the rung, and not on distance. The two ladders
    overlap across 48-108 kbit/s stereo, and inside the overlap the nearest rung
    of the plain one is regularly the nearer of the two - 32 kbit/s mono is
    preset `1` exactly, and eSBR preset `c` only within 2 - which would pick the
    frequency-domain-only encode for a spoken-word library at half the rate
    plain FD wants. eSBR is what makes those rates listenable, so it wins the
    whole overlap, and the plain ladder is reached only above eSBR's ceiling
    where it is the only one left.

    Within the chosen family the rung is the nearest, not the first at or above:
    a 46 kbps request is 2 under preset `b` (48 stereo) and 10 over preset `a`
    (36), and there is no reading of "46" that means 36.
    """
    family = _sbr_presets()
    if bitrate > max(_scaled(stereo, channels) for _c, stereo in family):
        family = _plain_presets()
    best, best_distance = "", -1
    for character, stereo in family:
        distance = abs(_scaled(stereo, channels) - bitrate)
        if best_distance < 0 or distance < best_distance:
            best, best_distance = character, distance
    return best


# --- the calls ----------------------------------------------------------------
# exhale accepts 32-48 kHz input and its README says to use nothing else, so a
# source outside that band is resampled to the nearest rate IN it rather than
# handed over and refused. These three are the rates the band holds.
SAMPLE_RATES = (32000, 44100, 48000)


def input_sample_rate(rate: int) -> int:
    """The rate the WAVE fed to the encoder is written at.

    A source already inside the band keeps its own rate exactly: resampling a
    48 kHz file to 48 kHz is a no-op, but resampling a 44.1 kHz one to 48 would
    be a real and pointless interpolation. Anything outside moves to the nearest
    end of the band, and an unknown rate - a probe that answered nothing - is
    read as the top, which is what a modern source almost always is.
    """
    if rate <= 0:
        return SAMPLE_RATES[-1]
    if rate in SAMPLE_RATES:
        return rate
    if rate < SAMPLE_RATES[0]:
        return SAMPLE_RATES[0]
    if rate > SAMPLE_RATES[-1]:
        return SAMPLE_RATES[-1]
    return min(SAMPLE_RATES, key=lambda candidate: abs(candidate - rate))


def wav_argv(source: str, rate: int, mono: bool,
             start: str = "", duration: str = "") -> list[str]:
    """ffmpeg decoding one source's first audio stream to WAVE on stdout.

    16-bit signed, because that is the one word size both encoders document
    reading - and it is the width the source's own lossy samples decode to
    anyway. The metadata is dropped (`-map_metadata -1`): the chapters and the
    cover are re-attached to the finished file from the ORIGINAL afterwards, the
    same way the Opus path does it, so carrying them through the intermediate
    would only give the encoder a WAVE header full of tags to ignore.

    <start> and <duration> cut one time range out instead of taking the whole
    file, and are placed BEFORE `-i` so ffmpeg seeks rather than decoding up to
    the mark - the same ordering the Opus chunk encoder uses.
    """
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start:
        argv += ["-ss", start]
    if duration:
        argv += ["-t", duration]
    argv += ["-i", source, "-map", "0:a:0", "-map_metadata", "-1"]
    if mono:
        argv += ["-ac", "1"]
    return argv + ["-ar", str(rate), "-c:a", "pcm_s16le", "-f", "wav", "-"]


def exhale_argv(preset: str, out: str) -> list[str]:
    """exhale encoding the WAVE on ITS stdin into <out>.

    Two arguments after the program is exhale's own stdin form - it reads an
    input file name from argv only when given three - which is what keeps a
    three-hour book from needing a three-hour WAVE on disk first.
    """
    return ["exhale", preset, out]
