"""Asking a web-searching language model for the release year of an undated
film folder: a concert, special, documentary or feature whose folder and file
names carry no year.

Only asked when :func:`medialib.lib.filmfolders.read_undated_folder` found no
year in the folder or its files. The model is given the folder's name, its
file names and, where measured, the video's running time and container write
year, and asked to verify the original release year on the web.

Strict guards keep a guess from ever touching the library:

- The answer must be a JSON object with an integer ``year`` in
  ``1888..current_year + 1`` (booleans, strings and floats are refused) and
  explicit high confidence (``"high"`` or ``"confident"``).
- Where the file container records a written/muxed year, any release year
  after that year is refused.
- The returned year is **never** used to rename or mutate files directly: it
  only feeds the deterministic TMDb identification ladder
  (:mod:`medialib.lib.tmdbidentify` / :mod:`medialib.lib.tmdblookup`), which
  still verifies title, runtime and container-year guards.

The ``undatedYearLlm`` setting turns this rung off. What the model was asked
is counted (:mod:`medialib.lib.modelmeter`), logged, and optionally recorded
for the run's total.
"""

from __future__ import annotations

import datetime
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from medialib.lib import formatting, llmclient, llmroster, modelmeter, runlog
from medialib.lib.enums import shell_lower

__all__ = ["SWITCH_VARIABLE", "PURPOSE", "MIN_YEAR", "YearLookup", "enabled",
           "prompt", "parse_answer", "lookup_year", "remember_answers"]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "undatedYearLlm"

# What the model's requests are for, which names a harness's sessions and
# modelmeter rate lines.
PURPOSE = "film-year"

# Roundhay Garden Scene (1888) - earliest plausible motion picture release year.
MIN_YEAR = 1888

# How many file names from the folder are listed in the prompt.
MAX_LISTED_FILES = 50

# The only confidence values that allow a year to be supplied to identification.
CONFIDENT_LEVELS = frozenset({"high", "confident"})

_log = runlog.log
_detail = runlog.detail

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, YearLookup] | None = None

SYSTEM = """You identify the original release year of a film, concert film, \
television special, or documentary for a personal video library. You can \
search the web and read pages (such as TMDb, IMDb, Wikipedia, or official \
release listings).

You are given an undated folder's name, its file names, and (when available) \
the video's running time and the year the file container was written.

Rules:
1. Return the ORIGINAL release year (the year the film, concert, special, or \
documentary first premiered or was released) ONLY when you are certain which \
specific work this is and have verified its release year from a reliable \
source.
2. When a running time is given, use it to distinguish remakes or identically \
named works; do not pick a work whose runtime clearly contradicts the file.
3. When a file written/muxed year is given, the original release year cannot \
be later than that year.
4. A wrong year is worse than none. If multiple works share the title and \
cannot be distinguished, or you cannot verify the year reliably, return \
{"year": null, "confidence": "unsure", "source": "", "reason": "..."}.

Answer with JSON only, in this exact shape:
{"year": 2019, "confidence": "high", "source": "https://... or source name", \
"reason": "one short sentence"}
Use "confidence": "high" only when you are certain; otherwise use "low" or \
"unsure" and set "year" to null."""


@dataclass(frozen=True)
class YearLookup:
    """Outcome of asking a model for an undated folder's release year.

    ``year`` is a 4-digit year string only when every guard passed; otherwise
    ``year`` is ``""`` and ``rejected`` says why.
    """

    year: str = ""
    confidence: str = ""
    source: str = ""
    reason: str = ""
    rejected: str = ""


def enabled() -> bool:
    """Whether a run asks the model for undated folder years: unless off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def remember_answers(on: bool = True) -> None:
    """Hold model year lookups in memory for the duration of a run, or drop
    them - keeps full ingest's pre-scan and final tagging from asking twice."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = {} if on else None


def _clean(value: object, limit: int = 240) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def prompt(folder_name: str, title: str, file_names: Sequence[str] = (), *,
           runtime_seconds: float = 0.0, written_year: str = "",
           parent_name: str = "") -> str:
    """What the model is told about one undated film folder."""
    sorted_files = sorted(str(name) for name in file_names if str(name).strip())
    listed = sorted_files[:MAX_LISTED_FILES]
    file_lines = ["- " + name for name in listed]
    if len(sorted_files) > len(listed):
        file_lines.append("- ... and %d more file(s)" % (
            len(sorted_files) - len(listed)))

    folder_lines = ["Folder name: %s" % folder_name]
    if title and title != folder_name:
        folder_lines.append("Parsed title: %s" % title)
    if parent_name:
        folder_lines.append("Parent folder: %s" % parent_name)

    media_lines: list[str] = []
    if runtime_seconds > 0:
        minutes = round(runtime_seconds / 60.0)
        media_lines.append("Measured running time: %s (%d min)" % (
            formatting.fmt_clock(runtime_seconds), minutes))
    if written_year and written_year.isdigit() and len(written_year) == 4:
        media_lines.append("File container written year: %s (release year "
                           "must not be later than this)" % written_year)

    sections = [
        "Find the original release year for this undated film, concert, "
        "special or documentary.",
        "## Folder\n\n" + "\n".join(folder_lines),
        "## Files\n\n" + ("\n".join(file_lines) or "(none)"),
    ]
    if media_lines:
        sections.append("## Media details\n\n" + "\n".join(media_lines))
    return "\n\n".join(sections) + "\n"


