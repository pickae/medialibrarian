"""Reporting the albums beets skipped during an ``ingest-music`` run, and
asking a language model that can search the web for their MusicBrainz release
IDs so beets can retry them with ``--search-id``.

``medialib/config/beets.yaml`` runs beets with ``quiet_fallback: skip``, and
``ingest-music`` passes ``-l logs/beets.log``. Without reading that log back,
skipped albums sit untagged and without cover art while the run says nothing
about them.

This module works in two steps:

1. **No model**: read only the lines newly appended to ``beets.log`` during
   this run (from the byte offset recorded before ``beet import`` started),
   parse ``skip``, ``duplicate-skip`` and ``asis`` entries (including
   multi-disc ``; ``-separated paths), and report every one in the run's
   output.
2. **Optional web-search model**: for each unmatched ``skip`` album, send the
   folder and file names, track durations, existing tags and any rip logs or
   cue sheets (``.log``, ``.cue``, ``.nfo``, ``.txt``) to a model for the
   :data:`llmroster.MUSIC_SEARCH` task. Validate the returned MusicBrainz
   release ID strictly as a 36-character UUID, then rerun ``beet import`` on
   that album folder with ``--search-id <mbid>`` in quiet mode (``-CIq``),
   so beets itself still judges the match and skips weak candidates. Nothing
   is ever deleted or overwritten on the model's word alone.

The :data:`SWITCH_VARIABLE` setting turns the model step off while leaving
step 1 intact. Model requests are timed and counted with
:mod:`medialib.lib.modelmeter`.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from medialib.lib import (
    enums,
    formatting,
    llmclient,
    llmroster,
    modelmeter,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "BeetsSkip",
    "ModelDecision",
    "Summary",
    "enabled",
    "log_offset",
    "read_new_log",
    "split_beets_paths",
    "common_folder",
    "parse_beets_log",
    "read_skips",
    "valid_mbid",
    "probe_track",
    "prompt",
    "parse_answer",
    "find_release",
    "resolve_skipped",
]

# Whether the model is asked for unmatched albums: on unless set to off.
SWITCH_VARIABLE = "musicSearchLlm"

# What the model's requests are for, which names a harness's sessions.
PURPOSE = "music"

# Prompt bounds so large box sets or huge text files stay within budget.
MAX_LISTED_FILES = 200
MAX_TRACKS = 60
MAX_TAG_CHARS = 300
MAX_RIP_LOG_FILES = 4
MAX_RIP_LOG_CHARS = 4000
MAX_RIP_LOG_TOTAL_CHARS = 8000

# Sidecar text files that often carry disc TOC, edition, catalogue number or
# tracklist details, in priority order.
RIP_LOG_EXTENSIONS = ("log", "cue", "nfo", "txt")

# Verbs beets writes to its -l import log that mean an album was not matched
# normally. Informational verbs ("import", "duplicate-keep",
# "duplicate-replace") are ignored.
_SKIP_VERBS = frozenset(("skip", "duplicate-skip", "asis"))

_AUDIO_EXTENSIONS = frozenset(
    enums.AUDIO_EXTENSIONS + enums.LOSSLESS_AUDIO_EXTENSIONS
)

# Strict 36-character canonical UUID (MusicBrainz release ID).
_MBID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

SYSTEM = """You identify music releases in MusicBrainz for a personal music \
library. You can search the web and read pages. You are given everything \
known about one album folder that beets could not match automatically: its \
folder and file names, track count and durations, existing tags, and any rip \
logs, cue sheets or release notes in the folder.

Your job is to find the exact MusicBrainz RELEASE (not a release group, not \
an artist, not a recording) that matches these tracks:
1. Verify track count, track order, track durations, disc count, edition, \
country, year, label and catalogue number on musicbrainz.org.
2. Prefer the exact pressing/edition (e.g. CD vs vinyl vs digital, deluxe vs \
standard, remaster year) whose track count and track durations match the \
files and any rip log TOC or cue sheet.
3. Return a release ID ONLY when you have verified it on musicbrainz.org and \
are confident it is this release. A wrong release ID is worse than none: \
beets will still check the match, and returning null leaves the folder \
untouched.

