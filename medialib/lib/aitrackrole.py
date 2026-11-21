"""Asking a light language model to classify a subtitle or audio track's role
where the deterministic rules cannot settle it.

Container flags, track names, subtitle cue counts and acoustic commentary
signals settle most tracks on their own, and the model is never called for
those. Only when the deterministic counting or tag check could not be run
(for example, no usable reference track, or an unrecognised descriptive track
title) or sits near its threshold (a cue ratio near the forced/full boundary,
an SDH marker share near the SDH boundary, or an inconclusive commentary
probe) is a light model asked through :data:`llmroster.TRACK_ROLE`.

The model is given the track's title, language, container flags, whatever
measurements exist, and sampled subtitle cues or a short audio transcript. Its
answer is validated strictly against :data:`enums.SUBTITLE_ROLES` or
:data:`enums.AUDIO_ROLES` (plus ``"unsure"``), and acted on only when the
model reports high confidence. On ``"unsure"``, lower confidence, malformed
JSON, a model error, no configured model, or ``trackRoleLlm=off``, the
deterministic result stands unchanged.

Every model decision is logged, and every request is counted with
:mod:`medialib.lib.modelmeter`.
"""

from __future__ import annotations

import functools
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from medialib.lib import (
    enums,
    languages,
    llmclient,
    llmroster,
    modelmeter,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "SUBTITLE_ROLES",
    "AUDIO_ROLES",
    "UNSURE",
    "CONFIDENCES",
    "HIGH_CONFIDENCE",
    "COUNT_MARGIN",
    "SDH_BORDERLINE_LOW",
    "SDH_BORDERLINE_HIGH",
    "MIN_REFERENCE_CUES",
    "MAX_REFERENCE_CUES",
    "MAX_SAMPLED_CUES",
    "MAX_CUE_CHARS",
    "MAX_TRANSCRIPT_CHARS",
    "SYSTEM",
    "Verdict",
    "enabled",
    "sample_cues",
    "sample_transcript",
    "subtitle_borderline_reason",
    "audio_borderline_reason",
    "subtitle_prompt",
    "audio_prompt",
    "parse_verdict",
    "is_actionable",
    "classify_subtitle",
    "classify_audio",
    "resolve_subtitle_role",
    "resolve_audio_role",
]

# Whether the model is asked about borderline track roles: on unless set off.
SWITCH_VARIABLE = "trackRoleLlm"

# What the model's requests are counted under.
PURPOSE = "track-role"

SUBTITLE_ROLES = enums.SUBTITLE_ROLES
AUDIO_ROLES = enums.AUDIO_ROLES
UNSURE = "unsure"
CONFIDENCES = ("low", "medium", "high")
HIGH_CONFIDENCE = "high"

# Mirrors subtitletracks.MIN_REFERENCE_CUES / MAX_REFERENCE_CUES without a
# circular import.
MIN_REFERENCE_CUES = 100
MAX_REFERENCE_CUES = 5000

# Margins around the deterministic thresholds where the heuristic is uncertain:
# - subtitle cue ratio near FORCED_CUE_SHARE (0.25) or FULL_CUE_SHARE (0.60)
# - subtitle SDH marker share near SDH_CUE_SHARE (0.10)
# - audio speech gain / new-word share near commentarydetection thresholds
COUNT_MARGIN = 0.10
SDH_BORDERLINE_LOW = 0.05
SDH_BORDERLINE_HIGH = 0.16

AUDIO_MIN_SPEECH = 0.60
AUDIO_MIN_SPEECH_GAIN = 0.10
AUDIO_MIN_NEW_WORDS = 0.50
AUDIO_SPEECH_MARGIN = 0.05
AUDIO_WORDS_MARGIN = 0.15

# How much text is sent to the model.
MAX_SAMPLED_CUES = 24
MAX_CUE_CHARS = 180
MAX_TRANSCRIPT_CHARS = 2400
MAX_WHY_CHARS = 240

