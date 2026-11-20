"""Asking a language model for an advisory likely cause and next step on
unexplained errors in a run's error log.

:mod:`medialib.lib.errorlog` matches common failures deterministically against
:data:`errorlog.NOTABLE` (damaged frames, a full disk, out-of-memory, network
failures, permission errors) and :func:`errorlog.signal_cause`. What falls
through - an unfamiliar ffmpeg filter error, a codec failure, a Python
traceback, an unexpected exit - is left in the log with no ``cause:`` line.

At the end of a run that opened an error log and recorded at least one such
unmatched error, this module reads the log back, deduplicates and caps the
unmatched records so a huge failure run fits comfortably in context, and asks
the :data:`llmroster.ERROR_TRIAGE` task for a concise likely cause and next
step for each. That task prefers a local model (``prefers=("reasoning",
"local")``) because error logs carry library paths.

Strictly advisory: nothing here retries a command, deletes a file or alters a
run's exit code. A model failure or malformed reply never breaks exit cleanup.
Turned off with ``errorTriageLlm=off``; counted through
:mod:`medialib.lib.modelmeter`.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from medialib.lib import errorlog, llmclient, llmroster, modelmeter, runcontext, runlog
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "MAX_RECORDS",
    "MAX_RECORD_CHARS",
    "MAX_OUTPUT_LINES",
    "MAX_FIELD_CHARS",
    "ADVISORY_MARKER",
    "SYSTEM",
    "ErrorRecord",
    "Advice",
    "enabled",
    "parse_records",
    "is_matched",
    "unmatched_records",
    "deduplicate",
    "prompt",
    "parse_advice",
    "format_console",
    "format_log",
    "triage_file",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "errorTriageLlm"

# What the model's requests are for, in modelmeter's rate line.
PURPOSE = "triage"

# Bounds keeping large failure runs within a small model's context window.
MAX_RECORDS = 12
MAX_RECORD_CHARS = 1500
MAX_OUTPUT_LINES = 25
MAX_LINE_CHARS = 240
MAX_FIELD_CHARS = 240

# Banner written into the error log so a triaged log is never triaged twice
# and parse_records never mistakes the advisory section for an error record.
ADVISORY_MARKER = "ADVISORY TRIAGE"

_SEPARATOR = "=" * 78

_RECORD_HEAD = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} [+-]\d{4}  (ERROR|WARNING)  (.+)$"
)
_INPUT_STATUS = re.compile(r"\s+\[(?:exists|not there)\]$")
_HEX_ADDR = re.compile(r"\b0x[0-9a-fA-F]+\b")
_POSIX_PATH = re.compile(r"(?:(?<=^)|(?<=[\s'\"=(:]))/(?:[^\s'\"<>|;:)]+/)*[^\s'\"<>|;:)]+")
_WIN_PATH = re.compile(r"[A-Za-z]:\\(?:[^\s'\"<>|;:]+\\)*[^\s'\"<>|;:]+")

# Kinds whose meaning is already deterministic from CLI parsing or the OS
# itself: no model call is needed to explain a typo on the command line or a
# missing input folder.
_SELF_EXPLAINING_KINDS = frozenset((
    "usage",
    "input-missing",
    "input-unusable",
    "output-inside-input",
    "config",
    "tool-missing",
    "disk-full",
    "out-of-memory",
    "permission",
))

SYSTEM = """You inspect unexplained error records from a media library \
command's error log. Your advice is strictly advisory: the user reads it at \
the end of the run to decide what to do next.

For each numbered record, give:
1. "cause": the most likely cause, grounded strictly in the log evidence shown.
2. "next_step": one concrete, safe next step the user can take to verify or \
fix it.

Rules:
- Base your answer ONLY on the record shown; never invent file paths, flags \
or errors not present in the log.
- Keep each "cause" and "next_step" to one concise plain-text sentence (no \
newlines, no markdown).
- If a record lacks enough detail to diagnose, omit it rather than guessing.

Answer with JSON only, in this form:
{"entries": [{"index": 1, "cause": "...", "next_step": "..."}]}
Use {"entries": []} when no record can be diagnosed."""

_detail = runlog.detail
_log = runlog.log
_say = runlog.say

# Guards against re-entrant triage if anything inside a triage call writes to
# errorlog or triggers triage again.
_TRIAGE_LOCK = threading.Lock()
_TRIAGING = False


@dataclass(frozen=True)
class ErrorRecord:
    """One error record parsed from a run's error log."""

    index: int
    severity: str
    kind: str
    what: str
    means: str = ""
    causes: tuple[str, ...] = ()
    item: str = ""
    input_path: str = ""
    tool: str = ""
    status: str = ""
    output_lines: tuple[str, ...] = ()
    traceback_lines: tuple[str, ...] = ()
    count: int = 1


