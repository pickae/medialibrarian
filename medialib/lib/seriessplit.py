"""Splitting a file that holds several episodes into one file per episode.

Opt-in, under -A: most of the seam can be found without a model, and the
model only confirms it.

1. **Where to look**: the catalogue's runtime of each episode in the range
   gives an expected seam; the file's own chapter marks near it, if any,
   narrow it.
2. **Where exactly**: black frames and hard scene changes in a window round
   that point (:mod:`medialib.lib.scenecuts`), and the silences in the sound
   (ffmpeg's silencedetect). A seam is where black and silence meet.
3. **Is it the seam**: a model (the ``episode-seam`` task) is shown what is
   said either side of each candidate - end credits, a recap, "previously
   on", a new opening - and may only pick one of the candidates, or answer
   ``unsure``. ``unsure`` leaves the file whole, and it is listed.
4. **The cut**: ``mkvmerge --split timestamps:`` at the chosen times, no
   re-encode, so the cut lands on the nearest keyframe - inside the black.
   The original is kept as "<name> - old.mkv", like the remux.

Steps 1, 2 and the cut are pure or tool-bound and testable without a model;
the asking is :func:`ask_model`, handed in so a test can stand in for it.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from typing import NamedTuple

from medialib.lib import formatting, llmclient, llmroster, modelmeter, scenecuts

__all__ = ["Candidate", "Plan", "expected_seams", "candidates_near",
           "silences", "agree", "plan_split", "ask_model", "cut",
           "SYSTEM", "WINDOW_SECONDS"]

# How far either side of an expected seam the picture and sound are read.
WINDOW_SECONDS = 150.0
# How close a black stretch and a silence have to be to be one seam.
MEET_SECONDS = 1.5
# How close a chapter mark has to be to an expected seam to narrow it.
CHAPTER_SECONDS = 240.0
# The silence the seam is looked for in: quieter than this, for this long.
SILENCE_NOISE = "-45dB"
SILENCE_SECONDS = "0.4"

SYSTEM = """You decide where one television episode ends and the next \
begins in a video file that holds several episodes back to back.

You are given, for each candidate time, the dialogue just before and just \
after it. A real seam usually shows end credits or a closing line before it, \
and a recap ("previously on"), a cold open or an opening theme after it.

Answer with JSON only: {"pick": <the number of the candidate>} when one \
candidate is clearly the seam, or {"pick": "unsure"} when none is, or more \
than one could be. A wrong cut is far worse than none."""


class Candidate(NamedTuple):
    """A place the seam might be: its time in seconds, and what was found
    there ("black+silence", "black", "silence", "scene", "chapter",
    "runtime")."""

    seconds: float
    found: str


class Plan(NamedTuple):
    """What came of looking for the seams of one file: the times to cut at
    (one fewer than the episodes), or none, and why."""

    cuts: tuple
    reason: str


def expected_seams(runtimes: Sequence[float], length: float) -> list[float]:
    """Where each seam is expected, from the catalogue's runtimes (in
    seconds) of the episodes in the file, scaled to the file's own length -
    a broadcast runtime is the slot, a disc's episode runs shorter."""
    total = sum(runtimes)
    if not runtimes or total <= 0 or length <= 0:
        return []
    scale = length / total
    seams, at = [], 0.0
    for runtime in runtimes[:-1]:
        at += runtime * scale
        seams.append(at)
    return seams


def candidates_near(expected: float, chapters: Sequence[float],
                    frames: Sequence[scenecuts.Frame],
                    quiet: Sequence[float]) -> list[Candidate]:
    """The candidate seams round one expected one, best first: where a black
    frame and a silence meet, then black alone, then the chapter mark
    nearest, then the expected point itself.

    ``frames`` are the window's frames (:func:`scenecuts.window_frames`),
    ``quiet`` the middles of its silences."""
    blacks = [frame.time for frame in frames if frame.black]
    found: list[Candidate] = []
    for black in blacks:
        if any(abs(black - point) <= MEET_SECONDS for point in quiet):
            found.append(Candidate(round(black, 3), "black+silence"))
    found = _distinct(sorted(found, key=lambda one: abs(one.seconds - expected)))
    if not found:
        cut = scenecuts.pick_cut(list(frames), expected)
        if cut is not None:
            found.append(Candidate(round(cut[0], 3), cut[1]))
    near = [mark for mark in chapters
            if abs(mark - expected) <= CHAPTER_SECONDS]
    if near:
        mark = min(near, key=lambda point: abs(point - expected))
        if not any(abs(one.seconds - mark) <= MEET_SECONDS for one in found):
            found.append(Candidate(round(mark, 3), "chapter"))
    return found[:4]


def _distinct(found: list[Candidate]) -> list[Candidate]:
    kept: list[Candidate] = []
    for one in found:
        if not any(abs(one.seconds - other.seconds) <= MEET_SECONDS * 2
                   for other in kept):
            kept.append(one)
    return kept


