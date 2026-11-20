"""Proposing additions to hand-kept word and extension lists from the reasons
``ingest-movies`` logged: foreign job titles for commentary listings, foreign
opening phrases for commentary transcripts, unrecognised junk extensions or
sample filenames, foreign leading articles for title matching, and foreign
multi-part keywords.

A model never edits any source file or curated list. When an ``ingest-movies``
run logs refusals, near-misses, unmatched or ambiguous folders, or skipped
files, this module asks the :data:`llmroster.CURATED_LISTS` task (preferring
a local reasoning model because the logs carry library paths and transcript
snippets) what additions the logged reasons support, validates and filters
the answer strictly against the live lists, and writes any surviving proposals
to a separate report under ``logs/`` for a person to review and merge by hand.

When nothing was logged, when ``listProposalsLlm`` is off, or when no model
fits the task, the step does nothing (saying why under ``-v``). What the model
was asked is counted (:mod:`medialib.lib.modelmeter`), logged, and recorded
when a meter file is given.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from medialib import commands
from medialib.lib import (
    commentarynames,
    enums,
    imdbdata,
    llmclient,
    llmroster,
    modelmeter,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "NOT_NAMES_WORDS",
    "INTRO_PHRASES",
    "JUNK_EXTENSIONS",
    "SAMPLE_NAMES",
    "ARTICLES",
    "PART_WORDS",
    "TARGET_LISTS",
    "TargetSpec",
    "TARGETS",
    "Reason",
    "Proposal",
    "enabled",
    "current_lists",
    "reasons_from_tagging",
    "reasons_from_commentary",
    "reasons_from_skips",
    "prompt",
    "parse_proposals",
    "format_report",
    "proposal_path",
    "write_report",
    "propose",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "listProposalsLlm"

# What the model's requests are for, in meter lines.
PURPOSE = "list-proposals"

# Canonical keys for the six hand-kept lists.
NOT_NAMES_WORDS = "commentarynames._NOT_NAMES_WORDS"
INTRO_PHRASES = "commentarynames._INTRO_PHRASES"
JUNK_EXTENSIONS = "ingest_movies.JUNK_EXTENSIONS"
SAMPLE_NAMES = "ingest_movies.SAMPLE_NAMES"
ARTICLES = "imdbdata.ARTICLES"
PART_WORDS = "enums.PART_WORDS"

TARGET_LISTS = (
    NOT_NAMES_WORDS,
    INTRO_PHRASES,
    JUNK_EXTENSIONS,
    SAMPLE_NAMES,
    ARTICLES,
    PART_WORDS,
)

# Bounds on what goes into a prompt and what is accepted back.
MAX_REASONS = 120
MAX_CONTEXT_LINES = 6
MAX_SNIPPET_CHARS = 300
MAX_PROPOSALS = 40
MAX_VALUE_CHARS = 64
MAX_EVIDENCE_CHARS = 240
MAX_RATIONALE_CHARS = 240


@dataclass(frozen=True)
class TargetSpec:
    """One hand-kept list a model may propose additions to."""

    key: str
    location: str
    description: str


TARGETS: dict[str, TargetSpec] = {
    NOT_NAMES_WORDS: TargetSpec(
        NOT_NAMES_WORDS,
        "medialib/lib/commentarynames.py",
        "Lowercase single folded word (role, job title, studio, or stop word) "
        "that must not be read as a commentator's name in a disc listing, "
        "e.g. foreign job titles such as 'regisseur' or 'realisateur'.",
    ),
    INTRO_PHRASES: TargetSpec(
        INTRO_PHRASES,
        "medialib/lib/commentarynames.py",
        "Short lowercase folded phrase (1-5 space-separated words) that opens "
        "a commentator's self-introduction in a transcript, e.g. 'ich bin', "
        "'mein name ist', 'je suis'.",
    ),
    JUNK_EXTENSIONS: TargetSpec(
        JUNK_EXTENSIONS,
        "medialib/cli/ingest_movies.py",
        "Lowercase file extension without a leading dot for release junk "
        "files that cleanup should remove (never a video, audio, subtitle, "
        "or image format).",
    ),
    SAMPLE_NAMES: TargetSpec(
        SAMPLE_NAMES,
        "medialib/cli/ingest_movies.py",
        "Exact filename (with extension) of a release sample clip that "
        "cleanup should remove, e.g. 'sample.mp4'.",
    ),
    ARTICLES: TargetSpec(
        ARTICLES,
        "medialib/lib/imdbdata.py (kept in step with medialib/lib/titlematch.py)",
        "Lowercase leading article word stripped when matching film titles "
        "against TMDb/IMDb, e.g. foreign articles such as 'det', 'ett', 'al'.",
    ),
    PART_WORDS: TargetSpec(
        PART_WORDS,
        "medialib/lib/enums.py",
        "Lowercase single word marking a multi-part film split for Plex "
        "stacking, e.g. 'teil', 'partie', 'parte'.",
    ),
}


@dataclass(frozen=True)
class Reason:
    """One refusal, near-miss, unmatched film, or skipped file logged during
    ``ingest-movies``."""

    kind: str
    subject: str
    reason: str
    context: tuple[str, ...] = ()


@dataclass(frozen=True)
class Proposal:
    """One validated, non-duplicate addition proposed for a hand-kept list."""

    target: str
    value: str
    evidence: str
    rationale: str


# Extensions a media library keeps or inspects: never accept as junk.
_PROTECTED_EXTENSIONS = frozenset({
    "mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "mpg", "mpeg",
    "webm", "iso", "img", "vob", "ifo", "bup",
    "srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "smi",
    "flac", "ac3", "eac3", "dts", "dtshd", "aac", "m4a", "mp3", "opus",
    "ogg", "wav", "thd", "mka", "pcm",
    "jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff",
    "xml", "json", "toml", "yaml", "yml", "py", "sh", "chapters",
})

# Overly generic filenames that must never be proposed as sample clips.
_GENERIC_FILENAMES = frozenset({
    "movie.mkv", "film.mkv", "video.mkv", "main.mkv", "title.mkv",
    "movie.mp4", "film.mp4", "video.mp4", "main.mp4", "title.mp4",
    "track.mkv", "disc1.mkv", "disc2.mkv", "part1.mkv", "part2.mkv",
    "index.mkv", "feature.mkv", "extra.mkv", "bonus.mkv",
})

# Overly generic single words that must not be proposed as intro phrases,
# articles, or part words.
_GENERIC_SINGLE_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "for",
    "from", "has", "have", "he", "her", "his", "i", "if", "in", "into", "is",
    "it", "its", "me", "my", "no", "not", "of", "on", "or", "our", "out",
    "she", "so", "that", "the", "their", "them", "they", "this", "to", "up",
    "us", "was", "we", "were", "with", "you", "your",
    "film", "movie", "video", "audio", "track", "file", "folder", "name",
    "mkv", "mp4", "avi", "srt", "sub", "commentary",
})

_GENERIC_NON_ARTICLES = frozenset({
    "in", "of", "to", "on", "at", "by", "for", "and", "or", "is", "it",
    "be", "my", "no", "so", "up", "as", "if", "do", "we", "he", "me", "us",
    "with", "from", "this", "that", "film", "movie", "video", "part", "disc",
})

_FOLDED_WORD = re.compile(r"^[a-z]{2,32}$")
_ARTICLE_WORD = re.compile(r"^[a-z\u00c0-\u024f]{1,12}$")
_PART_WORD = re.compile(r"^[a-z\u00c0-\u024f]{2,16}$")
_EXTENSION = re.compile(r"^[a-z0-9]{1,10}$")
_FILENAME_BAD = re.compile(r"[/\\*?<>|:\x00-\x1f]")

_log = runlog.log
_detail = runlog.detail

SYSTEM = """You review logged refusals, near-misses, unmatched folders, and \
skipped files from a movie-library ingest tool (ingest-movies) and propose \
additions to six hand-kept word and extension lists.