@dataclass(frozen=True)
class Advice:
    """One advisory cause and next step returned for a record index."""

    index: int
    cause: str
    next_step: str


def enabled() -> bool:
    """Whether a run asks a model for error-log triage: unless set to off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def parse_records(text: str) -> list[ErrorRecord]:
    """The error records in ``text`` (the contents of an error log file).

    Non-record blocks - such as an appended :data:`ADVISORY_MARKER` section -
    are ignored.
    """
    records: list[ErrorRecord] = []
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line == _SEPARATOR:
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)

    for block in blocks:
        lines = [line for line in block if line.strip()]
        if not lines:
            continue
        match = _RECORD_HEAD.match(lines[0])
        if not match:
            continue
        severity, kind = match.group(1), match.group(2).strip()
        what = means = item = input_path = tool = status = ""
        causes: list[str] = []
        output_lines: list[str] = []
        traceback_lines: list[str] = []
        section = ""

        for line in block[1:]:
            if not line.strip():
                continue
            if line.startswith("what:"):
                section = ""
                what = line[len("what:"):].strip()
            elif line.startswith("means:"):
                section = ""
                means = line[len("means:"):].strip()
            elif line.startswith("cause:"):
                section = ""
                value = line[len("cause:"):].strip()
                if value:
                    causes.append(value)
            elif line.startswith("item:"):
                section = ""
                item = line[len("item:"):].strip()
            elif line.startswith("input:"):
                section = ""
                raw_input = line[len("input:"):].strip()
                input_path = _INPUT_STATUS.sub("", raw_input)
            elif line.startswith("tool:"):
                section = ""
                tool = line[len("tool:"):].strip()
            elif line.startswith("status:"):
                section = ""
                status = line[len("status:"):].strip()
            elif line.startswith("tool output ("):
                section = "output"
            elif line.startswith("traceback ("):
                section = "traceback"
            elif line.startswith(("files:", "called from (", "run:",
                                  "started:", "process:", "for:")):
                section = ""
            elif section == "output" and line.startswith("  | "):
                output_lines.append(line[4:])
            elif section == "traceback":
                traceback_lines.append(line)

        records.append(ErrorRecord(
            index=len(records) + 1,
            severity=severity,
            kind=kind,
            what=what,
            means=means,
            causes=tuple(causes),
            item=item,
            input_path=input_path,
            tool=tool,
            status=status,
            output_lines=tuple(output_lines),
            traceback_lines=tuple(traceback_lines),
        ))
    return records


def is_matched(record: ErrorRecord) -> bool:
    """Whether ``record`` already has a deterministic explanation: a recorded
    ``cause:`` line, a self-explaining kind, or any line matching
    :data:`errorlog.NOTABLE`."""
    if record.causes:
        return True
    if record.kind in _SELF_EXPLAINING_KINDS:
        return True
    if record.what and errorlog.notable_key(record.what):
        return True
    if any(errorlog.notable_key(line) for line in record.output_lines):
        return True
    return any(errorlog.notable_key(line) for line in record.traceback_lines)


def unmatched_records(records: Sequence[ErrorRecord]) -> list[ErrorRecord]:
    """The records in ``records`` that no :data:`errorlog.NOTABLE` pattern or
    deterministic cause matched."""
    return [record for record in records if not is_matched(record)]


def _normalize_text(text: str, record: ErrorRecord) -> str:
    """Strip per-item paths and memory addresses so identical failures across
    different files share one signature."""
    cleaned = text
    for needle, token in (
        (record.input_path, "<input>"),
        (record.item, "<item>"),
        (os.path.basename(record.item) if record.item else "", "<item>"),
    ):
        if needle:
            cleaned = cleaned.replace(needle, token)
    cleaned = _HEX_ADDR.sub("0x...", cleaned)
    cleaned = _WIN_PATH.sub("<path>", cleaned)
    cleaned = _POSIX_PATH.sub("<path>", cleaned)
    return " ".join(cleaned.split()).lower()


def _signature(record: ErrorRecord) -> tuple[str, str, str, str]:
    """Deduplication key for ``record``."""
    tool_prog = record.tool.split()[0] if record.tool else ""
    if tool_prog:
        tool_prog = os.path.basename(tool_prog)
    detail_lines = [line for line in record.traceback_lines if line.strip()]
    if not detail_lines:
        detail_lines = [line for line in record.output_lines if line.strip()]
    if detail_lines:
        tail = "\n".join(_normalize_text(line, record)
                         for line in detail_lines[-3:])
    else:
        tail = _normalize_text(record.what, record)
    return (record.kind, tool_prog, record.status, tail)


def deduplicate(records: Sequence[ErrorRecord],
                limit: int = MAX_RECORDS) -> tuple[list[ErrorRecord], int]:
    """Collapse repeated failures into unique records (keeping the first
    occurrence and counting duplicates), re-numbered ``1..N`` and capped at
    ``limit``. Returns ``(kept, omitted_unique_count)``."""
    grouped: dict[tuple[str, str, str, str], tuple[ErrorRecord, int]] = {}
    order: list[tuple[str, str, str, str]] = []
    for record in records:
        key = _signature(record)
        if key in grouped:
            first, count = grouped[key]
            grouped[key] = (first, count + record.count)
        else:
            grouped[key] = (record, record.count)
            order.append(key)

    limit = max(0, limit)
    kept_keys = order[:limit]
    omitted = max(0, len(order) - len(kept_keys))
    kept = [
        replace(grouped[key][0], index=at, count=grouped[key][1])
        for at, key in enumerate(kept_keys, start=1)
    ]
    return kept, omitted


def _clip_line(line: str, limit: int = MAX_LINE_CHARS) -> str:
    text = line.rstrip()
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 3)].rstrip() + "..."


def _bounded_lines(lines: Sequence[str], limit: int = MAX_OUTPUT_LINES) -> list[str]:
    if len(lines) <= limit:
        return [_clip_line(line) for line in lines]
    head = min(5, limit // 3)
    tail = limit - head
    omitted = len(lines) - head - tail
    return (
        [_clip_line(line) for line in lines[:head]]
        + ["[... %d line(s) omitted ...]" % omitted]
        + [_clip_line(line) for line in lines[-tail:]]
    )


def _format_record(record: ErrorRecord) -> str:
    header = "Record %d" % record.index
    if record.count > 1:
        header += " (occurred %d times)" % record.count
    parts = [
        header,
        "kind: %s (%s)" % (record.kind, record.severity),
    ]
    if record.what:
        parts.append("what: %s" % _clip_line(record.what))
    if record.item:
        parts.append("item: %s" % _clip_line(record.item))
    if record.input_path:
        parts.append("input: %s" % _clip_line(record.input_path))
    if record.tool:
        parts.append("tool: %s" % _clip_line(record.tool))
    if record.status:
        parts.append("status: %s" % _clip_line(record.status))
    if record.traceback_lines:
        parts.append("traceback:")
        parts.extend("  " + line for line in _bounded_lines(record.traceback_lines))
    if record.output_lines:
        parts.append("tool output:")
        parts.extend("  | " + line for line in _bounded_lines(record.output_lines))

    text = "\n".join(parts)
    if len(text) > MAX_RECORD_CHARS:
        marker = "\n[... truncated ...]\n"
        budget = max(40, MAX_RECORD_CHARS - len(marker))
        head = budget // 2
        tail = budget - head
        text = text[:head].rstrip() + marker + text[-tail:].lstrip()
    return text


def prompt(records: Sequence[ErrorRecord], omitted: int = 0) -> str:
    """What the model is asked about the deduplicated unmatched ``records``."""
    sections = [
        "Diagnose these %d unexplained error record(s) from the run's error log."
        % len(records)
    ]
    for record in records:
        sections.append(_format_record(record))
    if omitted > 0:
        sections.append("Note: %d additional unique error pattern(s) were omitted." % omitted)
    indices = ", ".join(str(record.index) for record in records)
    sections.append(
        'Respond with JSON only: {"entries": [{"index": <number in {%s}>, '
        '"cause": "<one concise sentence>", '
        '"next_step": "<one concise sentence>"}]}' % indices
    )
    return errorlog.redact("\n\n".join(sections) + "\n")


def _clean_field(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text:
        return ""
    if len(text) > MAX_FIELD_CHARS:
        text = text[:MAX_FIELD_CHARS - 3].rstrip() + "..."
    return text


def parse_advice(answer: str, valid_indices: Iterable[int]) -> list[Advice]:
    """Strictly parse the model's JSON reply into validated :class:`Advice`
    entries. Invalid, duplicate, or out-of-range entries are dropped; malformed
    JSON yields an empty list."""
    allowed = set(valid_indices)
    if not allowed or not answer:
        return []
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return []
    entries = found.get("entries")
    if not isinstance(entries, list):
        return []

    seen: set[int] = set()
    parsed: list[Advice] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if index not in allowed or index in seen:
            continue
        cause = _clean_field(entry.get("cause"))
        next_step = _clean_field(entry.get("next_step"))
        if not cause or not next_step:
            continue
        seen.add(index)
        parsed.append(Advice(index=index, cause=cause, next_step=next_step))
    parsed.sort(key=lambda item: item.index)
    return parsed


def _record_label(record: ErrorRecord) -> str:
    label = record.item or record.what or record.kind
    label = _clip_line(" ".join(label.split()), 120)
    if record.count > 1:
        return "%s (x%d)" % (label, record.count)
    return label


def format_console(records: Sequence[ErrorRecord], advice: Sequence[Advice],
                   log_path: str = "", model_name: str = "") -> str:
    """Advisory lines formatted for stderr."""
    by_index = {record.index: record for record in records}
    lines: list[str] = []
    for item in advice:
        record = by_index.get(item.index)
        label = _record_label(record) if record is not None else "record %d" % item.index
        lines.append("    [%d] %s:" % (item.index, label))
        lines.append("        likely cause: %s" % item.cause)
        lines.append("        next step:    %s" % item.next_step)
    return errorlog.redact("\n".join(lines) + "\n") if lines else ""


def format_log(records: Sequence[ErrorRecord], advice: Sequence[Advice],
               model_name: str = "") -> str:
    """Advisory section appended to the run's error log file."""
    by_index = {record.index: record for record in records}
    who = "language model: %s, advisory only" % model_name if model_name else "advisory only"
    out = [_SEPARATOR, "%s (%s)" % (ADVISORY_MARKER, who)]
    for item in advice:
        record = by_index.get(item.index)
        if record is not None:
            label = "%s: %s" % (record.kind, _record_label(record))
        else:
            label = "record %d" % item.index
        out.append("[%d] %s" % (item.index, label))
        out.append("  likely cause: %s" % item.cause)
        out.append("  next step:    %s" % item.next_step)
    return errorlog.redact("\n".join(out) + "\n\n")


