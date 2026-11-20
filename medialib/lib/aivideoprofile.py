"""Asking a vision model to pick a video's encoding profile from sampled frames:
``convert-video -p auto``.

A mixed folder holds live-action films, 2D cel animation, 3D CGI and heavy-grain
film scans side by side. With a fixed profile, cel animation pays for a grain
probe and live action gets animation settings. Behind the explicit ``-p auto``
opt-in, a handful of frames sampled across each video (skipping opening logos
and closing credits) are shown to a vision model (:data:`llmroster.VIDEO_PROFILE`,
requiring ``vision`` and leaning ``light``), which classifies the material into
one of four content kinds mapped to ``convert-video``'s AV1 profiles:

  * ``live_action``  -> ``av1BluRay``
  * ``animation_2d`` -> ``av1Animation``
  * ``cgi_3d``       -> ``av1BluRay``
  * ``heavy_grain``  -> ``av1Grain``

Any uncertainty, unrecognised answer, frame extraction failure or model error
falls back cleanly to :data:`DEFAULT_PROFILE` (``av1BluRay``). Every pick is
logged with its reason, and model usage is recorded with
:mod:`medialib.lib.modelmeter`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from medialib.lib import llmclient, llmroster, modelmeter, runlog, safety

__all__ = [
    "AUTO_PROFILE",
    "DEFAULT_PROFILE",
    "CONTENT_PROFILES",
    "PURPOSE",
    "DEFAULT_SAMPLES",
    "MAX_FRAME_SIDE",
    "SYSTEM",
    "Pick",
    "sample_timestamps",
    "extract_frame",
    "extract_frames",
    "prompt",
    "parse_answer",
    "model",
    "classify",
]

# The -p profile spelling that opts in to per-file vision classification.
AUTO_PROFILE = "auto"

# Fallback profile whenever the model is unsure, answers invalidly, or fails.
DEFAULT_PROFILE = "av1BluRay"

# Allowed content kinds and the convert-video profile each maps to.
CONTENT_PROFILES: dict[str, str] = {
    "live_action": "av1BluRay",
    "animation_2d": "av1Animation",
    "cgi_3d": "av1BluRay",
    "heavy_grain": "av1Grain",
}

# What the model's requests are counted as in modelmeter.
PURPOSE = "profile"

# How many representative frames are sampled across each video (4-6), and the
# portion of the running time they span to skip black intros, logos and credits.
DEFAULT_SAMPLES = 5
SAMPLE_START_FRACTION = 0.12
SAMPLE_END_FRACTION = 0.88

# Maximum width or height of each extracted JPEG frame sent to the vision model.
MAX_FRAME_SIDE = 1024
FRAME_EXTRACT_TIMEOUT = 60

SYSTEM = """You classify video material from representative frames so a video \
encoder can pick the right encoding profile.

Classify the video into exactly one of these four kinds:
- "live_action": camera-shot live action (films, TV, documentaries, concerts, \
screencasts) with clean or normal film grain.
- "animation_2d": 2D animation (hand-drawn cel animation, anime, cartoons, \
vector/flash animation with flat colours and drawn outlines).
- "cgi_3d": 3D computer-generated animation (3D rendered characters and \
environments such as Pixar/DreamWorks-style features).
- "heavy_grain": live-action footage or a film scan with conspicuously heavy, \
coarse film grain or analog noise (e.g. grainy 16mm/35mm film scans).

If the frames are too dark, ambiguous or mixed to be confident, answer with \
"kind": null rather than guessing.

