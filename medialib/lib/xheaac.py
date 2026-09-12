"""The xHE-AAC encoders: which one this host has, and how to ask it.

xHE-AAC (MPEG-D USAC, ISO/IEC 23003-3) is the one output codec here that ffmpeg
cannot produce. It ships a DECODER for it and nothing else - there is no
`libxaac` and no `exhale` wrapper in ffmpeg, not even in master - so unlike every
other encode in this library, this one is an external binary reached over a pipe
rather than a `-c:a` argument. That is the whole reason this module exists: the
question "can this host encode xhe-aac at all" has to be asked and answered
before a run starts, the way :mod:`medialib.lib.imagemagick` asks whether an
ImageMagick build has the delegate for a format.

There are two encoders, and they are not interchangeable:

**libxaac** (Ittiam, binary ``xaacenc``) implements the whole USAC toolbox. The
one that matters for spoken word is ``-usac:0``, the SWITCHED core coder, which
lets the encoder drop into the TD (ACELP/TCX) speech coders on speech and stay in
FD on music - which is exactly the material this pipeline ingests and exactly why
it is preferred. It also has eSBR, harmonic SBR, MPEG Surround, MPEG-D DRC and a
random-access interval.

**exhale** (Christian R. Helmrich, project ecodis) implements the
frequency-domain path only. Its own README says the lower-rate tools - ACELP,
TCX, MPEG Surround with unified stereo - "won't be integrated", and warns that
the quality at its lowest presets "doesn't reflect the full capabilities of the
Extended HE-AAC standard". It is a smaller, simpler, self-contained program: it
reads WAVE on stdin and writes a finished MP4.

So the preference order is libxaac first, exhale second, and a host with only one
of them uses that one. It is not a quality ranking dressed up as a fallback -
they are a full USAC encoder and a partial one.

**libxaac is not usable here, and this module says so rather than half-driving
it.** Two things stand in the way, and only the first is medialib's to fix.

The PACKAGING. ``xaacenc`` at AOT 42 does not write a container: it writes a BARE
USAC elementary stream, prefixed by the AudioSpecificConfig, plus a sidecar
``.txt`` giving that config's length and the size of every frame after it. ADTS
cannot carry AOT 42 (its profile field has two bits), libxaac ships no packager -
only its own decoder reads that sidecar - and ffmpeg can neither demux nor mux
the pair. This much IS solvable here: the sidecar carries everything an MP4
writer needs, and a writer that puts the config in an ``esds`` and the frames in
an ``mdat`` produces a file ffmpeg reads back as xHE-AAC at the right rate and
duration, frames byte-identical to the encoder's own.

The BITRATE, which is what actually decides it. The USAC encoder accepts exactly
two rates and silently replaces every other with 96 kbps::

    if (i_bitrate != 64000 && i_bitrate != 96000)
        i_bitrate = USAC_BITRATE_DEFAULT_VALUE;   /* 96000 */

    -- encoder/ixheaace_api.c

So libxaac cannot encode a spoken-word library at all: 64 kbps is above the top
of the range this pipeline uses, and there is no way down. exhale's eSBR ladder
reaches 36 kbps stereo and 18 mono, which is the range that matters here, so the
partial encoder is the one that does the work and the full one is declined. That
is a limit of the encoder rather than a gap in medialib, which is why writing the
MP4 muxer would not change the answer.
"""

from __future__ import annotations

import sys
from typing import NamedTuple

from medialib.lib import tooldeps

__all__ = [
    "LIBXAAC",
    "EXHALE",
    "ENCODER_SPEC",
    "BACKENDS",
    "backend_named",
    "present_backends",
    "choose_backend",
    "require_encoder",
    "usable_tools",
    "exhale_preset",
    "preset_bitrate",
    "exhale_argv",
    "wav_argv",
    "SAMPLE_RATES",
    "input_sample_rate",
]

LIBXAAC = "libxaac"
EXHALE = "exhale"


class Backend(NamedTuple):
    """One xHE-AAC encoder: what it is called, what its binary is called, and
    whether this command can actually drive it from end to end.

    ``usable`` is not "installed" - that is asked separately, of PATH. It is
    whether medialib has everything ELSE the back-end needs, and ``gap`` is the
    sentence naming what is missing when it does not. Keeping the two apart is
    what lets a refusal say "you have xaacenc, and the muxer is the missing
    piece" rather than the untrue "xaacenc is not installed".
    """

    name: str
    tool: str
    usable: bool
    gap: str


# In preference order, so the first usable one that is present is the one a run
# encodes with.
BACKENDS = (
    Backend(
        name=LIBXAAC, tool="xaacenc", usable=False,
        # Short on purpose: it is printed inside a refusal line. The whole
        # story is in this module's docstring. The BITRATE is named rather than
        # the missing muxer: the muxer is a thing medialib could write, and the
        # two fixed rates are the reason writing it would not help.
        gap="encodes USAC only at 64 or 96 kbps, far above spoken word",
    ),
    Backend(name=EXHALE, tool="exhale", usable=True, gap=""),
)

