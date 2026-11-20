"""Ask a language model about ambiguous movie folders and differing cuts.

Two situations leave ``ingest-movies`` unable to settle names by rules alone:

1. An ambiguous film folder holds stray video files that name rules cannot
   assign (another cut, a sequel, a different film, or an extra).
2. Two cuts of the same film differ by more than a minute
   (:func:`medialib.lib.plexcompare.different_versions`) without edition tags
   saying which cut each one is.

The model's proposals are **suggestions only**: they are logged in the run's
output and written into ``ingest-movies-ambiguous-*.txt`` for a person to act
on. Nothing here ever renames or moves a file.

Every proposal is strictly validated before it is accepted:

* Referenced files MUST match one of the exact file names supplied in the
  request.
* Stray classifications MUST be one of :data:`STRAY_KINDS`.
* Suggested edition names are sanitized so they contain no path separators,
  braces, or control characters and stay within :data:`MAX_EDITION_CHARS`.

Switched off by ``ambiguousEditionsLlm=off`` (:data:`SWITCH_VARIABLE`). Only a
model that can search the web is asked (the ``ambiguous-editions`` task in
:mod:`medialib.lib.llmroster`).
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from medialib.lib import (
    durationcheck,
    filmfolders,
    formatting,
    llmclient,
    llmroster,
    modelmeter,
    plexnames,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "STRAY_KINDS",
    "MAX_LISTED_FILES",
    "MAX_EDITION_CHARS",
    "MAX_REASON_CHARS",
    "StrayProposal",
    "CutInfo",
    "CutProposal",
    "enabled",
    "sanitize_edition",
    "stray_prompt",
    "parse_strays",
    "classify_strays",
    "cuts_prompt",
    "parse_cuts",
    "identify_cuts",
    "format_stray_proposal",
    "format_cut_proposal",
    "format_stray_report_lines",
    "format_cut_report_lines",
    "suggest_for_ambiguous",
]

# The setting that switches the model off. Read each time so tests can flip it
# through the environment.
SWITCH_VARIABLE = "ambiguousEditionsLlm"

# Passed to Llm.searching and modelmeter.rate_line.
PURPOSE = "editions"

# Allowed classifications for a stray video file inside an ambiguous folder.
STRAY_KINDS = ("same_film_edition", "sequel", "another_film", "extra",
               "unsure")

# Cap on directory entries included in a folder prompt.
MAX_LISTED_FILES = 200

# Bounds on sanitized edition labels and explanations.
MAX_EDITION_CHARS = 80
MAX_REASON_CHARS = 300

# Words a model may emit in an edition field to mean "no edition known".
_UNSURE_WORDS = frozenset({"unsure", "unknown", "none", "n/a", "na", "null"})

# Matches a wrapped Plex edition tag such as "{edition-Director's Cut}".
_WRAPPED_EDITION_RE = re.compile(r"^\{\s*edition\s*[-:]\s*(.*?)\s*\}$",
                                 re.IGNORECASE)

# Characters forbidden in a sanitized edition name (path separators and braces).
_FORBIDDEN_EDITION_CHARS = frozenset("/\\{}")

STRAY_SYSTEM = """\
You are classifying stray video files inside a movie folder that automated
filename rules could not resolve.

You may search the web to check film titles, foreign or alternative titles,
sequels, spin-offs, cuts/editions, and bonus features.

For each stray video file listed, classify it as exactly one of:
- "same_film_edition": another cut, restoration, release, or language version
  of this folder's film. Also provide "edition": a short, clean edition name
  for a Plex {edition-...} tag (e.g. "Director's Cut", "Extended",
  "Unrated", "Colorized", "German"), or "" if you cannot name the edition.
- "sequel": a sequel, prequel, or other installment in the same series
  (belongs in its own film folder).
- "another_film": a different film altogether (belongs in its own folder).
- "extra": bonus material, featurette, making-of, interview, trailer, deleted
  scene, or short (belongs in a bonus subfolder).
- "unsure": uncertain; do not guess when evidence is weak.

Echo each file's exact name from the prompt. Do not invent file names.

Reply with JSON only, no prose around it:
{"files": [{"file": "<exact file name>",
            "kind": "same_film_edition|sequel|another_film|extra|unsure",
            "edition": "<short edition name or empty>",
            "reason": "<one concise sentence>"}]}"""

CUTS_SYSTEM = """\
You are identifying which known cut or edition of a film each video file is,
given the film's title, year, TMDb/IMDb id, file names, and exact runtimes.

