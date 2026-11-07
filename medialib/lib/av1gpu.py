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
- it crops and shrinks on the card after the decode (since av1gpu 0.7.0), with
  ImageMagick's ``-crop`` and ``-resize 'WxH>'`` geometry and filter, so a page
  the CPU would have trimmed or resized is still the card's.

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
import re
import shutil
import subprocess

from medialib.lib import errorlog, runlog, safety, toolcapture, tooldeps

__all__ = [
    "ENCODER_VARIABLE",
    "DEFAULT_BINARY",
    "EXTENSIONS",
    "CHUNK_MEGAPIXELS",
    "MAX_PIXELS",
    "transforms",
    "trims",
    "fitted_size",
    "max_size",
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

# The largest picture handed to the card, before and after its crop and
# resize: av1gpu is asked to take up to this much (--max-mp, --max-input-mp)
# rather than its own 35.6 MP, the most AV1 level 6.3 allows, so that its limit
# does not decide which road a scan takes. Above 35.6 MP the stream says level
# 31, which dav1d and libavif decode. Asking first keeps a larger image out of
# a chunk's budget.
MAX_PIXELS = 100_000_000
MAX_MEGAPIXELS = "100"

# What an av1gpu that crops and resizes lists in its --help; one older than
# that (0.7.0) is not used at all.
_TRANSFORM_OPTION = "--max-input-mp"
# What an av1gpu that trims on the card (0.11.0 or newer) says in its --help:
# --trim with the conditions convert-images puts on a trim, of which this is
# the last added.
_TRIM_OPTION = "--trim-lightness"
_help: dict[str, bytes] = {}

# The CPU threads av1gpu decodes PNG and WebP on, in place of its own 4, unless
# the environment already names a number. On 40 comic pages of 2035x2960 at
# 4:4:4 10-bit, turbo, on an RTX 5090 (av1gpu cbea9b8): 55-57 MP/s with 4,
# where the decode held the card up; 72-75 with 8, about what the same pages
# cost as JPEG; 71-77 with 12. Nothing for JPEG, which the card decodes.
DECODE_THREADS_VARIABLE = "GP_FALLBACK_WORKERS"
DECODE_THREADS = 8

def binary(log=None) -> str:
    """The encoder to run, or ``""`` when this host has none - or was told not
    to use one, or has one too old to crop and resize, which <log> is told."""
    name = os.environ.get(ENCODER_VARIABLE, DEFAULT_BINARY)
    if not name:
        return ""
    name = os.path.expanduser(name)
    if not tooldeps.tool_present(name):
        return ""
    if not transforms(name):
        if log:
            log("WARNING: %s predates av1gpu 0.7.0 and cannot crop or resize "
                "- encoding on the CPU; update it to use the card." % name)
        return ""
    return name


def _help_text(encoder: str) -> bytes:
    """<encoder>'s --help, asked once."""
    if encoder not in _help:
        try:
            answer = subprocess.run([encoder, "--help"], capture_output=True,
                                    timeout=30)
            _help[encoder] = answer.stdout + answer.stderr
        except (OSError, subprocess.SubprocessError):
            _help[encoder] = b""
    return _help[encoder]


def transforms(encoder: str) -> bool:
    """Whether <encoder> crops and resizes, from its --help; asked once."""
    return _TRANSFORM_OPTION.encode() in _help_text(encoder)


def trims(encoder: str) -> bool:
    """Whether <encoder> decides a trim on the card - finds the margin, and
    keeps or skips the page by the conditions it is given - from its --help.
    One that cannot is still given every page it takes with the crop measured
    on the CPU."""
    return _TRIM_OPTION.encode() in _help_text(encoder)


# av1gpu encodes images below 1 MP with a preset of their own, `base` by
# default, because the fast presets lose most there (+3.9 % against libaom
# speed 6 under 1 MP for `turbo`, by its own measurements). That is kept - but
# never as a FASTER preset than the one asked for, which would hand a small
# image less than the calibration promised.
SMALL_PRESET = "base"

# 4x4 AV1 tiles for images of 2 MP and more, at every preset - turbo's own
# rule is 2x2, the slower presets' as few as AV1 allows; only turbo was measured:
# `--tiles MIN_MP,COLS_LOG2,ROWS_LOG2`. Within a tile the reconstruction is a
# serial wavefront over its superblocks, and on a big card 2x2 leaves it mostly
# idle - comic pages, whose line art makes every superblock heavy, the most.
# Measured on an RTX 5090 at turbo, 4:4:4 10-bit: 60 comic pages of 6 MP went
# from 89-102 to 148-151 MP/s and photos from 152 to 246 MP/s, for +0.3-0.5 %
# bytes and SSIMULACRA2 unchanged within 0.05. The calibration was measured with
# 2x2 tiles, but its crops stay below 2 MP and so untiled either way, and half
# a percent at an equal score is within its noise; it was not measured again.
# Bigger GPU batches (GP_ENCODE_MP=96) add 16 % on 2x2 tiles, nothing on 4x4.
# av1gpu's --preset resets the tiling, so this has to come after it.
#
# The value is tuned for a 170-SM card (the RTX 5090); a smaller one has fewer
# chains to fill. Should av1gpu come to pick its tiles by GPU itself, this goes.
TILES = "2,2,2"
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


def takes_probe(text: str, jpeg: bool = True) -> tuple[int, int, str] | None:
    """``(width, height, orientation)`` of an image whose ``identify -ping
    -format PROBE_FORMAT`` answered <text>, when it can go to the GPU - and None
    when it cannot. It can when it has no alpha channel, up to 16 bits a sample,
    and no more than :data:`MAX_PIXELS`. An answer that cannot be read keeps it
    on the CPU, which can do everything the GPU can.

    The orientation is av1gpu's own shortfall rather than its format's: it
    writes a JPEG's EXIF orientation into the AVIF,
    as the CPU road does, but not a PNG's or a WebP's, which would come out
    displayed the way they are stored rather than the way they are meant. Such
    a file - not a <jpeg> - with any orientation but upright stays on the CPU.
    """
    words = text.split()
    if len(words) != 5:
        return None
    alpha, depth, width, height, orientation = words
    if alpha not in ("False", "Undefined"):
        return None
    if not jpeg and orientation not in UPRIGHT:
        return None
    try:
        depth_bits, columns, rows = int(depth), int(width), int(height)
    except ValueError:
        return None
    if depth_bits > 16 or not 0 < columns * rows <= MAX_PIXELS:
        return None
    return columns, rows, orientation


def fitted_size(width: int, height: int, max_height: int) -> tuple[int, int]:
    """<width> x <height> shrunk to at most <max_height> rows, aspect kept,
    never enlarged: ImageMagick's ``-resize 'x<max_height>>'``, which av1gpu
    matches - so the card's output is the size the CPU's would be."""
    if not max_height or height <= max_height:
        return width, height
    return max(1, round(width * max_height / height)), max_height


def max_size(max_height: int, orientation: str) -> str:
    """av1gpu's max-size for a cap of <max_height> stored rows. Its box is the
    DISPLAYED size, ImageMagick's -resize the stored one, so for a picture its
    EXIF orientation turns a quarter the cap is put on the displayed width."""
    if orientation in QUARTER_TURNS:
        return "%dx" % max_height
    return "x%d" % max_height


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


# What av1gpu says of an image --skip-blank kept from the encode, and of the
# trims in the whole call.
_BLANK = re.compile(r"^\[av1gpu\] skipped (.+): blank \(")
_TRIMMED = re.compile(r"^\[av1gpu\] trim: (\d+) images trimmed")


class _Listener(toolcapture.ToolOutput):
    """The encoder's output kept as any tool's is, and read on the way for
    the two lines above, which may fall where a long call's capture keeps
    none."""

    def __init__(self) -> None:
        super().__init__()
        self.blank: set[str] = set()
        self.trimmed = 0

    def _add(self, line: str) -> None:
        super()._add(line)
        found = _BLANK.match(line)
        if found:
            self.blank.add(found.group(1))
            return
        found = _TRIMMED.match(line)
        if found:
            self.trimmed += int(found.group(1))


def encode_chunk(encoder: str, items, workdir: str, quality: int,
                 preset: str, chroma: str = "420",
                 depth: int = 10, extra=(),
                 report: dict | None = None) -> tuple[list[int], int]:
    """Encode one chunk. <items> is a list of ``(source, destination)``, both
    absolute, or ``(source, destination, crop, max_size)``: av1gpu's own
    ``WxH+X+Y`` and ``WxH`` / ``xH``, either one "" for none, done on the card
    after the decode; <workdir> an empty directory for the call's own files.
    <quality> and <preset> are av1gpu's own - what a CPU -l and -s turn into
    is medialib/lib/encodercalibration.py's to say. <chroma> is "420" or
    "444", and <depth> 8 or 10 - 10 by default, which is what the CPU road
    writes.

    <extra> are further av1gpu options for every image of the call - the trim
    and its conditions. An image the trim found blank is not written and not a
    failure: its index goes into ``report["blank"]``, and the number of images
    the card trimmed into ``report["trimmed"]``, when there is a <report>.

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
    lines = []
    for index, (source, _destination, *transform) in enumerate(items):
        name = "%06d.%s" % (index, source.rpartition(".")[2])
        os.symlink(source, os.path.join(inputs, name))
        staged.append(name)
        crop, fit = transform if transform else ("", "")
        lines.append("\t".join([os.path.join(inputs, name)]
                               + (["crop=" + crop] if crop else [])
                               + (["max-size=" + fit] if fit else [])))
    listing = os.path.join(workdir, "list")
    with open(listing, "w") as handle:
        for line in lines:
            handle.write(line + "\n")

    # The process group's Ctrl+C reaches it as it reaches ImageMagick. What it
    # prints - stderr names what it skipped and why - is kept whole, both
    # streams in the order it wrote them: the fallback does not need it, but
    # the error log does, for an image it left unwritten.
    environment = dict(os.environ)
    environment.setdefault(DECODE_THREADS_VARIABLE, str(DECODE_THREADS))
    argv = [encoder, "--batch", listing, "--outdir", outputs,
            "--quality", str(quality), "--preset", preset,
            "--small-preset", small_preset(preset), "--tiles", TILES,
            "--yuv", chroma, "--depth", str(depth),
            "--max-mp", MAX_MEGAPIXELS, "--max-input-mp", MAX_MEGAPIXELS,
            *extra]
    said = _Listener()
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, env=environment)
    assert process.stdout is not None
    said.drain(process.stdout)
    status = process.wait()
    if status < 0:
        _report_unwritten(argv, status, said, items, set())
        return [], status
    blank = {index for index, name in enumerate(staged)
             if os.path.join(inputs, name) in said.blank}
    if report is not None:
        report["blank"] = sorted(blank)
        report["trimmed"] = said.trimmed

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
    _report_unwritten(argv, status, said, items, set(done) | blank)
    return done, status


def _report_unwritten(argv: list, status: int, said, items,
                      done: set) -> None:
    """Warn about the images a call left unwritten, and keep them - each one
    named by the file it was made from, the comic a page was unpacked from -
    with everything the encoder said. Nothing for a call that wrote them all,
    or one the run's own interrupt stopped."""
    missing = [items[index][0] for index in range(len(items))
               if index not in done]
    if not missing or safety.abort_requested():
        return
    run = toolcapture.finished(argv, status, said, tool="av1gpu")
    ended = run.failure or "exited 0"
    origins = list(dict.fromkeys(
        os.path.basename(errorlog.origin_of(source) or source)
        for source in missing))
    message = ("av1gpu left %d of %d image(s) unwritten (%s), of %s - "
               "they go to the CPU" % (len(missing), len(items), ended,
                                       ", ".join(origins[:5])
                                       + (" and %d more" % (len(origins) - 5)
                                          if len(origins) > 5 else "")))
    path = errorlog.record(
        "tool-skipped", message, files=missing, tool_argv=argv,
        tool_status=ended, tool_output=said.lines(), tool_lines=said.total,
        causes=toolcapture.causes(run))
    runlog.warn(message)
    if path and runlog.verbosity() >= runlog.NORMAL:
        runlog.error_continued("         what it said is kept in %s" % path)
