"""Asking a language model to suggest matches for unmatched or ambiguous
Letterboxd entries in ``plex-letterboxd-sync`` - for the sync report only.

:mod:`medialib.lib.filmmatch` settles most films by id or title fold, and
leaves alone any entry that matches two films equally well or matches nothing
at all. A translated title, a dropped or altered subtitle, or a festival year
off by more than one can still hide a film that is in Plex under another name.
A language model that reasons - and can search the web where available - can
often recognise those pairs, or name the film's TheMovieDB id.

Its answer is **report only**: it never marks, rates, or adds anything in Plex,
never alters the Letterboxd import CSVs, and never writes into the sync state
file. Every model decision is printed in the run's report with its confidence
and a concise reason, so the user can review it and fix the library or
Letterboxd entry by hand.

Two guards keep the suggestions grounded:

* **Choose, don't invent.** When candidate films are offered, the model must
  pick one of the numbered candidates (``1..N``) or ``null``; any index outside
  the offered range or wrong type is rejected. When no candidates are offered,
  ``choice`` must be ``null``.
* **Strict TMDb id.** An optional suggested TMDb id is accepted only as a
  positive integer that does not conflict with the subject's or chosen
  candidate's existing TMDb id, and is clearly marked as a suggestion in the
  report.

The task in :mod:`medialib.lib.llmroster` is :data:`llmroster.LETTERBOXD_MATCH`
(prefers reasoning and web search, balanced lean). The ``letterboxdMatchLlm``
setting turns this step off. What the model was asked is counted with
:mod:`medialib.lib.modelmeter` and reported with the run.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Sequence
from dataclasses import dataclass

from medialib.lib import (
    llmclient,
    llmroster,
    modelmeter,
    plexserver,
    runlog,
    titlematch,
)
from medialib.lib.enums import shell_lower
from medialib.lib.filmmatch import Film, FilmIndex, Found

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "MAX_CANDIDATES",
    "MAX_ENTRIES",
    "SYSTEM",
    "Entry",
    "Suggestion",
    "enabled",
    "close_candidates",
    "collect_entries",
    "prompt",
    "parse_answer",
    "suggest_matches",
    "suggest_for_sync",
    "format_suggestions",
    "report_suggestions",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "letterboxdMatchLlm"

# What the model's requests are for, naming harness sessions and meter lines.
PURPOSE = "letterboxd"

# How many close candidate films from Plex are offered for an unmatched entry.
MAX_CANDIDATES = 8

# Upper bound on how many entries one sync run hands to the model.
MAX_ENTRIES = 40

# Maximum length kept for a model's one-sentence explanation.
MAX_REASON_CHARS = 240

_CONFIDENCES = ("high", "medium", "low")

_log = runlog.log
_detail = runlog.detail

SYSTEM = """You help review unmatched or ambiguous film matches between a \
Letterboxd account and a Plex movie library. Your answer goes into a report \
for a human to inspect; nothing in Plex is changed automatically.

Rules:
1. When numbered candidate films are listed, "choice" MUST be either one of \
those exact candidate numbers (1..N) or null when none of the candidates is \
the same film. Never invent a candidate number outside the list.
2. When no candidates are listed, "choice" MUST be null.
3. Recognise alternate/original/translated titles, added or dropped subtitles, \
leading articles, numerals, and a 1-2 year festival vs theatrical release \
difference - but never confuse remakes, sequels, or unrelated films that \
merely share a word or year. A wrong suggestion is worse than null.
4. "tmdb" is an optional positive integer TheMovieDB movie id for the film \
(e.g. 129) only when you are sure of it; otherwise null. Never guess an id, \
and never use an IMDb id (tt...) in the "tmdb" field.
5. "confidence" is one of "high", "medium", or "low".
6. "reason" is one concise sentence explaining the pick or why none matched.

Answer with JSON only, in this form:
{"choice": <1..N or null>, "tmdb": <positive integer or null>, \
"confidence": "high|medium|low", "reason": "<one concise sentence>"}"""


@dataclass(frozen=True)
class Entry:
    """One unmatched or ambiguous sync entry to ask the model about."""

    kind: str                           # "ambiguous" or "unmatched"
    subject: Film                       # the entry being matched
    candidates: tuple[Film, ...] = ()   # offered Plex (or Letterboxd) candidates
    direction: str = "plex"             # "plex" (candidates are Plex) or "letterboxd"
    how: str = ""                       # FilmIndex rung when ambiguous


@dataclass(frozen=True)
class Suggestion:
    """One model decision for an :class:`Entry`, written to the report only."""

    entry: Entry
    choice: Film | None = None
    choice_index: int | None = None
    tmdb: str = ""
    confidence: str = ""
    reason: str = ""
    rejected: str = ""
    model: str = ""


def enabled() -> bool:
    """Whether a run asks a model for Letterboxd match suggestions at all."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def _year_gap(one: str, other: str) -> int | None:
    if not (one.isdigit() and other.isdigit()):
        return None
    return abs(int(one) - int(other))


