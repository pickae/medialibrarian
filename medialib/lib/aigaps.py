"""Asking a language model that can search the web to check the numbered,
episodic and dated runs found by :mod:`medialib.cli.find_gaps` against real
episode lists, season counts, specials and podcast release schedules.

The deterministic pass of ``find-gaps`` only spots interior gaps: it cannot
tell when a season stops at E08 out of 10, knows nothing about specials or
genuinely short seasons, and flags a podcast's scheduled winter hiatus as a
missing stretch. A model that can search the web (:data:`llmroster.GAPS_CHECK`)
can check the real release list and report:

* suspected missing trailing/final episodes, chapters or seasons
  (``missing_final``);
* deterministic date gaps that match a known hiatus or scheduled break
  (``false_gap_break``);
* genuinely short seasons or unusual numbering (``short_season_note``);
* notable specials or OVAs absent from a series (``missing_special``);
* deterministic gaps confirmed against the real schedule
  (``confirmed_missing``).

This step is strictly **report only**: it never modifies or deletes any files,
and never removes deterministic gaps from the core tree. Its structured JSON
output is validated strictly against the input folders and allowed finding
kinds before being appended as a clearly separated model-notes section.

The ``findGapsLlm`` setting turns this step off. What the model was asked is
counted (:mod:`medialib.lib.modelmeter`), logged, and recorded when a meter
file is given.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from medialib.lib import llmclient, llmroster, modelmeter, runlog
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "VALID_KINDS",
    "KIND_LABELS",
    "RunSummary",
    "Finding",
    "enabled",
    "normalize_folder",
    "select_runs",
    "prompt",
    "parse_findings",
    "format_findings",
    "check_gaps",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "findGapsLlm"

# What the model's requests are for, which names a harness's sessions and its
# modelmeter line.
PURPOSE = "gaps"

# Bounds on how much is sent to the model and how much is accepted back.
MAX_RUNS = 50
BATCH_SIZE = 25
MAX_SAMPLE_NAMES = 6
MAX_NAME_CHARS = 160
MAX_FINDINGS = 100
MAX_TARGET_CHARS = 120
MAX_NOTE_CHARS = 300
MAX_SOURCE_CHARS = 240

VALID_KINDS = (
    "missing_final",
    "false_gap_break",
    "short_season_note",
    "missing_special",
    "confirmed_missing",
)

KIND_LABELS = {
    "missing_final": "missing final",
    "false_gap_break": "likely hiatus/break",
    "short_season_note": "short season",
    "missing_special": "missing special",
    "confirmed_missing": "confirmed missing",
}

_URI_WITH_AUTHORITY = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
_UNSAFE_SCHEME = re.compile(r"^(?:file|javascript|data|vbscript):", re.IGNORECASE)

_log = runlog.log
_detail = runlog.detail

SYSTEM = """You review a personal media library's numbered, episodic, season \
and dated runs using web search to augment a deterministic gap report.

You are strictly report-only: never suggest modifying or deleting files, and \
never claim a folder exists that was not in the input. Deterministic gaps \
already listed in the input stay in the report; you add notes and suspected \
omissions that deterministic numbering cannot see.

Look up the real episode list, season count, specials, or podcast/show \
release schedule for each recognizable series, show, podcast, or numbered \
work, and report ONLY high-confidence findings backed by a real source. When \
unsure, or when a run looks complete and ordinary, omit it — an empty list is \
better than guessing.

Allowed values for "kind":
- "missing_final": trailing/final episodes, chapters or seasons missing after \
the highest item present (e.g. folder ends at S01E08 when Season 1 has 10 \
episodes, or has Seasons 1-2 when Season 3 aired).
- "false_gap_break": a deterministic date gap that corresponds to a known \
scheduled hiatus, seasonal break, or off-air period.
- "short_season_note": a genuinely short season, miniseries, or split-cour \
numbering worth noting so the user does not mistake it for an incomplete run.
- "missing_special": notable specials, OVAs, or S00 episodes belonging to the \
series/season that are absent.
- "confirmed_missing": a deterministic gap confirmed against the real release \
schedule (e.g. specific episodes or dates that aired during a flagged gap).