Rules:
1. Only propose an entry when a specific logged reason or snippet clearly \
supports it. When nothing in the logs justifies an addition, return an empty \
list.
2. Target ONLY the six exact list names given in the prompt. Never invent a \
list name.
3. Never propose an entry that is already in the target list.
4. Never propose film titles, personal names, overly generic stop words, or \
real media/subtitle/image extensions (mkv, mp4, srt, flac, jpg, etc.).
5. Keep every entry concise and in the target list's exact format:
   - commentarynames._NOT_NAMES_WORDS: one lowercase folded word (no accents)
   - commentarynames._INTRO_PHRASES: 1-5 lowercase folded words separated by \
spaces (no punctuation or accents)
   - ingest_movies.JUNK_EXTENSIONS: one lowercase extension without a leading \
dot
   - ingest_movies.SAMPLE_NAMES: one exact filename with extension
   - imdbdata.ARTICLES: one lowercase leading article word
   - enums.PART_WORDS: one lowercase multi-part keyword

Answer with JSON only, in this form:
{"proposals": [{"list": "<exact list name>", "value": "<entry>", \
"evidence": "<which logged file or reason triggered it>", \
"rationale": "<one concise sentence explaining why>"}]}
At most %d proposals. {"proposals": []} when no additions are warranted.""" % MAX_PROPOSALS


def enabled() -> bool:
    """Whether a run asks the model for curated-list proposals."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def _fold(text: str) -> str:
    """Accent-stripped lowercase form, matching commentarynames._fold."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower()


def _clean_line(text: str, limit: int) -> str:
    """Single-line whitespace-collapsed text, capped at ``limit`` chars."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 3)].rstrip() + "..."