def _different_ids(one: Film, other: Film) -> bool:
    return any(mine and theirs and mine != theirs
               for mine, theirs in ((one.tmdb, other.tmdb),
                                    (one.imdb, other.imdb)))


def _film_identity(film: Film) -> tuple[str, str, str, str]:
    return (film.title, film.year, film.tmdb, film.imdb)


def _dedupe_films(films: Sequence[Film]) -> list[Film]:
    """Unique films preserving first-seen order; when two share (title, year)
    and one carries ids, prefer the richer copy."""
    by_key: dict[tuple[str, str, str, str], Film] = {}
    for film in films:
        key = _film_identity(film)
        if key not in by_key:
            by_key[key] = film
    return list(by_key.values())


def _significant_words(fold: str) -> frozenset[str]:
    return frozenset(
        word for word in fold.split()
        if len(word) >= 2 and word not in titlematch.ARTICLES
    )


def _title_similarity(one: Film, other: Film) -> float:
    """Best similarity score in [0.0, 1.0] across every title/alias pair."""
    best = 0.0
    one_folds = [titlematch.normalize_title(t) for t in one.titles if t]
    other_folds = [titlematch.normalize_title(t) for t in other.titles if t]
    for left in one_folds:
        if not left:
            continue
        left_words = _significant_words(left)
        for right in other_folds:
            if not right:
                continue
            ratio = difflib.SequenceMatcher(None, left, right).ratio()
            right_words = _significant_words(right)
            score = ratio
            if left_words and right_words:
                shared = left_words & right_words
                union = left_words | right_words
                if shared:
                    score = max(score, len(shared) / len(union))
                    shorter = left_words if len(left_words) <= len(right_words) else right_words
                    if shorter == shared and (
                            len(shorter) >= 2 or any(len(w) >= 4 for w in shorter)):
                        score = max(score, 0.85)
            if score > best:
                best = score
    return best


def _candidate_rank(film: Film, candidate: Film) -> tuple[int, float, int, int, str, str] | None:
    """Sort key for how plausible ``candidate`` is for ``film``, or None when
    too distant to offer."""
    if _different_ids(film, candidate):
        return None
    gap = _year_gap(film.year, candidate.year)
    sim = _title_similarity(film, candidate)
    plausible = (
        gap == 0
        or (gap is not None and gap <= 2 and sim >= 0.45)
        or ((gap is None or gap <= 5) and sim >= 0.60)
        or sim >= 0.78
    )
    if not plausible:
        return None
    has_title_signal = 1 if sim >= 0.45 else 0
    same_year = 1 if gap == 0 else 0
    closeness = -(gap if gap is not None else 2)
    return (has_title_signal, round(sim, 3), same_year, closeness,
            candidate.title.lower(), candidate.year)


def close_candidates(film: Film, pool: Sequence[Film],
                     limit: int = MAX_CANDIDATES) -> tuple[Film, ...]:
    """The closest candidate films in ``pool`` for an unmatched ``film``.

    Prioritises title similarity with close years, and always includes
    same-year films from ``pool`` so translated or original titles can still be
    recognised. Never includes a candidate whose TMDb or IMDb id conflicts with
    ``film``'s.
    """
    scored: list[tuple[tuple[int, float, int, int, str, str], Film]] = []
    for candidate in _dedupe_films(pool):
        rank = _candidate_rank(film, candidate)
        if rank is not None:
            scored.append((rank, candidate))
    scored.sort(key=lambda item: (
        -item[0][0], -item[0][1], -item[0][2], -item[0][3], item[0][4], item[0][5]
    ))
    return tuple(candidate for _rank, candidate in scored[:limit])


