"""The GPU AVIF encoder: whether this host has it, which images it can take,
and how a batch of them is handed to it.

av1gpu (github.com/maddog107/av1gpu) encodes AV1 still images on an NVIDIA
card's CUDA cores, several times faster than libaom on every CPU thread at the
same file size, and leaves the CPU nearly idle. It is not a replacement for the
ImageMagick call, only a faster road for the images it can carry:

- it writes 4:2:0 or 4:4:4 at 8 or 10 bits, and alpha - which it is not
  given here: its alpha quality is not calibrated against the CPU's, and an
  older av1gpu drops it;
- it reads JPEG on the GPU, PNG and WebP on the CPU (on
  :data:`DECODE_THREADS` threads), and nothing else;
- it cannot resize or crop.

So the caller decides per image, and everything it does not take - and
everything it was given and did not write - goes to ImageMagick as before.

It is reached in batches, never one image per call, and the batches are
sized in megapixels, not images. Measured on an RTX 5090 with 873 camera
JPEGs (12.3 gigapixels, 12-15 MP each), against 34.6 s for one call over all
of them:

    images a call   64    128   256   512
    wall, s         61.0  47.2  41.1  37.5

Every further call costs about 2 s - CUDA start-up, and the last batch of a
call draining with nothing behind it - and running two or three calls at once
did not hide it (40.8 s and 40.5 s at 256) while each added about 6 GB of card
memory. So the card is fed by ONE process at a time, in calls large enough that
2 s is a small share of each: :data:`CHUNK_MEGAPIXELS` is about a minute of
encoding at the 363 MP/s measured there. One call for the whole run would be
fastest still, but its outputs can only be trusted once it has exited, so a run
of thousands would print nothing until the end and a crash would cost it all;
a chunk prints its progress when it ends, and a crash costs that chunk, which
then goes to the CPU.

The same two rules hold for anything else that feeds the card - a command
converting many small folders at once, say: their images have to be gathered
into one caller's chunks, since a call per folder would pay the 2 s per folder,
and several at once would only share the card.

Batch mode names every output after its input's BASE name, in one flat folder,
so two `cover.jpg` in different folders would land on each other. Each input is
therefore handed over as a numbered symlink, and the number maps the output back
to the image it belongs to.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from medialib.lib import tooldeps

__all__ = [
    "ENCODER_VARIABLE",
    "DEFAULT_BINARY",
    "EXTENSIONS",
    "CHUNK_MEGAPIXELS",
    "MAX_PIXELS",
    "DECODE_THREADS_VARIABLE",
    "DECODE_THREADS",
    "chunks",
    "binary",
    "small_preset",
    "PROBE_FORMAT",
    "takes_probe",
    "encode_chunk",
]

# The setting naming the encoder, by name on PATH or by path. Set to nothing,
# every image is encoded on the CPU - which is also how the test suite keeps a
# host's real GPU out of its runs.
ENCODER_VARIABLE = "imageGpuEncoder"
DEFAULT_BINARY = "av1gpu"

# What it reads. JPEG is decoded on the GPU, the other two on the CPU before
# the encode.
EXTENSIONS = frozenset(("jpg", "jpeg", "png", "webp"))

CHUNK_MEGAPIXELS = 24000

# Above this av1gpu declines an image itself; asking first keeps such an image
# out of a chunk's budget.
MAX_PIXELS = 35_600_000

# The CPU threads av1gpu decodes PNG and WebP on, in place of its own 4, unless
# the environment already names a number. On 40 comic pages of 2035x2960 at
# 4:4:4 10-bit, turbo, on an RTX 5090 (av1gpu cbea9b8): 55-57 MP/s with 4,
# where the decode held the card up; 72-75 with 8, about what the same pages
# cost as JPEG; 71-77 with 12. Nothing for JPEG, which the card decodes.
DECODE_THREADS_VARIABLE = "GP_FALLBACK_WORKERS"
DECODE_THREADS = 8

def binary() -> str:
    """The encoder to run, or ``""`` when this host has none - or was told not
    to use one."""
    name = os.environ.get(ENCODER_VARIABLE, DEFAULT_BINARY)
    if not name:
        return ""
    name = os.path.expanduser(name)
    return name if tooldeps.tool_present(name) else ""


# av1gpu encodes images below 1 MP with a preset of their own, `base` by
# default, because the fast presets lose most there (+3.9 % against libaom
# speed 6 under 1 MP for `turbo`, by its own measurements). That is kept - but
# never as a FASTER preset than the one asked for, which would hand a small
# image less than the calibration promised.
SMALL_PRESET = "base"
_PRESET_ORDER = ("slow", "base", "fast", "faster", "turbo", "turbo2", "turbo3")


def small_preset(preset: str) -> str:
    """The preset for images below 1 MP in a call asking for <preset>."""
    if _PRESET_ORDER.index(preset) < _PRESET_ORDER.index(SMALL_PRESET):
        return preset
    return SMALL_PRESET


PROBE_FORMAT = "%A %z %w %h %[orientation]"

# The EXIF orientations that leave a picture as it is stored.
UPRIGHT = ("Undefined", "TopLeft")
# Those that turn it a quarter, so that it is displayed with width and height
# swapped.
QUARTER_TURNS = ("LeftTop", "RightTop", "RightBottom", "LeftBottom")


def takes_probe(text: str, max_width: int, max_height: int,
                jpeg: bool = True) -> int:
    """The pixel count of an image whose ``identify -ping -format
    PROBE_FORMAT`` answered <text>, when it can go to the GPU - and 0 when it
    cannot. It can when it has no alpha channel, up to 16 bits a sample, and
    is small enough that the CPU road would not have resized it and av1gpu will not
    decline it. An answer that cannot be read keeps it on the CPU, which can do
    everything the GPU can.

    The orientation is av1gpu's own shortfall rather than its format's: it
    writes a JPEG's EXIF orientation into the AVIF,
    as the CPU road does, but not a PNG's or a WebP's, which would come out
    displayed the way they are stored rather than the way they are meant. Such
    a file - not a <jpeg> - with any orientation but upright stays on the CPU.
    """
    words = text.split()
    if len(words) != 5:
        return 0
    alpha, depth, width, height, orientation = words
    if alpha not in ("False", "Undefined"):
        return 0
    if not jpeg and orientation not in UPRIGHT:
        return 0
    try:
        depth_bits, columns, rows = int(depth), int(width), int(height)
    except ValueError:
        return 0
    if depth_bits > 16 or columns > max_width or rows > max_height:
        return 0
    pixels = columns * rows
    return pixels if 0 < pixels <= MAX_PIXELS else 0


def chunks(sized, budget: int = CHUNK_MEGAPIXELS * 1_000_000):
    """``(item, pixels)`` pairs, in order, as lists of items of about <budget>
    pixels each - the last one whatever is left. An image larger than the whole
    budget is a chunk of its own rather than a reason to stop."""
    chunk: list = []
    filled = 0
    for item, pixels in sized:
        if chunk and filled + pixels > budget:
            yield chunk
            chunk, filled = [], 0
        chunk.append(item)
        filled += pixels
    if chunk:
        yield chunk


def encode_chunk(encoder: str, items, workdir: str, quality: int,
                 preset: str, chroma: str = "420",
                 depth: int = 10) -> tuple[list[int], int]:
    """Encode one chunk. <items> is a list of ``(source, destination)``, both
    absolute; <workdir> an empty directory for the call's own files.
    <quality> and <preset> are av1gpu's own - what a CPU -l and -s turn into
    is medialib/lib/encodercalibration.py's to say. <chroma> is "420" or
    "444", and <depth> 8 or 10 - 10 by default, which is what the CPU road
    writes.

    Returns the indices into <items> whose destination was written, and the
    encoder's exit status. A destination is only written once the encoder has
    exited by itself: one killed by a signal - a crash, or the run being
    stopped - may have been in the middle of writing any of its outputs, so
    none of them is used.

    Each output is moved into place under a temporary name first and renamed
    over the destination, so a run stopped mid-move cannot leave a truncated
    file that the next run would take for one already done.
    """
    inputs = os.path.join(workdir, "in")
    outputs = os.path.join(workdir, "out")
    os.makedirs(inputs)
    os.makedirs(outputs)
    staged = []
    for index, (source, _destination) in enumerate(items):
        name = "%06d.%s" % (index, source.rpartition(".")[2])
        os.symlink(source, os.path.join(inputs, name))
        staged.append(name)
    listing = os.path.join(workdir, "list")
    with open(listing, "w") as handle:
        for name in staged:
            handle.write(os.path.join(inputs, name) + "\n")

    # The process group's Ctrl+C reaches it as it reaches ImageMagick; stderr
    # names what it skipped and why, which the fallback does not need.
    environment = dict(os.environ)
    environment.setdefault(DECODE_THREADS_VARIABLE, str(DECODE_THREADS))
    status = subprocess.run(
        [encoder, "--batch", listing, "--outdir", outputs,
         "--quality", str(quality), "--preset", preset,
         "--small-preset", small_preset(preset),
         "--yuv", chroma, "--depth", str(depth)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=environment).returncode
    if status < 0:
        return [], status

    done = []
    for index, name in enumerate(staged):
        produced = os.path.join(outputs, name + ".avif")
        try:
            if os.path.getsize(produced) == 0:
                continue
        except OSError:
            continue
        destination = items[index][1]
        partial = destination + ".part"
        shutil.move(produced, partial)
        os.replace(partial, destination)
        done.append(index)
    return done, status
