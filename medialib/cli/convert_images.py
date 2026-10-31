"""convert-images: a tree of images converted to AVIF, WebP or JPEG XL - or,
with -r, back to JPEG - into a mirrored output tree.

The interesting half is -c, the crop. What a `-trim` took off, measured as a
percentage of the original edge, is what decides what the trim MEANT: below the
minimum it found no margin worth removing, above the maximum it is cutting into
the picture rather than removing a border, and past the blank threshold there was
nothing on the page at all.

The other half is what this command declines to do. An image whose file size is
already below what its format, size and content need is STARVED: whatever is left
of it is all there is, and encoding it again spends a second generation of loss
on a picture that had none to spare. Those are left alone unless -a says
otherwise. The model behind the verdict is medialib/lib/imagebitrate.py.

Every conversion is one ImageMagick call whose output is checked and whose stderr
is silenced, so the tools are asked for up front: a missing ImageMagick would
otherwise read as a run in which no image could be converted.

Except where a GPU can do it: a plain AVIF run hands every image av1gpu can
take to the card first, in batches, and what the card did not write goes to
ImageMagick after it. medialib/lib/av1gpu.py says which images those are.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

from medialib import commands
from medialib.lib import (
    adequacy,
    av1gpu,
    clioptions,
    encodercalibration,
    enums,
    imagebitrate,
    imagemagick,
    ramscratch,
    runlog,
    safety,
    settings,
    tooldeps,
    workerpool,
)
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>
Options:"""

# -e writes the first of enums.IMAGE_CODECS unless it is told another one. Which
# format that is belongs to the list rather than to this line: the list is in
# preference order, so the default follows a change to it.
DEFAULT_FORMAT = enums.IMAGE_CODECS[0]

# The top of the -s range, and why it is checked rather than passed through.
#
# The AVIF encoder REFUSES a speed above this instead of clamping it:
# `heic:speed=10` fails with "Invalid parameter value" and leaves a ZERO-BYTE
# file behind. With stderr silenced that is a whole run of empty images and no
# word about why, so the number is settled before the first conversion.
# Measured against ImageMagick 7.1.2-30 (libheif 1.23.1), where 0-9 all encode
# and 10 and above do not.
MAX_SPEED = 9
DEFAULT_SPEED = 5

# -u, the AVIF chroma subsampling. libheif's own default is 4:2:0, which stores
# colour at half the width and half the height of the picture: invisible on a
# photograph, a coloured fringe on the thin lines and lettering of a comic page.
# 4:4:4 is AV1's High profile - every software decoder reads it, some hardware
# ones do not. WebP has no lossy 4:4:4 to ask for, and JPEG XL does not
# subsample colour in the first place, so there is nothing for -u to change in
# either and it is refused rather than accepted and ignored.
CHROMAS = ("420", "444")
DEFAULT_CHROMA = "420"
CHROMA_DEFINE = "heic:chroma"
CHROMA_FORMAT = "avif"


def _spoken_list(words) -> str:
    """The formats the way a sentence reads them - "avif, webp or jxl".

    The page below and the check under it are both generated from
    :data:`enums.IMAGE_CODECS`, so a codec added there is offered and accepted
    together. Written out by hand, the two drift and the page ends up
    advertising a format the check refuses.
    """
    words = list(words)
    if len(words) < 2:
        return "".join(words)
    return "%s or %s" % (", ".join(words[:-1]), words[-1])


OPT_SPEC = """
h |  | Print this help page.
c |  | crop excessive whitespace
a |  | convert every image, including one whose file size says it is
                    already starved. Without it such an image is left alone: see
                    below.
r |  | reverse: convert the -e format back to jpeg
e | <format> | output encoding: {formats}, default {default}
u | <chroma> | AVIF chroma: {chromas}. 444 keeps colour at full resolution,
                    420 at a quarter of it; 444 gives sharper coloured lines and
                    larger files. Default {chroma}. AVIF only.
j | <jobs> | Run up to <jobs> encoder processes in parallel.
                    Only needed when RAM is short, otherwise a good guess is made.
l | <quality> | quality level of the output images
s | <speed> | av1 speed preset {speeds}, lower is slower, default {speed}
m | <maxRes> | maximum resolution (height) to keep
f | <fuzz> | when trimming, how many percent color difference gets still trimmed
k |  | calibrate: measure what the GPU encoder has to be asked for to
                    match the CPU, on crops of the images in <inputDir>, and
                    save it. Works in <outputDir>; converts nothing.
""".format(formats=_spoken_list(enums.IMAGE_CODECS), default=DEFAULT_FORMAT,
           speeds="0-%d" % MAX_SPEED, speed=DEFAULT_SPEED,
           chromas=" or ".join(CHROMAS), chroma=DEFAULT_CHROMA)

# The format has to be one this command can write, or a typo would only surface
# as a per-image failure deep into the run; the speed preset has to be a number,
# because each format's own effort setting is derived from it. Both are settled
# here. The choices are joined with an ESCAPED pipe, because the spec is a wire
# format whose fields are separated by a plain one: `\|` is how a field says "a
# literal pipe of my own". Written with a bare pipe, the kind ends at the first
# codec and every other one is refused.
OPT_CHECKS = """
e | enum:{formats} | output encoding
s | int:0:{top} | speed preset
u | enum:{chromas} | chroma
""".format(formats="\\|".join(enums.IMAGE_CODECS), top=MAX_SPEED,
           chromas="\\|".join(CHROMAS))

OPT_VARS = ("c:crop a:alwaysConvert r:reverse e:outputFormat u:chroma "
            "j:jobs l:quality s:speedPreset m:maxRes f:fuzz k:calibrate")
OPT_COLUMN = 20
OPT_LONG = ("h:help c:crop a:always r:reverse e:encoding u:chroma j:jobs "
            "l:quality s:speed m:max-resolution f:fuzz k:calibrate")

# One conversion spans about this many threads, so the pool is the cores divided
# by it.
THREADS_PER_CONVERSION = 4.0
# Except where AVIF's speed moves it, measured on a 2560x3473 scan with
# ImageMagick 7.1.2 and libheif's aom: the fast end spends less of each page in
# the encoder's threaded search, so each conversion holds fewer cores. Chroma
# made no difference worth a column.
AVIF_THREADS_BY_SPEED = {9: 2.5, 8: 3.5, 7: 3.5, 5: 4.5}


def threads_per_conversion(image_format: str, speed: int) -> float:
    """How many cores one conversion keeps busy at this -s."""
    if image_format != "avif":
        return THREADS_PER_CONVERSION
    return AVIF_THREADS_BY_SPEED.get(speed, THREADS_PER_CONVERSION)

# Set by a run that encodes the images of many of these runs together -
# convert-comics, one of these per book: the folder this run writes the
# manifest of its pages into. Under it this run only sorts the pages - which go
# to the card, which to the CPU, and with what call - and encodes none of them.
LANE_VARIABLE = "imageGpuLane"
LANE_MANIFEST = "manifest"