def silences(path: str, start: float, span: float) -> list[float]:
    """The middles of the silences in ``span`` seconds of ``path`` from
    ``start``."""
    try:
        done = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-ss", "%.3f" % start,
             "-t", "%.3f" % span, "-i", path, "-vn", "-sn", "-af",
             "silencedetect=noise=%s:d=%s" % (SILENCE_NOISE, SILENCE_SECONDS),
             "-f", "null", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
    except OSError:
        return []
    midpoints, begun = [], None
    for line in done.stdout.decode("utf-8", "surrogateescape").splitlines():
        fields = line.split()
        for index, field in enumerate(fields[:-1]):
            if field == "silence_start:":
                begun = formatting.awk_number(fields[index + 1])
            elif field == "silence_end:" and begun is not None:
                end = formatting.awk_number(fields[index + 1])
                midpoints.append(start + (begun + end) / 2)
                begun = None
    return midpoints


def agree(picked: list[Candidate | None]) -> bool:
    """Whether every seam was settled."""
    return bool(picked) and all(one is not None for one in picked)


def plan_split(length: float, runtimes: Sequence[float],
               chapters: Sequence[float], read_window: Callable,
               choose: Callable) -> Plan:
    """The cuts for a file ``length`` seconds long holding episodes of
    ``runtimes``: each expected seam's candidates found by ``read_window``
    (``(start, span) -> (frames, silences)``) and settled by ``choose``
    (``(expected, candidates) -> Candidate | None``)."""
    seams = expected_seams(runtimes, length)
    if not seams:
        return Plan((), "the catalogue gives no runtime to look for a seam by")
    picked: list = []
    for expected in seams:
        start = max(0.0, expected - WINDOW_SECONDS)
        frames, quiet = read_window(start, 2 * WINDOW_SECONDS)
        found = candidates_near(expected, chapters, frames, quiet)
        if not found:
            return Plan((), "nothing near %s looks like a seam"
                        % _clock(expected))
        choice = choose(expected, found)
        if choice is None:
            return Plan((), "the seam near %s could not be told for sure"
                        % _clock(expected))
        picked.append(choice)
    return Plan(tuple(one.seconds for one in picked),
                ", ".join("%s (%s)" % (_clock(one.seconds), one.found)
                          for one in picked))


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def prompt(expected: float, found: Sequence[Candidate],
           said: Callable[[float, float], str]) -> str:
    """What the model is asked about one seam: each candidate with what is
    said in the minute before and after it."""
    lines = ["Expected near %s." % _clock(expected)]
    for number, one in enumerate(found, start=1):
        lines.append("")
        lines.append("Candidate %d at %s (%s)." % (number, _clock(one.seconds),
                                                   one.found))
        lines.append("Before: " + (said(one.seconds - 60, one.seconds)
                                   or "(nothing said)"))
        lines.append("After: " + (said(one.seconds, one.seconds + 60)
                                  or "(nothing said)"))
    return "\n".join(lines)


def parse_pick(answer: str, count: int) -> int | None:
    """The candidate (0-based) the model picked, or None for unsure or an
    answer that is not one."""
    parsed = llmclient.first_json(answer or "")
    if not isinstance(parsed, dict):
        try:
            parsed = json.loads(answer)
        except (TypeError, ValueError):
            return None
    pick = parsed.get("pick") if isinstance(parsed, dict) else None
    if isinstance(pick, int) and 1 <= pick <= count:
        return pick - 1
    if isinstance(pick, str) and pick.isdigit() and 1 <= int(pick) <= count:
        return int(pick) - 1
    return None


def ask_model(expected: float, found: Sequence[Candidate],
              said: Callable[[float, float], str], log,
              models=None, meter_file: str = "") -> Candidate | None:
    """The candidate the ``episode-seam`` model is sure of, or None."""
    if models is None:
        models, why = llmroster.optional(llmroster.EPISODE_SEAM)
        if not models:
            log("  No model to confirm the seam: " + why)
            return None
    request = prompt(expected, found, said)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    try:
        for llm in models:
            try:
                with llm.serving(log) as bound, spans.timing():
                    answer = bound.ask(SYSTEM, request, tally)
            except llmclient.LlmError as error:
                log("  No answer about the seam from %s: %s" % (llm.name, error))
                continue
            pick = parse_pick(answer, len(found))
            return found[pick] if pick is not None else None
    finally:
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    return None


def cut(path: str, cuts: Sequence[float], out_pattern: str) -> tuple[int, str]:
    """``mkvmerge --split timestamps:`` at ``cuts``, writing the pieces to
    ``out_pattern`` (mkvmerge numbers them ``-001``, ``-002``, …): its exit
    status and what it said."""
    argv = ["mkvmerge", "-o", out_pattern, "--split",
            "timestamps:" + ",".join("%.3fs" % point for point in cuts), path]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        return 127, "mkvmerge not found"
    return done.returncode, done.stdout.decode("utf-8", "replace").strip()


def pieces(out_pattern: str, count: int) -> list[str]:
    """The files mkvmerge wrote for ``out_pattern``, in order."""
    stem, extension = os.path.splitext(out_pattern)
    return ["%s-%03d%s" % (stem, number, extension)
            for number in range(1, count + 1)]
