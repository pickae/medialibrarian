"""Where a chunked video encode is cut: on a change of picture, not wherever the
arithmetic lands.

Every chunk opens with a keyframe and is rate-controlled on its own. Cut in the
middle of a shot, that keyframe is spent on a picture the frame before it already
described - the most expensive frame of the chunk, bought for nothing - and the
two encoders each see half of one shot, so a step in quality between them lands
where the eye is following something. Cut on a hard scene change, the keyframe is
the one the encoder would have placed there anyway; cut on black, it costs next to
nothing. Either way a change between the two chunks hides behind a change of
picture.

Only the neighbourhood of each even cut is looked at, never the whole file: there
are a handful of cuts, and a film averages a shot change every few seconds, so a
window of seconds either side nearly always holds one. The decode is of a small
grey copy of the picture, which is all a scene-change score and a black test
need. The chunks come out a few seconds uneven at most, which is nothing against
lengths measured in minutes.

A cut with nothing near it stays where the arithmetic put it, and so does every
cut of a source this cannot read.
"""

import shlex
import subprocess
from typing import NamedTuple

from medialib.lib import formatting

__all__ = [
    "SCENE_WINDOW",
    "SCENE_SCORE",
    "BLACK_AMOUNT",
    "BLACK_PIXEL",
    "Frame",
    "window_frames",
    "pick_cut",
    "aligned_bounds",
    "describe",
]

# How far either side of an even cut is searched, in seconds - and never more than
# a quarter of a chunk, so two searches cannot overlap and no chunk shrinks below
# half its share.
SCENE_WINDOW = 10.0

# The scdet score that counts as a hard cut: the filter's own default, on its
# 0-100 scale. A cut between shots scores well above it; motion, even fast
# motion, stays below.
SCENE_SCORE = 10.0

# A black frame: this percentage of its pixels at or under this 8-bit luma value -
# blackframe's own defaults. Limited-range black is 16, so the test holds for 8-
# and 10-bit sources alike once the picture is reduced to 8-bit grey.
BLACK_AMOUNT = 98
BLACK_PIXEL = 32

# How wide the copy that is analysed is. A scene change is a change of the whole
# picture, and it shows at any size.
_ANALYSIS_WIDTH = 320


class Frame(NamedTuple):
    """One decoded frame of a window: when it is shown, in the source's -ss
    seconds, how different it is from the frame before, and whether it is
    black."""

    time: float
    score: float
    black: bool


def _parse(text: str, start: float) -> list:
    """The frames the metadata filter printed, placed at ``start`` onwards."""
    frames: list = []
    time = score = None
    black = False

    def flush():
        if time is not None:
            frames.append(Frame(start + time, score or 0.0, black))

    for line in text.splitlines():
        if line.startswith("frame:"):
            flush()
            time, score, black = None, None, False
            for field in line.split():
                if field.startswith("pts_time:"):
                    time = formatting.awk_number(field[len("pts_time:"):])
        elif line.startswith("lavfi.scd.score="):
            score = formatting.awk_number(line.partition("=")[2])
        elif line.startswith("lavfi.blackframe.pblack="):
            black = True
    flush()
    return frames


def window_frames(source: str, start: float, span: float,
                  decode_accel_args: str = "") -> list:
    """Every frame of ``span`` seconds of ``source`` from ``start``, scored.

    The times the filter prints are measured from the seek point, which is the
    same zero a chunk's own -ss counts from, so ``start`` plus that time is the
    position a chunk would be cut at. Measured from the seek rather than read as
    absolute timestamps also keeps them small, where the six significant digits
    ffmpeg prints them with still resolve a single frame.

    Nothing comes back from a decode that failed, which leaves every cut in the
    window where it was.
    """
    chain = ",".join([
        # The default scaler, not fast_bilinear: on a source already this
        # width that one leaves scdet with no scores to print at all.
        "scale=%d:-2" % _ANALYSIS_WIDTH,
        "format=gray",
        # A threshold no score reaches: scdet is only asked for its scores,
        # not to drop frames or log its own verdicts.
        "scdet=threshold=100",
        "blackframe=amount=%d:threshold=%d" % (BLACK_AMOUNT, BLACK_PIXEL),
        "metadata=mode=print:file=-",
    ])
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-nostats",
            *shlex.split(decode_accel_args),
            "-ss", "%.3f" % start, "-t", "%.3f" % span, "-i", source,
            "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", chain,
            "-f", "null", "-"]
    try:
        ran = subprocess.run(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return []
    return _parse(ran.stdout.decode("utf-8", "surrogateescape"), start)


def pick_cut(frames: list, target: float) -> tuple | None:
    """``(seconds, "scene" | "black")``: the picture change in ``frames``
    nearest ``target``, or None when there is none.

    A hard cut and a black frame are equally good places for a chunk to start,
    so the nearer one wins, which keeps the chunks as even as the picture
    allows. The first frame of the window is never chosen - it has nothing
    before it to have changed from.

    The point returned is halfway between the chosen frame and the one before
    it, so the frame is the first of the next chunk whichever way the
    millisecond the cut is written to rounds.
    """
    best = None
    for before, frame in zip(frames, frames[1:]):
        if frame.black:
            kind = "black"
        elif frame.score >= SCENE_SCORE:
            kind = "scene"
        else:
            continue
        distance = abs(frame.time - target)
        if best is None or distance < best[0]:
            best = (distance, (before.time + frame.time) / 2, kind)
    return None if best is None else (best[1], best[2])


def aligned_bounds(source: str, bounds: list, decode_accel_args: str = "",
                   frames_of=window_frames) -> tuple:
    """``(bounds, kinds)``: the even chunk ``bounds``, "0" to the end, with each
    interior cut moved to the nearest picture change, and what each interior cut
    landed on - "scene", "black" or "even" where nothing was near.

    ``frames_of`` is :func:`window_frames`, taken as a parameter so the choice
    can be tested without a decoder.
    """
    if len(bounds) < 3:
        return list(bounds), []
    values = [formatting.awk_number(bound) for bound in bounds]
    shortest = min(b - a for a, b in zip(values, values[1:]))
    window = min(SCENE_WINDOW, shortest / 4)

    out = [bounds[0]]
    kinds = []
    for target in values[1:-1]:
        frames = frames_of(source, target - window, 2 * window,
                           decode_accel_args)
        cut = pick_cut(frames, target)
        if cut is None:
            out.append("%.3f" % target)
            kinds.append("even")
        else:
            out.append("%.3f" % cut[0])
            kinds.append(cut[1])
    out.append(bounds[-1])
    return out, kinds


def describe(kinds: list) -> str:
    """What the cuts landed on, as the chunking line says it: "2 on a scene
    change, 1 on black". Empty for a file with no cuts."""
    names = (("scene", "on a scene change"), ("black", "on black"),
             ("even", "with no change nearby"))
    return ", ".join("%d %s" % (kinds.count(kind), text)
                     for kind, text in names if kind in kinds)