def current_lists() -> dict[str, tuple[str, ...]]:
    """The live contents of each hand-kept list, read from the modules that
    define them."""
    from medialib.cli import ingest_movies

    return {
        NOT_NAMES_WORDS: tuple(sorted(set(commentarynames._NOT_NAMES_WORDS.split()))),
        INTRO_PHRASES: tuple(" ".join(phrase)
                             for phrase in commentarynames._INTRO_PHRASES),
        JUNK_EXTENSIONS: tuple(ingest_movies.JUNK_EXTENSIONS),
        SAMPLE_NAMES: tuple(ingest_movies.SAMPLE_NAMES),
        ARTICLES: tuple(imdbdata.ARTICLES),
        PART_WORDS: tuple(enums.PART_WORDS),
    }


def reasons_from_tagging(
    unmatched: Sequence[str] = (),
    ambiguous: Sequence[tuple] = (),
    near_misses: Sequence[tuple] = (),
    skips: Sequence[tuple] = (),
    root: str = "",
) -> list[Reason]:
    """Build structured :class:`Reason` entries from tagging worklists."""
    reasons: list[Reason] = []
    near_miss_subjects: set[str] = set()
    for folder, why, notes in near_misses:
        subject = _relative(folder, root)
        near_miss_subjects.add(os.path.basename(folder.rstrip("/\\")))
        context = tuple(_clean_line(note, MAX_SNIPPET_CHARS)
                        for note in (notes or ())[:MAX_CONTEXT_LINES] if note)
        reasons.append(Reason("near-miss", subject,
                              _clean_line(why, MAX_SNIPPET_CHARS), context))
    for folder, why, movies in ambiguous:
        subject = _relative(folder, root)
        context = tuple("file: " + _clean_line(name, MAX_SNIPPET_CHARS)
                        for name in (movies or ())[:MAX_CONTEXT_LINES] if name)
        reasons.append(Reason("ambiguous", subject,
                              _clean_line(why, MAX_SNIPPET_CHARS), context))
    for name in unmatched:
        if name in near_miss_subjects:
            continue
        reasons.append(Reason("unmatched", _clean_line(name, MAX_SNIPPET_CHARS),
                              "could not be identified on TMDb"))
    reasons.extend(reasons_from_skips(skips, root))
    return reasons