Answer with JSON only, in this form:
{"kind": "live_action" | "animation_2d" | "cgi_3d" | "heavy_grain" | null, \
"why": "<one short sentence>"}"""

_detail = runlog.detail

FrameExtractor = Callable[[str, str, str, str], bool]


@dataclass(frozen=True)
class Pick:
    """The encoding profile chosen for one video, with the kind and reason."""

    profile: str
    kind: str
    why: str
    fallback: bool = False


def sample_timestamps(duration: float, samples: int = DEFAULT_SAMPLES) -> list[str]:
    """``samples`` timestamps spread across ``duration`` seconds, skipping the
    first and last portions where intros, logos and credits sit.

    Returns ``[]`` when ``duration`` or ``samples`` is not positive.
    """
    if duration <= 0 or samples <= 0:
        return []
    if samples == 1:
        return ["%.3f" % (duration * 0.5)]
    span = SAMPLE_END_FRACTION - SAMPLE_START_FRACTION
    return [
        "%.3f" % (duration * (SAMPLE_START_FRACTION + span * index / (samples - 1)))
        for index in range(samples)
    ]


def extract_frame(source: str, timestamp: str, dest: str,
                  decode_accel_args: str = "") -> bool:
    """Extract one JPEG frame at ``timestamp`` from ``source`` to ``dest``,
    scaled down to fit within :data:`MAX_FRAME_SIDE` while keeping aspect ratio.

    Returns True only when ``dest`` exists and is non-empty.
    """
    scale = (
        "scale=w='min(%d,iw)':h='min(%d,ih)'"
        ":force_original_aspect_ratio=decrease"
        % (MAX_FRAME_SIDE, MAX_FRAME_SIDE)
    )
    argv = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
        *shlex.split(decode_accel_args),
        "-ss", timestamp, "-i", source,
        "-map", "0:v:0",
        "-frames:v", "1",
        "-vf", scale,
        "-q:v", "3",
        "-an", "-sn", "-dn",
        "-y", dest,
    ]
    try:
        subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=FRAME_EXTRACT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    try:
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return True
    except OSError:
        return False
    try:
        os.remove(dest)
    except OSError:
        pass
    return False


def extract_frames(source: str, duration: float, frames_dir: str, *,
                   samples: int = DEFAULT_SAMPLES,
                   decode_accel_args: str = "",
                   extractor: FrameExtractor = extract_frame) -> list[str]:
    """Extract up to ``samples`` representative frames from ``source`` into
    ``frames_dir`` and return the paths that succeeded."""
    timestamps = sample_timestamps(duration, samples)
    if not timestamps:
        return []
    os.makedirs(frames_dir, exist_ok=True)
    paths: list[str] = []
    for index, timestamp in enumerate(timestamps, start=1):
        if safety.abort_requested():
            break
        dest = os.path.join(frames_dir, "frame%02d.jpg" % index)
        if extractor(source, timestamp, dest, decode_accel_args):
            paths.append(dest)
    return paths


def prompt(frame_count: int) -> str:
    """The user prompt paired with the ``frame_count`` sampled frames."""
    return (
        "Look at the %d frame(s) above sampled across this video. "
        "Is it live action, 2D animation, 3D CGI or a heavy-grain film scan?\n\n"
        'Answer with JSON only: {"kind": "live_action" | "animation_2d" | '
        '"cgi_3d" | "heavy_grain" | null, "why": "<one short sentence>"}'
        % frame_count
    )


def parse_answer(answer: str) -> Pick:
    """Validate the model's JSON answer strictly against :data:`CONTENT_PROFILES`.

    Any missing JSON, missing ``kind``, ``null``, or unrecognised kind falls
    back cleanly to :data:`DEFAULT_PROFILE`.
    """
    verdict = llmclient.first_json(answer)
    if not isinstance(verdict, dict) or "kind" not in verdict:
        return Pick(DEFAULT_PROFILE, "", "model answer had no usable JSON kind",
                    fallback=True)
    why = " ".join(str(verdict.get("why") or "").split())
    raw_kind = verdict.get("kind")
    if raw_kind is None:
        return Pick(DEFAULT_PROFILE, "", why or "model was unsure", fallback=True)
    if not isinstance(raw_kind, str):
        return Pick(DEFAULT_PROFILE, "", "invalid kind %r" % (raw_kind,),
                    fallback=True)
    kind = raw_kind.strip().lower()
    profile = CONTENT_PROFILES.get(kind)
    if not profile:
        return Pick(DEFAULT_PROFILE, "", "unrecognised kind %r" % (raw_kind,),
                    fallback=True)
    return Pick(profile, kind, why, fallback=False)


def model(program: str,
          log: Callable[[str], None] = runlog.log) -> llmclient.Llm | None:
    """The vision model the roster selects for :data:`llmroster.VIDEO_PROFILE`,
    or None after refusing cleanly before any work starts."""
    llm = llmroster.require(program, llmroster.VIDEO_PROFILE)
    if llm is None:
        return None
    log("Video profile (-p auto): %s" % llm.describe())
    return llm


def classify(source: str, relative: str, duration: float, work_dir: str, *,
             models: Sequence[llmclient.Llm] | None = None,
             decode_accel_args: str = "",
             samples: int = DEFAULT_SAMPLES,
             meter_file: str = "",
             run_spans: modelmeter.Spans | None = None,
             run_tally: modelmeter.Tally | None = None,
             extractor: FrameExtractor = extract_frame,
             log: Callable[[str], None] = runlog.log) -> Pick:
    """Classify ``source`` from sampled frames and return its :class:`Pick`.

    Uses ``models`` in order (or the roster's ladder for
    :data:`llmroster.VIDEO_PROFILE` when ``models`` is None). Extracted frames
    are kept under ``work_dir`` and removed before returning. Usage is logged
    per file and recorded to ``meter_file`` / ``run_spans`` / ``run_tally``.
    """
    if models is None:
        models, why_none = llmroster.optional(llmroster.VIDEO_PROFILE)
        if not models:
            pick = Pick(DEFAULT_PROFILE, "", why_none or "no vision model available",
                        fallback=True)
            _report_pick(pick, relative, log)
            return pick

    frames_dir = os.path.join(work_dir, "profile_frames")
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    try:
        frames = extract_frames(
            source, duration, frames_dir, samples=samples,
            decode_accel_args=decode_accel_args, extractor=extractor,
        )
        if not frames:
            pick = Pick(DEFAULT_PROFILE, "", "could not extract sample frames",
                        fallback=True)
            _report_pick(pick, relative, log)
            return pick

        user_prompt = prompt(len(frames))
        for llm in models:
            try:
                with llm.serving(_detail) as bound:
                    if not bound.sees_images():
                        _detail("Video profile: %s does not see pictures"
                                % (bound.model or llm.name))
                        continue
                    with spans.timing():
                        answer = bound.ask(SYSTEM, user_prompt, tally,
                                           images=frames)
                    break
            except llmclient.LlmError as error:
                log("Video profile: %s failed for %s: %s"
                    % (llm.name, relative, error))
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
        if tally.requests:
            log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
            if run_spans is not None:
                run_spans.merge(spans)
            if run_tally is not None:
                run_tally.merge(tally)
            if meter_file:
                modelmeter.record(meter_file, spans, tally)

    if not answer:
        pick = Pick(DEFAULT_PROFILE, "", "no vision model answered", fallback=True)
    else:
        pick = parse_answer(answer)
    _report_pick(pick, relative, log)
    return pick


def _report_pick(pick: Pick, relative: str,
                 log: Callable[[str], None]) -> None:
    if pick.fallback:
        log("Video profile: %s (default fallback - %s): %s"
            % (pick.profile, pick.why, relative))
    else:
        detail = " - " + pick.why if pick.why else ""
        log("Video profile: %s (%s%s): %s"
            % (pick.profile, pick.kind, detail, relative))