You may search the web (IMDb alternate versions, TMDb, DVDCompare,
Movie-Censorship, Wikipedia) to match each runtime against known cuts of this
film (for example: "Theatrical", "Director's Cut", "Extended", "Unrated",
"Final Cut", "International", "Special Edition", "Workprint").

A wrong edition name is worse than saying unsure: if the runtimes do not
clearly match known cuts of the film, set "edition" to "unsure".

Echo each cut's exact file name from the prompt. Do not invent file names.

Reply with JSON only, no prose around it:
{"cuts": [{"file": "<exact file name>",
           "edition": "<short edition name or unsure>",
           "reason": "<one concise sentence citing the matched runtime or why unsure>"}]}"""


@dataclass(frozen=True)
class StrayProposal:
    """One stray video file's classification suggested by the model."""

    file: str
    kind: str
    edition: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CutInfo:
    """One version of a film held up against another cut."""

    file: str
    seconds: float
    edition: str = ""
    role: str = ""


@dataclass(frozen=True)
class CutProposal:
    """One cut's suggested edition name, or unsure."""

    file: str
    edition: str = ""
    unsure: bool = False
    reason: str = ""


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def _detail(message: str) -> None:
    """Why no model was asked - quiet unless verbose, like aicover."""
    if runlog.verbosity() >= runlog.VERBOSE:
        runlog.detail(message)