Answer with JSON only, in this form:
{"mbid": "<36-character MusicBrainz release UUID or null>", \
"release": "<artist - album (year, format/edition)>", \
"why": "<one sentence on why this release matches>"}
Use {"mbid": null, "release": "", "why": "..."} when you cannot find a \
matching MusicBrainz release."""

_log = runlog.log
_detail = runlog.detail

TrackProbe = Callable[[str], tuple[float, dict[str, str]]]
BeetRunner = Callable[[str, list[str]], int]


@dataclass(frozen=True)
class BeetsSkip:
    """One album beets logged as skipped or imported as-is."""

    verb: str
    folder: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ModelDecision:
    """What a model answered for one unmatched album."""

    mbid: str = ""
    raw_mbid: str = ""
    release: str = ""
    why: str = ""
    model: str = ""


@dataclass
class Summary:
    """Counts of what happened to the albums beets skipped in this run."""

    skipped: int = 0
    duplicate_skips: int = 0
    asis: int = 0
    matched: int = 0
    still_skipped: int = 0

    @property
    def total_logged(self) -> int:
        return self.skipped + self.duplicate_skips + self.asis


def enabled() -> bool:
    """Whether a run asks the model for skipped albums: unless set to off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


# --- step zero: reading and parsing beets.log ---------------------------------


def log_offset(path: str) -> int:
    """Current byte size of ``path``, or 0 when it does not exist yet."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_new_log(path: str, offset: int = 0) -> str:
    """Text appended to ``path`` after byte ``offset``; ``""`` if unreadable.

    If the file shrank (truncated or recreated since ``offset`` was taken),
    reads from the start instead.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = offset if 0 < offset <= size else 0
            handle.seek(start)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _looks_abs(path: str) -> bool:
    """Whether ``path`` looks absolute on either POSIX or Windows."""
    return (
        os.path.isabs(path)
        or path.startswith("/")
        or bool(_WIN_DRIVE.match(path))
    )


def split_beets_paths(paths_text: str) -> list[str]:
    """Split a beets logfile path list joined with ``; ``.

    Handles folder names that themselves contain ``; `` the same way beets
    does: when the first chunk is absolute, any following chunk that is not
    absolute is stitched back onto the preceding path.
    """
    text = paths_text.strip()
    if not text:
        return []
    chunks = text.split("; ")
    if len(chunks) > 1 and _looks_abs(chunks[0]):
        merged: list[str] = []
        for chunk in chunks:
            if not merged or _looks_abs(chunk):
                merged.append(chunk)
            else:
                merged[-1] += "; " + chunk
        chunks = merged
    return [chunk for chunk in chunks if chunk]


def common_folder(paths: Sequence[str]) -> str:
    """Common directory of ``paths`` (for multi-disc entries), preserving the
    path style of the input."""
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    try:
        if all(p.startswith("/") and "\\" not in p for p in paths):
            return posixpath.commonpath(list(paths))
        if any(_WIN_DRIVE.match(p) or "\\" in p for p in paths):
            return ntpath.commonpath(list(paths))
        return os.path.commonpath(list(paths))
    except ValueError:
        return paths[0]


def parse_beets_log(text: str) -> list[BeetsSkip]:
    """Parse ``skip``, ``duplicate-skip`` and ``asis`` entries from beets
    import log text, deduplicated in order of appearance."""
    entries: list[BeetsSkip] = []
    seen: set[tuple[str, str]] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        verb, sep, rest = line.partition(" ")
        if not sep or verb not in _SKIP_VERBS:
            continue
        paths = tuple(split_beets_paths(rest))
        if not paths:
            continue
        folder = common_folder(paths)
        key = (verb, folder)
        if key in seen:
            continue
        seen.add(key)
        entries.append(BeetsSkip(verb=verb, folder=folder, paths=paths))
    return entries


def read_skips(log_path: str, offset: int = 0) -> list[BeetsSkip]:
    """New skip/asis entries appended to ``log_path`` since ``offset``."""
    return parse_beets_log(read_new_log(log_path, offset))


