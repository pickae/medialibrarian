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

Splitting a long file works here as it does for Opus, but the pieces need
placing rather than merely cutting: the encoder primes its decoder with a frame
whose output a joined MP4 has no way to discard, so the cuts leave that frame a
gap of its own to fill. `encoder_timing` measures it and the three helpers below
it do the arithmetic.

Ittiam's libxaac implements the whole USAC toolbox including those speech
coders, and is deliberately not used: its encoder takes exactly two bitrates and
silently replaces every other with 96 kbps, which is above the top of the range
a spoken-word library works in, while exhale's eSBR ladder reaches 36 kbit/s
stereo and 18 mono. The fuller encoder cannot do this pipeline's job, so this
module knows about one encoder rather than carrying a preference order between
them.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile

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
    "SAMPLE_BYTES",
    "WAVE_DATA_CEILING",
    "wave_seconds_ceiling",
    "fits_in_one_wave",
    "encoder_timing",
    "chunk_start_offset",
    "align_to_frame",
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

# How far before a chunk's first sample the decode is told to seek. The seek is
# allowed to be imprecise - that is the point of it - and the filter graph makes
# the exact cut, so this only has to be more slack than a seek can plausibly
# miss by. Seconds of decoding nobody keeps, per chunk.
SEEK_LEAD = 5.0


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


# --- how long a WAVE can be ---------------------------------------------------
# RIFF states a chunk's length in 32 BITS, so a WAVE can describe at most 4 GiB
# of samples however large the file carrying it is - and exhale reads exactly
# the count the header declares and then stops, with a finished MP4 and a zero
# exit status. Over a pipe ffmpeg has no length to state and writes the field
# full of ones, which is that same 4 GiB rather than "read to the end", so the
# ceiling is reached the same way with or without a file in between.
#
# That is a real limit on this route and not a detail of it: 16-bit PCM at
# 44.1 kHz mono runs out after about thirteen and a half hours, and an audiobook
# read in one file is regularly longer. The caller asks BEFORE encoding, because
# what it prevents is not an error - it is hours of work producing a book with
# five sixths of it missing.

# The sample width the WAVE is written at - `pcm_s16le` in `wav_argv` below,
# which is what both this ceiling and that encoder call have to agree on.
SAMPLE_BYTES = 2

# The largest a 32-bit unsigned length can say.
WAVE_DATA_CEILING = 0xFFFFFFFF


def wave_seconds_ceiling(rate: int, channels: int) -> float:
    """The longest recording one WAVE can carry at this rate and channel count.

    The rate and the count are the ones the WAVE is WRITTEN at - the resampled
    rate from `input_sample_rate` and the channels after any downmix - not the
    source's own, which is why the caller settles both before asking.
    """
    per_second = max(1, rate) * max(1, channels) * SAMPLE_BYTES
    return WAVE_DATA_CEILING / per_second


def fits_in_one_wave(seconds: float, rate: int, channels: int) -> bool:
    """Whether a recording of this length can reach the encoder whole.

    A length of zero or less is a source nothing could measure, and it answers
    true: refusing a file because its duration could not be probed would turn an
    unreadable header into a refusal to encode, and the finished output is
    measured against the source either way.
    """
    if seconds <= 0:
        return True
    return seconds <= wave_seconds_ceiling(rate, channels)


# --- joining what was encoded apart ------------------------------------------
# A chunked encode has to be re-joined sample for sample, and what stands in the
# way is one frame: the encoder emits a PRIMING frame ahead of the audio, whose
# output the decoder is meant to discard. Each chunk's own MP4 says so in its
# edit list and each chunk therefore decodes to exactly its own length - but a
# joined file has ONE edit list, at the head, so every chunk after the first has
# its priming frame played as audio instead. That, and nothing else, is the seam.
#
# It cannot be cut out afterwards. The frame is not silence to be trimmed and it
# is not droppable either: USAC overlap-adds, so the frame after it decodes from
# it, and a join that drops the packet corrupts the frame that follows.
#
# What CAN be done is to leave it a hole to land in: cut each chunk to start one
# priming's worth LATE, and the frame the decoder plays fills exactly the gap the
# cut left. The join is then sample-exact, and the cost is that the priming's
# worth of audio at each boundary is the decoder's fade-in ramp rather than the
# source - which is why the boundaries are the silences the planner already finds.
#
# Both numbers are MEASURED from the encoder rather than written down here. They
# are an implementation detail of a binary this library does not ship, and a
# wrong constant would not fail: it would drift a long book by a frame per chunk.


