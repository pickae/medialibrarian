"""Run ffsubsync, and say how sure its alignment is.

    <ffsubsync's python> ffsubsync_confidence.py <ffsubsync's own arguments>
    <ffsubsync's python> ffsubsync_confidence.py --probe

ffsubsync slides the subtitle along the audio and keeps the offset where the
two agree most. The score it reports for that offset is no measure of whether
the subtitle belongs to the film at all: it counts agreement between speech and
subtitle over the whole runtime, so a film with more dialogue scores higher
whatever is laid over it, and a wrong film's subtitle routinely outscores a
right one. What does tell them apart is the SHAPE of the agreement. The right
subtitle agrees at one offset far better than at any other - a spike - and the
wrong one agrees about equally badly everywhere, its best offset a random
bump in noise.

So this measures the spike: how far the best offset's agreement stands above
the best agreement found more than ``PEAK_WIDTH_SECONDS`` away from it, in
units of the spread of agreement across the whole search window (a median
absolute deviation, which the spike itself does not inflate). It is taken from
the very agreement curves ffsubsync computes - one per framerate it tries -
by watching them go past, so ffsubsync's alignment, its output and its own
quality check are exactly what they are without this.

The measurement is appended to ffsubsync's own log, which ``--log-dir-path``
must name, as ``alignment confidence: <number>``. A run in which ffsubsync
caught its own failure - which it logs and then exits 0 for, the same as a
success - exits 1 here, so a subtitle it never aligned is not taken for one it
did. ``--probe`` exits 0 when this ffsubsync is one the measurement can watch.
"""

import os
import sys

import numpy as np
from ffsubsync import aligners
from ffsubsync.constants import SAMPLE_RATE
from ffsubsync.ffsubsync import make_parser, run

PEAK_WIDTH_SECONDS = 2

# (best score, confidence) of every curve ffsubsync scored.
_measured: list[tuple[float, float]] = []


def _watch(original):
    def compute_argmax(self, convolve, substring):
        original(self, convolve, substring)
        _measured.append((float(self.best_score_), _confidence(convolve)))
    return compute_argmax


def _confidence(convolve):
    finite = np.isfinite(convolve)
    values = convolve[finite]
    if values.size == 0:
        return 0.0
    positions = np.flatnonzero(finite)
    best = int(np.argmax(values))
    away = np.abs(positions - positions[best]) > PEAK_WIDTH_SECONDS * SAMPLE_RATE
    if not away.any():
        return 0.0
    median = np.median(values)
    spread = 1.4826 * np.median(np.abs(values - median))
    if spread <= 0:
        return 0.0
    return float((values[best] - values[away].max()) / spread)


def _watchable():
    return callable(getattr(aligners.FFTAligner, "_compute_argmax", None))


def main(argv):
    if argv == ["--probe"]:
        return 0 if _watchable() else 1
    if not _watchable():
        return 1
    aligners.FFTAligner._compute_argmax = _watch(aligners.FFTAligner._compute_argmax)
    args = make_parser().parse_args(argv)
    result = run(args)
    if result.get("retval", 1) != 0:
        return 1
    log_path = os.path.join(args.log_dir_path or "", "ffsubsync.log")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            refused = "low-quality alignment" in handle.read()
    except OSError:
        return 1
    if result.get("offset_seconds") is None and not refused:
        return 1
    if _measured:
        # The alignment ffsubsync keeps is the curve whose best score is best.
        _score, confidence = max(_measured)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("alignment confidence: %.2f\n" % confidence)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