def reasons_from_commentary(
    refused: Sequence[tuple[str, str]],
    contexts: Mapping[str, Sequence[str]] | None = None,
) -> list[Reason]:
    """Build structured :class:`Reason` entries from commentary-naming refusals
    and optional transcript/listing context snippets."""
    reasons: list[Reason] = []
    extra = contexts or {}
    for relative, why in refused:
        notes = extra.get(relative, ())
        context = tuple(_clean_line(line, MAX_SNIPPET_CHARS)
                        for line in notes[:MAX_CONTEXT_LINES] if line)
        reasons.append(Reason("commentary-refusal",
                              _clean_line(relative, MAX_SNIPPET_CHARS),
                              _clean_line(why, MAX_SNIPPET_CHARS), context))
    return reasons


def reasons_from_skips(skips: Sequence[tuple], root: str = "") -> list[Reason]:
    """Build structured :class:`Reason` entries from held-back renames or
    skipped files."""
    reasons: list[Reason] = []
    for pair in skips:
        if len(pair) < 2:
            continue
        source, target = pair[0], pair[1]
        reasons.append(Reason(
            "skip",
            _relative(str(source), root),
            "held back; destination already exists: %s" % _relative(str(target), root),
        ))
    return reasons


def _relative(path: str, root: str) -> str:
    if root and path:
        try:
            rel = os.path.relpath(path, root)
            if not rel.startswith(".."):
                return "./" + rel.replace("\\", "/")
        except ValueError:
            pass
    return _clean_line(path, MAX_SNIPPET_CHARS)


