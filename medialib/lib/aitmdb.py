"""Ask a language model which offered film matches an unmatched folder.

When :mod:`medialib.lib.tmdbidentify`'s certainty rules and the local IMDb
lists cannot settle on one film, every candidate they looked at (and every
note they recorded about why each was refused) is already in hand. This
module hands that dossier - the folder's name, its file names, runtime on
disk, earliest container creation year, near-miss notes and the offered
candidates - to a model from :data:`medialib.lib.llmroster.FILM_IDENTIFY`.

The model chooses among the offered ids or answers ``null``; it may never
invent an id that was not offered. Any unoffered id is ignored and logged.

By default (:data:`SWITCH_VARIABLE` = ``on``), a pick is accepted outright
ONLY when the model marks it ``high`` confidence, the film was not released
after the container was written, the folder has a year (or the chosen offer
carries one) and the runtime on disk fits the candidate's stated runtime
under :func:`medialib.lib.tmdbidentify._runtime_agrees` (5 min / 7%). Every
other offered pick is left for review as a pre-filled suggestion row in the
unmatched TSV, with its confidence and one-sentence reason in a comment.
Setting :data:`SWITCH_VARIABLE` to ``suggest`` never accepts outright and
always writes suggestions only; ``off`` never asks the model.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from typing import NamedTuple

from medialib.lib import (
    llmclient,
    llmroster,
    modelmeter,
    plexnames,
    runlog,
    tmdbidentify,
)

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "Verdict",
    "mode",
    "enabled",
    "accept_outright",
    "prompt",
    "parse",
    "evaluate",
    "identify_film",
]

SWITCH_VARIABLE = "filmIdentifyLlm"
PURPOSE = "identify"

MAX_LISTED_FILES = 100
MAX_OFFERS = 40
MAX_NOTES = 80
MAX_REASON_CHARS = 240

_OFF_VALUES = frozenset(("off", "0", "no", "false"))
_CONFIDENCES = frozenset(("high", "medium", "low"))
_IMDB_TOKEN = re.compile(r"^(?:imdb[-:])?(tt\d+)$", re.IGNORECASE)
_TMDB_TOKEN = re.compile(r"^(?:tmdb[-:])?(\d+)$", re.IGNORECASE)

SYSTEM = """You are identifying one film in a personal movie library after automatic
catalogue matching could not settle on a single candidate.

You are given the folder name, parent folders, file names inside the folder,
the runtime measured on disk, the earliest container creation year (mux year),
the catalogue's near-miss notes, and the numbered list of offered candidates.

Rules:
1. Choose AT MOST ONE candidate from the Offered candidates list, or null if
   none of them is this film or you cannot be reasonably sure.
2. NEVER invent an id that does not appear in Offered candidates. Choosing
   null is always better than guessing or inventing an id.
3. Weigh translated and alternative titles, TV specials, concerts, re-releases,
   PAL 4% speed-up or alternate cuts, file-name clues (language tags, edition
   markers, release notes), and the mux year (a film cannot have been muxed
   before it existed).
4. Set "confidence" to:
   - "high" only when title/translation, year and runtime/metadata clearly
     identify this exact entry;
   - "medium" when it is the likely entry but some detail is missing or
     uncertain;
   - "low" when tentative.