def collect_entries(movies: Sequence[plexserver.Movie],
                    films: Sequence[Film],
                    plan_ambiguous: Sequence[tuple[Film, Found]] = (),
                    max_entries: int = MAX_ENTRIES) -> list[Entry]:
    """Every ambiguous or candidate-bearing unmatched entry in a sync run.

    Ambiguous matches come first (both unplayed Plex films matching multiple
    Letterboxd entries from the plan, and Letterboxd entries matching multiple
    distinct Plex films), followed by unmatched Letterboxd entries that have at
    least one close candidate among Plex's unmatched films.
    """
    plex_films = _dedupe_films([movie.film for movie in movies])
    if not plex_films and not plan_ambiguous:
        return []
    in_plex = FilmIndex(plex_films)
    on_letterboxd = FilmIndex(films)

    matched_plex: set[Film] = set()
    for pfilm in plex_films:
        found = on_letterboxd.find_from(pfilm, in_plex)
        if found.matched:
            matched_plex.add(pfilm)
    unmatched_plex = [pfilm for pfilm in plex_films if pfilm not in matched_plex]

    ambiguous_entries: list[Entry] = []
    seen_subjects: set[tuple[str, tuple[str, str, str, str]]] = set()

    for pfilm, found in plan_ambiguous:
        cands = tuple(_dedupe_films(list(found.ambiguous)))
        if len(cands) >= 2:
            key = ("letterboxd", _film_identity(pfilm))
            if key not in seen_subjects:
                seen_subjects.add(key)
                ambiguous_entries.append(Entry(
                    kind="ambiguous", subject=pfilm, candidates=cands,
                    direction="letterboxd", how=found.how))

    unmatched_scored: list[tuple[tuple[int, float, int, int], Entry]] = []
    for lb_film in films:
        lb_key = ("plex", _film_identity(lb_film))
        if lb_key in seen_subjects:
            continue
        found = in_plex.find(lb_film)
        if found.ambiguous:
            distinct = tuple(_dedupe_films(list(found.ambiguous)))
            if len(distinct) >= 2:
                seen_subjects.add(lb_key)
                ambiguous_entries.append(Entry(
                    kind="ambiguous", subject=lb_film, candidates=distinct,
                    direction="plex", how=found.how))
                continue
        mutual = in_plex.find_from(lb_film, on_letterboxd)
        if mutual.matched:
            continue
        cands = close_candidates(lb_film, unmatched_plex)
        if not cands:
            continue
        seen_subjects.add(lb_key)
        top_rank = _candidate_rank(lb_film, cands[0])
        priority = (top_rank[0], top_rank[1], top_rank[2], top_rank[3]) \
            if top_rank is not None else (0, 0.0, 0, -99)
        unmatched_scored.append((
            priority,
            Entry(kind="unmatched", subject=lb_film, candidates=cands,
                  direction="plex"),
        ))

    unmatched_scored.sort(key=lambda item: (
        -item[0][0], -item[0][1], -item[0][2], -item[0][3],
        item[1].subject.title.lower(), item[1].subject.year,
    ))
    combined = ambiguous_entries + [entry for _prio, entry in unmatched_scored]
    if max_entries > 0 and len(combined) > max_entries:
        _detail("Letterboxd match suggestions capped at %d of %d entry(s)"
                % (max_entries, len(combined)))
        return combined[:max_entries]
    return combined


def _describe_film(film: Film) -> str:
    """One film as shown to the model: title, year, aliases and any ids."""
    parts = ["%s (%s)" % (film.title, film.year or "year unknown")]
    aliases = [alias for alias in film.aliases if alias and alias != film.title]
    if aliases:
        parts.append("aka: %s" % "; ".join(aliases))
    ids = [("%s:%s" % (scheme, value))
           for scheme, value in (("tmdb", film.tmdb), ("imdb", film.imdb)) if value]
    if ids:
        parts.append("[%s]" % ", ".join(ids))
    return " ".join(parts)


def prompt(entry: Entry) -> str:
    """What the model is told about one unmatched or ambiguous ``entry``."""
    if entry.direction == "letterboxd":
        subject_side, candidate_side = "Plex", "Letterboxd"
    else:
        subject_side, candidate_side = "Letterboxd", "Plex"

    if entry.kind == "ambiguous":
        lead = (
            "This %s film matched more than one %s film equally well%s. "
            "Pick which candidate below, if any, is the same film."
            % (subject_side, candidate_side,
               " (by %s)" % entry.how if entry.how else "")
        )
    elif entry.candidates:
        lead = (
            "This %s film had no automatic match in %s. Below are the closest "
            "candidate film(s) in the %s library. Pick which candidate, if any, "
            "is the same film, and optionally provide its TMDb movie id if you "
            "are sure." % (subject_side, candidate_side, candidate_side)
        )
    else:
        lead = (
            "This %s film had no automatic match in %s and no close candidate "
            "films were found in the library. Set \"choice\" to null; if you "
            "are sure of its TMDb movie id, provide it as a positive integer, "
            "otherwise null." % (subject_side, candidate_side)
        )

    sections = [
        lead,
        "## %s entry\n\n- %s" % (subject_side, _describe_film(entry.subject)),
    ]
    if entry.candidates:
        numbered = "\n".join(
            "%d. %s" % (index, _describe_film(cand))
            for index, cand in enumerate(entry.candidates, start=1)
        )
        sections.append("## Candidate %s films\n\n%s" % (candidate_side, numbered))
    else:
        sections.append("## Candidate %s films\n\n(none)" % candidate_side)
    sections.append(
        'Answer with JSON only: {"choice": <1..%d or null>, '
        '"tmdb": <positive integer or null>, '
        '"confidence": "high|medium|low", "reason": "<one concise sentence>"}'
        % len(entry.candidates)
        if entry.candidates else
        'Answer with JSON only: {"choice": null, '
        '"tmdb": <positive integer or null>, '
        '"confidence": "high|medium|low", "reason": "<one concise sentence>"}'
    )
    return "\n\n".join(sections) + "\n"