# --- validating MBIDs and probing album folders -------------------------------


def valid_mbid(value: object) -> str:
    """Return lowercase canonical MusicBrainz UUID if ``value`` is strictly
    a valid UUID string, else ``""``."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not _MBID.fullmatch(text):
        return ""
    return text.lower()


def probe_track(path: str) -> tuple[float, dict[str, str]]:
    """``(duration_seconds, tags)`` for one audio file via ffprobe, or
    ``(0.0, {})`` when ffprobe will not say."""
    try:
        done = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format=duration:format_tags:stream_tags",
                "-of",
                "json",
                path,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0, {}
    try:
        data = json.loads(done.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return 0.0, {}
    if not isinstance(data, dict):
        return 0.0, {}
    fmt = data.get("format") or {}
    try:
        seconds = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    tags: dict[str, str] = {}
    for stream in data.get("streams") or ():
        if isinstance(stream, dict):
            for key, value in (stream.get("tags") or {}).items():
                tags[shell_lower(str(key))] = str(value)
    for key, value in (fmt.get("tags") or {}).items():
        tags[shell_lower(str(key))] = str(value)
    return seconds, tags


def _walk_files(roots: Sequence[str], base: str) -> list[tuple[str, str]]:
    """``(absolute_path, relative_posix_path)`` for every file under
    ``roots``, sorted deterministically."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        for parent, dirs, names in os.walk(root):
            dirs.sort()
            for name in sorted(names):
                path = os.path.join(parent, name)
                if path in seen:
                    continue
                seen.add(path)
                rel = os.path.relpath(path, base).replace(os.sep, "/")
                found.append((path, rel))
    found.sort(key=lambda item: item[1])
    return found


def _listing(files: Sequence[tuple[str, str]]) -> list[str]:
    lines: list[str] = []
    for path, rel in files[:MAX_LISTED_FILES]:
        try:
            size = formatting.fmt_bytes(os.path.getsize(path))
        except OSError:
            size = "?"
        lines.append("%s (%s)" % (rel, size))
    if len(files) > MAX_LISTED_FILES:
        lines.append("... and %d more file(s)" % (len(files) - MAX_LISTED_FILES))
    return lines