Answer with JSON only, in this exact form:
{"findings": [{"folder": "<exact folder path from input>", \
"kind": "<one of the 5 allowed kinds>", \
"target": "<episode/range/date gap or empty string>", \
"note": "<one concise sentence explaining the finding>", \
"source": "<https://... URL or brief source name>"}]}

Return {"findings": []} when nothing needs noting."""


@dataclass(frozen=True)
class RunSummary:
    """One detected run in a folder: episodes, dates, numbers, seasons or
    numbered sub-folders."""

    folder: str
    shape: str
    present: str
    gaps: tuple[str, ...] = ()
    sample_names: tuple[str, ...] = ()
    cadence: str = ""
    date_range: str = ""


@dataclass(frozen=True)
class Finding:
    """One strictly validated model finding referencing an input folder."""

    folder: str
    kind: str
    note: str
    target: str = ""
    source: str = ""


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def normalize_folder(folder: str) -> str:
    """Canonical relative folder key with forward slashes and no trailing slash."""
    cleaned = folder.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.rstrip("/")
    return cleaned or "."


def _collapse(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _is_disallowed_uri(text: str) -> bool:
    """Whether ``text`` uses a non-http(s) URI scheme such as file:// or javascript:."""
    if _UNSAFE_SCHEME.match(text):
        return True
    match = _URI_WITH_AUTHORITY.match(text)
    if match:
        scheme = urllib.parse.urlsplit(text).scheme.lower()
        return scheme not in ("http", "https")
    return False


def _run_priority(run: RunSummary) -> tuple[int, int]:
    """Runs with deterministic gaps first, then episodes/dates/seasons/numbers."""
    shape_rank = {"episodes": 0, "dates": 1, "seasons": 2, "numbers": 3, "folders": 4}
    return (0 if run.gaps else 1, shape_rank.get(run.shape, 5))


def select_runs(runs: Sequence[RunSummary], limit: int = MAX_RUNS) -> list[RunSummary]:
    """Up to ``limit`` runs to send to the model, prioritizing runs with
    deterministic gaps and episodic/dated runs while preserving input order."""
    if len(runs) <= limit:
        return list(runs)
    ranked = sorted(range(len(runs)), key=lambda idx: (_run_priority(runs[idx]), idx))
    chosen = set(ranked[:limit])
    return [run for idx, run in enumerate(runs) if idx in chosen]


def prompt(root_name: str, runs: Sequence[RunSummary]) -> str:
    """Build the user prompt describing ``runs`` under ``root_name``."""
    lines = [
        "Check these numbered, episodic and dated runs under library folder: %s"
        % _collapse(root_name or ".", MAX_NAME_CHARS),
        "",
        "Use the exact `folder` value shown for each entry when returning findings.",
    ]
    for run in runs:
        folder = normalize_folder(run.folder)
        lines.append("")
        lines.append("## Folder: %s" % folder)
        lines.append("- Kind: %s" % _collapse(run.shape, 40))
        lines.append("- Present: %s" % _collapse(run.present, MAX_NOTE_CHARS))
        if run.date_range:
            cadence = " (%s)" % _collapse(run.cadence, 40) if run.cadence else ""
            lines.append("- Date range: %s%s" % (_collapse(run.date_range, 80), cadence))
        if run.gaps:
            lines.append("- Deterministic gaps: %s" % "; ".join(
                _collapse(gap, MAX_NAME_CHARS) for gap in run.gaps))
        else:
            lines.append("- Deterministic gaps: none")
        if run.sample_names:
            samples = [_collapse(name, MAX_NAME_CHARS)
                       for name in run.sample_names[:MAX_SAMPLE_NAMES]]
            lines.append("- Sample names: %s" % "; ".join(samples))
    return "\n".join(lines) + "\n"


