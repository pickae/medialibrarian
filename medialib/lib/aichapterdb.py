"""Asking a language model to pick among fitting ChapterDB chapter sets for the
library's language, or to translate a well-fitting set's chapter names into it.

:func:`medialib.lib.chapterdb.choose` already enforces every structural check:
exact duration match, chapter ordering, coverage across the film, and - for a
film with numbered chapters - identical chapter timestamps. Left to itself, it
always prefers English, because it has no way to judge chapter names across
languages or translate them.

This module adds a bounded model step on top of those already-fitting named
sets, using the roster's :data:`llmroster.CHAPTERDB_CHOOSE` task:

- When the deterministic winner is already a clear match in the library's
  primary language (the first language in ``spokenLanguages``) and no
  other-language candidate rivals its confirmations, no model is asked.
- Otherwise, the model is offered the fitting named candidates (numbered
  ``1..N``) and may either pick one of them as-is or pick one and translate
  its chapter names into the primary target language.
- Every structural guard is enforced on the answer: the chosen index must be
  in ``1..N``; translated titles must match the chosen set's exact chapter
  count, be non-empty single-line strings within :data:`MAX_TITLE_CHARS`,
  remain real unique chapter names (:func:`chapterdb.is_named`), and never
  alter a single timestamp.
- On ``"unsure"``, any guard failure, :class:`llmclient.LlmError`, the
  ``chapterDbLlm`` setting turned off, or no model available, the
  deterministic choice stands unchanged.

What the model was asked is counted (:mod:`medialib.lib.modelmeter`), logged
under the film, and optionally recorded for the run.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import NamedTuple

from medialib.lib import (
    chapterdb,
    formatting,
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
    "MAX_TITLE_CHARS",
    "SYSTEM",
    "Decision",
    "enabled",
    "target_languages",
    "needs_model",
    "prompt",
    "parse_decision",
    "choose",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "chapterDbLlm"

# Label used in modelmeter.rate_line.
PURPOSE = "chapters"

# Maximum length for a single translated chapter title.
MAX_TITLE_CHARS = 200

_log = runlog.log
_detail = runlog.detail

SYSTEM = """You choose or translate film chapter sets for a personal media \
library. Every candidate set you are shown has already been verified to match \
the film's duration and disc marks.

Your job is to give the library clean chapter titles in its preferred language:
1. Choose, don't invent: you MUST select one of the offered candidate numbers \
(1..N). Never invent new chapters, never merge candidates, never add or drop \
chapters.
2. If one candidate is already well-written in the library's primary target \
language, pick it as-is:
   {"action": "choose", "choice": <number>, "why": "<one sentence>"}
3. If no candidate is in the primary target language (or the candidate in the \
primary language is mislabeled or clearly inferior to a well-confirmed set in \
another language), pick the best-fitting candidate and translate all of its \
chapter titles into the primary target language:
   {"action": "translate", "choice": <number>, "titles": ["...", ...], \
"why": "<one sentence>"}
   When translating:
   - "titles" MUST have the EXACT same number of entries as the chosen \
candidate's chapters, in the exact same order.
   - Every entry must be a single non-empty line (no newlines, at most %d \
characters), natural in the target language, keeping proper names intact.
   - Every entry must remain a real unique chapter name (never generic \
placeholders like "Chapter 1" or repeated identical titles).
   - Do NOT include timestamps in "titles" - timestamps are kept unchanged.
4. If you are not sure which candidate is better or cannot translate reliably, \
say so:
   {"action": "unsure", "why": "<one sentence>"}

Answer with JSON only.""" % MAX_TITLE_CHARS


class Decision(NamedTuple):
    """Parsed outcome of a model's chapter-selection answer."""

    chapter_set: chapterdb.ChapterSet | None
    action: str   # "choose", "translate", "unsure", or "rejected"
    choice: int | None
    why: str
    problem: str = ""


def enabled() -> bool:
    """Whether a run asks the model at all: on unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def target_languages() -> tuple[languages.Language, ...]:
    """The library's preferred languages, primary first: ``spokenLanguages``."""
    return tuple(row for row in languages.spoken_languages() if row is not None)