# The width half of every -resize geometry. Only the height is ever capped (-m),
# so the width is given a value no page can reach: "no limit" and "cap the height
# at N" then take the same code path instead of needing two geometries.
UNBOUNDED_EDGE = 10000


class Format(NamedTuple):
    """One output format: the bit depth it is written at, the define its
    encoder takes its effort under, and the two ends of that encoder's own
    scale - the value meaning "slowest" and the value meaning "fastest"."""

    depth: int
    effort: str
    slowest: int
    fastest: int

    def level(self, speed: int) -> int:
        """<speed> from the -s scale, as this encoder's own effort number.

        Linear between the two ends, so -s 0 is exactly `slowest` and -s
        MAX_SPEED exactly `fastest` whichever direction the encoder counts in.
        The step is computed on the DISTANCE and then added or subtracted,
        because a negative span through floor division rounds the wrong way -
        `5 * -6 // 9` is -4, not -3, which would put WebP a method out.
        """
        step = speed * abs(self.fastest - self.slowest) // MAX_SPEED
        if self.fastest >= self.slowest:
            return self.slowest + step
        return self.slowest - step


# How each of enums.IMAGE_CODECS is asked for, which is the whole of what
# differs between them.
#
# AVIF and JPEG XL are written 10-bit even from 8-bit sources: at these quality
# levels the extra headroom costs almost nothing and keeps flat gradients from
# banding. WebP has no more than 8 bits to be written at. The JPEG XL header
# records 16 rather than 10: ImageMagick's JXL writer only offers integer
# samples of 8 or 16 bits, so it stores any depth above 8 as 16.
#
# The effort setting is where they disagree most, in name AND in direction.
# AVIF counts a SPEED, so its slowest end is 0; WebP's method and JPEG XL's
# effort both count effort, so theirs are 6 and 9. -s stays the single knob -
# the av1 speed of its own name - and each encoder's scale is walked from its
# own slow end to its own fast one, so turning -s down asks for a slower encode
# in every format rather than in one of them.
#
# Every bound below was measured against ImageMagick 7.1.2-30, not read off a
# manual page: webp:method refuses 7, and jxl:effort is silently CLAMPED
# outside 1-9 rather than refused, which is the worse of the two failures
# because nothing says it happened.
FORMATS = {
    "avif": Format(depth=10, effort="heic:speed", slowest=0, fastest=9),
    "webp": Format(depth=8, effort="webp:method", slowest=6, fastest=0),
    "jxl": Format(depth=10, effort="jxl:effort", slowest=9, fastest=1),
}

# The same perceived quality needs a higher number in JPEG than in AVIF.
JPEG_QUALITY_BONUS = 25

# What a trim took off, in percent of the original edge, and what each threshold
# means about it.
MIN_TRIM_PERCENT = 1
MAX_TRIM_PERCENT = 20
BLANK_TRIM_PERCENT = 99
LIGHTNESS_THRESHOLD = 40

# The stems a disambiguated output has to look out for: two sources in one folder
# sharing a stem would otherwise map to the same converted name. Every extension
# this command reads or writes counts, in either case - the tree is lower-cased
# later in the run, not before this is computed - so a format added above is
# covered here without anybody having to add it twice.
_SAME_STEM = frozenset(
    spelling
    for extension in enums.IMAGE_EXTENSIONS + enums.IMAGE_CODECS
    for spelling in (extension, extension.upper()))


def spec(program: str) -> clioptions.Spec:
    return clioptions.with_verbosity(clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        checks=OPT_CHECKS,
        column=OPT_COLUMN,
    ))


def disambiguated_output(relative: str, new_extension: str, input_dir: str,
                         output_dir: str) -> str:
    """Where this image's conversion goes.

    Normally the mirrored path with the new extension; but when another source in
    the same folder shares the stem - a.jpg and a.png would both become a.avif -
    the source extension is folded into the name so the two cannot clobber one
    another. Deterministic, so each worker can compute it alone, and stable
    across re-runs so the resume check keeps working.
    """
    relative_dir, _, file_name = relative.rpartition("/")
    stem, _, extension = file_name.rpartition(".")
    if not stem:                       # a name with no dot at all
        stem, extension = file_name, ""
    prefix = relative_dir + "/" if relative_dir else ""

    same_stem = 0
    directory = os.path.join(input_dir, relative_dir) if relative_dir \
        else input_dir
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith(stem + "."):
                    continue
                if name.rpartition(".")[2] in _SAME_STEM:
                    same_stem += 1
    except OSError:
        pass

    if same_stem > 1:
        return os.path.join(output_dir,
                            "%s%s-%s.%s" % (prefix, stem, extension,
                                            new_extension))
    return os.path.join(output_dir, "%s%s.%s" % (prefix, stem, new_extension))


def _parse_size(text: str) -> tuple[int, int] | None:
    """``identify -format "%w %h"``'s answer as (width, height), or None when
    it is not one."""
    words = text.split()
    if len(words) != 2:
        return None
    try:
        return int(words[0]), int(words[1])
    except ValueError:
        return None


def _parse_box(text: str) -> tuple[int, int, int, int] | None:
    """``identify -format %@``'s trim box, ``WxH+X+Y``, as (width, height,
    left, top), or None when it is not one. A blank page's box is empty, 0x0,
    which is what makes the trim's verdict "blank"."""
    size, _, offsets = text.strip().partition("+")
    try:
        width, height = (int(part) for part in size.split("x"))
        left, top = (int(part) for part in offsets.split("+"))
    except ValueError:
        return None
    return width, height, left, top


def _parse_corner(text: str) -> tuple[str, int] | None:
    """The corner probe's ``<colour> <lightness>`` as (colour, lightness), or
    None when it is not that. The lightness is truncated at the dot: 39.9 is 39
    and not 40."""
    parts = text.strip().split(" ")
    if len(parts) < 2:
        return None
    try:
        return parts[0], int(parts[1].split(".")[0])
    except ValueError:
        return None