_SDH_CUE = re.compile("|".join(enums.SDH_CUE_MARKERS), re.M)

# Generic codec, channel, region and format words that commonly appear in a
# plain track title without implying a special role.
_PLAIN_TITLE_WORDS = frozenset((
    "aac", "ac", "america", "american", "ass", "atmos", "audio", "bit",
    "brazil", "brazilian", "canadian", "castilian", "ch", "channel",
    "channels", "complete", "dd", "ddp", "default", "dialog", "dialogue",
    "digital", "dolby", "dts", "dub", "dubbed", "eac", "eu", "european",
    "extended", "flac", "full", "hd", "hi", "kbps", "khz", "latin",
    "lossless", "lpcm", "ma", "main", "mono", "mpeg", "normal", "original",
    "pcm", "pgs", "plus", "regular", "remastered", "res", "simplified", "srt",
    "ssa", "standard", "stereo", "sub", "subrip", "subs", "subtitle",
    "subtitles", "surround", "theatrical", "track", "traditional", "truehd",
    "uk", "us", "utf", "version", "vobsub", "vtt", "webvtt",
))

SYSTEM = """You classify the role of a single subtitle or audio track for a \
personal media library. You are given the track's title, language, container \
flags, any measurements that were taken, and sampled subtitle cues or a short \
transcript excerpt.

Allowed subtitle roles:
- full: the film's ordinary dialogue (with at most rare signs or lyrics)
- sdh: dialogue for the deaf and hard of hearing, regularly including \
non-speech sounds ([door slams], (gasps)), music cues, or speaker labels
- forced: only on-screen signs, titles, or occasional foreign-language lines
- commentary: a transcript of a commentary track rather than the film's script

Allowed audio roles:
- main: the film's soundtrack (original language or dubbed dialogue)
- commentary: people talking over the film about its making, not the film's \
dialogue
- description: audio description for the blind / visually impaired — a \
narrator describing visual action between or over the film's dialogue
- effects: music and effects only (M&E), without spoken dialogue
- score: isolated musical score only
- interview: a spoken interview or Q&A extra

Rules:
1. Pick ONLY from the allowed roles for the track kind given, or "unsure".
2. A wrong reclassification is worse than keeping the current role. Use \
"high" confidence ONLY when the evidence clearly settles the role; otherwise \
use "medium" or "low" confidence, or role "unsure".
3. Never guess a special role from the language alone.

Answer with JSON only, in this exact shape:
{"role": "<allowed role or unsure>", "confidence": "high"|"medium"|"low", \
"why": "<one concise sentence>"}"""

_log = runlog.log
_detail = runlog.detail


@dataclass(frozen=True)
class Verdict:
    """One validated classification from the model."""

    role: str
    confidence: str
    why: str = ""