def _parse_choice(raw: object, count: int) -> tuple[int | None, str]:
    """Validate ``choice`` as None or a 1-based index in ``1..count``."""
    if raw is None or raw == "":
        return None, ""
    if isinstance(raw, str) and raw.strip().lower() == "none":
        return None, ""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, "invalid choice %r" % (raw,)
    if count == 0:
        return None, "invented candidate index %d (no candidates offered)" % raw
    if not (1 <= raw <= count):
        return None, "invented candidate index %d (only %d offered)" % (raw, count)
    return raw, ""


def _parse_tmdb(raw: object) -> tuple[str, str]:
    """Validate ``tmdb`` as empty or a strictly positive integer."""
    if raw is None or raw == "":
        return "", ""
    if isinstance(raw, bool):
        return "", "invalid TMDb id %r" % (raw,)
    if isinstance(raw, int):
        if raw > 0:
            return str(raw), ""
        return "", "invalid TMDb id %r (must be a positive integer)" % (raw,)
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.lower() == "none":
            return "", ""
        if text.isascii() and text.isdigit() and int(text) > 0:
            return str(int(text)), ""
        return "", "invalid TMDb id %r (must be a positive integer)" % (raw,)
    return "", "invalid TMDb id %r" % (raw,)


def parse_answer(answer: str, entry: Entry, model: str = "") -> Suggestion:
    """Strictly parse and guard a model's JSON answer for ``entry``.

    Rejects non-JSON answers, invented or out-of-range candidate indices,
    non-integer or non-positive TMDb ids, and TMDb ids that conflict with the
    subject or chosen candidate. Whenever ``rejected`` is set, ``choice`` is
    None and ``tmdb`` is "".
    """
    data = llmclient.first_json(answer)
    if not isinstance(data, dict):
        return Suggestion(entry=entry, rejected="answer was not a JSON object",
                          model=model)
    if "choice" not in data and "tmdb" not in data:
        return Suggestion(entry=entry,
                          rejected="answer omitted both choice and tmdb",
                          model=model)

    choice_index, choice_error = _parse_choice(data.get("choice"),
                                               len(entry.candidates))
    if choice_error:
        return Suggestion(entry=entry, rejected=choice_error, model=model)

    tmdb, tmdb_error = _parse_tmdb(data.get("tmdb"))
    if tmdb_error:
        return Suggestion(entry=entry, rejected=tmdb_error, model=model)

    chosen = entry.candidates[choice_index - 1] if choice_index is not None else None
    if (chosen is not None and choice_index is not None
            and chosen.tmdb and tmdb and tmdb != chosen.tmdb):
        return Suggestion(
            entry=entry,
            rejected="suggested TMDb id %s conflicts with candidate %d's tmdb:%s"
                     % (tmdb, choice_index, chosen.tmdb),
            model=model,
        )
    if entry.subject.tmdb and tmdb and tmdb != entry.subject.tmdb:
        return Suggestion(
            entry=entry,
            rejected="suggested TMDb id %s conflicts with entry's tmdb:%s"
                     % (tmdb, entry.subject.tmdb),
            model=model,
        )

    raw_conf = str(data.get("confidence") or "").strip().lower()
    confidence = raw_conf if raw_conf in _CONFIDENCES else ""
    reason = " ".join(str(data.get("reason") or "").split())[:MAX_REASON_CHARS]
    return Suggestion(
        entry=entry,
        choice=chosen,
        choice_index=choice_index,
        tmdb=tmdb,
        confidence=confidence,
        reason=reason,
        model=model,
    )