def trim_verdict(width: int, height: int, cut: tuple[int, int] | None,
                 corner: Callable[[], tuple[str, int] | None]
                 ) -> tuple[str, str, int]:
    """What a -c trim meant, from how much it took off a <width> x <height>
    page: ``(verdict, colour, border)``, the verdict being "blank", "trimmed"
    or "converted". <cut> is the trimmed size, None when it could not be
    measured.

    Past BLANK_TRIM_PERCENT in either direction there was nothing on the page.
    Between MIN_ and MAX_TRIM_PERCENT in both, the trim took a margin - if the
    corner is light enough to be one. <corner> is only asked then, because it
    costs a call of its own; what it answers is the colour, and the border is
    the thin margin given back so the content does not end flush with the edge.
    Anything else is converted whole.
    """
    if cut is None:
        return "converted", "", 0
    width_diff = width - cut[0]
    height_diff = height - cut[1]
    min_w = width * MIN_TRIM_PERCENT // 100
    min_h = height * MIN_TRIM_PERCENT // 100
    if (width_diff > width * BLANK_TRIM_PERCENT // 100
            or height_diff > height * BLANK_TRIM_PERCENT // 100):
        return "blank", "", 0
    if (min_w <= width_diff <= width * MAX_TRIM_PERCENT // 100
            and min_h <= height_diff <= height * MAX_TRIM_PERCENT // 100):
        found = corner()
        if found is not None and found[1] >= LIGHTNESS_THRESHOLD:
            return "trimmed", found[0], min(min_w, min_h)
    return "converted", "", 0


def encode_arguments(options, source, out, extra=()):
    """The conversion, in whichever format <options> asks for.

    <extra> goes between the source and the resize, which is where an
    operation on the loaded image has to sit.

    -depth is the one setting that must come AFTER the source. Placed before
    it, it is a read setting: it tells the decoder how to read the source
    and the image keeps the source's own depth. The encoder then writes
    whatever the source had. An 8-bit JPEG came out as 8-bit AVIF, and a
    16-bit PNG came out as 12-bit AVIF, which is profile 2 and which many
    decoders cannot play. Placed just before the output, it sets the depth
    of the image being written, whatever the source was.
    """
    chosen = FORMATS[options["format"]]
    # Named only when it is not the encoder's own default, so a run that
    # does not ask is the call it always was.
    chroma = (["-define", "%s=%s" % (CHROMA_DEFINE, options["chroma"])]
              if options["chroma"] != DEFAULT_CHROMA else [])
    return ["-format", options["format"],
            "-quality", str(options["quality"]),
            "-define", "%s=%d" % (chosen.effort,
                                  options["effortLevel"]),
            *chroma,
            source, *extra,
            "-resize", options["maxResCommand"],
            "-depth", str(chosen.depth), out]


class Run:
    """One conversion run: the settled options, and the counters its workers
    share through files because they are separate processes."""

    def __init__(self, input_dir, output_dir, counter_dir, options, total):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.counter_dir = counter_dir
        self.options = options
        self.total = total
        # When the CPU's pool began, which is when the GPU's chunks ended.
        self.cpu_started = 0.0

    # --- the counters and the one progress line ---------------------------
    def record(self, category: str, label: str, path: str,
               stream=None, pixels: int = 0, road: str = "cpu") -> None:
        """Bump the global counter and this category's, then print one clean
        line. The lock serialises both across the parallel workers - or, without
        flock, takes nothing and the line comes out unnumbered. Under -q only
        the error line is printed; the counting goes on regardless.

        <pixels> is what an encode was given, added to <road>'s total for the
        closing report's speed: "cpu" or "gpu"."""
        out = sys.stderr if stream is None else stream
        with open(os.path.join(self.counter_dir, "lock"), "w") as handle, \
                runlog.take_lock(handle):
                current = self._bump("current")
                self._bump(category)
                if category in ("converted", "trimmed"):
                    self._bump(road + "Images")
                    self._bump(road + "Pixels", pixels)
                line = "%s%s: %s\n" % (
                    runlog.counted_prefix(current, self.total), label, path)
                if category == "notFound":
                    out.write(line)
                else:
                    runlog.say(line, out)

    def _bump(self, name: str, amount: int = 1) -> int:
        path = os.path.join(self.counter_dir, name)
        try:
            with open(path) as handle:
                value = int(handle.read() or "0")
        except (OSError, ValueError):
            value = 0
        value += amount
        with open(path, "w") as handle:
            handle.write(str(value))
        return value

    def counter(self, name: str) -> int:
        try:
            with open(os.path.join(self.counter_dir, name)) as handle:
                return int(handle.read() or "0")
        except (OSError, ValueError):
            return 0

    # --- the conversions --------------------------------------------------
    def _convert(self, arguments) -> int:
        return subprocess.run(imagemagick.convert_argv(arguments),
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode

    def _identify(self, arguments) -> str:
        proc = subprocess.run(imagemagick.identify_argv(arguments),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", "replace").strip()

    def _pixels(self, source: str) -> int:
        """The picture's pixel count, from its header, or 0 when it cannot be
        read - which leaves the image out of the speed and nothing else."""
        size = _parse_size(self._identify(["-ping", "-format", "%w %h", source]))
        return size[0] * size[1] if size else 0

    def _encode_arguments(self, source, out, extra=()):
        return encode_arguments(self.options, source, out, extra)

    def crop_convert(self, relative: str) -> None:
        """The -c path: trim, then decide from HOW MUCH came off what the trim
        meant."""
        out = disambiguated_output(relative, self.options["format"],
                                   self.input_dir, self.output_dir)
        source = relative
        dimensions = _parse_size(self._identify(["-ping", "-format", "%w %h",
                                                 source]))
        if dimensions is None:
            self.record("converted", "CONV", relative)
            self._convert(self._encode_arguments(source, out))
            return
        width, height = dimensions
        pixels = width * height
        if self.options.get("gpuLane"):
            self._crop_for_lane(relative, out, width, height)
            return

        trimmed = os.path.join(self.counter_dir, "trimmed_%d.miff" % os.getpid())
        self._convert([source, "-fuzz", self.options["fuzzCommand"], "-trim",
                       "miff:" + trimmed])
        cut = _parse_size(self._identify(["-format", "%w %h",
                                          "miff:" + trimmed]))
        try:
            verdict, colour, border = trim_verdict(
                width, height, cut, lambda: self._identify_corner(source))
            if verdict == "blank":
                self.record("blank", "SKIP (blank)", relative,
                            stream=sys.stderr)
                return
            if verdict == "trimmed":
                self.record("trimmed", "TRIM", relative, pixels=pixels)
                self._convert(self._encode_arguments(
                    "miff:" + trimmed, out,
                    ["-bordercolor", colour, "-border", str(border)]))
                return
            if self._to_lane(relative, out):
                return
            self.record("converted", "CONV", relative, pixels=pixels)
            self._convert(self._encode_arguments(source, out))
        finally:
            try:
                os.remove(trimmed)
            except OSError:
                pass

    def _crop_for_lane(self, relative: str, out: str, width: int,
                       height: int) -> None:
        """The -c path under a GPU lane: each page sorted, none encoded here.

        The trim is measured as a box rather than written out. A JPEG it cuts
        is cut by jpegtran: on the JPEG's own coefficients, so the page the
        card is given is the same pixels as the source, still a JPEG the card
        decodes itself. Two things differ from the CPU's trim. The margin
        given back is the page's own rather than a border of the corner's
        colour, and the left and top edges can only move in whole blocks of 8
        or 16 pixels, so up to that much more of the margin stays.

        A PNG or WebP is cut by ImageMagick, exactly as the CPU's trim cuts
        it, into a PNG at zlib's fastest level: a decode and a quick write,
        a small share of what encoding it on the CPU would cost. Only one the
        card would take uncut - its alpha, depth and orientation - is cut.

        What the card cannot take - taller than -m even cut, a format av1gpu
        does not read, a JPEG without jpegtran - is listed for the lane's CPU
        encoders, with the call that trims and encodes it.
        """
        pixels = width * height
        box = _parse_box(self._identify(
            ["-fuzz", self.options["fuzzCommand"], "-format", "%@", relative]))
        verdict, colour, border = trim_verdict(
            width, height, box[:2] if box else None,
            lambda: self._identify_corner(relative))
        if verdict == "blank":
            self.record("blank", "SKIP (blank)", relative, stream=sys.stderr)
            return
        if verdict == "converted":
            if not self._to_lane(relative, out):
                self._cpu_to_lane(relative, out, (), "converted", "CONV",
                                  pixels)
            return
        assert box is not None
        trim = ["-crop", "%dx%d+%d+%d" % box, "+repage",
                "-bordercolor", colour, "-border", str(border)]
        if (self.options.get("losslessCrop")
                and relative.endswith((".jpg", ".jpeg"))):
            cut_w, cut_h, left, top = box
            left, top = max(0, left - border), max(0, top - border)
            right = min(width, box[2] + cut_w + border)
            bottom = min(height, box[3] + cut_h + border)
            geometry = "%dx%d+%d+%d" % (right - left, bottom - top, left, top)
            descriptor, cropped = tempfile.mkstemp(
                suffix=".jpg", dir=self.options["gpuLane"])
            os.close(descriptor)
            done = subprocess.run(
                ["jpegtran", "-copy", "all", "-crop", geometry,
                 "-outfile", cropped, relative],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if done.returncode == 0 and self._to_lane(
                    relative, out, cropped, "trimmed", "TRIM (GPU)"):
                return
            os.remove(cropped)
        elif (relative.endswith((".png", ".webp"))
              and av1gpu.takes_probe(
                  self._identify(["-ping", "-format", av1gpu.PROBE_FORMAT,
                                  relative]),
                  UNBOUNDED_EDGE, UNBOUNDED_EDGE, jpeg=False)):
            descriptor, cropped = tempfile.mkstemp(
                suffix=".png", dir=self.options["gpuLane"])
            os.close(descriptor)
            done = subprocess.run(
                imagemagick.convert_argv(
                    [relative, *trim, "-define", "png:compression-level=1",
                     cropped]),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if done.returncode == 0 and self._to_lane(
                    relative, out, cropped, "trimmed", "TRIM (GPU)"):
                return
            os.remove(cropped)
        self._cpu_to_lane(relative, out, trim, "trimmed", "TRIM", pixels)

    def _cpu_to_lane(self, relative: str, out: str, extra, category: str,
                     label: str, pixels: int) -> None:
        """List a page for the lane's CPU encoders, with the ImageMagick call
        that encodes it - <extra> being the trim, when there is one."""
        out = os.path.abspath(out)
        self._list({"output": out, "pixels": pixels,
                    "cpu": imagemagick.convert_argv(self._encode_arguments(
                        os.path.abspath(relative), out, extra))})
        self.record(category, label, relative, pixels=pixels)

    def _list(self, entry: dict) -> None:
        """One line of the lane's manifest."""
        lane = self.options["gpuLane"]
        with open(os.path.join(lane, LANE_MANIFEST + ".lock"), "w") as handle, \
                runlog.take_lock(handle), \
                open(os.path.join(lane, LANE_MANIFEST), "a") as manifest:
            manifest.write(json.dumps(entry) + "\n")

    def _identify_corner(self, source: str):
        """The corner pixel's colour and lightness, in one call - what decides
        whether a margin is light enough to be a margin."""
        answer = subprocess.run(
            imagemagick.convert_argv(
                [source, "-crop", "1x1+0+0", "-colorspace", "HSL",
                 "-format", "%[pixel:p{0,0}] %[fx:100*lightness]", "info:"]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if answer.returncode != 0:
            return None
        return _parse_corner(answer.stdout.decode("utf-8", "replace"))

    def starved(self, relative: str) -> bool:
        """Whether this image is already too small to be worth re-encoding.

        One ``identify -ping`` - a read of the header and no decoding at all -
        against the model's requirement for that format at that size. What is IN
        the picture is deliberately not measured: that costs a full decode per
        image, which is the conversion's own cost paid twice, and leaving the axis
        unmeasured is the reading that cannot skip an image that deserved
        converting.

        An image nothing could measure is CONVERTED: the check exists to avoid
        pointless work, and refusing a conversion over a missing measurement
        would lose one worth doing.
        """
        return adequacy.is_starved(imagebitrate.image_adequacy(relative))

    def transcode(self, relative: str) -> None:
        out = disambiguated_output(relative, self.options["format"],
                                   self.input_dir, self.output_dir)
        if not os.path.isfile(relative):
            self.record("notFound", "ERROR (not found)", relative,
                        stream=sys.stderr)
            return
        if os.path.isfile(out):
            self.record("alreadyDone", "SKIP (already done)", relative,
                        stream=sys.stderr)
            return
        if self.options["skipStarved"] and self.starved(relative):
            self.record("starved", "SKIP (starved)", relative,
                        stream=sys.stderr)
            return
        if self.options["crop"]:
            self.crop_convert(relative)
            return
        if self._to_lane(relative, out):
            return
        if self.options.get("gpuLane"):
            self._cpu_to_lane(relative, out, (), "converted", "CONV",
                              self._pixels(relative))
            return
        self.record("converted", "CONV", relative,
                    pixels=self._pixels(relative))
        self._convert(self._encode_arguments(relative, out))

    def _to_lane(self, relative: str, out: str, gpu_input: str = "",
                 category: str = "converted",
                 label: str = "CONV (GPU)") -> bool:
        """Hand this image to the GPU lane of the run that started this one,
        and say whether it was taken. Only under a lane (:data:`LANE_VARIABLE`):
        that run owns the card and encodes the pages of many of these runs
        together, so this one only writes down where each page is.

        What av1gpu reads goes - a JPEG, decoded on the card, or a PNG or WebP,
        decoded on several CPU threads at about 80 % of a JPEG's speed - when
        it is encoded whole: within -m, without alpha, and for a PNG or WebP
        upright (see :func:`av1gpu.takes_probe`). Nothing is resized to make
        it fit. <gpu_input> is what the card is given in place of the page
        itself: the page cropped by :meth:`_crop_for_lane`.

        Each line of the manifest is the card's input, the output, its pixels,
        and the ImageMagick call that encodes that input on the CPU - which the
        lane runs for a page the card did not write, or one a CPU encoder
        takes while the card has more than it can start on.
        """
        lane = self.options.get("gpuLane", "")
        source = gpu_input or relative
        if not lane or source.rpartition(".")[2] not in av1gpu.EXTENSIONS:
            return False
        pixels = av1gpu.takes_probe(
            self._identify(["-ping", "-format", av1gpu.PROBE_FORMAT, source]),
            UNBOUNDED_EDGE, self.options["maxHeight"],
            jpeg=source.endswith((".jpg", ".jpeg")))
        if not pixels:
            return False
        source, out = os.path.abspath(source), os.path.abspath(out)
        self._list({"input": source, "output": out, "pixels": pixels,
                    "cpu": imagemagick.convert_argv(
                        self._encode_arguments(source, out))})
        self.record(category, label, relative, pixels=pixels, road="gpu")
        return True

    def gpu_pixels(self, relative: str) -> int:
        """The pixel count of this image when it goes to the GPU encoder
        rather than to the transcode below, and 0 when it does not.

        Everything the transcode would SKIP stays with it - a missing source,
        an output already there, a starved image - so that it is counted and
        reported there, the one place that does. What is left goes to the GPU
        when av1gpu can read it and the picture needs no resize: the extension
        is free to ask, and alpha and size cost one header read.
        """
        if relative.rpartition(".")[2] not in av1gpu.EXTENSIONS:
            return 0
        if not os.path.isfile(relative):
            return 0
        if os.path.isfile(disambiguated_output(relative, self.options["format"],
                                               self.input_dir, self.output_dir)):
            return 0
        if self.options["skipStarved"] and self.starved(relative):
            return 0
        return av1gpu.takes_probe(
            self._identify(["-ping", "-format", av1gpu.PROBE_FORMAT, relative]),
            UNBOUNDED_EDGE, self.options["maxHeight"],
            jpeg=relative.endswith((".jpg", ".jpeg")))

    def reverse_to_jpeg(self, relative: str) -> None:
        """The -r path, which the starved check has nothing to say about: -r
        exists to get a picture into a format something else can open, and a
        source too small to improve is exactly as unopenable as a large one."""
        out = disambiguated_output(relative, "jpg", self.input_dir,
                                   self.output_dir)
        if os.path.isfile(out):
            self.record("alreadyDone", "SKIP (already done)", relative,
                        stream=sys.stderr)
            return
        quality = self.options["quality"] + JPEG_QUALITY_BONUS
        self.record("converted", "REV", relative, pixels=self._pixels(relative))
        geometry = self.options["maxResCommand"]
        if self.options["crop"]:
            self._convert([relative, "-fuzz", self.options["fuzzCommand"],
                           "-trim", "-quality", str(quality),
                           "-resize", geometry, out])
        else:
            self._convert([relative, "-quality", str(quality),
                           "-resize", geometry, out])


# --- the run -------------------------------------------------------------------

def _images_under(directory: str, extensions, lowercase_only=False):
    """Every image below <directory>, as paths relative to it."""
    wanted = set(extensions) if lowercase_only else {
        e.lower() for e in extensions}
    found = []
    for parent, _dirs, names in os.walk(directory):
        for name in names:
            extension = name.rpartition(".")[2]
            hit = extension in wanted if lowercase_only else \
                extension.lower() in wanted
            if hit:
                found.append(os.path.relpath(os.path.join(parent, name),
                                             directory))
    return sorted(found, key=os.fsencode)


def _holds_bytes(path: str) -> bool:
    """Whether <path> is a file that _prune() would leave in place."""
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _prune(directory: str) -> None:
    """Empty folders and zero-byte files go, deepest first - the pretreatment
    both trees get before anything is converted."""
    for parent, dirs, files in os.walk(directory, topdown=False):
        for name in files:
            path = os.path.join(parent, name)
            try:
                if os.path.getsize(path) == 0:
                    os.remove(path)
            except OSError:
                pass
        for name in dirs:
            path = os.path.join(parent, name)
            try:
                if not os.listdir(path):
                    os.rmdir(path)
            except OSError:
                pass


def _mirror_folders(input_dir: str, output_dir: str) -> None:
    for parent, _dirs, _files in os.walk(input_dir):
        relative = os.path.relpath(parent, input_dir)
        target = output_dir if relative == "." else os.path.join(output_dir,
                                                                 relative)
        os.makedirs(target, exist_ok=True)


def _in_worker(state, worker_name: str, relative: str) -> None:
    """One item, in a worker PROCESS.

    The worker's interrupt handling is installed here and not in the conversion
    itself, because the serial path runs that same conversion in the RUN's own
    process - where installing the worker's handler would replace the run's, and
    a Ctrl+C would leave with the queue-stopping status instead of the run's own
    and without its closing report.
    """
    safety.trap_worker_abort()
    getattr(state, worker_name)(relative)


def _run_pool(state, work, jobs: int, worker_name: str) -> None:
    if jobs <= 1:
        worker = getattr(state, worker_name)
        for relative in work:
            if safety.abort_requested():
                return
            worker(relative)
        return

    workerpool.run(work, jobs, _in_worker,
                   lambda relative: (state, worker_name, relative))


def _encode_on_gpu(state, work, jobs: int, encoder: str) -> list:
    """Hand every image the GPU can take to <encoder>, in chunks of
    av1gpu.CHUNK_MEGAPIXELS, and return the work that is left for the CPU:
    what it could not take, what it did not write, and every skip, which the
    CPU road reports.

    The question of which images it can take is a header read each, asked
    <jobs> at a time - those are subprocesses, and asked one after another they
    would cost more than the GPU's own encode of a small page.

    An encoder that ran and wrote nothing at all could not start - no card, no
    driver, no memory to spare - and it is not asked again this run: every
    further chunk would pay the same start-up only to fail the same way.
    """
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        sized = [(relative, pixels) for relative, pixels in
                 zip(work, pool.map(state.gpu_pixels, work)) if pixels]
    pixels_of = dict(sized)
    done = set()
    for chunk in av1gpu.chunks(sized):
        if safety.abort_requested():
            break
        items = [(os.path.join(state.input_dir, relative),
                  disambiguated_output(relative, state.options["format"],
                                       state.input_dir, state.output_dir))
                 for relative in chunk]
        workdir = tempfile.mkdtemp(prefix="av1gpu_", dir=state.counter_dir)
        try:
            written, status = av1gpu.encode_chunk(
                encoder, items, workdir, state.options["gpuQuality"],
                state.options["gpuPreset"], chroma=state.options["chroma"])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        for index in written:
            relative = chunk[index]
            state.record("converted", "CONV (GPU)", relative,
                         pixels=pixels_of[relative], road="gpu")
            done.add(relative)
        if not written and status > 0:
            log("WARNING: %s could not encode (exit status %d) - the rest of "
                "this run is encoded on the CPU." % (encoder, status))
            break
    return [relative for relative in work if relative not in done]


def main(argv: list, program: str = "convert-images") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
        clioptions.settle_verbosity(result)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        sys.stderr.write(clioptions.usage_error_text(declaration,
                                                     error.message))
        return 1

    if clioptions.args_out_of_range(len(result.positionals), 2, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    input_dir, output_dir = result.positionals[0], result.positionals[1]
    if safety.require_separate_output(input_dir, output_dir):
        return 1

    if "k" in result.given:
        return _calibrate(declaration, program, result, input_dir, output_dir)

    crop = "c" in result.given
    always = "a" in result.given
    reverse = "r" in result.given
    image_format = result.values["outputFormat"] or DEFAULT_FORMAT
    chroma = result.values["chroma"] or DEFAULT_CHROMA
    # -r writes JPEG whatever -e says, so -u has nothing to act on there either.
    if result.values["chroma"] and (reverse or image_format != CHROMA_FORMAT):
        sys.stderr.write(clioptions.usage_error_text(
            declaration, "-u only applies when writing %s, not %s."
            % (CHROMA_FORMAT, "jpeg" if reverse else image_format)))
        return 1
    speed = int(result.values["speedPreset"] or DEFAULT_SPEED)
    max_res = result.values["maxRes"] or str(UNBOUNDED_EDGE)
    quality = int(result.values["quality"] or 60)
    # -r never asks: converting back to JPEG is about what can open the file, not
    # about what it would cost to store, so the check would only refuse a
    # conversion nobody asked it to judge.
    skip_starved = not always and not reverse
    options = {
        "crop": crop,
        "skipStarved": skip_starved,
        "format": image_format,
        "chroma": chroma,
        "quality": quality,
        # Derived once, here: it is the same number for every conversion of the
        # run, and the workers that read it are separate processes.
        "effortLevel": FORMATS[image_format].level(speed),
        "fuzzCommand": "%s%%" % (result.values["fuzz"] or "10"),
        "maxResCommand": "%dx%s>" % (UNBOUNDED_EDGE, max_res),
        "maxHeight": int(max_res) if max_res.isdigit() else 0,
    }
    # The GPU encodes the whole picture as it is, so this run only asks it
    # where that is what is wanted: not for -c, whose trim decides per image
    # what to encode, and not for an -m it could not read as a height. Under a
    # lane the run that set it asks instead, -c included: a page the trim does
    # not cut is encoded whole, as the card would.
    encoder = ""
    lane = os.environ.get(LANE_VARIABLE, "")
    if image_format == CHROMA_FORMAT and not reverse and options["maxHeight"]:
        if lane:
            options["gpuLane"] = lane
            # The trim done losslessly, where jpegtran is there to do it.
            options["losslessCrop"] = bool(shutil.which("jpegtran"))
        elif not crop:
            encoder = av1gpu.binary()
    # -l and -s mean what they mean on the CPU; what av1gpu has to be asked
    # for to give the same was measured, not assumed.
    if encoder:
        options["gpuPreset"], options["gpuQuality"] = \
            encodercalibration.active().gpu_settings(speed, quality)
    jobs = int(result.values["jobs"]
               or max(1, int(runlog.cpu_count()
                             / threads_per_conversion(image_format, speed))))

    runlog.settle_flock()
    tools = [imagemagick.CONVERT_SPEC] + (
        [imagemagick.IDENTIFY_SPEC] if crop or skip_starved else [])
    skip_preflight = bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))
    if tooldeps.require_tools(program, tools,
                              skip_preflight=skip_preflight):
        return 1
    # Having ImageMagick is not the same as having an ImageMagick that can do
    # THIS format: each one is a separate delegate the build was or was not
    # given, and a build without it converts nothing while failing one image at
    # a time. -r reads the format instead of writing it, so that is what is
    # asked about.
    if imagemagick.require_format(program, image_format, writing=not reverse,
                                  skip_preflight=skip_preflight):
        return 1
    # fdupes is the one tool whose absence changes WHAT happens rather than
    # whether it happens. A parent may have settled it already; inherit that
    # instead of warning a second time.
    if "HAVE_FDUDES" not in os.environ:
        if tooldeps.tool_present("fdupes"):
            os.environ["HAVE_FDUDES"] = "1"
        else:
            os.environ["HAVE_FDUDES"] = ""
            log("WARNING: fdupes not found (apt install fdupes) - the input "
                "will not be de-duplicated,")
            log("         so identical files are converted and stored as "
                "separate copies. The")
            log("         conversion itself is unaffected.")
    runlog.warn_uncounted_progress()

    # Nothing to convert? Say so before the input is de-duplicated, pruned and
    # lower-cased, so a refused run leaves it exactly as it was. A 0-byte image
    # is not something to convert: the pruning would take it away first.
    if reverse:
        wanted = "%s images (.%s) to convert back to JPEG" % (
            image_format.upper(), image_format)
        extensions = [image_format]
    else:
        wanted = "images (%s)" % enums.extension_list(
            list(enums.IMAGE_EXTENSIONS))
        extensions = list(enums.IMAGE_EXTENSIONS)
    if not any(_holds_bytes(os.path.join(input_dir, relative))
               for relative in _images_under(input_dir, extensions)):
        return safety.fail_no_relevant_input(input_dir, wanted)

    counter_dir = ""
    started = None
    state = None
    own_safety_log = not (os.environ.get("SAFETY_LOG")
                          and os.path.isfile(os.environ["SAFETY_LOG"]))
    try:
        ramscratch.init_ram_base()
        counter_dir, status = ramscratch.ram_scratch_dir("counters")
        if status != 0 or not counter_dir:
            sys.stderr.write("\nError: no scratch directory could be made for "
                             "this run.\nNothing was changed.\n")
            return 1
        ramscratch.add_exit_cleanup([counter_dir])
        if own_safety_log:
            safety.init_safety_log(os.path.join(counter_dir, "safetySkips.log"))
        safety.init_abort_flag(os.path.join(counter_dir, "abortRequested"))
        safety.trap_run_abort()

        def footer():
            # A run stopped before the conversion began has only the safety
            # recap to give, and gives that rather than a page of zeroes.
            if started is not None and state is not None:
                _print_footer(state, started, result.values, own_safety_log)
            elif own_safety_log:
                safety.report_safety_skips()

        safety.set_run_footer(footer)

        if os.environ.get("HAVE_FDUDES"):
            subprocess.run(["fdupes", "-rdN", input_dir],
                           stdout=subprocess.DEVNULL)
        os.makedirs(output_dir, exist_ok=True)
        _prune(input_dir)
        _prune(output_dir)
        safety.lower_case_extensions(os.path.realpath(input_dir))
        _mirror_folders(input_dir, output_dir)

        # Resolved BEFORE the move into the input: a relative pair resolved
        # after it lands inside the input folder, where nothing exists, and
        # every conversion then fails silently.
        input_dir, output_dir = (os.path.realpath(input_dir),
                                 os.path.realpath(output_dir))
        os.chdir(input_dir)
        # Lower-cased above, so the search is case-sensitive from here on.
        work = _images_under(".", extensions, lowercase_only=True)
        total = len(work)
        # Belt and braces: the probe refused an empty input, but the preparation
        # between the two could have taken the last image away.
        if total == 0:
            return safety.fail_no_relevant_input(input_dir, wanted)

        for name in ("current", "converted", "trimmed", "blank", "starved",
                     "alreadyDone", "notFound", "gpuImages", "gpuPixels",
                     "cpuImages", "cpuPixels"):
            with open(os.path.join(counter_dir, name), "w") as handle:
                handle.write("0")

        state = Run(input_dir, output_dir, counter_dir, options, total)
        started = time.time()
        if encoder:
            work = _encode_on_gpu(state, work, jobs, encoder)
        # Each road's speed is over its own stretch of the wall clock: the
        # GPU's chunks come first, and the CPU's pool runs after them.
        state.cpu_started = time.time()
        _run_pool(state, work, jobs,
                  "reverse_to_jpeg" if reverse else "transcode")
        safety.exit_if_aborted()
        safety.print_run_footer()
        return workerpool.exit_status()
    finally:
        ramscratch.run_exit_cleanup()


# --- --calibrate ---------------------------------------------------------------

# What the measurement is made on: crops rather than whole pictures, because the
# grid is a few thousand encodes and a crop scores in a second where a 15 MP
# photo takes twenty. Two from each image - the middle and an off-centre one -
# of up to CALIBRATION_IMAGES images spread through the input.
CALIBRATION_IMAGES = 12
CALIBRATION_CROP = 1280

# The grid. -s 0 and 1 are left out: libaom's slowest speeds take minutes a
# crop, and they take -s 2's answer (the nearest measured). The GPU's quality
# steps are finer than the CPU's because they are what is interpolated INTO.
CALIBRATION_SPEEDS = tuple(range(2, MAX_SPEED + 1))
CALIBRATION_CPU_QUALITIES = (30, 40, 50, 60, 70, 80, 90)
CALIBRATION_GPU_QUALITIES = tuple(range(20, 100, 5))

SCORER = "ssimulacra2_rs"

# The options a calibration has no use for: it measures AVIF 4:2:0 at every
# speed and quality there is, on pictures it cuts itself.
_NOT_WITH_CALIBRATE = "acremuls"


def _calibrate(declaration, program, result, input_dir, output_dir) -> int:
    """convert-images -k: crop, encode the crops on both roads over the grid,
    score every encode, and save the mapping the scores say.

    The input is only read. Everything the measurement makes - crops, encodes,
    decoded pictures, and the scores as a CSV - stays under <outputDir>, which
    is left for inspection.
    """
    clashing = [letter for letter in _NOT_WITH_CALIBRATE
                if letter in result.given]
    if clashing:
        sys.stderr.write(clioptions.usage_error_text(
            declaration, "-k measures every speed and quality itself; it "
            "takes no -%s." % ", -".join(clashing)))
        return 1
    if safety.require_separate_output(input_dir, output_dir):
        return 1
    skip_preflight = bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))
    if tooldeps.require_tools("calibration (-k)",
                              [imagemagick.CONVERT_SPEC,
                               imagemagick.IDENTIFY_SPEC, SCORER],
                              skip_preflight=skip_preflight):
        return 1
    if imagemagick.require_format(program, CHROMA_FORMAT,
                                  skip_preflight=skip_preflight):
        return 1
    encoder = av1gpu.binary()
    if not encoder:
        sys.stderr.write("\nCannot calibrate: there is no GPU encoder to "
                         "calibrate (%s is not on PATH, or %s is empty).\n"
                         "Nothing was changed.\n"
                         % (av1gpu.DEFAULT_BINARY, av1gpu.ENCODER_VARIABLE))
        return 1
    jobs = int(result.values["jobs"] or runlog.cpu_count())

    work = os.path.realpath(output_dir)
    crops_dir = os.path.join(work, "crops")
    os.makedirs(crops_dir, exist_ok=True)
    crops = _calibration_crops(os.path.realpath(input_dir), crops_dir, jobs)
    if not crops:
        return safety.fail_no_relevant_input(
            input_dir, "8-bit JPEG, PNG or WebP images without transparency")
    runlog.say("Calibrating on %d crops\n" % len(crops))

    started = time.time()
    encodes = _calibration_gpu(encoder, crops, os.path.join(work, "gpu"))
    runlog.say("GPU: %d encodes in %.0f seconds\n"
               % (len(encodes), time.time() - started))
    started = time.time()
    cpu = _calibration_cpu(crops, os.path.join(work, "cpu"), jobs)
    runlog.say("CPU: %d encodes in %.0f seconds\n"
               % (len(cpu), time.time() - started))
    encodes += cpu
    safety.exit_if_aborted()

    started = time.time()
    decoded = os.path.join(work, "decoded")
    os.makedirs(decoded, exist_ok=True)
    with ThreadPoolExecutor(max_workers=runlog.cpu_count()) as pool:
        scores = list(pool.map(
            lambda encode: _score(encode, decoded), encodes))
    rows = [encode[:3] + score for encode, score in zip(encodes, scores)
            if score is not None]
    runlog.say("Scored %d of %d encodes in %.0f seconds\n\n"
               % (len(rows), len(encodes), time.time() - started))
    with open(os.path.join(work, "scores.csv"), "w") as handle:
        handle.write("road,setting,quality,bits,pixels,ssimulacra2\n")
        for row in rows:
            handle.write("%s,%s,%s,%d,%d,%.4f\n" % row)

    found = encodercalibration.curves(rows)
    try:
        calibration = encodercalibration.derive(found)
    except (KeyError, StopIteration):
        sys.stderr.write("\nCalibration failed: too few encodes could be "
                         "scored to compare the two encoders (see %s).\n"
                         % os.path.join(work, "scores.csv"))
        return 1
    runlog.say(encodercalibration.report(found, calibration))
    runlog.say("\nSaved to %s\n" % encodercalibration.save(calibration))
    return 0


def _calibration_crops(input_dir: str, crops_dir: str, jobs: int) -> list:
    """The crops, as ``(path, pixels)``: up to CALIBRATION_IMAGES images the
    GPU can read, spread evenly through the input, two crops each, written as
    8-bit PNG so both roads read the same picture."""
    candidates = [os.path.join(input_dir, relative) for relative in
                  _images_under(input_dir, sorted(av1gpu.EXTENSIONS))]

    def probe(path):
        answer = subprocess.run(
            imagemagick.identify_argv(["-ping", "-format", av1gpu.PROBE_FORMAT,
                                       path]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        text = answer.stdout.decode("utf-8", "replace")
        if not av1gpu.takes_probe(text, UNBOUNDED_EDGE, UNBOUNDED_EDGE,
                                  jpeg=path.lower().endswith((".jpg", ".jpeg"))):
            return None
        _alpha, _depth, width, height, orientation = text.split()
        # The crop is cut from the picture turned upright, so its corners are
        # placed on the upright size.
        if orientation in av1gpu.QUARTER_TURNS:
            return int(height), int(width)
        return int(width), int(height)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        usable = [(path, size) for path, size in
                  zip(candidates, pool.map(probe, candidates)) if size]
    step = max(1, len(usable) // CALIBRATION_IMAGES)
    chosen = usable[::step][:CALIBRATION_IMAGES]

    crops = []
    for number, (path, (width, height)) in enumerate(chosen, 1):
        side_w, side_h = min(CALIBRATION_CROP, width), min(CALIBRATION_CROP,
                                                           height)
        corners = {((width - side_w) // 2, (height - side_h) // 2),
                   (width // 5 if width // 5 + side_w <= width else 0,
                    height // 5 if height // 5 + side_h <= height else 0)}
        for letter, (left, top) in zip("ab", sorted(corners)):
            out = os.path.join(crops_dir, "%02d%s.png" % (number, letter))
            # Turned upright first: a phone photo's EXIF orientation would
            # otherwise ride along on the PNG, the CPU encoder would honour it
            # and av1gpu would not, and the two roads' pictures would be
            # compared a quarter turn apart.
            done = subprocess.run(imagemagick.convert_argv(
                [path, "-auto-orient",
                 "-crop", "%dx%d+%d+%d" % (side_w, side_h, left, top),
                 "+repage", "-depth", "8", "-strip", "PNG24:" + out]),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if done.returncode == 0:
                crops.append((out, side_w * side_h))
    return crops


def _calibration_gpu(encoder: str, crops, out_dir: str) -> list:
    """Every crop at every preset and quality, one av1gpu call per pair:
    ``(road, setting, quality, source, encode, pixels)`` for each written."""
    encodes: list = []
    for preset in encodercalibration.GPU_PRESETS:
        for quality in CALIBRATION_GPU_QUALITIES:
            if safety.abort_requested():
                return encodes
            target = os.path.join(out_dir, "%s_q%d" % (preset, quality))
            os.makedirs(target, exist_ok=True)
            items = [(source, os.path.join(target, os.path.basename(source)
                                           + ".avif")) for source, _ in crops]
            workdir = tempfile.mkdtemp(prefix="av1gpu_", dir=out_dir)
            try:
                written, _status = av1gpu.encode_chunk(
                    encoder, items, workdir, quality, preset)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            encodes += [("gpu", preset, quality, crops[i][0], items[i][1],
                         crops[i][1]) for i in written]
    return encodes


def _calibration_cpu(crops, out_dir: str, jobs: int) -> list:
    """Every crop at every speed and quality, through the very call a run
    makes (:func:`encode_arguments`)."""
    os.makedirs(out_dir, exist_ok=True)
    grid = [(speed, quality, source, pixels)
            for speed in CALIBRATION_SPEEDS
            for quality in CALIBRATION_CPU_QUALITIES
            for source, pixels in crops]

    def encode(cell):
        speed, quality, source, pixels = cell
        if safety.abort_requested():
            return None
        out = os.path.join(out_dir, "s%d_q%d_%s.avif" % (
            speed, quality, os.path.basename(source)))
        options = {"format": CHROMA_FORMAT, "chroma": DEFAULT_CHROMA,
                   "quality": quality,
                   "effortLevel": FORMATS[CHROMA_FORMAT].level(speed),
                   "maxResCommand": "%dx%d>" % (UNBOUNDED_EDGE, UNBOUNDED_EDGE)}
        done = subprocess.run(
            imagemagick.convert_argv(encode_arguments(options, source, out)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if done.returncode != 0 or not _holds_bytes(out):
            return None
        return ("cpu", str(speed), quality, source, out, pixels)

    workers = max(1, int(jobs / THREADS_PER_CONVERSION))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [found for found in pool.map(encode, grid) if found]


def _score(encode, decoded_dir: str):
    """``(bits, pixels, score)`` for one encode: decoded to 16-bit PNG by
    ImageMagick - the scorer reads no AVIF - and scored against its source.
    None when either step fails."""
    road, setting, quality, source, out, pixels = encode
    picture = os.path.join(decoded_dir, "%s_%s_q%d_%s" % (
        road, setting, quality, os.path.basename(source)))
    done = subprocess.run(
        imagemagick.convert_argv([out, "-depth", "16", "PNG48:" + picture]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if done.returncode != 0:
        return None
    answer = subprocess.run([SCORER, "image", source, picture],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    os.remove(picture)
    text = answer.stdout.decode("utf-8", "replace")
    if answer.returncode != 0 or "Score:" not in text:
        return None
    try:
        score = float(text.split("Score:")[1].split()[0])
    except (ValueError, IndexError):
        return None
    return os.path.getsize(out) * 8, pixels, score


def _print_footer(state, started, values, own_safety_log) -> None:
    runtime = time.time() - started
    total = state.total
    # What was actually encoded - a trimmed image is converted too - which is
    # what the time is spent on: an image skipped as already done or starved
    # costs a stat or a header read, and averaging over those would flatter the
    # encoder.
    encoded = state.counter("converted") + state.counter("trimmed")
    runlog.say("\n")
    if encoded == total:
        runlog.say("Converted %d images in %.2f seconds\n" % (total, runtime))
    else:
        runlog.say("Converted %d of %d images in %.2f seconds\n"
                   % (encoded, total, runtime))
    if encoded > 0:
        runlog.say("%.3f seconds per image\n" % (runtime / encoded))
    ended = time.time()
    cpu_started = state.cpu_started or ended
    for label, road, seconds in (("GPU", "gpu", cpu_started - started),
                                 ("CPU", "cpu", ended - cpu_started)):
        line = _road_line(label, state.counter(road + "Images"),
                          state.counter(road + "Pixels"), seconds)
        if line:
            runlog.say(line)

    # The breakdown only once there is one: a run in which every image was
    # plainly converted has said all of it in the line above.
    if total > 0 and state.counter("converted") < total:
        for label, name in (("input not found", "notFound"),
                            ("already done", "alreadyDone"),
                            ("blank", "blank"),
                            ("starved, left alone", "starved"),
                            ("converted", "converted"),
                            ("trimmed", "trimmed")):
            count = state.counter(name)
            if count >= 1:
                runlog.say("%s: %d (%.1f%%)\n"
                           % (label, count, 100.0 * count / total))

    # The same numbers again, machine-readable, for a caller that runs this
    # script once per unit of work and throws each child's output away. Under a
    # lock, because several of those children can finish at once and each one's
    # record has to survive whole.
    stats_file = os.environ.get("imageStatsFile", "")
    if stats_file:
        with open(stats_file + ".lock", "w") as handle, \
                runlog.take_lock(handle), \
                open(stats_file, "a") as out:
            out.write("%d %d %d %d %d %d %d\n" % (
                total, state.counter("converted"),
                state.counter("trimmed"), state.counter("blank"),
                state.counter("alreadyDone"),
                state.counter("notFound"),
                state.counter("starved")))

    if own_safety_log:
        safety.report_safety_skips()


def _road_line(label: str, images: int, pixels: int, seconds: float) -> str:
    """One road's share of the run and its speed, in megapixels a second of
    that road's own stretch of the wall clock - or nothing, for a road that
    encoded nothing."""
    if images == 0:
        return ""
    megapixels = pixels / 1e6
    speed = (" = %.1f MP/s" % (megapixels / seconds)
             if seconds > 0 and pixels else "")
    return "%s: %d image(s), %.0f MP in %.1f seconds%s\n" % (
        label, images, megapixels, seconds, speed)


def cli(argv: list | None = None) -> int:
    settings.load()
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name))


if __name__ == "__main__":
    sys.exit(cli())
