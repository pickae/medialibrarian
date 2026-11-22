"""Asking a light language model what kind of audio a recording is before the
catalogue cover search (:mod:`medialib.lib.onlinecover`) runs.

The deterministic heuristic (:func:`onlinecover.probable_content`) guesses from
running time and channel count alone: 30 to 200 minutes in mono as a podcast,
in stereo as an audio drama, longer as an audiobook, shorter as other. That
cannot see clues already sitting on disk: folder names such as ``Hörspiele`` or
``Podcasts``, file names such as ``CD1/Kapitel 01.mp3`` or ``Folge 12``, or
tags such as ``genre: Hörspiel`` or ``artist`` / ``album``.

When enabled (:data:`SWITCH_VARIABLE`, ``contentKindLlm``) and a model is
available for :data:`llmroster.AUDIO_CONTENT` (no special traits required,
leaning light), the model is shown the folder path and file names, sampled
track tags, duration, and channel count, and answers strictly in JSON with one
of :data:`onlinecover.CONTENTS` (``podcast``, ``drama``, ``audiobook``,
``music``, ``classical``, ``other``) and a confidence level. On ``other``, low
confidence, invalid JSON, a model error, the switch being off, or no model
being available, this returns
``None`` so the caller falls back cleanly to :func:`onlinecover.probable_content`.

What the model was asked is counted (:mod:`medialib.lib.modelmeter`), logged,
and recorded for the run's total when ``meter_file`` is given.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from medialib.lib import (
    formatting,
    llmclient,
    llmroster,
    modelmeter,
    onlinecover,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "CONFIDENCES",
    "USABLE_CONFIDENCES",
    "SYSTEM",
    "Verdict",
    "enabled",
    "prompt",
    "parse",
    "classify",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "contentKindLlm"

# Label used in modelmeter rate lines.
PURPOSE = "content"

# How much of a folder is listed for the model, and how many tracks' tags.
MAX_LISTED_FILES = 100
MAX_TAGGED_TRACKS = 6
MAX_TAG_CHARS = 300
MAX_WHY_CHARS = 200

CONFIDENCES = ("high", "medium", "low")
USABLE_CONFIDENCES = ("high", "medium")

SYSTEM = """You classify audio recordings for a personal audio library so a \
cover search queries the right catalogues.

Choose one of these six exact values for "content":
- "podcast": a podcast episode, interview, talk show, panel discussion, radio \
or web talk show, or YouTube commentary.
- "drama": a full-cast audio drama, radio play, or Hoerspiel (a dramatised \
story with voice actors, sound effects, or an audio drama series such as ARD \
Radio Tatort, BBC Radio drama, Die drei ???, etc.).
- "audiobook": a narrated book (one or a few narrators reading a novel, \
non-fiction book, or story collection; often split into chapters, CDs, or \
parts).
- "music": a music album or single that is not classical: pop, rock, jazz, \
folk, electronic, film score, a song collection.
- "classical": classical music (a symphony, concerto, sonata, opera, \
chamber or choral work, early or contemporary art music), where a release is \
known by its composer, work and performers - soloist, conductor, orchestra.
- "other": a standalone lecture, comedy album, soundscape, or anything that \
does not clearly fit the kinds above (or when you cannot tell).

Judge from all clues together: parent folder names, folder/file names, track \
tags (genre, album, artist, comment, publisher), running time, and channel \
count. Do not assume length or stereo alone decides the category. A wrong \
classification sends the search to the wrong catalogues, so report "low" \
confidence or "other" when you are unsure.

Answer with JSON only, in this exact form:
{"content": "podcast|drama|audiobook|music|classical|other", \
"confidence": "high|medium|low", \
"why": "one short sentence"}"""

_log = runlog.log
_detail = runlog.detail


@dataclass(frozen=True)
class Verdict:
    """One validated classification parsed from the model's JSON answer."""

    content: str
    confidence: str
    why: str = ""


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def _norm_slash(path: str) -> str:
    return path.replace("\\", "/").replace(os.sep, "/")


