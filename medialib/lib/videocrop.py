"""The black-band measurement: how much of a stored frame is bars rather than
picture.

A film is delivered in whatever frame its master was cut to and the rest of the
container is filled with black - the bands above and below a scope film in a 16:9
frame, the pillars beside a 4:3 one. To an encoder those bands are picture: they
are scaled, filtered and coded like everything else, and a player that has to
letterbox anyway puts them back for nothing.

Two things make this more than running ffmpeg's cropdetect over the head of a file.
A film does not have ONE shape - a feature with IMAX sequences opens up to a taller
frame for them and closes again afterwards - so a crop measured at any one place
cuts the picture somewhere else. Moments spread across the whole running time are
measured instead, and only the band EVERY one of them agreed on comes off: the
SMALLEST common band, which cannot cut a frame that any sampled moment filled. And
the crop is symmetric by construction, the same lines off the top and the bottom
and the same columns off each side, because taking more off one side re-centres the
picture, and "this master is off-centre" is a claim no measurement here can make
safely.

The direction every uncertainty is resolved in is the same one: keep the pixels. A
moment that could not be read is a skipped sample rather than a wider crop, a file
too few of whose moments could be read is not cropped at all, and a band too thin to
be worth a filter is left on.
"""

from __future__ import annotations

import concurrent.futures
import re
import shlex
import subprocess
import sys
from typing import NamedTuple

from medialib.lib import aspectratios

__all__ = [
    "CROP_PROBE_SAMPLES",
    "CROP_PROBE_FRAMES",
    "CROP_PROBE_MIN_SAMPLES",
    "CROP_PROBE_MIN_BAND",
    "CROP_ROUND",
    "Crop",
    "crop_probe_sample",
    "common_crop",
    "crop_probe",
    "crop_for",
]

# How much of the file is looked at. What makes the answer safe is the SPREAD of
# the samples rather than the length of any one of them: it takes a single moment
# of the widest shape in the film to hold the crop back from cutting it, and the
# only way to be sure of finding one is to look all over the running time.
CROP_PROBE_SAMPLES = 24
CROP_PROBE_FRAMES = 12

# How many of them have to come back before the answer is believed. A file whose
# moments mostly could not be read has not been measured, and a crop from the
# handful that did is a guess at the shape of a film from a few seconds of it.
CROP_PROBE_MIN_SAMPLES = 6

# The thinnest band worth a filter, in pixels. Below this it is not letterboxing
# but the edge of the picture - a mastering artefact, a column of dead pixels, a
# codec's ringing - and cropping it buys nothing an encoder can measure.
CROP_PROBE_MIN_BAND = 8

# What every band is rounded DOWN to a multiple of. Even, because the 10-bit 4:2:0
# pixel formats these encoders use subsample chroma by two and reject an odd side;
# down, because rounding a band up would cut the picture by a line.
CROP_ROUND = 2

# cropdetect's own report: the rectangle to KEEP, as the crop filter would take it.
_CROP_RECT = re.compile(r"crop=(-?\d+):(-?\d+):(-?\d+):(-?\d+)")


class Crop(NamedTuple):
    """A rectangle to keep: the size of the picture and where it sits in the
    stored frame."""

    width: int
    height: int
    x: int
    y: int

    @property
    def spec(self) -> str:
        """The rectangle as ffmpeg's crop filter takes it."""
        return "%d:%d:%d:%d" % (self.width, self.height, self.x, self.y)


def _int(value: object) -> int:
    text = "" if value is None else str(value)
    return int(text) if text.isascii() and text.isdigit() else 0


def crop_probe_sample(input: str, t: str,
                      decode_accel_args: str = "") -> Crop | None:
    """The rectangle ONE moment of the source justifies keeping, or None when
    that moment could not be measured.

    cropdetect accumulates across the frames it is given rather than answering
    per frame, so what the last of its lines reports is the smallest crop that
    fits every frame of this sample - the union of their pictures. A fade to
    black has no picture in it at all and comes back as a negative rectangle,
    which is a SKIPPED sample and not a reason to crop the whole frame away.

    ``decode_accel_args`` is the hardware decode flags the run settled on, empty
    when there is none. It is word-split, so it is plain flag words with no
    quoting of its own.

    The filter's own black threshold is deliberately left at its default rather
    than pinned here: it is the one ffmpeg scales to the source's bit depth by
    itself, so one setting covers 8- and 10-bit sources, and it is the threshold
    every other tool's autocrop is calibrated against.
    """
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-nostats",
            *shlex.split(decode_accel_args),
            "-ss", t, "-i", input, "-map", "0:v:0",
            "-vf", "cropdetect=round=%d:reset=0:skip=0" % CROP_ROUND,
            "-frames:v", str(CROP_PROBE_FRAMES), "-an", "-sn", "-dn",
            "-f", "null", "-"]
    try:
        ran = subprocess.run(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE)
    except OSError:
        return None
    # What the filter PRINTED stands, and the decode's status is not consulted:
    # a seek that ended badly still measured the frames it reached, and a decode
    # that failed outright printed nothing, which is the skipped sample already.
    # Only the filter's own lines are read, because at this log level ffmpeg also
    # prints the input's name, and a file may be called anything at all.
    found = [match.groups()
             for match in (_CROP_RECT.search(line) for line
                           in ran.stderr.decode("utf-8",
                                                "surrogateescape").splitlines()
                           if "cropdetect" in line)
             if match]
    if not found:
        return None
    width, height, x, y = (int(value) for value in found[-1])
    if width <= 0 or height <= 0 or x < 0 or y < 0:
        return None
    return Crop(width, height, x, y)