def _strip_controls(text: str) -> str:
    """Replace line/tab whitespace with spaces and drop all other control
    characters."""
    out: list[str] = []
    for ch in text:
        if ch in ("\r", "\n", "\t"):
            out.append(" ")
        elif ord(ch) < 32 or ord(ch) == 127 or unicodedata.category(ch).startswith("C"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def _clean_reason(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(_strip_controls(value).split())
    if len(cleaned) > MAX_REASON_CHARS:
        cleaned = cleaned[:MAX_REASON_CHARS].rstrip()
    return cleaned


def sanitize_edition(value: object) -> str:
    """Clean a model-suggested edition name for safe display as
    ``{edition-...}``.

    Strips wrapping ``{edition-...}`` if present, removes path separators,
    braces, and control characters, collapses whitespace, rejects placeholder
    words such as ``"unsure"``, and caps length at :data:`MAX_EDITION_CHARS`.
    Returns ``""`` when nothing valid remains.
    """
    if not isinstance(value, str):
        return ""
    text = _strip_controls(value).strip()
    wrapped = _WRAPPED_EDITION_RE.match(text)
    if wrapped:
        text = wrapped.group(1).strip()
    if not text or text.casefold() in _UNSURE_WORDS:
        return ""
    # Remove path separators and braces so no edition label can smuggle paths
    # or break Plex's {edition-...} syntax.
    text = "".join(" " if ch in _FORBIDDEN_EDITION_CHARS else ch for ch in text)
    text = " ".join(text.split()).strip(plexnames.SEPARATORS + " -_.,:;")
    if not text or text.casefold() in _UNSURE_WORDS:
        return ""
    if not any(ch.isalnum() for ch in text):
        return ""
    if len(text) > MAX_EDITION_CHARS:
        text = text[:MAX_EDITION_CHARS].rstrip(plexnames.SEPARATORS + " -_.,:;")
    return text


def _format_runtime(seconds: float) -> str:
    """Human-readable runtime with both minutes and clock form."""
    rounded = int(round(seconds))
    minutes, rem = divmod(rounded, 60)
    return "%dm %02ds (%s, %ds)" % (
        minutes, rem, formatting.fmt_clock(seconds), rounded)


def _folder_entries(folder_path: str) -> list[str]:
    """Files directly inside ``folder_path`` with human-readable sizes, capped
    at :data:`MAX_LISTED_FILES`."""
    try:
        entries = sorted(os.listdir(folder_path))
    except OSError:
        return []
    shown: list[str] = []
    for name in entries[:MAX_LISTED_FILES]:
        full = os.path.join(folder_path, name)
        try:
            if os.path.isdir(full):
                shown.append("  %s/" % name)
            else:
                shown.append("  %s  (%s)" % (
                    name, formatting.fmt_bytes(os.path.getsize(full))))
        except OSError:
            shown.append("  %s" % name)
    if len(entries) > MAX_LISTED_FILES:
        shown.append("  ... (%d more)" % (len(entries) - MAX_LISTED_FILES))
    return shown


def stray_prompt(folder_path: str, reason: str, strays: Sequence[str], *,
                 title: str = "", year: str = "", tag: str = "",
                 all_files: Sequence[str] | None = None,
                 runtimes: Mapping[str, float] | None = None) -> str:
    """Build the user prompt for classifying stray video files in an ambiguous
    movie folder."""
    folder_name = os.path.basename(folder_path.rstrip("/\\")) or folder_path
    parent_name = os.path.basename(os.path.dirname(folder_path.rstrip("/\\")))
    lines = ["Folder: %s" % folder_name]
    if parent_name:
        lines.append("Parent folder: %s" % parent_name)
    if title:
        lines.append("Film title: %s" % title)
    if year:
        lines.append("Year: %s" % year)
    if tag:
        lines.append("ID tag: %s" % tag)
    if reason:
        lines.append("Flagged because: %s" % reason)

    if all_files is not None:
        listed = ["  %s" % name for name in all_files[:MAX_LISTED_FILES]]
        if len(all_files) > MAX_LISTED_FILES:
            listed.append("  ... (%d more)" % (len(all_files) - MAX_LISTED_FILES))
    else:
        listed = _folder_entries(folder_path)
    if listed:
        lines.append("")
        lines.append("All files in folder:")
        lines.extend(listed)

    lines.append("")
    lines.append("Stray video files to classify (use these exact file names):")
    for name in strays:
        seconds = (runtimes or {}).get(name, 0.0)
        if seconds > 0:
            lines.append("  - %s  [runtime: %s]" % (
                name, _format_runtime(seconds)))
        else:
            lines.append("  - %s" % name)
    return "\n".join(lines)


def parse_strays(answer: str,
                 allowed_files: Sequence[str]) -> list[StrayProposal]:
    """Parse and strictly validate the model's stray-file classifications.

    Every returned proposal has ``file`` equal to one of ``allowed_files``,
    ``kind`` in :data:`STRAY_KINDS`, and a sanitized ``edition`` (kept only
    for ``same_film_edition``). Unrecognized files or invalid kinds are
    dropped.
    """
    data = llmclient.first_json(answer)
    if isinstance(data, dict):
        items = data.get("files")
    elif isinstance(data, list):
        items = data
    else:
        return []
    if not isinstance(items, list):
        return []

    allowed = set(allowed_files)
    by_file: dict[str, StrayProposal] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("file")
        if not isinstance(name, str) or name not in allowed or name in by_file:
            continue
        raw_kind = item.get("kind")
        if not isinstance(raw_kind, str):
            continue
        kind = raw_kind.strip().lower()
        if kind not in STRAY_KINDS:
            continue
        edition = (sanitize_edition(item.get("edition"))
                   if kind == "same_film_edition" else "")
        reason = _clean_reason(item.get("reason"))
        by_file[name] = StrayProposal(
            file=name, kind=kind, edition=edition, reason=reason)

    return [by_file[name] for name in allowed_files if name in by_file]


def cuts_prompt(title: str, year: str, cuts: Sequence[CutInfo], *,
                tmdb: str = "", imdb: str = "") -> str:
    """Build the user prompt for identifying known cuts/editions from file
    names and runtimes."""
    lines: list[str] = []
    if title:
        lines.append("Film title: %s" % title)
    if year:
        lines.append("Year: %s" % year)
    if tmdb:
        lines.append("TMDb id: %s" % tmdb)
    if imdb:
        lines.append("IMDb id: %s" % imdb)
    lines.append("")
    lines.append("Cuts to identify (use these exact file names):")
    for cut in cuts:
        bits = ["runtime: %s" % _format_runtime(cut.seconds)]
        if cut.edition:
            bits.append("existing edition tag: %s" % cut.edition)
        if cut.role:
            bits.append("copy: %s" % cut.role)
        lines.append("  - %s  [%s]" % (cut.file, "; ".join(bits)))
    if len(cuts) >= 2:
        runtimes = [cut.seconds for cut in cuts if cut.seconds > 0]
        if len(runtimes) >= 2:
            gap = max(runtimes) - min(runtimes)
            lines.append("")
            lines.append("Runtime difference: %s" % _format_runtime(gap))
    return "\n".join(lines)


def parse_cuts(answer: str,
               allowed_files: Sequence[str]) -> list[CutProposal]:
    """Parse and strictly validate the model's cut/edition proposals.

    Every returned proposal has ``file`` equal to one of ``allowed_files``.
    Any unrecognized file name is dropped. An empty, null, or ``"unsure"``
    edition becomes ``CutProposal(..., edition="", unsure=True)``.
    """
    data = llmclient.first_json(answer)
    if isinstance(data, dict):
        items = data.get("cuts")
    elif isinstance(data, list):
        items = data
    else:
        return []
    if not isinstance(items, list):
        return []

    allowed = set(allowed_files)
    by_file: dict[str, CutProposal] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("file")
        if not isinstance(name, str) or name not in allowed or name in by_file:
            continue
        if "edition" not in item and item.get("unsure") is not True:
            continue
        reason = _clean_reason(item.get("reason"))
        if item.get("unsure") is True:
            by_file[name] = CutProposal(
                file=name, edition="", unsure=True, reason=reason)
            continue
        edition = sanitize_edition(item.get("edition"))
        if edition:
            by_file[name] = CutProposal(
                file=name, edition=edition, unsure=False, reason=reason)
        else:
            by_file[name] = CutProposal(
                file=name, edition="", unsure=True, reason=reason)

    return [by_file[name] for name in allowed_files if name in by_file]


def format_stray_proposal(proposal: StrayProposal) -> str:
    """One-line human-readable suggestion for a stray video file."""
    label = proposal.kind
    if proposal.kind == "same_film_edition" and proposal.edition:
        label = "%s ({edition-%s})" % (proposal.kind, proposal.edition)
    line = 'model suggestion for "%s": %s' % (proposal.file, label)
    if proposal.reason:
        line += " - " + proposal.reason
    return line


def format_cut_proposal(proposal: CutProposal) -> str:
    """One-line human-readable suggestion for a film cut/edition."""
    label = ("unsure" if proposal.unsure or not proposal.edition
             else "{edition-%s}" % proposal.edition)
    line = 'model suggestion for "%s": %s' % (proposal.file, label)
    if proposal.reason:
        line += " - " + proposal.reason
    return line


def format_stray_report_lines(proposals: Sequence[StrayProposal]) -> list[str]:
    """Lines ready to embed under an ambiguous folder in
    ``ingest-movies-ambiguous-*.txt``."""
    return [format_stray_proposal(proposal) for proposal in proposals]


def format_cut_report_lines(proposals: Sequence[CutProposal]) -> list[str]:
    """Lines ready to embed under a differing-cut entry in
    ``ingest-movies-ambiguous-*.txt``."""
    return [format_cut_proposal(proposal) for proposal in proposals]


def _resolve_searchers(
    searchers: Sequence[llmclient.Llm] | None,
) -> Sequence[llmclient.Llm]:
    """Return the models to try, or an empty sequence if none are available."""
    if searchers is not None:
        return searchers
    ladder, why = llmroster.optional(llmroster.AMBIGUOUS_EDITIONS)
    if not ladder:
        _detail("    No model asked for ambiguous editions: %s" % why)
        return ()
    return ladder


def _ask(system: str, request: str, *,
         searchers: Sequence[llmclient.Llm] | None = None,
         meter_file: str = "",
         log: Callable[[str], None] = runlog.log,
         tally: modelmeter.Tally | None = None,
         spans: modelmeter.Spans | None = None) -> str:
    """Send one request through the web-capable ladder, metering and recording
    usage."""
    if not enabled():
        _detail("    No model asked for ambiguous editions: the %s setting "
                "is off" % SWITCH_VARIABLE)
        return ""
    ladder = _resolve_searchers(searchers)
    if not ladder:
        return ""

    call_tally = modelmeter.Tally()
    call_spans = modelmeter.Spans()
    answer = ""
    try:
        for llm in ladder:
            searcher = llm.searching(PURPOSE)
            if searcher is None:
                continue
            try:
                with call_spans.timing():
                    answer = searcher.ask(system, request, call_tally)
                break
            except llmclient.LlmError as error:
                log("    No edition suggestion from %s: %s" % (
                    llm.name, error))
    finally:
        if call_tally.requests:
            log("    " + modelmeter.rate_line(
                PURPOSE, call_spans.seconds, call_tally))
        if meter_file:
            modelmeter.record(meter_file, call_spans, call_tally)
        if tally is not None:
            tally.merge(call_tally)
        if spans is not None:
            spans.merge(call_spans)
    return answer


def classify_strays(folder_path: str, reason: str, strays: Sequence[str], *,
                    title: str = "", year: str = "", tag: str = "",
                    all_files: Sequence[str] | None = None,
                    runtimes: Mapping[str, float] | None = None,
                    searchers: Sequence[llmclient.Llm] | None = None,
                    meter_file: str = "",
                    log: Callable[[str], None] = runlog.log,
                    tally: modelmeter.Tally | None = None,
                    spans: modelmeter.Spans | None = None
                    ) -> list[StrayProposal]:
    """Ask the model to classify stray video files in an ambiguous folder.

    Logs every validated model decision to ``log``. Never renames or moves any
    file.
    """
    if not strays:
        return []
    if not enabled():
        _detail("    No model asked for ambiguous editions: the %s setting "
                "is off" % SWITCH_VARIABLE)
        return []
    request = stray_prompt(
        folder_path, reason, strays, title=title, year=year, tag=tag,
        all_files=all_files, runtimes=runtimes)
    answer = _ask(STRAY_SYSTEM, request, searchers=searchers,
                  meter_file=meter_file, log=log, tally=tally, spans=spans)
    if not answer:
        return []
    proposals = parse_strays(answer, strays)
    if proposals:
        for proposal in proposals:
            log("    " + format_stray_proposal(proposal))
    else:
        log("    Model gave no valid stray-file suggestions")
    return proposals


def identify_cuts(title: str, year: str, cuts: Sequence[CutInfo], *,
                  tmdb: str = "", imdb: str = "",
                  searchers: Sequence[llmclient.Llm] | None = None,
                  meter_file: str = "",
                  log: Callable[[str], None] = runlog.log,
                  tally: modelmeter.Tally | None = None,
                  spans: modelmeter.Spans | None = None
                  ) -> list[CutProposal]:
    """Ask the model which known cut/edition each runtime matches.

    Logs every validated model decision to ``log``. Never renames or moves any
    file.
    """
    if len(cuts) < 2:
        return []
    if not enabled():
        _detail("    No model asked for ambiguous editions: the %s setting "
                "is off" % SWITCH_VARIABLE)
        return []
    request = cuts_prompt(title, year, cuts, tmdb=tmdb, imdb=imdb)
    answer = _ask(CUTS_SYSTEM, request, searchers=searchers,
                  meter_file=meter_file, log=log, tally=tally, spans=spans)
    if not answer:
        return []
    proposals = parse_cuts(answer, [cut.file for cut in cuts])
    if proposals:
        for proposal in proposals:
            log("    " + format_cut_proposal(proposal))
    else:
        log("    Model gave no valid cut/edition suggestions")
    return proposals


def _probe_runtimes(folder_path: str,
                    names: Sequence[str]) -> dict[str, float]:
    """Measure durations of files that exist on disk; returns empty when
    ffprobe is absent or files are stubs."""
    runtimes: dict[str, float] = {}
    for name in names:
        full = os.path.join(folder_path, name)
        if not os.path.isfile(full):
            continue
        seconds = durationcheck.media_duration(full)
        if seconds > 0:
            runtimes[name] = seconds
    return runtimes


def suggest_for_ambiguous(
    folders: Sequence[tuple[str, str, Sequence[str]]], *,
    searchers: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
    log: Callable[[str], None] = runlog.log,
    tally: modelmeter.Tally | None = None,
    spans: modelmeter.Spans | None = None,
    probe_runtimes: bool = True,
) -> dict[str, list[str]]:
    """Ask the model for suggestions on each ambiguous folder from tagging.

    Returns a mapping ``folder_path -> formatted suggestion lines`` ready for
    :func:`medialib.lib.tmdbworklists.write_ambiguous_list`. Never renames or
    moves any file.
    """
    if not folders:
        return {}
    if not enabled():
        _detail("    No model asked for ambiguous editions: the %s setting "
                "is off" % SWITCH_VARIABLE)
        return {}
    ladder = _resolve_searchers(searchers)
    if not ladder:
        return {}

    suggestions: dict[str, list[str]] = {}
    for folder_path, reason, movies in folders:
        if not movies:
            continue
        folder_name = os.path.basename(folder_path.rstrip("/\\"))
        base, raw_tag, title, year, _edition = filmfolders.read_folder(
            folder_name)
        if not base:
            base, raw_tag = plexnames.untagged_base(folder_name)
        tag = plexnames.canonical_id(raw_tag) if raw_tag else ""
        runtimes = (_probe_runtimes(folder_path, movies)
                    if probe_runtimes else {})
        log('  Model check for ambiguous folder "%s":' % (base or folder_path))
        proposals = classify_strays(
            folder_path, reason, movies, title=title, year=year, tag=tag,
            runtimes=runtimes, searchers=ladder, meter_file=meter_file,
            log=log, tally=tally, spans=spans)
        if proposals:
            suggestions[folder_path] = format_stray_report_lines(proposals)
    return suggestions
