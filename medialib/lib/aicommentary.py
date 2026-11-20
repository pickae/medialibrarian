"""Asking a language model to judge an audio track that may be a commentary,
when :func:`medialib.lib.commentarydetection.judge` came back unsure.

The commentary probe already listened to the film's main audio track and the
suspect track over the same three windows and kept what whisper heard in each.
When only one of the two signals fires - more speech than the film, or words
the film does not have - the heuristic leaves the track alone. A reasoning
model can usually tell from those two transcripts, together with the tracks'
names and languages, what the suspect track really is:

* ``commentary`` - people talking about the film over ducked film audio;
* ``description`` - audio description for the visually impaired (a narrator
  describing visual action in pauses between the film's lines);
* ``dub`` - the film's own dialogue spoken in another language or re-voiced;
* ``score`` - an isolated musical score;
* ``music_and_effects`` - music and sound effects without the dialogue;
* ``unsure`` - not enough in the transcripts to tell.

The guard is strict: the run acts ONLY on a high-confidence ``commentary`` or
``description``. Every other answer - any lower confidence, or any other kind
- leaves the track unflagged, and adds the model's opinion to the line the run
prints for somebody to listen to.

The task is the roster's :data:`llmroster.COMMENTARY_JUDGE`, which needs no
web search and prefers reasoning. The :data:`SWITCH_VARIABLE` setting turns it
off. What the model was asked is counted (:mod:`medialib.lib.modelmeter`),
said under the film, and recorded for the run's total.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import NamedTuple

from medialib.lib import (
    commentarydetection,
    llmclient,
    llmroster,
    modelmeter,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "KINDS",
    "CONFIDENCES",
    "ACTED_KINDS",
    "TrackSample",
    "Opinion",
    "Usage",
    "enabled",
    "prompt",
    "parse",
    "judge",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "commentaryJudgeLlm"

# What the model's requests are counted under.
PURPOSE = "commentary"

# Every kind the model may answer with, and the confidence levels.
KINDS = ("commentary", "dub", "description", "score", "music_and_effects",
         "unsure")
CONFIDENCES = ("high", "medium", "low")

# The only kinds the run acts on automatically, and only at high confidence.
ACTED_KINDS = ("commentary", "description")

# Bounds on what goes into a prompt and what comes out into a log line.
MAX_TRANSCRIPT_CHARS = 4000
MAX_REASON_CHARS = 240

_KIND_LABELS = {
    "commentary": "commentary",
    "dub": "dub",
    "description": "audio description",
    "score": "isolated score",
    "music_and_effects": "music and effects",
    "unsure": "unsure",
}

SYSTEM = """You classify an ambiguous secondary audio track of a movie for a \
personal video library. You are given whisper transcripts sampled over the \
same three windows of the film's main audio track and the suspect track, \
along with each track's name, container language tag, detected language and \
speech share.