def enabled() -> bool:
    """Whether borderline track roles may be sent to a language model: on
    unless :data:`SWITCH_VARIABLE` says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def _clean_name(name: str) -> str:
    text = (name or "").strip()
    return "" if shell_lower(text) == "null" else text


def _fold_word(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", shell_lower(word))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@functools.cache
def _language_words() -> frozenset[str]:
    """Lower-cased, accent-folded words that name a language in ISO 639 or
    the track-language table."""
    words: set[str] = set()
    for row in languages.recognised():
        for raw in (row.code2, row.code3, row.code3b, row.sub_word,
                    *row.keywords):
            for token in re.findall(r"[^\W\d_]+", raw):
                folded = _fold_word(token)
                if folded:
                    words.add(folded)
    import pycountry
    for entry in pycountry.languages:
        raw_name = getattr(entry, "name", "")
        for token in re.findall(r"[^\W\d_]+", raw_name):
            folded = _fold_word(token)
            if folded:
                words.add(folded)
    return frozenset(words)


def _is_descriptive_title(name: str) -> bool:
    """Whether ``name`` carries substantive words beyond a language name and
    ordinary technical/channel tags — suggesting a role phrasing the regex
    patterns did not recognise."""
    clean = _clean_name(name)
    if not clean:
        return False
    lang_words = _language_words()
    remaining = []
    for token in re.findall(r"[^\W\d_]+", clean):
        folded = _fold_word(token)
        if len(folded) <= 1:
            continue
        if folded in lang_words or folded in _PLAIN_TITLE_WORDS:
            continue
        remaining.append(folded)
    return bool(remaining)


def _sdh_share(cues: Sequence[str]) -> float:
    non_empty = [cue for cue in cues if cue.strip()]
    if not non_empty:
        return 0.0
    marked = sum(1 for cue in non_empty if _SDH_CUE.search(cue))
    return marked / len(non_empty)


def sample_cues(cues: Sequence[str], limit: int = MAX_SAMPLED_CUES) -> list[str]:
    """Up to ``limit`` non-empty subtitle cues sampled evenly across the track,
    each trimmed to :data:`MAX_CUE_CHARS` characters."""
    cleaned = [" ".join(cue.split())[:MAX_CUE_CHARS]
               for cue in cues if cue and cue.strip()]
    if len(cleaned) <= limit or limit <= 0:
        return cleaned[:max(limit, 0)]
    if limit == 1:
        return [cleaned[len(cleaned) // 2]]
    last = len(cleaned) - 1
    indices = [round(i * last / (limit - 1)) for i in range(limit)]
    return [cleaned[at] for at in indices]


def sample_transcript(text: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """A compact excerpt of ``text`` up to ``max_chars`` characters, taking
    start, middle and end slices when the transcript is longer."""
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars or max_chars <= 0:
        return normalized[:max(max_chars, 0)]
    separator = " [...] "
    budget = max_chars - 2 * len(separator)
    if budget < 30:
        return normalized[:max_chars]
    chunk = budget // 3
    mid_start = max(chunk, (len(normalized) - chunk) // 2)
    parts = (
        normalized[:chunk].rstrip(),
        normalized[mid_start:mid_start + chunk].strip(),
        normalized[-chunk:].lstrip(),
    )
    return separator.join(parts)


def subtitle_borderline_reason(
    role: str,
    cues: int = 0,
    reference_cues: int = 0,
    texts: Sequence[str] | None = None,
    *,
    name: str = "",
    flagged: bool = False,
    min_reference_cues: int = MIN_REFERENCE_CUES,
    max_reference_cues: int = MAX_REFERENCE_CUES,
) -> str:
    """Why a subtitle track's deterministic role is borderline or unmeasurable,
    or ``""`` when it is already clearly settled (or lacks evidence to check)."""
    if role == "commentary":
        return ""
    non_empty = [cue for cue in texts if cue and cue.strip()] if texts is not None else []
    clean_name = _clean_name(name)
    descriptive = _is_descriptive_title(clean_name)
    if not non_empty and not descriptive:
        return ""

    measured = (cues > 0
                and min_reference_cues <= reference_cues <= max_reference_cues)
    sdh_share = _sdh_share(non_empty) if non_empty else None

    if measured:
        ratio = cues / reference_cues
        if role == "forced":
            if enums.FORCED_CUE_SHARE <= ratio < enums.FULL_CUE_SHARE + COUNT_MARGIN:
                return "cue ratio %.2f sits near the forced/full threshold" % ratio
            return ""
        if role in ("full", "sdh"):
            low = enums.FORCED_CUE_SHARE - COUNT_MARGIN
            high = enums.FORCED_CUE_SHARE + COUNT_MARGIN
            if low < ratio < high:
                return "cue ratio %.2f sits near the forced threshold" % ratio
            if ratio <= low:
                return ""
            if (role == "full" and sdh_share is not None
                    and SDH_BORDERLINE_LOW < sdh_share < SDH_BORDERLINE_HIGH):
                return ("SDH marker share %.0f%% sits near the SDH threshold"
                        % (sdh_share * 100))
            if role == "full" and not flagged and descriptive:
                return "unrecognised descriptive track title %r" % clean_name
            return ""
        return ""

    # Counting test could not be run.
    if (role == "full" and sdh_share is not None
            and SDH_BORDERLINE_LOW < sdh_share < SDH_BORDERLINE_HIGH):
        return ("SDH marker share %.0f%% sits near the SDH threshold"
                % (sdh_share * 100))
    if role == "forced":
        if non_empty:
            return "count test unavailable for forced-marked track"
        return ""
    if role == "sdh":
        return ""
    if role != "full":
        return ""

    effective_cues = cues if cues > 0 else len(non_empty)
    if sdh_share is not None and sdh_share >= SDH_BORDERLINE_HIGH:
        if 0 < cues < min_reference_cues and reference_cues > 0:
            return "reference cue count %d outside measurable range" % reference_cues
        return ""
    if non_empty:
        if reference_cues > max_reference_cues or 0 < reference_cues < min_reference_cues:
            return "reference cue count %d outside measurable range" % reference_cues
        if 0 < effective_cues < min_reference_cues:
            return ("count test unavailable (%d cues, no measurable reference)"
                    % effective_cues)
    if not flagged and descriptive:
        return "unrecognised descriptive track title %r" % clean_name
    return ""


def audio_borderline_reason(
    role: str,
    *,
    name: str = "",
    transcript: str = "",
    speech: float | None = None,
    main_speech: float | None = None,
    new_words: float | None = None,
    unsure: bool = False,
    flagged: bool = False,
) -> str:
    """Why an audio track's deterministic role is borderline or unmeasurable,
    or ``""`` when it is already clearly settled (or lacks evidence to check)."""
    if role != "main" or flagged:
        return ""
    clean_name = _clean_name(name)
    has_transcript = bool((transcript or "").strip())
    descriptive = _is_descriptive_title(clean_name)
    if not has_transcript and not descriptive:
        return ""
    if unsure:
        return "acoustic commentary signals were inconclusive"

    if speech is not None and main_speech is not None and new_words is not None:
        gain = speech - main_speech
        talked_over = (speech >= AUDIO_MIN_SPEECH
                       and gain >= AUDIO_MIN_SPEECH_GAIN)
        other_words = new_words >= AUDIO_MIN_NEW_WORDS
        if talked_over != other_words:
            return ("acoustic commentary signals split (speech gain %.2f, "
                    "new words %.0f%%)" % (gain, new_words * 100))
        near_gain = (speech >= AUDIO_MIN_SPEECH - 0.10
                     and abs(gain - AUDIO_MIN_SPEECH_GAIN) <= AUDIO_SPEECH_MARGIN)
        near_words = abs(new_words - AUDIO_MIN_NEW_WORDS) <= AUDIO_WORDS_MARGIN
        if near_gain:
            return "speech gain %.2f sits near the commentary threshold" % gain
        if near_words:
            return ("new-word share %.0f%% sits near the commentary threshold"
                    % (new_words * 100))
        if descriptive:
            return "unrecognised descriptive track title %r" % clean_name
        return ""

    if has_transcript:
        return "no acoustic comparison available for audio transcript"
    if descriptive:
        return "unrecognised descriptive track title %r" % clean_name
    return ""


def _format_flags(flags: Mapping[str, bool] | None) -> str:
    if not flags:
        return "(none)"
    active = [key for key, value in sorted(flags.items()) if value]
    return ", ".join(active) if active else "(none)"


def subtitle_prompt(
    *,
    role: str,
    name: str = "",
    language: str = "",
    flags: Mapping[str, bool] | None = None,
    cues: int = 0,
    reference_cues: int = 0,
    texts: Sequence[str] | None = None,
    reason: str = "",
) -> str:
    """The prompt describing a borderline subtitle track for the model."""
    clean_name = _clean_name(name)
    non_empty = [cue for cue in texts if cue and cue.strip()] if texts is not None else []
    sampled = sample_cues(non_empty)
    lines = [
        "Classify this subtitle track's role.",
        "Allowed roles: %s, or %s." % (", ".join(SUBTITLE_ROLES), UNSURE),
        "",
        "## Track metadata",
        "Title: %s" % (clean_name or "(none)"),
        "Language: %s" % ((language or "").strip() or "(unknown)"),
        "Flags set: %s" % _format_flags(flags),
        "Current deterministic role: %s" % role,
    ]
    if reason:
        lines.append("Why this track is being checked: %s" % reason)

    lines.append("")
    lines.append("## Measurements")
    effective_cues = cues if cues > 0 else len(non_empty)
    lines.append("Cue count: %s" % (effective_cues if effective_cues > 0 else "unknown"))
    if reference_cues > 0:
        ratio_text = (" (ratio %.2f)" % (effective_cues / reference_cues)
                      if effective_cues > 0 else "")
        lines.append("Longest other subtitle in film: %d cues%s"
                     % (reference_cues, ratio_text))
    else:
        lines.append("Longest other subtitle in film: unknown")
    if non_empty:
        lines.append("SDH-like cue marker share: %.1f%% (of %d non-empty cues)"
                     % (_sdh_share(non_empty) * 100, len(non_empty)))

    lines.append("")
    lines.append("## Sampled subtitle cues")
    if sampled:
        for number, cue in enumerate(sampled, start=1):
            lines.append("%d. %s" % (number, cue))
    else:
        lines.append("(no subtitle cues available)")

    lines.append("")
    lines.append(
        'Answer with JSON only: {"role": "<%s|%s>", '
        '"confidence": "high"|"medium"|"low", "why": "<one concise sentence>"}'
        % ("|".join(SUBTITLE_ROLES), UNSURE))
    return "\n".join(lines) + "\n"


def audio_prompt(
    *,
    role: str,
    name: str = "",
    language: str = "",
    flags: Mapping[str, bool] | None = None,
    transcript: str = "",
    main_transcript: str = "",
    speech: float | None = None,
    main_speech: float | None = None,
    new_words: float | None = None,
    reason: str = "",
) -> str:
    """The prompt describing a borderline audio track for the model."""
    clean_name = _clean_name(name)
    excerpt = sample_transcript(transcript)
    main_excerpt = sample_transcript(main_transcript)
    lines = [
        "Classify this audio track's role.",
        "Allowed roles: %s, or %s." % (", ".join(AUDIO_ROLES), UNSURE),
        "",
        "## Track metadata",
        "Title: %s" % (clean_name or "(none)"),
        "Language: %s" % ((language or "").strip() or "(unknown)"),
        "Flags set: %s" % _format_flags(flags),
        "Current deterministic role: %s" % role,
    ]
    if reason:
        lines.append("Why this track is being checked: %s" % reason)

    if speech is not None or main_speech is not None or new_words is not None:
        lines.append("")
        lines.append("## Acoustic comparison")
        if speech is not None:
            lines.append("Track speech share: %.2f" % speech)
        if main_speech is not None:
            lines.append("Film main track speech share: %.2f" % main_speech)
        if new_words is not None:
            lines.append("Words absent from film main track: %.0f%%"
                         % (new_words * 100))

    lines.append("")
    lines.append("## Track transcript excerpt")
    lines.append(excerpt or "(no transcript available)")

    if main_excerpt:
        lines.append("")
        lines.append("## Film main track transcript excerpt (same windows)")
        lines.append(main_excerpt)

    lines.append("")
    lines.append(
        'Answer with JSON only: {"role": "<%s|%s>", '
        '"confidence": "high"|"medium"|"low", "why": "<one concise sentence>"}'
        % ("|".join(AUDIO_ROLES), UNSURE))
    return "\n".join(lines) + "\n"


def parse_verdict(answer: str, allowed: Iterable[str]) -> Verdict | None:
    """Parse and strictly validate a JSON verdict from ``answer``.

    Returns ``None`` on malformed JSON, non-object replies, unknown roles, or
    unknown confidence levels.
    """
    found = llmclient.first_json(answer or "")
    if not isinstance(found, dict):
        return None
    raw_role = found.get("role")
    raw_confidence = found.get("confidence")
    if not isinstance(raw_role, str) or not isinstance(raw_confidence, str):
        return None
    role = shell_lower(raw_role.strip())
    confidence = shell_lower(raw_confidence.strip())
    valid_roles = set(allowed) | {UNSURE}
    if role not in valid_roles or confidence not in CONFIDENCES:
        return None
    raw_why = found.get("why")
    why = " ".join(str(raw_why).split())[:MAX_WHY_CHARS] if raw_why is not None else ""
    return Verdict(role=role, confidence=confidence, why=why)


def is_actionable(verdict: Verdict | None, allowed: Iterable[str]) -> bool:
    """Whether ``verdict`` may refine the deterministic role: high confidence
    and a concrete role in ``allowed`` (never ``"unsure"``)."""
    return (verdict is not None
            and verdict.confidence == HIGH_CONFIDENCE
            and verdict.role != UNSURE
            and verdict.role in set(allowed))


def _ask(
    prompt_text: str,
    allowed: Iterable[str],
    *,
    models: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
) -> Verdict | None:
    """Send ``prompt_text`` to the first usable model on the ``track-role``
    ladder, record usage in :mod:`medialib.lib.modelmeter`, and parse the
    strict verdict."""
    if not enabled():
        _detail("    No model asked for track role: the %s setting is off"
                % SWITCH_VARIABLE)
        return None
    if models is None:
        models, why = llmroster.optional(llmroster.TRACK_ROLE)
        if not models:
            _detail("    No model asked for track role: %s" % why)
            return None
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    try:
        for llm in models:
            try:
                with llm.serving(_detail) as bound, spans.timing():
                    answer = bound.ask(SYSTEM, prompt_text, tally)
                break
            except llmclient.LlmError as error:
                _log("    Track role not classified by %s: %s"
                     % (llm.name, error))
    finally:
        if tally.requests:
            _detail("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    if not answer:
        return None
    return parse_verdict(answer, allowed)


def _log_decision(kind: str, name: str, current: str,
                  verdict: Verdict | None, allowed: Iterable[str]) -> None:
    label = '%s track "%s"' % (kind, _clean_name(name)) if _clean_name(name) else (
        "%s track" % kind)
    if verdict is None:
        _log("    Model gave no usable %s verdict; keeping %s"
             % (label, current))
        return
    because = ": " + verdict.why if verdict.why else ""
    if not is_actionable(verdict, allowed):
        _log("    Model answered %s (%s confidence) for %s; keeping %s%s"
             % (verdict.role, verdict.confidence, label, current, because))
        return
    if verdict.role == current:
        _log("    Model confirmed %s as %s (%s confidence)%s"
             % (label, verdict.role, verdict.confidence, because))
    else:
        _log("    Model classified %s as %s -> %s (%s confidence)%s"
             % (label, current, verdict.role, verdict.confidence, because))


def classify_subtitle(
    *,
    role: str,
    cues: int = 0,
    reference_cues: int = 0,
    texts: Sequence[str] | None = None,
    name: str = "",
    language: str = "",
    flags: Mapping[str, bool] | None = None,
    reason: str = "",
    models: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
) -> Verdict | None:
    """Ask the ``track-role`` model to classify one subtitle track."""
    request = subtitle_prompt(
        role=role, name=name, language=language, flags=flags,
        cues=cues, reference_cues=reference_cues, texts=texts, reason=reason,
    )
    return _ask(request, SUBTITLE_ROLES, models=models, meter_file=meter_file)


def classify_audio(
    *,
    role: str,
    name: str = "",
    language: str = "",
    flags: Mapping[str, bool] | None = None,
    transcript: str = "",
    main_transcript: str = "",
    speech: float | None = None,
    main_speech: float | None = None,
    new_words: float | None = None,
    reason: str = "",
    models: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
) -> Verdict | None:
    """Ask the ``track-role`` model to classify one audio track."""
    request = audio_prompt(
        role=role, name=name, language=language, flags=flags,
        transcript=transcript, main_transcript=main_transcript,
        speech=speech, main_speech=main_speech, new_words=new_words,
        reason=reason,
    )
    return _ask(request, AUDIO_ROLES, models=models, meter_file=meter_file)


def resolve_subtitle_role(
    deterministic: str,
    *,
    said: str,
    cues: int = 0,
    reference_cues: int = 0,
    texts: Sequence[str] | None = None,
    name: str = "",
    language: str = "",
    flags: Mapping[str, bool] | None = None,
    flagged: bool = False,
    models: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
) -> str:
    """Refine ``deterministic`` for a subtitle track using a language model
    ONLY when the deterministic check was borderline or could not run."""
    reason = subtitle_borderline_reason(
        said, cues, reference_cues, texts, name=name, flagged=flagged,
    )
    if not reason:
        return deterministic
    if not enabled():
        _detail("    No model asked for subtitle role (%s): the %s setting is off"
                % (reason, SWITCH_VARIABLE))
        return deterministic
    if models is None:
        models, why = llmroster.optional(llmroster.TRACK_ROLE)
        if not models:
            _detail("    No model asked for subtitle role (%s): %s"
                    % (reason, why))
            return deterministic
    elif not models:
        return deterministic
    verdict = classify_subtitle(
        role=deterministic, cues=cues, reference_cues=reference_cues,
        texts=texts, name=name, language=language, flags=flags,
        reason=reason, models=models, meter_file=meter_file,
    )
    _log_decision("subtitle", name, deterministic, verdict, SUBTITLE_ROLES)
    if verdict is not None and is_actionable(verdict, SUBTITLE_ROLES):
        return verdict.role
    return deterministic


def resolve_audio_role(
    deterministic: str,
    *,
    name: str = "",
    language: str = "",
    flags: Mapping[str, bool] | None = None,
    transcript: str = "",
    main_transcript: str = "",
    speech: float | None = None,
    main_speech: float | None = None,
    new_words: float | None = None,
    unsure: bool = False,
    flagged: bool = False,
    models: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
) -> str:
    """Refine ``deterministic`` for an audio track using a language model
    ONLY when the deterministic check was borderline or could not run."""
    reason = audio_borderline_reason(
        deterministic, name=name, transcript=transcript,
        speech=speech, main_speech=main_speech, new_words=new_words,
        unsure=unsure, flagged=flagged,
    )
    if not reason:
        return deterministic
    if not enabled():
        _detail("    No model asked for audio role (%s): the %s setting is off"
                % (reason, SWITCH_VARIABLE))
        return deterministic
    if models is None:
        models, why = llmroster.optional(llmroster.TRACK_ROLE)
        if not models:
            _detail("    No model asked for audio role (%s): %s"
                    % (reason, why))
            return deterministic
    elif not models:
        return deterministic
    verdict = classify_audio(
        role=deterministic, name=name, language=language, flags=flags,
        transcript=transcript, main_transcript=main_transcript,
        speech=speech, main_speech=main_speech, new_words=new_words,
        reason=reason, models=models, meter_file=meter_file,
    )
    _log_decision("audio", name, deterministic, verdict, AUDIO_ROLES)
    if verdict is not None and is_actionable(verdict, AUDIO_ROLES):
        return verdict.role
    return deterministic
