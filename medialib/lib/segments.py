"""Planning where a long file is cut into pieces that encode in parallel.

Everything the shell's segment planning did except ``detectWindow``, which is an
ffmpeg decode rather than a calculation.

What is here is the arithmetic either side of it: the per-source scratch paths
that let the planner, the parallel encoders and the stitcher agree on a location
with no shared state, and the two awk passes that turn a duration into a chunk
count and a list of silence-nudged cut points.

Both awk passes read their inputs the way awk does - a duration of "" or "later"
is zero, not an error - so a missing probe result plans a file of length zero
rather than stopping the run. That is a deliberate property of the original and
is kept, through the port's one coercion rule in ``formatting.awk_number``,
including where that rule already departs from gawk on values too large for a
double.
"""

import hashlib
import os
from collections.abc import Iterable

from medialib.lib.formatting import awk_number

__all__ = [
    "chunk_dir_for",
    "plan_file_for",
    "seg_plan",
    "select_boundaries",
]


def _md5(text: str) -> str:
    """The hex digest ``printf '%s' text | md5sum`` prints."""
    return hashlib.md5(os.fsencode(text)).hexdigest()


def chunk_dir_for(chunk_root: str, relative_path: str) -> str:
    """The chunk directory for one source, under ``chunk_root``."""
    return f"{chunk_root}/{_md5(relative_path)}"


def plan_file_for(plan_root: str, relative_path: str) -> str:
    """The planning base path for one source, under ``plan_root``.

    The same hash as :func:`chunk_dir_for`, on purpose: the two are read by
    different processes that never talk to each other.
    """
    return f"{plan_root}/{_md5(relative_path)}"


def seg_plan(duration: object, jobs: object) -> str | None:
    """``"<n> <segment> <window>"`` for a file of ``duration`` seconds.

    None when there are fewer than two cores, which leaves the file whole.

    ``jobs`` is used twice and not the same way each time: the count is printed
    with ``%d``, which truncates, while the segment length divides by the value
    itself. A fractional core count is not a thing a machine reports, but the two
    readings have to stay two: rounding once would change the segment length the
    moment anything passed a fraction through.

    Anything that is not a whole number is refused outright, because a word
    compared as TEXT gets past a numeric guard and then divides by zero.
    """
    count = awk_number(jobs)
    if count < 2:
        return None
    segment = awk_number(duration) / count
    return f"{int(count)} {segment:.6f} {segment / 2:.6f}"


def _first_field(line: str) -> str:
    """awk's ``$1``: the first whitespace-separated field, or "" for a blank line.

    Reading the whole line would give the same number today - the numeric prefix
    cannot cross a blank, which is also what separates the fields - so this is
    kept for being what awk does, not for what it changes.
    """
    fields = line.split()
    return fields[0] if fields else ""


def select_boundaries(
    midpoints: Iterable[str], duration: object, jobs: object
) -> list[str]:
    """The interior cut points, ascending, for candidate silence ``midpoints``.

    One boundary is wanted every ``duration / jobs`` seconds. Each is moved to
    the nearest candidate within half a segment of it, and stays where the
    arithmetic put it when no candidate is near enough - so a file with no
    detected silence is still cut, just not at a quiet moment.

    Two guards keep the pieces from collapsing. A candidate is not eligible
    within a fifth of a segment of the previous cut - which starts out as the
    beginning of the file - or of the end. And a chosen boundary within one
    second of the previous one is dropped rather than moved: the mark does not
    advance with it, so the piece before simply runs on and there is one piece
    fewer, never a one-second one.
    """
    lines = list(midpoints)
    # sort -n, whose tie-break between equal numbers is the whole line's bytes.
    # The tie-break cannot change the answer (equal numbers are interchangeable
    # once read), but sorting by it keeps the two implementations comparable
    # line for line rather than only in their result.
    lines.sort(key=lambda line: (awk_number(line), os.fsencode(line)))
    mids = [awk_number(_first_field(line)) for line in lines]

    count = awk_number(jobs)
    if count < 2:
        return []
    total = awk_number(duration)
    segment = total / count
    window = segment / 2

    out: list[str] = []
    previous = 0.0
    k = 1
    while k < count:
        target = k * segment
        best = target
        best_distance = window + 1
        for mid in mids:
            distance = abs(mid - target)
            if (
                distance <= window
                and distance < best_distance
                and mid > previous + segment * 0.2
                and mid < total - segment * 0.2
            ):
                best_distance = distance
                best = mid
        k += 1
        if best <= previous + 1:
            continue
        out.append(f"{best:.3f}")
        previous = best
    return out