Classify the suspect track as one of:
- "commentary": an audio commentary (filmmakers, cast or critics talking \
about making the film, its scenes, actors or history, usually over ducked \
film audio).
- "description": an audio description for the visually impaired (the film's \
dialogue interspersed with a narrator describing visual action, settings or \
movements in the present tense).
- "dub": a dubbed or alternate-language soundtrack (the film's own dialogue \
spoken in another language or re-voiced, matching the drama rather than \
discussing it).
- "score": an isolated musical score (music only, perhaps with occasional \
lyrics or whisper hallucinations).
- "music_and_effects": a music-and-effects track (music and sound effects \
with dialogue removed, perhaps with faint bleed or whisper hallucinations).
- "unsure": the transcripts do not give enough evidence to tell.

Rate your confidence as "high", "medium" or "low". A wrong "commentary" or \
"description" flag hurts more than leaving a track unflagged: use "high" ONLY \
when the transcripts make the kind unmistakable.

Answer with JSON only, in this form:
{"kind": "commentary|dub|description|score|music_and_effects|unsure", \
"confidence": "high|medium|low", \
"reason": "one brief sentence explaining what in the transcripts shows it"}"""

_log = runlog.log
_detail = runlog.detail


class TrackSample(NamedTuple):
    """What is known about one probed audio track: its container name and
    language tag, and the probe's measurement."""

    name: str
    language: str
    measure: commentarydetection.Measure


class Opinion(NamedTuple):
    """The model's judgement on one suspect audio track."""

    kind: str
    confidence: str
    reason: str

    @property
    def confident(self) -> bool:
        """Whether the model is confident enough for the run to act on it."""
        return self.confidence == "high"

    @property
    def acted_kind(self) -> str:
        """The kind to act on automatically (``commentary`` or
        ``description``), or "" when the track is to be left unflagged."""
        if self.confident and self.kind in ACTED_KINDS:
            return self.kind
        return ""

    def describe(self) -> str:
        """One short clause saying the model's opinion, for a log line or the
        end-of-run list."""
        label = _KIND_LABELS.get(self.kind, self.kind)
        text = "model thinks %s (%s confidence)" % (label, self.confidence)
        if self.reason:
            text += ": " + self.reason
        return text


class Usage:
    """What the model was asked to judge ambiguous audio tracks over a run:
    the requests and tokens (:class:`modelmeter.Tally`) and the stretches of
    time it was at work (:class:`modelmeter.Spans`)."""

    def __init__(self) -> None:
        self.tally = modelmeter.Tally()
        self.spans = modelmeter.Spans()

    @property
    def requests(self) -> int:
        return self.tally.requests

    @property
    def seconds(self) -> float:
        return self.spans.seconds

    def merge(self, other: Usage) -> None:
        self.tally.merge(other.tally)
        self.spans.merge(other.spans)

    def line(self) -> str:
        """The usage formatted with :func:`modelmeter.rate_line`; "" when the
        model was never asked."""
        if not self.requests:
            return ""
        return modelmeter.rate_line(PURPOSE, self.seconds, self.tally)


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def _clean_text(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit - 3].rstrip() + "..."


def _clean_meta(value: str) -> str:
    value = (value or "").strip()
    return "" if value.lower() == "null" else value


def _track_section(heading: str, sample: TrackSample) -> str:
    name = _clean_meta(sample.name) or "(none)"
    tag_lang = _clean_meta(sample.language) or "(none)"
    heard_lang = _clean_meta(sample.measure.language) or "(none)"
    transcript = _clean_text(sample.measure.text, MAX_TRANSCRIPT_CHARS) or "(empty)"
    return (
        "## %s\n\n"
        "Track name: %s\n"
        "Container language tag: %s\n"
        "Whisper detected language: %s\n"
        "Speech share across sampled windows: %.2f\n\n"
        "Transcript:\n%s" % (
            heading, name, tag_lang, heard_lang, sample.measure.speech,
            transcript))


def prompt(main: TrackSample, suspect: TrackSample, *, film: str = "",
           why: str = "") -> str:
    """What the model is told about the film's ``main`` track and the
    ``suspect`` track over the same sampled windows."""
    intro = ["Classify the suspect audio track."]
    if film:
        intro.append("Film: %s" % film)
    if why:
        intro.append("Why the heuristic was unsure: %s" % why)
    sections = [
        "\n".join(intro),
        _track_section("Main audio track", main),
        _track_section("Suspect audio track", suspect),
        'Answer with JSON only: {"kind": "...", "confidence": "...", '
        '"reason": "..."}',
    ]
    return "\n\n".join(sections) + "\n"


def parse(answer: str) -> Opinion | None:
    """The :class:`Opinion` in a model's answer, or None when the answer has
    no valid JSON object with an allowed kind and confidence."""
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return None
    raw_kind = found.get("kind")
    raw_confidence = found.get("confidence")
    if not isinstance(raw_kind, str) or not isinstance(raw_confidence, str):
        return None
    kind = raw_kind.strip().lower()
    confidence = raw_confidence.strip().lower()
    if kind not in KINDS or confidence not in CONFIDENCES:
        return None
    raw_reason = found.get("reason")
    reason = _clean_text(raw_reason, MAX_REASON_CHARS) if isinstance(
        raw_reason, str) else ""
    return Opinion(kind, confidence, reason)


def judge(main: TrackSample, suspect: TrackSample, *, film: str = "",
          why: str = "", models: Sequence[llmclient.Llm] | None = None,
          usage: Usage | None = None,
          meter_file: str = "") -> Opinion | None:
    """Ask a language model what ``suspect`` is, held against ``main`` over
    the same sampled windows; None when the switch is off, no model can be
    had, every model failed, or the answer could not be read.

    ``models`` are the models to try, best first - the roster's ladder for
    :data:`llmroster.COMMENTARY_JUDGE` unless given. What the model was asked
    is said under the film, merged into ``usage`` when given, and recorded in
    ``meter_file`` when given (:func:`modelmeter.record`)."""
    if not enabled():
        _detail("    No model asked about the suspect track: the %s setting "
                "is off" % SWITCH_VARIABLE)
        return None
    if models is None:
        models, why_none = llmroster.optional(llmroster.COMMENTARY_JUDGE)
        if not models:
            _detail("    No model asked about the suspect track: %s" % why_none)
            return None
    request = prompt(main, suspect, film=film, why=why)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    try:
        for llm in models:
            try:
                with llm.serving(_detail) as bound:
                    with spans.timing():
                        answer = bound.ask(SYSTEM, request, tally)
                    break
            except llmclient.LlmError as error:
                _log("    No commentary judgement from %s: %s"
                     % (llm.name, error))
    finally:
        if tally.requests:
            _log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if usage is not None:
            usage.spans.merge(spans)
            usage.tally.merge(tally)
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    if not answer:
        return None
    opinion = parse(answer)
    if opinion is None:
        _log("    The commentary judgement gave no readable answer")
        return None
    return opinion