5. Answer with JSON only, no prose outside the object:
   {"id": "<offered id or null>", "confidence": "high|medium|low",
    "reason": "<one concise sentence>"}"""


class Verdict(NamedTuple):
    """What the model decided for one unmatched film folder."""

    offer: tmdbidentify.Offer | None = None
    confidence: str = ""
    reason: str = ""
    accepted: bool = False
    rejected_id: str = ""
    hold_reason: str = ""
    asked: bool = False


def mode() -> str:
    """``on``, ``suggest`` or ``off`` from :data:`SWITCH_VARIABLE`."""
    value = os.environ.get(SWITCH_VARIABLE, "on").strip().lower()
    if value in _OFF_VALUES:
        return "off"
    if value == "suggest":
        return "suggest"
    return "on"


def enabled() -> bool:
    """Whether unmatched films may be sent to a language model."""
    return mode() != "off"


def accept_outright() -> bool:
    """Whether a high-confidence, runtime-verified pick may be applied
    outright rather than only written as a suggestion."""
    return mode() == "on"


def _detail(message: str) -> None:
    runlog.detail(message)


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "(unknown)"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return "%d:%02d:%02d (%s min)" % (hours, minutes, secs,
                                      _minutes(seconds / 60.0))


def _minutes(value: float) -> str:
    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return "%.1f" % rounded


def _format_size(path: str) -> str:
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size >= 1024 * 1024 * 1024:
        return "%.2f GiB" % (size / (1024 * 1024 * 1024))
    if size >= 1024 * 1024:
        return "%.1f MiB" % (size / (1024 * 1024))
    if size >= 1024:
        return "%.0f KiB" % (size / 1024)
    return "%d B" % size


def _parent_names(folder_path: str, limit: int = 3) -> list[str]:
    parents: list[str] = []
    current = os.path.dirname(os.path.normpath(folder_path))
    while current and len(parents) < limit:
        head, tail = os.path.split(current)
        if not tail or head == current:
            break
        parents.append(tail)
        current = head
    parents.reverse()
    return parents


def _offer_ids(offer: tmdbidentify.Offer) -> str:
    parts = []
    if offer.imdb:
        parts.append(offer.imdb)
    if offer.tmdb:
        parts.append("tmdb-" + offer.tmdb)
    return " / ".join(parts)


def prompt(
    folder_path: str,
    base: str,
    names: Sequence[str],
    offered: Sequence[tmdbidentify.Offer],
    notes: Sequence[str],
    seconds: float = 0.0,
    written_year: str = "",
    year: str = "",
) -> str:
    """The user message describing one unmatched folder and its offered
    candidates."""
    lines = ["## The folder"]
    folder_name = os.path.basename(os.path.normpath(folder_path)) or base
    lines.append("Folder: %s" % folder_name)
    if base and base != folder_name:
        lines.append("Cleaned name: %s" % base)
    parents = _parent_names(folder_path)
    if parents:
        lines.append("Parent folders: %s" % " / ".join(parents))
    lines.append("Year from folder: %s" % (year or "(none)"))
    lines.append("Runtime on disk: %s" % _format_duration(seconds))
    lines.append("Earliest mux/container year: %s"
                 % (written_year or "(unknown)"))

    lines.append("")
    lines.append("## Its files")
    sorted_names = sorted(names)
    shown_files = sorted_names[:MAX_LISTED_FILES]
    if shown_files:
        for name in shown_files:
            size = _format_size(os.path.join(folder_path, name)) if folder_path else ""
            lines.append("- %s (%s)" % (name, size) if size else "- %s" % name)
        if len(sorted_names) > MAX_LISTED_FILES:
            lines.append("- ... (%d more files)"
                         % (len(sorted_names) - MAX_LISTED_FILES))
    else:
        lines.append("(no files listed)")

    lines.append("")
    lines.append("## Offered candidates")
    shown_offers = list(offered)[:MAX_OFFERS]
    for index, offer in enumerate(shown_offers, 1):
        titles = ", ".join('"%s"' % t for t in offer.titles[:6]) or "(untitled)"
        years = "/".join(offer.years) if offer.years else "undated"
        runtime = ("%s min" % _minutes(offer.runtime)
                   if offer.runtime > 0 else "runtime unknown")
        source = ("TMDb search only" if offer.source == "tmdb-search"
                  else offer.source.upper())
        lines.append("%d. %s | %s (%s) | %s | %s"
                     % (index, _offer_ids(offer), titles, years, runtime, source))
    if len(offered) > MAX_OFFERS:
        lines.append("... (%d more candidates omitted)"
                     % (len(offered) - MAX_OFFERS))

    if notes:
        lines.append("")
        lines.append("## Near-miss notes")
        for note in list(notes)[:MAX_NOTES]:
            lines.append(note)
        if len(notes) > MAX_NOTES:
            lines.append("... (%d more lines omitted)" % (len(notes) - MAX_NOTES))

    lines.append("")
    lines.append("Pick one id from Offered candidates above, or null. "
                 "Answer with JSON only.")
    return "\n".join(lines)


def _normalize_confidence(value) -> str:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _CONFIDENCES:
            return lowered
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value >= 0.8:
            return "high"
        if value >= 0.5:
            return "medium"
        return "low"
    return "low"


def _clean_reason(value) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    if len(cleaned) > MAX_REASON_CHARS:
        cleaned = cleaned[:MAX_REASON_CHARS - 3].rstrip() + "..."
    return cleaned


def _parse_id_token(raw) -> tuple[str, str]:
    """Return ``(kind, value)`` for an IMDb or TMDb id token, or ``("", "")``."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return ("tmdb", str(raw)) if raw > 0 else ("", "")
    if not isinstance(raw, str):
        return ("", "")
    text = raw.strip()
    if not text:
        return ("", "")
    kind, value = plexnames.id_of(text)
    if kind in ("imdb", "tmdb") and value:
        return (kind, value.lower() if kind == "imdb" else value)
    if (text.startswith("{") and text.endswith("}")) \
            or (text.startswith("[") and text.endswith("]")):
        text = text[1:-1].strip()
    matched = _IMDB_TOKEN.fullmatch(text)
    if matched:
        return ("imdb", matched.group(1).lower())
    matched = _TMDB_TOKEN.fullmatch(text)
    if matched:
        return ("tmdb", matched.group(1))
    return ("", "")