def parse_findings(answer: str, valid_folders: Collection[str]) -> list[Finding]:
    """Strictly parse and validate the model's JSON findings against
    ``valid_folders`` and :data:`VALID_KINDS`."""
    if not valid_folders:
        return []
    canonical_folders = {normalize_folder(folder): normalize_folder(folder)
                         for folder in valid_folders}
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return []
    raw_items = found.get("findings")
    if not isinstance(raw_items, list):
        return []

    kept: list[Finding] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in raw_items:
        if len(kept) >= MAX_FINDINGS:
            break
        if not isinstance(item, dict):
            continue
        raw_folder = item.get("folder")
        if not isinstance(raw_folder, str) or not raw_folder.strip():
            continue
        folder = canonical_folders.get(normalize_folder(raw_folder))
        if folder is None:
            continue

        raw_kind = item.get("kind")
        if not isinstance(raw_kind, str):
            continue
        kind = raw_kind.strip().lower()
        if kind not in VALID_KINDS:
            continue

        raw_note = item.get("note")
        if not isinstance(raw_note, str):
            continue
        note = _collapse(raw_note, MAX_NOTE_CHARS)
        if not note:
            continue

        raw_target = item.get("target")
        if raw_target is None:
            target = ""
        elif isinstance(raw_target, str):
            target = _collapse(raw_target, MAX_TARGET_CHARS)
        else:
            continue

        raw_source = item.get("source")
        if raw_source is None:
            source = ""
        elif isinstance(raw_source, str):
            source = _collapse(raw_source, MAX_SOURCE_CHARS)
            if source and _is_disallowed_uri(source):
                continue
        else:
            continue

        key = (folder, kind, target, note)
        if key in seen:
            continue
        seen.add(key)
        kept.append(Finding(folder=folder, kind=kind, note=note,
                            target=target, source=source))
    return kept


def format_findings(findings: Sequence[Finding]) -> list[str]:
    """Render validated findings as a clearly separated report section that
    leaves deterministic gaps untouched."""
    if not findings:
        return []
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.folder, []).append(finding)

    lines = ["Model notes (web-checked; deterministic gaps above are unchanged):"]
    for folder, items in grouped.items():
        lines.append("." if folder == "." else folder + "/")
        for item in items:
            label = KIND_LABELS.get(item.kind, item.kind)
            body = ("%s: %s" % (item.target, item.note)
                    if item.target else item.note)
            suffix = " (source: %s)" % item.source if item.source else ""
            lines.append("  [%s] %s%s" % (label, body, suffix))
    lines.append("")
    lines.append("%d model note(s) across %d folder(s)" % (len(findings), len(grouped)))
    return lines


def check_gaps(root_name: str, runs: Sequence[RunSummary], *,
               searchers: Sequence[llmclient.Llm] | None = None,
               meter_file: str = "") -> list[Finding]:
    """Ask a web-searching model (:data:`llmroster.GAPS_CHECK`) to review
    ``runs`` under ``root_name`` and return strictly validated findings.

    Returns ``[]`` when there are no runs, ``findGapsLlm`` is off, no
    web-searching model is available, or the model fails/returns nothing.
    Usage is counted and logged via :mod:`medialib.lib.modelmeter` (and
    recorded to ``meter_file`` when given).
    """
    if not runs:
        return []
    if not enabled():
        _detail("No model asked for gap notes: the %s setting is off"
                % SWITCH_VARIABLE)
        return []
    if searchers is None:
        searchers, why = llmroster.optional(llmroster.GAPS_CHECK)
        if not searchers:
            _detail("No model asked for gap notes: %s" % why)
            return []

    selected = select_runs(runs, MAX_RUNS)
    batches = [selected[i:i + BATCH_SIZE]
               for i in range(0, len(selected), BATCH_SIZE)]
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    findings: list[Finding] = []
    seen: set[tuple[str, str, str, str]] = set()
    try:
        for batch in batches:
            request = prompt(root_name, batch)
            answer = ""
            for llm in searchers:
                searcher = llm.searching(PURPOSE)
                if searcher is None:
                    continue
                try:
                    with spans.timing():
                        answer = searcher.ask(SYSTEM, request, tally)
                    break
                except llmclient.LlmError as error:
                    _log("No gap notes from %s: %s" % (llm.name, error))
            if not answer:
                continue
            valid = {normalize_folder(run.folder) for run in batch}
            for item in parse_findings(answer, valid):
                if len(findings) >= MAX_FINDINGS:
                    break
                key = (item.folder, item.kind, item.target, item.note)
                if key not in seen:
                    seen.add(key)
                    findings.append(item)
    finally:
        if tally.requests:
            _log(modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    return findings