def _ask_one(entry: Entry, models: Sequence[llmclient.Llm],
             spans: modelmeter.Spans, tally: modelmeter.Tally) -> Suggestion:
    request = prompt(entry)
    last_error = ""
    for llm in models:
        try:
            with llm.serving(_detail) as bound:
                asker = bound.searching(PURPOSE) or bound
                with spans.timing():
                    answer = asker.ask(SYSTEM, request, tally)
            return parse_answer(answer, entry, model=llm.name)
        except llmclient.LlmError as error:
            last_error = str(error)
            _log("No Letterboxd match suggestion from %s for %s: %s"
                 % (llm.name, entry.subject, error))
    return Suggestion(
        entry=entry,
        rejected="model error: %s" % (last_error or "no model answered"),
    )


def suggest_matches(entries: Sequence[Entry], *,
                    models: Sequence[llmclient.Llm] | None = None,
                    meter_file: str = "") -> list[Suggestion]:
    """Ask a language model to suggest matches for ``entries`` (report only).

    ``models`` is the ladder to try, best first - the roster's ladder for
    :data:`llmroster.LETTERBOXD_MATCH` unless given. Returns ``[]`` when
    disabled, when ``entries`` is empty, or when no model is available. What
    the model was asked is logged and, with ``meter_file``, recorded via
    :func:`modelmeter.record`.
    """
    if not entries:
        return []
    if not enabled():
        _detail("No model asked for Letterboxd matches: the %s setting is off"
                % SWITCH_VARIABLE)
        return []
    if models is None:
        models, why = llmroster.optional(llmroster.LETTERBOXD_MATCH)
        if not models:
            _detail("No model asked for Letterboxd matches: %s" % why)
            return []
    if not models:
        return []

    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    suggestions: list[Suggestion] = []
    try:
        for entry in entries:
            suggestions.append(_ask_one(entry, models, spans, tally))
    finally:
        if tally.requests:
            _log(modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    return suggestions


def suggest_for_sync(movies: Sequence[plexserver.Movie],
                     films: Sequence[Film],
                     plan_ambiguous: Sequence[tuple[Film, Found]] = (), *,
                     models: Sequence[llmclient.Llm] | None = None,
                     meter_file: str = "",
                     max_entries: int = MAX_ENTRIES) -> list[Suggestion]:
    """Collect unmatched/ambiguous entries from a sync run and ask a model for
    report-only match suggestions."""
    if not enabled():
        _detail("No model asked for Letterboxd matches: the %s setting is off"
                % SWITCH_VARIABLE)
        return []
    entries = collect_entries(movies, films, plan_ambiguous,
                              max_entries=max_entries)
    if not entries:
        return []
    return suggest_matches(entries, models=models, meter_file=meter_file)


def _short_film(film: Film) -> str:
    label = str(film)
    if film.tmdb:
        return "%s [tmdb:%s]" % (label, film.tmdb)
    return label


def format_suggestions(suggestions: Sequence[Suggestion]) -> list[str]:
    """Human-readable report lines for ``suggestions`` (every decision said)."""
    if not suggestions:
        return []
    lines = [
        "Model match suggestions (report only - nothing changed in Plex): "
        "%d entry(s) reviewed" % len(suggestions)
    ]
    for suggestion in suggestions:
        entry = suggestion.entry
        subject = _short_film(entry.subject)
        tag = "[%s]" % entry.kind
        if suggestion.rejected:
            lines.append("  %s %s: no suggestion (rejected: %s)"
                         % (subject, tag, suggestion.rejected))
            continue
        conf = " (%s confidence)" % suggestion.confidence if suggestion.confidence else ""
        why = " - %s" % suggestion.reason if suggestion.reason else ""
        if suggestion.choice is not None:
            target_side = "Letterboxd" if entry.direction == "letterboxd" else "Plex"
            target = "%s: %s" % (target_side, _short_film(suggestion.choice))
            if suggestion.tmdb and suggestion.tmdb != suggestion.choice.tmdb:
                target += ", suggested tmdb:%s" % suggestion.tmdb
            lines.append("  %s %s -> %s%s%s" % (subject, tag, target, conf, why))
        elif suggestion.tmdb:
            lines.append("  %s %s -> suggested tmdb:%s%s%s"
                         % (subject, tag, suggestion.tmdb, conf, why))
        else:
            lines.append("  %s %s -> no match%s%s" % (subject, tag, conf, why))
    return lines


def report_suggestions(suggestions: Sequence[Suggestion]) -> None:
    """Print every model match decision into the sync report."""
    for line in format_suggestions(suggestions):
        runlog.result(line)