def _sampled(items: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    if len(items) <= MAX_TRACKS:
        return list(items)
    tail = min(5, MAX_TRACKS // 4)
    head = MAX_TRACKS - tail
    return [*items[:head], *items[-tail:]]


def _clean_tags(raw: dict[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        text = " ".join(str(value).split())[:MAX_TAG_CHARS]
        if text:
            cleaned[shell_lower(str(key))] = text
    return cleaned


def _track_section(
    files: Sequence[tuple[str, str]],
    probe: TrackProbe,
) -> str:
    audio = [
        (path, rel)
        for path, rel in files
        if enums.lower_extension_of(rel) in _AUDIO_EXTENSIONS
    ]
    if not audio:
        return "(no audio files found)"
    sampled = _sampled(audio)
    probed: list[tuple[str, float, dict[str, str]]] = []
    for path, rel in sampled:
        seconds, raw_tags = probe(path)
        probed.append((rel, max(0.0, float(seconds or 0.0)), _clean_tags(raw_tags)))

    shared: dict[str, str] = {}
    if len(probed) >= 2:
        first_tags = probed[0][2]
        for key, value in sorted(first_tags.items()):
            if all(tags.get(key) == value for _rel, _sec, tags in probed[1:]):
                shared[key] = value

    total_seconds = sum(sec for _rel, sec, _tags in probed)
    lines: list[str] = []
    if shared:
        lines.append("Tags shared across all %d listed tracks:" % len(probed))
        lines.extend("  %s: %s" % (k, v) for k, v in shared.items())
        lines.append("")

    header = "Tracks (%d total" % len(audio)
    if len(sampled) < len(audio):
        header += ", showing %d" % len(sampled)
    if total_seconds > 0 and len(sampled) == len(audio):
        header += ", total %s" % formatting.fmt_clock(total_seconds)
    header += "):"
    lines.append(header)

    for rel, sec, tags in probed:
        dur = " (%s)" % formatting.fmt_clock(sec) if sec > 0 else ""
        distinct = {k: v for k, v in sorted(tags.items()) if k not in shared}
        if distinct:
            summary = "; ".join("%s: %s" % (k, v) for k, v in distinct.items())
            lines.append("- %s%s — %s" % (rel, dur, summary))
        elif shared:
            lines.append("- %s%s" % (rel, dur))
        else:
            lines.append("- %s%s — no tags" % (rel, dur))
    return "\n".join(lines)


def _read_text_file(path: str, limit: int) -> str:
    """Read up to ``limit`` characters of a text/log/cue file, handling
    UTF-16 (common in EAC rip logs) as well as UTF-8."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit * 4)
    except OSError:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "\n... (truncated)"
    return text


def _rip_logs_section(files: Sequence[tuple[str, str]]) -> str:
    priority = {ext: idx for idx, ext in enumerate(RIP_LOG_EXTENSIONS)}
    candidates = [
        (priority[ext], rel, path)
        for path, rel in files
        if (ext := enums.lower_extension_of(rel)) in priority
    ]
    candidates.sort()
    if not candidates:
        return ""
    blocks: list[str] = []
    budget = MAX_RIP_LOG_TOTAL_CHARS
    for _prio, rel, path in candidates[:MAX_RIP_LOG_FILES]:
        if budget <= 0:
            break
        text = _read_text_file(path, min(MAX_RIP_LOG_CHARS, budget))
        if not text:
            continue
        budget -= len(text)
        blocks.append("### %s\n\n%s" % (rel, text))
    return "\n\n".join(blocks)


def prompt(
    folder: str,
    *,
    probe: TrackProbe = probe_track,
    targets: Sequence[str] = (),
) -> str:
    """What the model is told about an album folder beets skipped."""
    norm = os.path.normpath(folder)
    parents = [part for part in norm.replace(os.sep, "/").split("/")[-4:-1] if part]
    roots = list(targets) if targets else [norm]
    files = _walk_files(roots, norm)
    sections = [
        "Find the exact MusicBrainz release UUID for this album that beets "
        "could not match.",
        "## The album folder\n\nIts name: %s\nThe folders it sits in, "
        "outermost first: %s\n\nIts files:\n%s"
        % (
            os.path.basename(norm),
            " / ".join(parents) or "(none)",
            "\n".join("- " + line for line in _listing(files)) or "(empty)",
        ),
        "## Audio tracks, durations and tags\n\n%s" % _track_section(files, probe),
    ]
    rip_logs = _rip_logs_section(files)
    if rip_logs:
        sections.append("## Rip logs, cue sheets and notes\n\n%s" % rip_logs)
    return "\n\n".join(sections) + "\n"


# --- asking the model ---------------------------------------------------------


def parse_answer(answer: str, model: str = "") -> ModelDecision:
    """Extract and strictly validate the MusicBrainz release ID from a model
    reply."""
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return ModelDecision(model=model)
    release = " ".join(str(found.get("release") or "").split())[:200]
    why = " ".join(str(found.get("why") or "").split())[:300]
    raw = found.get("mbid")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ModelDecision(release=release, why=why, model=model)
    mbid = valid_mbid(raw)
    if mbid:
        return ModelDecision(mbid=mbid, release=release, why=why, model=model)
    bad = " ".join(str(raw).split())[:120]
    return ModelDecision(raw_mbid=bad, release=release, why=why, model=model)


def find_release(
    folder: str,
    *,
    searchers: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
    probe: TrackProbe = probe_track,
    targets: Sequence[str] = (),
) -> ModelDecision:
    """Ask a web-searching model for the MusicBrainz release UUID of
    ``folder``. Returns an empty :class:`ModelDecision` if disabled, no
    model is available, or all models fail."""
    if not enabled():
        _detail(
            "    No model asked for MusicBrainz lookup: the %s setting is off"
            % SWITCH_VARIABLE
        )
        return ModelDecision()
    if searchers is None:
        searchers, why = llmroster.optional(llmroster.MUSIC_SEARCH)
        if not searchers:
            _detail("    No model asked for MusicBrainz lookup: %s" % why)
            return ModelDecision()

    request = prompt(folder, probe=probe, targets=targets)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    answered_by = ""
    try:
        for llm in searchers:
            searcher = llm.searching(PURPOSE)
            if searcher is None:
                continue
            try:
                with spans.timing():
                    answer = searcher.ask(SYSTEM, request, tally)
                answered_by = llm.name
                break
            except llmclient.LlmError as error:
                _log("    No MusicBrainz match from %s: %s" % (llm.name, error))
    finally:
        if tally.requests:
            _log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    if not answer:
        return ModelDecision()
    return parse_answer(answer, model=answered_by)


# --- orchestrating reporting and beets --search-id retries --------------------


def _resolve_folder(path: str, ingest_dir: str) -> str:
    if _looks_abs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(ingest_dir, path))


def _same_path(left: str, right: str) -> bool:
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except OSError:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )


def _display_name(folder: str, ingest_dir: str) -> str:
    if not ingest_dir:
        return folder
    try:
        rel = os.path.relpath(folder, ingest_dir)
    except ValueError:
        return folder
    if rel == "." or rel.startswith(".." + os.sep) or rel == "..":
        return folder
    return rel.replace(os.sep, "/")


def _retry_targets(entry: BeetsSkip, ingest_dir: str) -> tuple[str, list[str]]:
    """``(display_folder, beet_target_paths)`` for retrying ``entry``.

    For a single-disc or normal multi-disc album nested under its own folder,
    beets receives that common folder. If a multi-path entry's common folder
    would be ``ingest_dir`` itself (e.g. ``CD 1; CD 2`` directly in the root),
    only those specific disc directories are passed so sibling albums in
    ``ingest_dir`` are never touched.
    """
    folder = _resolve_folder(entry.folder, ingest_dir)
    resolved_paths = [_resolve_folder(p, ingest_dir) for p in entry.paths]
    if len(resolved_paths) > 1 and _same_path(folder, ingest_dir):
        existing = [p for p in resolved_paths if os.path.isdir(p)]
        return folder, existing
    return folder, ([folder] if os.path.isdir(folder) else [])


def _default_run_beet(directory: str, argv: list[str]) -> int:
    try:
        return subprocess.run(argv, cwd=directory).returncode
    except OSError:
        return 127


def resolve_skipped(
    ingest_dir: str,
    beets_config: str,
    beets_log: str,
    log_offset_before: int,
    *,
    searchers: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
    probe: TrackProbe = probe_track,
    run_beet: BeetRunner | None = None,
    dry_run: bool = False,
    beet_options: Sequence[str] = (),
    import_flags: str = "-CIq",
) -> Summary:
    """Read new skips from ``beets_log``, report them, and (when enabled and a
    web-searching model is available) look up MusicBrainz release IDs and
    retry beets in quiet mode with ``--search-id``.

    ``beet_options`` go before ``import`` (a music layout's ``-d``), and
    ``import_flags`` are the retry's: ``-CIq`` leaves the album where it is,
    and a layout that moves albums passes ``-mIq``."""
    entries = read_skips(beets_log, log_offset_before)
    if not entries:
        return Summary()

    unmatched = [e for e in entries if e.verb == "skip"]
    duplicates = [e for e in entries if e.verb == "duplicate-skip"]
    asis_entries = [e for e in entries if e.verb == "asis"]

    if duplicates:
        _log("  Beets skipped %d duplicate album(s):" % len(duplicates))
        for entry in duplicates:
            _log("    - %s" % _display_name(
                _resolve_folder(entry.folder, ingest_dir), ingest_dir
            ))

    if asis_entries:
        _log("  Beets imported %d album(s) as-is (unmatched):" % len(asis_entries))
        for entry in asis_entries:
            _log("    - %s" % _display_name(
                _resolve_folder(entry.folder, ingest_dir), ingest_dir
            ))

    if not unmatched:
        return Summary(
            duplicate_skips=len(duplicates),
            asis=len(asis_entries),
        )

    _log("  Beets skipped %d unmatched album(s):" % len(unmatched))
    for entry in unmatched:
        _log("    - %s" % _display_name(
            _resolve_folder(entry.folder, ingest_dir), ingest_dir
        ))

    summary = Summary(
        skipped=len(unmatched),
        duplicate_skips=len(duplicates),
        asis=len(asis_entries),
    )

    if not enabled():
        _log(
            "  No model asked for unmatched albums: the %s setting is off"
            % SWITCH_VARIABLE
        )
        summary.still_skipped = len(unmatched)
        return summary

    if searchers is None:
        searchers, why = llmroster.optional(llmroster.MUSIC_SEARCH)
        if not searchers:
            _log("  No model asked for unmatched albums: %s" % why)
            summary.still_skipped = len(unmatched)
            return summary

    runner = run_beet or _default_run_beet

    for entry in unmatched:
        folder, targets = _retry_targets(entry, ingest_dir)
        label = _display_name(folder, ingest_dir)
        if not targets:
            _log("  %s: folder no longer exists — leaving skipped" % label)
            summary.still_skipped += 1
            continue

        decision = find_release(
            folder,
            searchers=searchers,
            meter_file=meter_file,
            probe=probe,
            targets=targets,
        )
        if decision.raw_mbid:
            _log(
                "  %s: model returned invalid MusicBrainz ID %r — leaving skipped"
                % (label, decision.raw_mbid)
            )
            summary.still_skipped += 1
            continue
        if not decision.mbid:
            _log(
                "  %s: model found no confident MusicBrainz release%s"
                % (label, ": " + decision.why if decision.why else "")
            )
            summary.still_skipped += 1
            continue

        _log(
            "  %s: model suggests MusicBrainz release %s%s%s"
            % (
                label,
                decision.mbid,
                " (%s)" % decision.release if decision.release else "",
                " — " + decision.why if decision.why else "",
            )
        )

        if dry_run:
            _log(
                "  %s: dry-run — would rerun beets with --search-id %s"
                % (label, decision.mbid)
            )
            summary.still_skipped += 1
            continue

        # -C keeps nocopy (-m moves, for a layout that names the library) and
        # -q keeps quiet mode (quiet_fallback: skip so a weak match is still
        # skipped); -I disables incremental history lookup so beets does not
        # ignore the folder it just recorded during pass 1.
        retry_offset = log_offset(beets_log)
        argv = [
            "beet",
            "-c",
            beets_config,
            *beet_options,
            "import",
            import_flags,
            "-l",
            beets_log,
            "--search-id",
            decision.mbid,
            *targets,
        ]
        rc = runner(ingest_dir, argv)
        retry_entries = read_skips(beets_log, retry_offset)
        if rc != 0:
            _log(
                "  %s: beets retry exited with status %d — still skipped"
                % (label, rc)
            )
            summary.still_skipped += 1
        elif any(e.verb == "skip" for e in retry_entries):
            _log(
                "  %s: beets still skipped release %s (match not strong enough)"
                % (label, decision.mbid)
            )
            summary.still_skipped += 1
        elif any(e.verb == "duplicate-skip" for e in retry_entries):
            _log(
                "  %s: beets skipped release %s as a duplicate"
                % (label, decision.mbid)
            )
            summary.still_skipped += 1
        elif any(e.verb == "asis" for e in retry_entries):
            _log(
                "  %s: beets imported release %s as-is (unmatched)"
                % (label, decision.mbid)
            )
            summary.still_skipped += 1
        else:
            _log("  %s: tagged by beets with release %s" % (label, decision.mbid))
            summary.matched += 1

    if meter_file:
        spans, tally = modelmeter.gathered(meter_file)
        if tally.requests > 1:
            _log(
                "Music search model over the run: "
                + modelmeter.rate_line("all", spans.seconds, tally)
            )

    return summary