def common_crop(rects, width: int, height: int) -> Crop | None:
    """The one symmetric rectangle that cuts nothing any of ``rects`` kept, or
    None when there is no band worth taking off.

    Every sample is read as four band widths against the stored frame, and each
    band comes off at its SMALLEST across the samples - so one moment that filled
    the frame to the top holds the whole run's top band at nothing. The two of a
    pair are then levelled to the smaller of themselves, which is what makes the
    crop symmetric without ever cutting deeper than the measurement allows.
    """
    bands = []
    for rect in rects:
        top, left = rect.y, rect.x
        bottom, right = height - rect.height - rect.y, width - rect.width - rect.x
        # A rectangle reaching outside the frame is not a measurement of it - a
        # sample of a stream whose size changed mid-file - and reads as no band.
        bands.append((max(top, 0), max(bottom, 0), max(left, 0), max(right, 0)))
    if not bands:
        return None

    vertical = min(min(top, bottom) for top, bottom, _l, _r in bands)
    horizontal = min(min(left, right) for _t, _b, left, right in bands)
    vertical -= vertical % CROP_ROUND
    horizontal -= horizontal % CROP_ROUND
    if vertical < CROP_PROBE_MIN_BAND:
        vertical = 0
    if horizontal < CROP_PROBE_MIN_BAND:
        horizontal = 0
    if not vertical and not horizontal:
        return None

    kept_width, kept_height = width - 2 * horizontal, height - 2 * vertical
    # Nothing left to encode: a file whose every sampled moment was black would
    # measure like this, and there is no crop that follows from it.
    if kept_width <= 0 or kept_height <= 0:
        return None
    return Crop(kept_width, kept_height, horizontal, vertical)


def crop_probe(input: str, media_duration, video_dimensions, jobs_per_core,
               decode_accel_args: str = "",
               samples: int = CROP_PROBE_SAMPLES) -> tuple:
    """What this source's black bands come to, as
    ``(crop, measured, frame size)`` - the crop being None when there is nothing
    to take off or nothing could be measured.

    The callers' own probes are taken as functions: ``media_duration`` answers in
    seconds, ``video_dimensions`` with the coded size first, and ``jobs_per_core``
    settles how many samples decode at once.
    """
    dims = video_dimensions(input).split()
    width = _int(dims[0]) if dims else 0
    height = _int(dims[1]) if len(dims) > 1 else 0
    duration = float(media_duration(input) or 0)
    if width <= 0 or height <= 0 or duration <= 0 or samples < 2:
        return None, 0, (width, height)

    # The samples stop short of both ends: the first and last few percent of a
    # film are logos, fades and credits, which are black far more often than they
    # are representative of what shape the film is.
    times = ["%.3f" % (duration * (0.04 + 0.92 * step / (samples - 1)))
             for step in range(samples)]
    rects = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, jobs_per_core(2))) as pool:
        def one(t):
            try:
                return crop_probe_sample(input, t, decode_accel_args)
            except Exception:
                # A worker that dies mid-sample is a SKIPPED sample, which the
                # minimum below is what answers for.
                return None
        for rect in pool.map(one, times):
            if rect is not None:
                rects.append(rect)

    if len(rects) < CROP_PROBE_MIN_SAMPLES:
        return None, len(rects), (width, height)
    return common_crop(rects, width, height), len(rects), (width, height)


def crop_for(input: str, label, media_duration, video_dimensions, jobs_per_core,
             decode_accel_args: str = "") -> str:
    """The crop rectangle to encode this file with as ffmpeg's crop filter takes
    it, or "" for a file that keeps its whole frame, the reasoning logged.

    Every outcome is reported with the shape it measured, because a crop is the
    one decision here that cannot be seen in the output afterwards: a file that
    comes out 1920x800 looks the same whether it was measured that way or cut
    there by accident.
    """
    label = label or input
    crop, measured, (width, height) = crop_probe(
        input, media_duration, video_dimensions, jobs_per_core,
        decode_accel_args)

    if measured < CROP_PROBE_MIN_SAMPLES:
        sys.stderr.write(
            "Crop: only %d of %d moments could be read, which is too few to say "
            "what shape this film is - encoding the whole frame: %s\n"
            % (measured, CROP_PROBE_SAMPLES, label))
        return ""
    if crop is None:
        sys.stderr.write(
            "Crop: no black bands worth removing at any of %d moments, encoding "
            "the whole %dx%d frame (%s): %s\n"
            % (measured, width, height,
               aspectratios.label_of(width, height), label))
        return ""

    sys.stderr.write(
        "Crop: %dx%d (%s) -> %dx%d (%s), %d line(s) off the top and the bottom "
        "and %d column(s) off each side - the smallest bands at any of %d "
        "moments: %s\n"
        % (width, height, aspectratios.label_of(width, height),
           crop.width, crop.height,
           aspectratios.label_of(crop.width, crop.height),
           crop.y, crop.x, measured, label))
    return crop.spec