def parse_answer(answer: str, *, written_year: str = "",
                 max_year: int | None = None) -> YearLookup:
    """Validate the model's JSON reply strictly.

    Returns a :class:`YearLookup` with ``year`` populated only when ``year``
    is a non-boolean integer within ``MIN_YEAR..max_year`` (defaulting to
    ``current_year + 1``), not after ``written_year``, and ``confidence`` is
    explicitly ``"high"`` or ``"confident"``.
    """
    data = llmclient.first_json(answer)
    if not isinstance(data, dict):
        return YearLookup(rejected="reply contained no JSON object")

    source = _clean(data.get("source"))
    reason = _clean(data.get("reason"))
    raw_conf = data.get("confidence")
    confidence = raw_conf.strip().lower() if isinstance(raw_conf, str) else ""
    raw_year = data.get("year")

    if raw_year is None:
        why = ("model was unsure" if confidence not in CONFIDENT_LEVELS
               else "model returned no year")
        return YearLookup(confidence=confidence, source=source,
                          reason=reason, rejected=why)

    if confidence not in CONFIDENT_LEVELS:
        return YearLookup(
            confidence=confidence, source=source, reason=reason,
            rejected="confidence %r is not high" % (confidence or "missing"))

    if isinstance(raw_year, bool) or not isinstance(raw_year, int):
        return YearLookup(
            confidence=confidence, source=source, reason=reason,
            rejected="year %r is not an integer" % (raw_year,))

    upper = max_year if max_year is not None else datetime.date.today().year + 1
    if raw_year < MIN_YEAR or raw_year > upper:
        return YearLookup(
            confidence=confidence, source=source, reason=reason,
            rejected="year %d is outside %d..%d" % (raw_year, MIN_YEAR, upper))

    if (written_year and written_year.isdigit() and len(written_year) == 4
            and raw_year > int(written_year)):
        return YearLookup(
            confidence=confidence, source=source, reason=reason,
            rejected="year %d is after file written year %s" % (
                raw_year, written_year))

    return YearLookup(year=str(raw_year), confidence=confidence,
                      source=source, reason=reason)


def _describe(verdict: YearLookup, include_rejected: bool = False) -> str:
    parts: list[str] = []
    if include_rejected and verdict.rejected:
        parts.append(verdict.rejected)
    if verdict.source:
        parts.append("source: %s" % verdict.source)
    if verdict.reason:
        parts.append(verdict.reason)
    return " - " + "; ".join(parts) if parts else ""


def lookup_year(folder_name: str, title: str,
                file_names: Sequence[str] = (), *,
                runtime_seconds: float | Callable[[], float] = 0.0,
                written_year: str | Callable[[], str] = "",
                parent_name: str = "",
                searchers: Sequence[llmclient.Llm] | None = None,
                log: Callable[[str], None] = _log,
                meter_file: str = "",
                max_year: int | None = None) -> YearLookup:
    """Look up the release year of an undated folder via a web-searching model.

    ``runtime_seconds`` and ``written_year`` may be values or zero-argument
    callables evaluated only once the setting is on and a web-capable model is
    available. Returns a :class:`YearLookup` whose ``year`` is ``""`` whenever
    disabled, unavailable, errored, unsure, or rejected by validation.
    """
    if not enabled():
        _detail('  "%s": no model asked for a release year: the %s setting is '
                "off" % (folder_name, SWITCH_VARIABLE))
        return YearLookup()
    use_cache = searchers is None
    if searchers is None:
        searchers, why = llmroster.optional(llmroster.FILM_YEAR)
        if not searchers:
            _detail('  "%s": no model asked for a release year: %s' % (
                folder_name, why))
            return YearLookup()

    seconds = runtime_seconds() if callable(runtime_seconds) else runtime_seconds
    written = written_year() if callable(written_year) else written_year

    cache_key = (folder_name, title, tuple(sorted(file_names)),
                 round(seconds), written, parent_name)
    if use_cache:
        with _CACHE_LOCK:
            if _CACHE is not None and cache_key in _CACHE:
                return _CACHE[cache_key]

    request = prompt(folder_name, title, file_names, runtime_seconds=seconds,
                     written_year=written, parent_name=parent_name)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    used_model = ""
    try:
        for llm in searchers:
            searcher = llm.searching(PURPOSE)
            if searcher is None:
                continue
            try:
                with spans.timing():
                    answer = searcher.ask(SYSTEM, request, tally)
                used_model = llm.name
                break
            except llmclient.LlmError as error:
                log('  "%s": no release year from %s: %s' % (
                    folder_name, llm.name, error))
    finally:
        if tally.requests:
            log("  " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)

    if not answer:
        return YearLookup()

    verdict = parse_answer(answer, written_year=written, max_year=max_year)
    model_tag = " (%s)" % used_model if used_model else ""
    if verdict.year:
        log('  "%s": model%s suggests release year %s (%s)%s' % (
            folder_name, model_tag, verdict.year, verdict.confidence,
            _describe(verdict)))
    else:
        log('  "%s": model%s gave no usable release year%s' % (
            folder_name, model_tag, _describe(verdict, include_rejected=True)))

    if use_cache:
        with _CACHE_LOCK:
            if _CACHE is not None:
                _CACHE[cache_key] = verdict
    return verdict