def _append_log(path: str, block: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as handle, \
                runcontext.take_lock(handle):
            handle.write(block)
            handle.flush()
    except OSError:
        pass


def triage_file(path: str, *,
                models: Sequence[llmclient.Llm] | None = None,
                meter_file: str = "",
                append: bool = True,
                announce: bool = True) -> list[Advice]:
    """Read ``path`` (a run's error log), ask a model for advisory likely
    cause and next step on each unique unmatched error record, append the
    advisory block to ``path`` when ``append`` is True, and print it on
    stderr when ``announce`` is True.

    Returns the validated :class:`Advice` entries (empty when disabled, no
    unmatched errors exist, no model is available, or the model fails). Never
    raises on model errors, malformed replies, or I/O failures.
    """
    global _TRIAGING
    with _TRIAGE_LOCK:
        if _TRIAGING:
            return []
        _TRIAGING = True
    try:
        return _triage_file_inner(path, models=models, meter_file=meter_file,
                                  append=append, announce=announce)
    finally:
        with _TRIAGE_LOCK:
            _TRIAGING = False


def _triage_file_inner(path: str, *,
                       models: Sequence[llmclient.Llm] | None,
                       meter_file: str,
                       append: bool,
                       announce: bool) -> list[Advice]:
    if not enabled():
        _detail("No model asked to triage errors: the %s setting is off"
                % SWITCH_VARIABLE)
        return []
    if not path:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return []
    if not text.strip() or ADVISORY_MARKER in text:
        return []

    unmatched = unmatched_records(parse_records(text))
    if not unmatched:
        return []

    unique, omitted = deduplicate(unmatched)
    if not unique:
        return []

    if models is None:
        models, why = llmroster.optional(llmroster.ERROR_TRIAGE)
        if not models:
            _detail("No model asked to triage errors: %s" % why)
            return []
    elif not models:
        return []

    request = prompt(unique, omitted=omitted)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    used_model = ""
    try:
        for llm in models:
            try:
                with llm.serving(_detail) as bound:
                    with spans.timing():
                        answer = bound.ask(SYSTEM, request, tally)
                    used_model = bound.model or llm.name
                break
            except llmclient.LlmError as error:
                _detail("Error triage skipped by %s: %s" % (llm.name, error))
    finally:
        if meter_file and tally.requests:
            modelmeter.record(meter_file, spans, tally)

    advice = parse_advice(answer, [record.index for record in unique])
    if not advice:
        if tally.requests:
            _detail("Error triage gave no usable advice (%s)"
                    % modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        return []

    if append:
        _append_log(path, format_log(unique, advice, model_name=used_model))
    if announce:
        who = " from %s" % used_model if used_model else ""
        _log("Advisory triage for %d unexplained error(s) in %s%s (advisory only):"
             % (len(advice), path, who))
        _say(format_console(unique, advice, log_path=path, model_name=used_model))
        if tally.requests:
            _log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
    return advice