def _lang_code(tag: str) -> str:
    """Normalised language code for comparing a chapter set's ``xml:lang``
    against a target language, or ``""`` when untagged or unknown."""
    norm = (tag or "").strip().replace("_", "-")
    if not norm:
        return ""
    return languages.track_language(norm, norm)


def needs_model(candidates: Sequence[chapterdb.ChapterSet],
                best: chapterdb.ChapterSet,
                targets: Sequence[languages.Language]) -> bool:
    """Whether asking a model can improve on the deterministic ``best`` set.

    No model call is spent when ``best`` is already a clear match in the
    library's primary language: either every candidate is in that language, or
    ``best`` strictly outranks every other-language candidate in confirmations.
    We ask when ``best`` is not in the primary language (including untagged
    sets) or when an other-language candidate rivals or exceeds ``best`` in
    confirmations.
    """
    if not best.named or not candidates or not targets:
        return False
    primary = _lang_code(targets[0].code2)
    if not primary:
        return False
    if _lang_code(best.language) != primary:
        return True
    others = [candidate for candidate in candidates
              if _lang_code(candidate.language) != primary]
    if not others:
        return False
    return any(candidate.confirmations >= best.confirmations
               for candidate in others)


def prompt(film_title: str, candidates: Sequence[chapterdb.ChapterSet],
           targets: Sequence[languages.Language]) -> str:
    """What the model is told about the film, target languages, and candidates."""
    primary = targets[0]
    display_title = film_title.strip() or (
        candidates[0].title.strip() if candidates else "") or "(unknown)"
    lines = [
        "Film: %s" % display_title,
        "Primary target language: %s (%s / %s)" % (
            primary.sub_word, primary.code2, primary.code3b),
    ]
    if len(targets) > 1:
        others = ", ".join("%s (%s)" % (row.sub_word, row.code2)
                           for row in targets[1:])
        lines.append("Other preferred library languages: %s" % others)
    lines.append("")
    lines.append("Candidate chapter sets (%d):" % len(candidates))
    for number, candidate in enumerate(candidates, start=1):
        duration = (formatting.fmt_clock(candidate.seconds)
                    if candidate.seconds > 0 else "unknown")
        lines.append("")
        lines.append(
            "Candidate %d (set %s): language=%s, source=%s, "
            "confirmations=%d, duration=%s, chapters=%d" % (
                number, candidate.set_id or "?",
                candidate.language or "unknown",
                candidate.source or "unknown",
                candidate.confirmations, duration, len(candidate.chapters)))
        if candidate.title and candidate.title.strip() != display_title:
            lines.append("  Set title: %s" % candidate.title.strip())
        for idx, (start, name) in enumerate(candidate.chapters, start=1):
            lines.append("  %d. [%s] %s" % (
                idx, formatting.fmt_clock(start), name))
    return "\n".join(lines) + "\n"


def parse_decision(answer: str, candidates: Sequence[chapterdb.ChapterSet],
                   target: languages.Language) -> Decision:
    """Validate the model's JSON answer against every structural guard."""
    data = llmclient.first_json(answer)
    if not isinstance(data, dict):
        return Decision(None, "rejected", None, "",
                        "answer held no JSON object")
    why = " ".join(str(data.get("why") or "").split())
    raw_action = data.get("action")
    raw_choice = data.get("choice")

    if isinstance(raw_action, str):
        action = raw_action.strip().lower()
    elif raw_action is None:
        if raw_choice is None or (isinstance(raw_choice, str)
                                  and raw_choice.strip().lower() == "unsure"):
            action = "unsure"
        elif data.get("titles") is not None:
            action = "translate"
        else:
            action = "choose"
    else:
        return Decision(None, "rejected", None, why,
                        "unexpected action %r" % (raw_action,))

    if action == "unsure":
        return Decision(None, "unsure", None, why)
    if action not in ("choose", "translate"):
        return Decision(None, "rejected", None, why,
                        "unknown action %r" % (action,))

    if isinstance(raw_choice, bool) or not isinstance(raw_choice, int) \
            or not (1 <= raw_choice <= len(candidates)):
        return Decision(None, "rejected", None, why,
                        "choice %r is not in 1..%d"
                        % (raw_choice, len(candidates)))

    picked = candidates[raw_choice - 1]
    if action == "choose":
        return Decision(picked, "choose", raw_choice, why)

    titles = data.get("titles")
    if not isinstance(titles, list):
        return Decision(None, "rejected", raw_choice, why,
                        "translate answer missing titles list")
    if len(titles) != len(picked.chapters):
        return Decision(None, "rejected", raw_choice, why,
                        "got %d title(s) for %d chapter(s)"
                        % (len(titles), len(picked.chapters)))

    cleaned: list[str] = []
    for index, entry in enumerate(titles, start=1):
        if not isinstance(entry, str):
            return Decision(None, "rejected", raw_choice, why,
                            "title %d is not text" % index)
        if "\n" in entry or "\r" in entry:
            return Decision(None, "rejected", raw_choice, why,
                            "title %d spans multiple lines" % index)
        title = entry.strip()
        if not title:
            return Decision(None, "rejected", raw_choice, why,
                            "title %d is empty" % index)
        if len(title) > MAX_TITLE_CHARS:
            return Decision(None, "rejected", raw_choice, why,
                            "title %d is %d chars (limit %d)"
                            % (index, len(title), MAX_TITLE_CHARS))
        cleaned.append(title)

    if not chapterdb.is_named(cleaned):
        return Decision(None, "rejected", raw_choice, why,
                        "translated titles are generic or duplicated")

    translated_chapters = tuple(
        (start, title)
        for (start, _old), title in zip(picked.chapters, cleaned, strict=True)
    )
    translated = picked._replace(language=target.code3b,
                                 chapters=translated_chapters)
    return Decision(translated, "translate", raw_choice, why)