def _match_offer(raw, offered: Sequence[tmdbidentify.Offer]) \
        -> tmdbidentify.Offer | None:
    """Find ``raw`` among ``offered`` - strictly one of the offered ids, never
    an invented one."""
    kind, value = _parse_id_token(raw)
    if not kind:
        return None
    for offer in offered:
        if kind == "imdb" and offer.imdb and offer.imdb.lower() == value:
            return offer
        if kind == "tmdb" and offer.tmdb and offer.tmdb == value:
            return offer
    return None


def parse(answer: str, offered: Sequence[tmdbidentify.Offer]) -> Verdict:
    """Read the model's JSON reply and enforce the offered-id guard."""
    parsed = llmclient.first_json(answer)
    if not isinstance(parsed, dict):
        return Verdict(asked=True, reason="reply held no JSON object")
    confidence = _normalize_confidence(parsed.get("confidence"))
    reason = _clean_reason(parsed.get("reason") or parsed.get("why"))
    raw_id = parsed.get("id")
    if raw_id is None:
        return Verdict(confidence=confidence, reason=reason, asked=True)
    if isinstance(raw_id, str) \
            and raw_id.strip().lower() in ("", "none", "null"):
        return Verdict(confidence=confidence, reason=reason, asked=True)
    matched = _match_offer(raw_id, offered)
    if matched is None:
        return Verdict(
            confidence=confidence,
            reason=reason,
            rejected_id=str(raw_id).strip(),
            asked=True,
        )
    return Verdict(
        offer=matched,
        confidence=confidence,
        reason=reason,
        asked=True,
    )