def prompt(
    reasons: Sequence[Reason],
    lists: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """The prompt describing the live curated lists and the run's logged
    reasons."""
    live = current_lists() if lists is None else lists
    parts = ["Current hand-kept lists (do not propose entries already listed):"]
    for key in TARGET_LISTS:
        spec = TARGETS[key]
        entries = live.get(key, ())
        parts.append("")
        parts.append("%s (%s)" % (spec.key, spec.location))
        parts.append("  Purpose: %s" % spec.description)
        parts.append("  Current (%d): %s" % (len(entries), ", ".join(entries)))

    shown = list(reasons[:MAX_REASONS])
    parts.append("")
    parts.append("Logged reasons from this ingest-movies run (%d shown of %d):"
                 % (len(shown), len(reasons)))
    for index, item in enumerate(shown, start=1):
        parts.append("%d. [%s] %s -- %s" % (
            index,
            _clean_line(item.kind, 40),
            _clean_line(item.subject, MAX_SNIPPET_CHARS),
            _clean_line(item.reason, MAX_SNIPPET_CHARS),
        ))
        for line in item.context[:MAX_CONTEXT_LINES]:
            cleaned = _clean_line(line, MAX_SNIPPET_CHARS)
            if cleaned:
                parts.append("   %s" % cleaned)
    return "\n".join(parts)


def _existing_keys(lists: dict[str, tuple[str, ...]]) -> dict[str, set[str]]:
    """Normalised membership sets for deduplicating proposals against the live
    lists."""
    return {
        NOT_NAMES_WORDS: {_fold(w) for w in lists.get(NOT_NAMES_WORDS, ())},
        INTRO_PHRASES: {" ".join(re.findall(r"[^\W_]+", _fold(p)))
                        for p in lists.get(INTRO_PHRASES, ())},
        JUNK_EXTENSIONS: {shell_lower(e).lstrip(".")
                          for e in lists.get(JUNK_EXTENSIONS, ())},
        SAMPLE_NAMES: set(lists.get(SAMPLE_NAMES, ())),
        ARTICLES: {a.lower().strip() for a in lists.get(ARTICLES, ())},
        PART_WORDS: {w.lower().strip() for w in lists.get(PART_WORDS, ())},
    }


def _normalise_value(target: str, raw: object) -> str | None:
    """Validate and normalise one proposed entry for ``target``; ``None`` when
    blank, malformed, overlong, or overly generic."""
    if target == INTRO_PHRASES and isinstance(raw, list | tuple):
        if not all(isinstance(part, str) for part in raw):
            return None
        text = " ".join(raw)
    elif isinstance(raw, str):
        text = raw
    else:
        return None

    text = text.strip()
    if not text or len(text) > MAX_VALUE_CHARS:
        return None
    if any(ch in text for ch in ("\n", "\r", "\t")):
        return None

    if target == NOT_NAMES_WORDS:
        if any(ch.isspace() for ch in text):
            return None
        folded = _fold(text)
        if not _FOLDED_WORD.match(folded):
            return None
        return folded

    if target == INTRO_PHRASES:
        words = re.findall(r"[^\W\d_]+", _fold(text))
        if not words or len(words) > 5:
            return None
        if not all(w.isascii() and w.isalpha() for w in words):
            return None
        if len(words) == 1 and (len(words[0]) < 3 or words[0] in _GENERIC_SINGLE_WORDS):
            return None
        phrase = " ".join(words)
        if len(phrase) > 48:
            return None
        return phrase

    if target == JUNK_EXTENSIONS:
        ext = shell_lower(text).lstrip(".")
        if not _EXTENSION.match(ext) or ext in _PROTECTED_EXTENSIONS:
            return None
        return ext

    if target == SAMPLE_NAMES:
        if len(text) < 4 or _FILENAME_BAD.search(text):
            return None
        if ".." in text or "." not in text or text.startswith(".") or text.endswith("."):
            return None
        if shell_lower(text) in _GENERIC_FILENAMES:
            return None
        return text

    if target == ARTICLES:
        if any(ch.isspace() for ch in text):
            return None
        word = text.lower()
        if not _ARTICLE_WORD.match(word) or word in _GENERIC_NON_ARTICLES:
            return None
        return word

    if target == PART_WORDS:
        if any(ch.isspace() for ch in text):
            return None
        word = text.lower()
        if not _PART_WORD.match(word) or word in _GENERIC_SINGLE_WORDS:
            return None
        return word

    return None


def parse_proposals(
    answer: str,
    lists: dict[str, tuple[str, ...]] | None = None,
) -> list[Proposal]:
    """Strictly parse and filter a model's JSON reply into validated
    :class:`Proposal` items.

    Rejects unknown list names, blank/overlong/malformed/overly generic values,
    entries missing evidence or rationale, entries already present in the live
    list, and duplicate proposals.
    """
    data = llmclient.first_json(answer)
    if not isinstance(data, dict):
        return []
    raw_items = data.get("proposals")
    if not isinstance(raw_items, list):
        return []

    live = current_lists() if lists is None else lists
    existing = _existing_keys(live)
    seen: set[tuple[str, str]] = set()
    accepted: list[Proposal] = []

    for item in raw_items:
        if len(accepted) >= MAX_PROPOSALS:
            break
        if not isinstance(item, dict):
            continue
        target = str(item.get("list") or "").strip()
        if target not in TARGETS:
            continue
        value = _normalise_value(target, item.get("value"))
        if value is None:
            continue
        if value in existing[target]:
            continue
        evidence = _clean_line(item.get("evidence") or "", MAX_EVIDENCE_CHARS)
        rationale = _clean_line(item.get("rationale") or "", MAX_RATIONALE_CHARS)
        if not evidence or not rationale:
            continue
        key = (target, value if target == SAMPLE_NAMES else value.lower())
        if key in seen:
            continue
        seen.add(key)
        accepted.append(Proposal(target, value, evidence, rationale))

    return accepted


def format_report(proposals: Sequence[Proposal], model_name: str = "") -> str:
    """Format validated proposals grouped by target list for human review."""
    grouped: dict[str, list[Proposal]] = {key: [] for key in TARGET_LISTS}
    for proposal in proposals:
        if proposal.target in grouped:
            grouped[proposal.target].append(proposal)

    lines = [
        "# Curated list addition proposals from ingest-movies",
        "# Review and merge by hand - no source files were modified.",
    ]
    if model_name:
        lines.append("# Model: %s" % model_name)

    for key in TARGET_LISTS:
        items = grouped[key]
        if not items:
            continue
        spec = TARGETS[key]
        lines.append("")
        lines.append("[%s]" % spec.key)
        lines.append("# File: %s" % spec.location)
        for proposal in items:
            if key == INTRO_PHRASES:
                as_tuple = tuple(proposal.value.split())
                lines.append("- %s  (tuple: %r)" % (proposal.value, as_tuple))
            else:
                lines.append("- %s" % proposal.value)
            lines.append("  evidence: %s" % proposal.evidence)
            lines.append("  rationale: %s" % proposal.rationale)

    return "\n".join(lines) + "\n"


def proposal_path(script_dir: str, now: datetime | None = None) -> str:
    """Where a run's proposal report is written, under ``logs/``."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = "ingest-movies-list-proposals-%s" % stamp
    candidate = commands.logs_file(script_dir, base + ".txt")
    if not os.path.exists(candidate):
        return candidate
    suffix = 2
    while True:
        numbered = commands.logs_file(script_dir, "%s-%d.txt" % (base, suffix))
        if not os.path.exists(numbered):
            return numbered
        suffix += 1


def write_report(
    path: str,
    proposals: Sequence[Proposal],
    model_name: str = "",
) -> str:
    """Write ``proposals`` to ``path``, creating its parent directory if
    needed. Never modifies any Python source file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(format_report(proposals, model_name=model_name))
    return path


def propose(
    reasons: Sequence[Reason],
    script_dir: str,
    *,
    models: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
    output_path: str = "",
    lists: dict[str, tuple[str, ...]] | None = None,
    log: Callable[[str], None] = _log,
) -> str:
    """Ask the :data:`llmroster.CURATED_LISTS` ladder for additions to the six
    hand-kept lists supported by ``reasons``, and write any validated proposals
    to a separate report under ``logs/``.

    Returns the proposal report path when at least one proposal was written,
    or ``""`` when nothing was logged, the setting is off, no model fits, or
    no validated proposals were produced.
    """
    if not reasons:
        return ""
    if not enabled():
        _detail("No model asked for curated-list proposals: the %s setting is "
                "off" % SWITCH_VARIABLE)
        return ""
    if models is None:
        models, why = llmroster.optional(llmroster.CURATED_LISTS)
        if not models:
            _detail("No model asked for curated-list proposals: %s" % why)
            return ""

    live = current_lists() if lists is None else lists
    request = prompt(reasons, live)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    used_model = ""
    try:
        for llm in models:
            try:
                with llm.serving(_detail) as bound, spans.timing():
                    answer = bound.ask(SYSTEM, request, tally)
                used_model = llm.name
                break
            except llmclient.LlmError as error:
                log("No curated-list proposals from %s: %s" % (llm.name, error))
    finally:
        if tally.requests:
            log("Curated-list proposals model: "
                + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)

    if not answer:
        return ""
    proposals = parse_proposals(answer, live)
    if not proposals:
        _detail("No curated-list additions proposed from this run's logged reasons")
        return ""

    path = output_path or proposal_path(script_dir)
    write_report(path, proposals, model_name=used_model)
    log('%d curated-list addition proposal(s) written to "%s" for review'
        % (len(proposals), path))
    return path