def _silence_wave(rate: int, channels: int, seconds: int = 1) -> bytes:
    """One WAVE of silence, for asking the encoder about its own timing.

    Built here rather than decoded out of ffmpeg because the answer has to be
    about the encoder alone, and because a probe that spawns two processes to
    learn two integers is a probe that gets skipped.
    """
    channels = max(1, channels)
    data = b"\0" * (rate * channels * SAMPLE_BYTES * seconds)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, channels, rate,
                          rate * channels * SAMPLE_BYTES,
                          channels * SAMPLE_BYTES, SAMPLE_BYTES * 8)
    return header + b"data" + struct.pack("<I", len(data)) + data


# Answers already paid for, per (preset, rate, channels). One process at most per
# combination, and a run's files share very few of them.
_TIMING: dict = {}


def encoder_timing(preset: str, rate: int, channels: int) -> tuple[int, int]:
    """(priming samples, frame samples) for what this encoder writes here.

    Both come off one second of silence encoded at the settings the real chunks
    will use: the first packet's timestamp is the priming the container asks the
    decoder to discard, and its duration is the frame.

    (0, 0) is "could not be asked", which a caller reads as "do not chunk this" -
    the join arithmetic has no meaning without these two numbers, and guessing
    them would drift the very file that needed chunking most.
    """
    key = (preset, rate, channels)
    if key in _TIMING:
        return _TIMING[key]

    answer = (0, 0)
    directory = ""
    try:
        directory = tempfile.mkdtemp(prefix="xheaacTiming.")
        # A name that does not exist: exhale opens its output O_CREAT|O_EXCL.
        out = os.path.join(directory, "probe.m4a")
        done = subprocess.run(exhale_argv(preset, out),
                              input=_silence_wave(rate, channels),
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        if done.returncode == 0:
            answer = _first_packet(out)
    except OSError:
        answer = (0, 0)
    finally:
        if directory:
            _discard(directory)
    _TIMING[key] = answer
    return answer


def _first_packet(path: str) -> tuple[int, int]:
    """The first packet's timestamp and duration, in samples."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "packet=pts,duration", "-of", "compact=p=0",
             path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return 0, 0
    fields = {}
    line = done.stdout.decode("utf-8", "surrogateescape").split("\n")[0]
    # The compact writer ends a row with a separator, so the last piece of the
    # split is empty rather than a field.
    for field in [piece for piece in line.split("|") if piece]:
        key, _sep, value = field.partition("=")
        try:
            fields[key] = int(value)
        except ValueError:
            return 0, 0
    frame = fields.get("duration", 0)
    # A negative timestamp is the container saying "discard this much"; a
    # non-negative one is an encoder with nothing to discard.
    priming = max(0, -fields.get("pts", 0))
    return (priming, frame) if frame > 0 else (0, 0)


def _discard(directory: str) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


def align_to_frame(seconds: float, rate: int, frame: int) -> float:
    """<seconds> moved to the nearest whole frame.

    A boundary that falls mid-frame leaves the chunk before it a partial last
    frame, which the join writes out whole - a second frame of drift per seam, on
    top of the priming. Half a frame is about 23 ms, far inside the silences the
    boundaries sit in.
    """
    if frame <= 0 or rate <= 0:
        return seconds
    samples = round(seconds * rate / frame) * frame
    return samples / rate


def chunk_start_offset(priming: int, rate: int) -> float:
    """How long after its boundary a chunk starts, so the priming frame the
    decoder plays fills the gap instead of pushing everything after it along."""
    if rate <= 0:
        return 0.0
    return max(0, priming) / rate


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
    file, which is what makes this the chunk decoder as well as the whole-file
    one - and the range has to come out SAMPLE-exact, because the pieces are
    joined end to end and every sample one of them does not carry is a hole in
    the finished book.

    `-ss` alone does not give that. It seeks to a packet and hands over from
    wherever the decoder settles, which on an m4b lands late by a fixed amount
    per seek position - a thousand samples, reliably, for every chunk. So the
    seek is only ROUGH, deliberately short of the mark by `SEEK_LEAD`, and the
    cut itself is made in the filter graph where a timestamp means a sample.
    `-copyts` is what makes that possible: without it the decoded frames are
    renumbered from zero and the range would have nothing absolute to name.
    """
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    cut = []
    if start and duration:
        begin = max(0.0, float(start) - SEEK_LEAD)
        argv += ["-copyts", "-ss", "%.9f" % begin]
        cut = ["-af", "atrim=start=%.9f:end=%.9f,asetpts=PTS-STARTPTS"
               % (float(start), float(start) + float(duration))]
    elif start:
        argv += ["-ss", start]
    argv += ["-i", source, "-map", "0:a:0", "-map_metadata", "-1"] + cut
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