def evaluate(
    verdict: Verdict,
    seconds: float = 0.0,
    written_year: str = "",
    folder_year: str = "",
) -> Verdict:
    """Decide whether a model pick is accepted outright or held for review.

    A pick is accepted outright ONLY when :func:`accept_outright` is true,
    confidence is ``high``, the film was not released after ``written_year``,
    the folder has a year (or the offer supplies one), and the measured disk
    runtime agrees with the candidate's stated runtime under
    :func:`medialib.lib.tmdbidentify._runtime_agrees`.
    """
    offer = verdict.offer
    if offer is None:
        return verdict
    if not accept_outright():
        return verdict._replace(
            accepted=False,
            hold_reason="%s is set to suggest" % SWITCH_VARIABLE,
        )
    if verdict.confidence != "high":
        return verdict._replace(
            accepted=False,
            hold_reason="confidence is %s" % (verdict.confidence or "unspecified"),
        )
    if written_year and offer.years and min(offer.years) > written_year:
        return verdict._replace(
            accepted=False,
            hold_reason="released in %s, after the file was written in %s"
            % (min(offer.years), written_year),
        )
    if not folder_year and not offer.released:
        return verdict._replace(
            accepted=False,
            hold_reason="folder has no year and candidate has no release year",
        )
    agrees = tmdbidentify._runtime_agrees(seconds, offer.runtime)
    if agrees is None:
        why = ("disk runtime unknown" if seconds <= 0
               else "catalogue runtime unknown")
        return verdict._replace(accepted=False, hold_reason=why)
    if not agrees:
        return verdict._replace(
            accepted=False,
            hold_reason="runtime does not fit (%s min on disk vs %s min stated)"
            % (_minutes(seconds / 60.0), _minutes(offer.runtime)),
        )
    return verdict._replace(accepted=True, hold_reason="")


def _ask_one(llm: llmclient.Llm, request: str,
             spans: modelmeter.Spans, tally: modelmeter.Tally) -> str:
    searcher = llm.searching(PURPOSE)
    if searcher is not None:
        with spans.timing():
            return searcher.ask(SYSTEM, request, tally)
    with llm.serving(_detail) as bound, spans.timing():
        return bound.ask(SYSTEM, request, tally)


def identify_film(
    folder_path: str,
    base: str,
    names: Sequence[str],
    offered: Sequence[tmdbidentify.Offer],
    notes: Sequence[str],
    *,
    seconds: float | Callable[[], float] | None = None,
    written_year: str | Callable[[], str] | None = None,
    year: str = "",
    models: Sequence[llmclient.Llm] | None = None,
    log: Callable[[str], None] = runlog.log,
    meter: tuple[modelmeter.Spans, modelmeter.Tally] | None = None,
    meter_file: str = "",
) -> Verdict:
    """Ask the configured ladder for :data:`llmroster.FILM_IDENTIFY` to pick
    among ``offered``, or return an unasked :class:`Verdict` when disabled,
    unconfigured, given no candidates, or every model fails."""
    if not enabled():
        _detail("  No model asked to identify %s: %s is off"
                % (base, SWITCH_VARIABLE))
        return Verdict()
    if not offered:
        _detail("  No model asked to identify %s: no candidates were offered"
                % base)
        return Verdict()

    if models is None:
        ladder, why = llmroster.optional(llmroster.FILM_IDENTIFY)
        if not ladder:
            _detail("  No model asked to identify %s: %s" % (base, why))
            return Verdict()
    else:
        ladder = list(models)
        if not ladder:
            return Verdict()

    actual_seconds = (float(seconds()) if callable(seconds)
                      else float(seconds or 0.0))
    actual_written = (str(written_year() or "") if callable(written_year)
                      else str(written_year or ""))
    request = prompt(folder_path, base, names, offered, notes,
                     seconds=actual_seconds, written_year=actual_written,
                     year=year)

    spans = modelmeter.Spans()
    tally = modelmeter.Tally()
    answer: str | None = None
    try:
        for llm in ladder:
            try:
                answer = _ask_one(llm, request, spans, tally)
                break
            except llmclient.LlmError as error:
                log("  No film identification from %s: %s" % (llm.name, error))
    finally:
        if tally.requests:
            log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter is not None:
            meter[0].merge(spans)
            meter[1].merge(tally)
        if meter_file:
            modelmeter.record(meter_file, spans, tally)

    if answer is None:
        return Verdict()
    verdict = parse(answer, offered)
    return evaluate(verdict, actual_seconds, actual_written, year)