def _listing(folder_path: str) -> list[str]:
    """Relative file paths below ``folder_path`` with sizes, capped at
    :data:`MAX_LISTED_FILES`."""
    lines: list[str] = []
    total = 0
    for parent, dirs, names in os.walk(folder_path):
        dirs.sort()
        for name in sorted(names):
            total += 1
            if len(lines) >= MAX_LISTED_FILES:
                continue
            path = os.path.join(parent, name)
            try:
                size = formatting.fmt_bytes(os.path.getsize(path))
            except OSError:
                size = "?"
            rel = _norm_slash(os.path.relpath(path, folder_path))
            lines.append("%s (%s)" % (rel, size))
    if total > len(lines):
        lines.append("... and %d more file(s)" % (total - len(lines)))
    return lines


def _sampled(tracks: Sequence[str]) -> list[str]:
    """Up to :data:`MAX_TAGGED_TRACKS` of ``tracks``: the first ones and the
    last, where parts of a series or multi-disc book differ."""
    if len(tracks) <= MAX_TAGGED_TRACKS:
        return list(tracks)
    head = MAX_TAGGED_TRACKS - 1
    return [*tracks[:head], tracks[-1]]


def _display_track(track: str, folder_path: str) -> str:
    if folder_path:
        try:
            rel = os.path.relpath(track, folder_path)
            if not rel.startswith(".."):
                return _norm_slash(rel)
        except ValueError:
            pass
    return os.path.basename(track)


def _tag_lines(folder_path: str, tracks: Sequence[str],
               probe: Callable[[str], dict]) -> list[str]:
    lines: list[str] = []
    for track in _sampled(tracks):
        tags = {key: " ".join(str(value).split())[:MAX_TAG_CHARS]
                for key, value in probe(track).items() if str(value).strip()}
        label = _display_track(track, folder_path)
        if not tags:
            lines.append("%s: no tags" % label)
            continue
        lines.append("%s:" % label)
        lines.extend("  %s: %s" % (key, value)
                     for key, value in sorted(tags.items()))
    return lines


def prompt(name: str, *, folder_path: str = "",
           tracks: Sequence[str] = (),
           seconds: float = 0.0, stereo: bool = False,
           heuristic: tuple[str, str] | None = None,
           list_folder: bool = True,
           probe: Callable[[str], dict] = onlinecover.probe_tags) -> str:
    """What the model is told about the recording to classify it."""
    folder_lines: list[str] = []
    if folder_path:
        norm = os.path.normpath(folder_path)
        parts = [p for p in _norm_slash(norm).split("/") if p]
        folder_name = parts[-1] if parts else ""
        parents = parts[-4:-1]
        if folder_name:
            folder_lines.append("Folder name: %s" % folder_name)
        folder_lines.append("Parent folders (outermost first): %s"
                            % (" / ".join(parents) or "(none)"))
        if list_folder and os.path.isdir(folder_path):
            listing = _listing(folder_path)
            if listing:
                folder_lines.append("\nFiles in folder:\n"
                                    + "\n".join("- " + line for line in listing))
        elif tracks:
            folder_lines.append(
                "\nAudio file(s):\n"
                + "\n".join("- " + _display_track(t, folder_path)
                            for t in tracks[:MAX_LISTED_FILES]))
    elif tracks:
        folder_lines.append(
            "Audio file(s):\n"
            + "\n".join("- " + os.path.basename(t)
                        for t in tracks[:MAX_LISTED_FILES]))
    else:
        folder_lines.append("(no folder path given)")

    duration = formatting.fmt_clock(seconds) if seconds > 0 else "unknown"
    channels = "stereo (2 or more channels)" if stereo else "mono (or unknown)"
    meta_lines = [
        "Running time: %s" % duration,
        "Channels: %s" % channels,
    ]
    if heuristic is not None:
        h_content, h_why = heuristic
        meta_lines.append(
            "Fallback length/channel heuristic: %s%s (only a fallback - "
            "ignore it when the folder, file names, or tags say otherwise)"
            % (h_content, " (%s)" % h_why if h_why else ""))

    tag_text = "\n".join(_tag_lines(folder_path, tracks, probe)) \
        if tracks else "(no audio files)"

    sections = [
        "Classify this audio recording.",
        "## Recording\n\nName: %s\n%s" % (name or "(unnamed)",
                                          "\n".join(folder_lines)),
        "## Audio file tags\n\n%s" % tag_text,
        "## Technical properties\n\n%s" % "\n".join(meta_lines),
    ]
    return "\n\n".join(sections) + "\n"


