"""What the GPU encoder has to be asked for to give what the CPU gives.

convert-images takes one -l and one -s, and they mean what they mean on the
CPU: ImageMagick's AVIF quality and libaom's speed. av1gpu has a quality scale
of its own (avifenc's, which maps the same number to a different quantiser) and
seven presets where libaom has ten speeds, and it writes 8 bits where the CPU
writes 10. Passing the numbers through would give a different picture at a
different size for the same command line, depending on which road an image
happened to take.

So the two are MEASURED against each other rather than assumed: the same crops
are encoded on both roads over a grid of settings, every encode is scored
against its source with SSIMULACRA2, and from the curves that gives:

- the PRESET for each -s: -s's own ranking carried over, with the two
  defaults tied together and each side spread by the measured sizes
  (:func:`derive` says why it is the ranking and not the sizes themselves);
- the QUALITY for each -l at that speed: the av1gpu quality whose mean score
  equals the CPU's at that -l.

`convert-images --calibrate` runs the measurement and saves what it finds
(:func:`save`); a run reads it back (:func:`active`), and :data:`DEFAULT` -
measured on the machine this was written on - stands in until then. A new
build of either encoder is a reason to measure again.

Sizes are compared the way the codec literature does (BD-rate): the mean of
the log size ratio at equal score, over the score range both curves share.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field

from medialib.lib import settings

__all__ = [
    "GPU_PRESETS",
    "REFERENCE_SPEED",
    "GPU_ANCHOR",
    "Point",
    "Calibration",
    "DEFAULT",
    "curves",
    "bd_rate",
    "derive",
    "report",
    "path",
    "save",
    "load",
    "active",
]

# av1gpu's presets, slowest first.
GPU_PRESETS = ("slow", "base", "fast", "faster", "turbo", "turbo2", "turbo3")

# What every size is reported against: convert-images' default -s.
REFERENCE_SPEED = 5

# The GPU preset the CPU's REFERENCE_SPEED is tied to: av1gpu's own default,
# as -s 5 is this command's.
GPU_ANCHOR = "turbo"

FILE_NAME = "av1gpu-calibration.json"


@dataclass(frozen=True)
class Point:
    """One setting's mean over the crops: its quality number, the bits per
    pixel it wrote, and the score its pictures got."""

    quality: float
    bpp: float
    score: float


@dataclass
class Calibration:
    """For each CPU -s, the av1gpu preset and the (CPU -l, av1gpu quality)
    pairs that were measured to match. A speed that was not measured takes the
    nearest one that was."""

    presets: dict = field(default_factory=dict)
    qualities: dict = field(default_factory=dict)

    def _nearest(self, speed: int) -> int:
        return min(self.presets, key=lambda known: (abs(known - speed), known))

    def gpu_settings(self, speed: int, quality: int) -> tuple[str, int]:
        """The av1gpu preset and quality that give what the CPU gives at this
        -s and -l. Between two measured -l the quality is interpolated, and
        past the ends it is carried on along the nearest step, then held to
        av1gpu's 0-100."""
        known = self._nearest(speed)
        pairs = sorted(self.qualities[known])
        matched = _interpolate([p[0] for p in pairs], [p[1] for p in pairs],
                               quality)
        return self.presets[known], max(0, min(100, round(matched)))

    def to_json(self) -> str:
        return json.dumps({
            "presets": {str(k): v for k, v in sorted(self.presets.items())},
            "qualities": {str(k): v for k, v in sorted(self.qualities.items())},
        }, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Calibration":
        data = json.loads(text)
        presets = {int(k): v for k, v in data["presets"].items()}
        qualities = {int(k): [(float(a), float(b)) for a, b in v]
                     for k, v in data["qualities"].items()}
        if not presets or set(presets) != set(qualities) \
                or any(p not in GPU_PRESETS for p in presets.values()) \
                or any(len(v) < 2 for v in qualities.values()):
            raise ValueError("not a calibration")
        return cls(presets, qualities)


# Measured 2026-10-31 on an RTX 5090 (av1gpu 3abcbcb) against ImageMagick
# 7.1.2-18 / libheif's aom, over 24 1280x1280 crops of 12 camera photos.
# On those the GPU was smaller than the CPU at every setting: turbo3 6.6 %
# below CPU -s 5 at equal SSIMULACRA2, slow 12.7 %.
DEFAULT = Calibration(
    presets={2: "slow", 3: "slow", 4: "fast", 5: "turbo", 6: "turbo2",
             7: "turbo3", 8: "turbo3", 9: "turbo3"},
    qualities={
        2: [(30, 33.1), (40, 42.2), (50, 51.3), (60, 64.3),
            (70, 73.4), (80, 85.3), (90, 97.7)],
        3: [(30, 33.1), (40, 42.1), (50, 51.1), (60, 64.0),
            (70, 73.2), (80, 85.1), (90, 97.7)],
        4: [(30, 33.6), (40, 42.4), (50, 51.4), (60, 64.2),
            (70, 73.5), (80, 85.4), (90, 97.4)],
        5: [(30, 34.1), (40, 42.5), (50, 51.2), (60, 64.0),
            (70, 73.4), (80, 85.3), (90, 97.4)],
        6: [(30, 33.3), (40, 42.0), (50, 50.5), (60, 62.6),
            (70, 71.9), (80, 83.6), (90, 96.5)],
        7: [(30, 28.8), (40, 38.1), (50, 46.8), (60, 58.8),
            (70, 69.2), (80, 81.2), (90, 96.1)],
        8: [(30, 28.8), (40, 38.1), (50, 46.8), (60, 58.7),
            (70, 69.2), (80, 81.1), (90, 96.1)],
        9: [(30, 29.7), (40, 38.7), (50, 47.0), (60, 58.6),
            (70, 68.9), (80, 80.8), (90, 95.9)],
    })


def _interpolate(xs, ys, x: float) -> float:
    """Piecewise linear through (xs, ys), xs ascending, carried on past either
    end along the nearest step."""
    if len(xs) < 2:
        return ys[0]
    i = 0
    while i < len(xs) - 2 and x > xs[i + 1]:
        i += 1
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def curves(rows) -> dict:
    """``(road, setting) -> [Point]``, ordered by quality, from the scored
    encodes: each row ``(road, setting, quality, bits, pixels, score)``. The
    bits are summed over the crops before dividing, so a large crop weighs
    what it costs; the score is the plain mean."""
    sums: dict = defaultdict(lambda: [0, 0, 0.0, 0])
    for road, setting, quality, bits, pixels, score in rows:
        total = sums[(road, setting, quality)]
        total[0] += bits
        total[1] += pixels
        total[2] += score
        total[3] += 1
    found: dict = defaultdict(list)
    for (road, setting, quality), (bits, pixels, score, n) in sums.items():
        if pixels:
            found[(road, setting)].append(Point(quality, bits / pixels,
                                                score / n))
    return {key: sorted(points, key=lambda p: p.quality)
            for key, points in found.items()}


def _by_score(points):
    ordered = sorted(points, key=lambda p: p.score)
    return [p.score for p in ordered], [math.log(p.bpp) for p in ordered]


def bd_rate(test, reference, steps: int = 200) -> float | None:
    """How much larger <test>'s files are than <reference>'s at equal score,
    as a fraction (0.05 is 5 % larger), over the scores both reach - or None
    when they share no range to compare over."""
    test_scores, test_sizes = _by_score(test)
    ref_scores, ref_sizes = _by_score(reference)
    low = max(test_scores[0], ref_scores[0])
    high = min(test_scores[-1], ref_scores[-1])
    if high <= low:
        return None
    total = 0.0
    for k in range(steps + 1):
        score = low + (high - low) * k / steps
        total += (_interpolate(test_scores, test_sizes, score)
                  - _interpolate(ref_scores, ref_sizes, score))
    return math.exp(total / (steps + 1)) - 1


def _quality_for_score(points, score: float) -> float:
    ordered = sorted(points, key=lambda p: p.score)
    return _interpolate([p.score for p in ordered],
                        [p.quality for p in ordered], score)


def _place(value: float, low: float, high: float) -> float:
    """Where <value> sits between <low> (0) and <high> (1); 1 when there is
    no span to sit in, which puts it on the anchor."""
    if high == low:
        return 1.0
    return min(1.0, max(0.0, (value - low) / (high - low)))


def _preset_for(position: float, side, gpu_size: dict) -> str:
    """The preset of <side> - presets ordered from the far end to the
    anchor - whose own place between those two is nearest <position>; on a
    tie, the one nearer the anchor."""
    far, anchor = gpu_size[side[0]], gpu_size[side[-1]]
    return min(side, key=lambda preset: (
        abs(_place(gpu_size[preset], far, anchor) - position),
        -side.index(preset)))


def derive(found: dict) -> Calibration:
    """The calibration the curves say: for every measured CPU speed a preset,
    and the av1gpu quality matching each of its -l at that preset.

    The presets keep -s's ranking rather than its sizes, which the GPU beats
    at every setting here: an encoder that is smaller than the CPU at every
    speed would otherwise take its fastest preset for all of them, and -s would
    change nothing. The two defaults are anchored to each other - CPU -s
    REFERENCE_SPEED to GPU_ANCHOR - and each side of them is spread by the
    sizes measured on it: the slower speeds over the presets from the slowest
    to the anchor, by how far each CPU speed is from the slowest CPU speed's
    size to the reference's, and the faster ones likewise up to the fastest.
    Anchoring keeps a jump in the CPU's sizes - libaom's step between -s 6 and
    7 is most of its whole range - from dragging the default to the slowest
    preset on the GPU.
    """
    reference = found[("cpu", str(REFERENCE_SPEED))]
    gpu_size: dict[str, float] = {}
    for preset in GPU_PRESETS:
        if ("gpu", preset) in found:
            size = bd_rate(found[("gpu", preset)], reference)
            if size is not None:
                gpu_size[preset] = size
    cpu_size: dict[int, float] = {}
    for road, setting in found:
        if road == "cpu":
            size = bd_rate(found[(road, setting)], reference)
            if size is not None:
                cpu_size[int(setting)] = size
    present = [p for p in GPU_PRESETS if p in gpu_size]
    anchor = GPU_ANCHOR if GPU_ANCHOR in gpu_size else present[0]
    slower = present[:present.index(anchor) + 1]
    faster = present[present.index(anchor):][::-1]
    slowest = cpu_size[min(cpu_size)]
    fastest = cpu_size[max(cpu_size)]

    calibration = Calibration()
    for speed, size in sorted(cpu_size.items()):
        if speed <= REFERENCE_SPEED:
            preset = _preset_for(_place(size, slowest, 0.0), slower, gpu_size)
        else:
            preset = _preset_for(_place(size, fastest, 0.0), faster, gpu_size)
        calibration.presets[speed] = preset
        calibration.qualities[speed] = [
            (point.quality,
             round(_quality_for_score(found[("gpu", preset)], point.score), 1))
            for point in found[("cpu", str(speed))]]
    return calibration


def report(found: dict, calibration: Calibration) -> str:
    """The measurement as a person reads it: each setting's size against the
    reference at equal score, then the mapping that follows from it."""
    reference = found[("cpu", str(REFERENCE_SPEED))]
    lines = ["File size at equal SSIMULACRA2, against CPU -s %d:"
             % REFERENCE_SPEED]
    for speed in sorted(calibration.presets):
        size = bd_rate(found[("cpu", str(speed))], reference)
        lines.append("  CPU -s %d       %s" % (speed, _percent(size)))
    for preset in GPU_PRESETS:
        if ("gpu", preset) in found:
            size = bd_rate(found[("gpu", preset)], reference)
            lines.append("  GPU %-11s%s" % (preset, _percent(size)))
    lines += ["", "What the GPU is asked for, per CPU -s and -l:"]
    speeds = sorted(calibration.presets)
    qualities = sorted({q for s in speeds for q, _ in calibration.qualities[s]})
    lines.append("  -l   " + "".join("%9s" % ("-s %d" % s) for s in speeds))
    lines.append("       " + "".join("%9s" % calibration.presets[s]
                                     for s in speeds))
    for quality in qualities:
        cells = []
        for speed in speeds:
            matched = dict(calibration.qualities[speed]).get(quality)
            cells.append("%9s" % ("" if matched is None else "%.0f" % matched))
        lines.append("  %-4g " % quality + "".join(cells))
    lines += ["", "CPU -s %d, mean over the crops:" % REFERENCE_SPEED,
              "  -l    bits/pixel  SSIMULACRA2"]
    for point in reference:
        lines.append("  %-4g  %10.3f  %11.1f" % (point.quality, point.bpp,
                                                 point.score))
    return "\n".join(lines) + "\n"


def _percent(size: float | None) -> str:
    return "   n/a" if size is None else "%+6.1f %%" % (100 * size)


def path() -> str:
    """Where the measurement is kept: beside the settings file."""
    return os.path.join(os.path.dirname(settings.settings_file()), FILE_NAME)


def save(calibration: Calibration) -> str:
    target = path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    partial = target + ".part"
    with open(partial, "w") as handle:
        handle.write(calibration.to_json())
    os.replace(partial, target)
    return target


def load() -> Calibration | None:
    """The saved measurement, or None when there is none or it cannot be
    read - which a run answers with :data:`DEFAULT`, never with a refusal."""
    try:
        with open(path()) as handle:
            return Calibration.from_json(handle.read())
    except (OSError, ValueError, KeyError, TypeError):
        return None


def active() -> Calibration:
    return load() or DEFAULT