def choose(candidates: Sequence[chapterdb.ChapterSet],
           best: chapterdb.ChapterSet,
           *,
           film_title: str = "",
           targets: Sequence[languages.Language] | None = None,
           models: Sequence[llmclient.Llm] | None = None,
           meter_file: str = "",
           log: Callable[[str], None] = _log) -> chapterdb.ChapterSet:
    """Ask the roster's :data:`llmroster.CHAPTERDB_CHOOSE` model to pick among
    ``candidates`` or translate the best one into the library's primary
    language, falling back cleanly to ``best`` on any skip, unsure, guard
    failure, or error."""
    if not enabled():
        _detail("  No model asked for chapters: the %s setting is off"
                % SWITCH_VARIABLE)
        return best
    wanted = tuple(targets) if targets is not None else target_languages()
    if not wanted:
        return best
    if not needs_model(candidates, best, wanted):
        _detail("  Chapter set %s already matches %s clearly; no model asked"
                % (best.set_id, wanted[0].sub_word))
        return best
    if models is None:
        models, why_none = llmroster.optional(llmroster.CHAPTERDB_CHOOSE)
        if not models:
            _detail("  No model asked for chapters: %s" % why_none)
            return best
    if not models:
        return best

    request = prompt(film_title, candidates, wanted)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    answered_by = ""
    try:
        for llm in models:
            try:
                with llm.serving(_detail) as bound:
                    with spans.timing():
                        answer = bound.ask(SYSTEM, request, tally)
                    answered_by = bound.model or llm.name
                break
            except llmclient.LlmError as error:
                log("  Chapter choice not asked of %s: %s" % (llm.name, error))
    finally:
        if tally.requests:
            log("  " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)

    if not answer:
        log("  No chapter choice from the model; keeping set %s" % best.set_id)
        return best

    decision = parse_decision(answer, candidates, wanted[0])
    if decision.action == "choose" and decision.chapter_set is not None:
        picked = decision.chapter_set
        log("  Model (%s) chose set %s (%s)%s" % (
            answered_by, picked.set_id, picked.language or "unknown",
            ": " + decision.why if decision.why else ""))
        return picked
    if decision.action == "translate" and decision.chapter_set is not None:
        translated = decision.chapter_set
        log("  Model (%s) translated set %s into %s (%d chapters)%s" % (
            answered_by, translated.set_id, wanted[0].sub_word,
            len(translated.chapters),
            ": " + decision.why if decision.why else ""))
        return translated
    if decision.action == "unsure":
        log("  Model (%s) was unsure about chapters; keeping set %s%s" % (
            answered_by, best.set_id,
            ": " + decision.why if decision.why else ""))
        return best
    log("  Model (%s) chapter answer rejected (%s); keeping set %s" % (
        answered_by, decision.problem, best.set_id))
    return best