def parse(answer: str) -> Verdict | None:
    """Strictly parse and validate the model's JSON answer, or return None."""
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return None
    raw_content = found.get("content")
    if not isinstance(raw_content, str):
        return None
    content = raw_content.strip().lower()
    if content not in onlinecover.CONTENTS:
        return None
    raw_confidence = found.get("confidence")
    if not isinstance(raw_confidence, str):
        return None
    confidence = raw_confidence.strip().lower()
    if confidence not in CONFIDENCES:
        return None
    raw_why = found.get("why")
    why = (" ".join(raw_why.split())[:MAX_WHY_CHARS]
           if isinstance(raw_why, str) else "")
    return Verdict(content=content, confidence=confidence, why=why)


def classify(name: str, *, folder_path: str = "",
             tracks: Sequence[str] = (),
             seconds: float = 0.0, stereo: bool = False,
             heuristic: tuple[str, str] | None = None,
             list_folder: bool = True,
             classifiers: Sequence[llmclient.Llm] | None = None,
             meter_file: str = "",
             probe: Callable[[str], dict] = onlinecover.probe_tags
             ) -> tuple[str, str] | None:
    """Classify the recording via :data:`llmroster.AUDIO_CONTENT`, returning
    ``(content, why)`` when confident about any kind but ``other``, or
    ``None`` to fall back to
    :func:`onlinecover.probable_content`."""
    if not enabled():
        _detail("    No model asked for content kind: the %s setting is off"
                % SWITCH_VARIABLE)
        return None
    if classifiers is None:
        classifiers, why_none = llmroster.optional(llmroster.AUDIO_CONTENT)
        if not classifiers:
            _detail("    No model asked for content kind: %s" % why_none)
            return None
    request = prompt(
        name, folder_path=folder_path, tracks=tracks, seconds=seconds,
        stereo=stereo, heuristic=heuristic, list_folder=list_folder,
        probe=probe)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    used = ""
    try:
        for llm in classifiers:
            try:
                with llm.serving(_detail) as bound:
                    with spans.timing():
                        answer = bound.ask(SYSTEM, request, tally)
                    used = bound.model or llm.name
                break
            except llmclient.LlmError as error:
                _log("    No content kind from %s: %s" % (llm.name, error))
    finally:
        if tally.requests:
            _log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    if not answer:
        return None
    model_label = used or "model"
    verdict = parse(answer)
    if verdict is None:
        _log("    Content kind from %s was not usable: falling back to the "
             "length heuristic" % model_label)
        return None
    detail = ": " + verdict.why if verdict.why else ""
    if verdict.confidence not in USABLE_CONFIDENCES:
        _log("    Content kind from %s uncertain (%s, %s confidence%s): "
             "falling back to the length heuristic"
             % (model_label, verdict.content, verdict.confidence, detail))
        return None
    if verdict.content == "other":
        _log("    Content kind from %s is other (%s confidence%s): "
             "falling back to the length heuristic"
             % (model_label, verdict.confidence, detail))
        return None
    _log("    Content kind from %s: %s (%s confidence%s)"
         % (model_label, verdict.content, verdict.confidence, detail))
    reason = "the model: %s" % verdict.why if verdict.why else "the model"
    return verdict.content, reason