# The two binaries as one tooldeps alternatives spec, for a CALLER that wants to
# ask the ordinary preflight "is there any xHE-AAC encoder here" in the same list
# as its ffmpeg and its rsync. `require_encoder` below deliberately does not use
# it - see its docstring. Derived from BACKENDS so the two can never name
# different binaries.
ENCODER_SPEC = "|".join(backend.tool for backend in BACKENDS)


def backend_named(name: str) -> Backend | None:
    """The back-end by name, or None. The callers and the cases name them by the
    constants above rather than by their index in the tuple."""
    for backend in BACKENDS:
        if backend.name == name:
            return backend
    return None


def present_backends(present=None) -> list[Backend]:
    """Every back-end whose binary is on PATH, in preference order.

    ``present`` is the presence test, which is how a case asks about a host it
    is not running on. Defaulted to None and resolved HERE rather than written
    as ``present=tooldeps.tool_present`` in the signature: a default argument is
    bound once, at import, so the signature form would freeze this to whatever
    the function was then and quietly ignore a caller that replaced it.
    """
    present = present or tooldeps.tool_present
    return [backend for backend in BACKENDS if present(backend.tool)]


def choose_backend(present=None) -> Backend | None:
    """The back-end a run will encode with: the first one that is both present
    and usable, or None.

    An unusable back-end is SKIPPED rather than chosen and then failed, so a
    host with both xaacenc and exhale encodes with exhale instead of refusing
    over the one it cannot drive.
    """
    for backend in present_backends(present):
        if backend.usable:
            return backend
    return None


def usable_tools() -> str:
    """The binaries a run could actually encode with, as a sentence reads them.

    What the help page tells a reader to install, and what a refusal points at.
    Derived from `usable` rather than from the whole table on purpose: a page
    that named a gated back-end would send somebody off to build the one tool
    that will then be declined.
    """
    return " or ".join(backend.tool for backend in BACKENDS if backend.usable)


def _hint(tool: str) -> str:
    """Just the install half of the shared notes table's line for <tool>."""
    return tooldeps.tool_note(tool).split("|", 1)[1]


def require_encoder(what: str, *, present=None,
                    skip_preflight: bool = False, file=None) -> int:
    """Refuse before any work starts when this host cannot encode xhe-aac, and
    return 0 silently when it can.

    Two different refusals, because the reader has to do two different things.

    With NEITHER binary installed, both are named, with what each is for and
    where to get it - and the message is written here rather than handed to
    ``tooldeps.require_tools``, which prints ONE role and ONE install hint for an
    alternatives spec. That is right for `7z|7zz|7za`, three spellings of one
    package; it would be wrong here, where the two are different programs from
    different projects, and a reader told only about libxaac would never learn
    that exhale is the one which works today.

    With a GATED back-end installed and no usable one - xaacenc but no exhale -
    the tool IS there, so naming it as missing would be a lie. That refusal says
    what medialib is missing instead, and offers `-o opus` as the way to get the
    run done now.

    ``skip_preflight`` is the same escape hatch the tool preflight and the
    ImageMagick delegate check honour, for a host whose tools this cannot see.
    """
    if file is None:
        file = sys.stderr
    if skip_preflight:
        return 0
    if choose_backend(present) is not None:
        return 0

    gated = [backend for backend in present_backends(present)
             if not backend.usable]
    lines = [""]
    if gated:
        lines.append("Cannot run %s: this host's only xHE-AAC encoder is one "
                     "medialib cannot drive yet." % what)
        lines.append("")
        for backend in gated:
            lines.append("  %s  is installed, but %s" % (backend.tool,
                                                         backend.gap))
        lines.append("")
        lines.append("Either install %s as well:" % usable_tools())
        lines.append("")
        for backend in BACKENDS:
            if backend.usable:
                lines.append("  %s  %s" % (backend.tool, _hint(backend.tool)))
        lines.append("")
        lines.append("or encode Opus instead (-o opus). Nothing was changed.")
    else:
        lines.append("Cannot run %s: it needs an xHE-AAC encoder, and this "
                     "machine has neither." % what)
        lines.append("")
        lines.append("Either one is enough:")
        lines.append("")
        for backend in BACKENDS:
            role, hint = tooldeps.tool_note(backend.tool).split("|", 1)
            lines.append("  %s  %s" % (backend.tool, role))
            lines.append("      %s" % hint)
            if not backend.usable:
                lines.append("      not usable yet: %s" % backend.gap)
        lines.append("")
        lines.append("Install %s (or put it on PATH) and run again, or encode "
                     "Opus instead (-o opus)." % usable_tools())
        lines.append("Nothing was changed.")
    file.write("\n".join(lines) + "\n")
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
    three-hour book from needing a three-hour WAVE on disk first. The libxaac
    back-end will not have that option: ``xaacenc`` takes `-ifile:` and no pipe,
    which is a second reason it is the harder of the two to reach.
    """
    return ["exhale", preset, out]
