"""Asking a language model to classify recurring filename fragment candidates
found by :mod:`medialib.cli.find_fragment_candidates` and propose additions to
``data/fragments.txt`` as a separate unified diff under ``logs/``.

Each candidate token (with its prevalence and a few sample folder/file names
from the tree) is sorted by the model into one of five categories:

- ``release_group``: scene, P2P, ripper or encoder group tag
- ``quality_or_source``: resolution, codec, source, audio or rip tag
- ``publisher``: recurring publisher, platform or site watermark tag
- ``title_word``: an ordinary word or title/creator word — NEVER proposed
- ``unsure``: ambiguous — NEVER proposed

``data/fragments.txt`` is NEVER edited directly. Proposed additions are written
as a separate proposal / unified diff file under ``logs/`` for human review.
Every proposed token must come verbatim from the candidate list actually found
by ``find-fragment-candidates`` and must not already exist in
``data/fragments.txt``; invented tokens, already-present tokens, ``title_word``
and ``unsure`` tokens are never proposed.

The roster task :data:`llmroster.FRAGMENT_CLASSIFY` picks the model (preferring
reasoning and web search, leaning balanced). The ``fragmentCandidatesLlm``
setting turns this step off alone. What the model was asked is counted
(:mod:`medialib.lib.modelmeter`), printed in the run's output, and optionally
recorded for the run.
"""

from __future__ import annotations

import difflib
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass

from medialib import commands
from medialib.lib import llmclient, llmroster, modelmeter, runlog
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "ADDABLE_CATEGORIES",
    "EXCLUDED_CATEGORIES",
    "CATEGORIES",
    "DEFAULT_SECTIONS",
    "Section",
    "FragmentsDoc",
    "Decision",
    "Classification",
    "ProposalResult",
    "enabled",
    "parse_fragments",
    "read_fragments",
    "sample_names",
    "prompt",
    "parse_decisions",
    "apply_proposals",
    "build_diff",
    "render_proposal",
    "propose",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "fragmentCandidatesLlm"

# What the model's requests are for, which names a harness's sessions and the
# modelmeter rate line.
PURPOSE = "fragments"

ADDABLE_CATEGORIES = ("release_group", "quality_or_source", "publisher")
EXCLUDED_CATEGORIES = ("title_word", "unsure")
CATEGORIES = ADDABLE_CATEGORIES + EXCLUDED_CATEGORIES

DEFAULT_SECTIONS: dict[str, str] = {
    "release_group": "Release groups",
    "quality_or_source": "Quality and source",
    "publisher": "Publishers",
}

# Bounds keeping the prompt compact even on a large library.
MAX_CANDIDATES = 200
MAX_SAMPLES_PER_TOKEN = 3
MAX_NAME_CHARS = 160
MAX_SECTION_EXAMPLES = 15

_EXTENSION = re.compile(r"\.[a-z][a-z0-9]{0,3}$")
_WORD_RUN = re.compile(r"[^\W_]+")

_log = runlog.log
_detail = runlog.detail

SYSTEM = """You curate a list of filename junk fragments (`data/fragments.txt`) \
for a personal media library. The name cleaner strips every token in that list \
(case-insensitively, at word boundaries) from folder and file names.

You are given recurring candidate tokens extracted from library names, sample \
folder/file names carrying each token, and the existing sections of \
`data/fragments.txt`.

Classify each candidate token into exactly ONE of these categories:
- `release_group`: scene, P2P, ripper or encoder group tag (e.g. yify, rarbg, \
sparks, ntb, fgt, mkvcage)
- `quality_or_source`: video/audio quality, resolution, codec, source or rip \
tag (e.g. 1080p, x264, hevc, webrip, bluray, remux, ddp5, 10bit)
- `publisher`: recurring publisher, distributor, audiobook platform or site \
watermark tag that does not belong in a cleaned title (e.g. audible, \
audiobookbay, libgen)
- `title_word`: a real word, title word, series name, creator name, episode \
marker or ordinary vocabulary word that MUST NOT be stripped from names
- `unsure`: ambiguous or unclear — MUST NOT be added

Rules:
1. Choose ONLY from the candidate tokens listed in the prompt. Never invent \
new tokens and never alter the spelling of a candidate token.
2. Never propose a token that is already present in `data/fragments.txt`.
3. Never propose `title_word` or `unsure` tokens for addition. Stripping a \
real word from titles is far worse than leaving a release tag behind; when in \
doubt, classify as `title_word` or `unsure`.
4. For each `release_group`, `quality_or_source` or `publisher` token, assign \
a `section`: use an existing section heading from `data/fragments.txt` when \
one fits, or otherwise one of `Release groups`, `Quality and source`, or \
`Publishers`.

Answer with JSON only, in this exact form:
{"decisions": [{"token": "<candidate>", \
"category": "release_group|quality_or_source|publisher|title_word|unsure", \
"section": "<section heading, or empty for title_word/unsure>", \
"reason": "<short one-line reason>"}]}"""


@dataclass(frozen=True)
class Section:
    """One section of ``data/fragments.txt``: its heading text (empty for
    leading unheaded lines), the line index where new tokens for it belong,
    and the entries it already holds."""

    name: str
    insert_at: int
    entries: tuple[str, ...]


@dataclass(frozen=True)
class FragmentsDoc:
    """The parsed structure of ``data/fragments.txt``."""

    lines: tuple[str, ...]
    sections: tuple[Section, ...]
    existing: frozenset[str]
    exists: bool = True


@dataclass(frozen=True)
class Decision:
    """One validated model decision for a candidate token."""

    token: str
    category: str
    section: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Classification:
    """Validated model decisions split into proposed additions, excluded
    decisions (``title_word`` and ``unsure``), and ignored/rejected items."""

    proposed: tuple[Decision, ...]
    excluded: tuple[Decision, ...]
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProposalResult:
    """Outcome of a fragment-classification run."""

    classification: Classification
    proposal_path: str = ""
    model_name: str = ""


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def _heading_text(line: str) -> str:
    """Heading label from a ``# ...`` comment line, empty for divider lines."""
    text = line.lstrip()[1:].strip()
    return text if text.strip("-= ") else ""


def parse_fragments(text: str, *, exists: bool = True) -> FragmentsDoc:
    """Parse ``data/fragments.txt`` into its verbatim lines, sections, and
    lowercased set of existing entries."""
    raw_lines = [line.rstrip("\r") for line in text.splitlines()]
    sections: list[Section] = []
    existing: set[str] = set()

    pending_comments: list[tuple[int, str]] = []
    active_heading: str | None = None
    active_entries: list[str] = []
    active_heading_idx = -1
    active_last_entry_idx = -1

    def flush() -> None:
        nonlocal active_heading, active_entries, active_heading_idx, active_last_entry_idx
        if active_heading is not None:
            insert_at = (active_last_entry_idx + 1 if active_last_entry_idx >= 0
                         else active_heading_idx + 1)
            sections.append(Section(active_heading, insert_at, tuple(active_entries)))
        elif active_entries:
            sections.append(Section("", active_last_entry_idx + 1, tuple(active_entries)))
        active_heading = None
        active_entries = []
        active_heading_idx = -1
        active_last_entry_idx = -1

    for idx, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            if pending_comments:
                if len(pending_comments) == 1 and pending_comments[0][1]:
                    # A standalone "# Heading" followed by a blank line is an
                    # empty section where new tokens can still be placed.
                    sections.append(Section(pending_comments[0][1],
                                            pending_comments[0][0] + 1, ()))
                pending_comments = []
            continue
        if stripped.startswith("#"):
            if active_heading is not None or active_entries:
                flush()
            pending_comments.append((idx, _heading_text(line)))
            continue
        existing.add(line.lower())
        existing.add(stripped.lower())
        if pending_comments:
            heading = next((label for _i, label in pending_comments if label), "")
            active_heading = heading if heading else None
            active_heading_idx = pending_comments[-1][0]
            pending_comments = []
        active_entries.append(line)
        active_last_entry_idx = idx

    if pending_comments and len(pending_comments) == 1 and pending_comments[0][1]:
        sections.append(Section(pending_comments[0][1],
                                pending_comments[0][0] + 1, ()))
    flush()

    return FragmentsDoc(
        lines=tuple(raw_lines),
        sections=tuple(sections),
        existing=frozenset(existing),
        exists=exists,
    )


def read_fragments(path: str) -> FragmentsDoc:
    """Read ``path`` without modifying it. Missing files yield an empty
    :class:`FragmentsDoc` with ``exists=False``."""
    if not path or not os.path.isfile(path):
        return parse_fragments("", exists=False)
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as handle:
            text = handle.read()
    except OSError:
        return parse_fragments("", exists=False)
    return parse_fragments(text, exists=True)


def sample_names(names: Sequence[str], tokens: Sequence[str],
                 max_per_token: int = MAX_SAMPLES_PER_TOKEN) -> dict[str, list[str]]:
    """Up to ``max_per_token`` distinct sample names from ``names`` carrying
    each candidate in ``tokens``."""
    wanted = {token.lower(): token for token in tokens}
    samples: dict[str, list[str]] = {token: [] for token in tokens}
    remaining = set(wanted)
    for raw in names:
        if not remaining:
            break
        cleaned = " ".join(raw.split())[:MAX_NAME_CHARS]
        if not cleaned:
            continue
        stem = _EXTENSION.sub("", raw.lower())
        found = set(_WORD_RUN.findall(stem))
        hits = found & remaining
        for key in hits:
            bucket = samples[wanted[key]]
            if cleaned not in bucket:
                bucket.append(cleaned)
                if len(bucket) >= max_per_token:
                    remaining.discard(key)
    return samples


def prompt(rows: Sequence[tuple[int, str]],
           samples: dict[str, list[str]],
           doc: FragmentsDoc) -> str:
    """Build the user prompt with existing sections of ``data/fragments.txt``,
    candidate tokens, prevalences, and sample folder/file names."""
    section_lines: list[str] = []
    named_sections = [sec for sec in doc.sections if sec.name]
    unheaded = [sec for sec in doc.sections if not sec.name]

    if named_sections:
        for sec in named_sections:
            shown = sec.entries[:MAX_SECTION_EXAMPLES]
            more = len(sec.entries) - len(shown)
            preview = ", ".join(shown) if shown else "(empty)"
            if more > 0:
                preview += ", ... (+%d more)" % more
            section_lines.append("- `# %s`: %s" % (sec.name, preview))
    if unheaded:
        flat = [entry for sec in unheaded for entry in sec.entries]
        shown_flat = flat[:MAX_SECTION_EXAMPLES]
        more = len(flat) - len(shown_flat)
        preview = ", ".join(shown_flat)
        if more > 0:
            preview += ", ... (+%d more)" % more
        section_lines.append("- (unheaded existing entries): %s" % preview)
    if not section_lines:
        section_lines.append(
            "(none yet — use the default sections: %s)"
            % ", ".join("`%s`" % name for name in DEFAULT_SECTIONS.values())
        )

    candidate_lines: list[str] = []
    for count, token in rows:
        examples = samples.get(token) or []
        suffix = (" — e.g. " + "; ".join("`%s`" % ex for ex in examples)
                  if examples else "")
        candidate_lines.append("- `%s` (%d name(s))%s" % (token, count, suffix))

    sections = [
        "Classify these recurring filename fragment candidates.",
        "## Existing sections in `data/fragments.txt`\n\n%s"
        % "\n".join(section_lines),
        "## Candidate tokens to classify\n\n%s"
        % "\n".join(candidate_lines),
        "## Output reminder\n\nReturn JSON with `decisions` covering only the "
        "candidate tokens listed above. Never invent tokens, never propose "
        "tokens already in `data/fragments.txt`, and classify real title words "
        "as `title_word` and ambiguous ones as `unsure`.",
    ]
    return "\n\n".join(sections) + "\n"


def parse_decisions(answer: str,
                    candidates: Sequence[str],
                    existing: frozenset[str] | set[str] | Sequence[str] = ()
                    ) -> Classification:
    """Strictly parse and validate the model's JSON answer.

    Guards enforced:
    - Only tokens present in ``candidates`` are accepted (invented tokens are
      rejected).
    - Tokens already in ``existing`` are rejected and never proposed.
    - ``title_word`` and ``unsure`` decisions are kept in ``excluded`` and
      never proposed for addition.
    - Unknown categories, non-dict entries, and duplicate tokens are ignored.
    """
    parsed = llmclient.first_json(answer)
    if not isinstance(parsed, dict):
        return Classification((), (), ("answer contained no JSON object",))
    raw_items = parsed.get("decisions")
    if not isinstance(raw_items, list):
        return Classification((), (), ("JSON object missing 'decisions' list",))

    allowed = {token.strip().lower(): token for token in candidates if token.strip()}
    existing_lower = {entry.strip().lower() for entry in existing if entry.strip()}
    seen: set[str] = set()

    proposed: list[Decision] = []
    excluded: list[Decision] = []
    rejected: list[str] = []

    for item in raw_items:
        if not isinstance(item, dict):
            rejected.append("non-object entry")
            continue
        raw_token = item.get("token")
        if not isinstance(raw_token, str) or not raw_token.strip():
            rejected.append("missing token")
            continue
        key = raw_token.strip().lower()
        if key not in allowed:
            rejected.append("invented token %r" % raw_token.strip())
            continue
        if key in seen:
            continue
        seen.add(key)

        canonical = allowed[key]
        raw_cat = item.get("category")
        if not isinstance(raw_cat, str) or raw_cat.strip().lower() not in CATEGORIES:
            rejected.append("invalid category %r for %r" % (raw_cat, canonical))
            continue
        category = raw_cat.strip().lower()
        reason = " ".join(str(item.get("reason") or "").split())

        if key in existing_lower:
            rejected.append("already in fragments.txt: %r" % canonical)
            continue

        if category in EXCLUDED_CATEGORIES:
            excluded.append(Decision(token=canonical, category=category,
                                     section="", reason=reason))
            continue

        raw_section = item.get("section")
        section = (" ".join(str(raw_section or "").split()).lstrip("#").strip()
                   if isinstance(raw_section, str) else "")
        if not section:
            section = DEFAULT_SECTIONS[category]
        proposed.append(Decision(token=canonical, category=category,
                                 section=section, reason=reason))

    return Classification(
        proposed=tuple(proposed),
        excluded=tuple(excluded),
        rejected=tuple(rejected),
    )


def apply_proposals(doc: FragmentsDoc,
                    proposed: Sequence[Decision]) -> list[str]:
    """Return what ``data/fragments.txt`` lines WOULD look like with
    ``proposed`` inserted into their sections (pure function; touches no file)."""
    lines = list(doc.lines)
    by_name = {sec.name.lower(): sec for sec in doc.sections if sec.name}

    into_existing: dict[int, list[str]] = {}
    new_sections: dict[str, list[str]] = {}
    new_section_labels: dict[str, str] = {}
    added: set[str] = set()

    for decision in proposed:
        key = decision.token.strip().lower()
        if not key or key in doc.existing or key in added:
            continue
        added.add(key)
        section_name = decision.section or DEFAULT_SECTIONS.get(
            decision.category, "Fragments")
        sec = by_name.get(section_name.lower())
        if sec is not None:
            into_existing.setdefault(sec.insert_at, []).append(decision.token)
        else:
            sec_key = section_name.lower()
            new_section_labels.setdefault(sec_key, section_name)
            new_sections.setdefault(sec_key, []).append(decision.token)

    for insert_at in sorted(into_existing, reverse=True):
        lines[insert_at:insert_at] = into_existing[insert_at]

    for sec_key, tokens in new_sections.items():
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("# %s" % new_section_labels[sec_key])
        lines.extend(tokens)

    return lines


def build_diff(doc: FragmentsDoc,
               proposed: Sequence[Decision],
               target_label: str = "data/fragments.txt") -> str:
    """Unified diff adding ``proposed`` to ``doc``; empty string if none."""
    if not proposed:
        return ""
    updated = apply_proposals(doc, proposed)
    if updated == list(doc.lines):
        return ""
    from_file = target_label if doc.exists else "/dev/null"
    diff_lines = list(difflib.unified_diff(
        list(doc.lines), updated,
        fromfile=from_file, tofile=target_label, lineterm="",
    ))
    return "\n".join(diff_lines) + "\n" if diff_lines else ""


def render_proposal(classification: Classification,
                    doc: FragmentsDoc,
                    *,
                    tree_file: str = "",
                    target_label: str = "data/fragments.txt",
                    model_name: str = "") -> str:
    """Complete proposal file text: human-readable classification summary
    (proposed and excluded tokens with reasons) followed by a unified diff."""
    header = [
        "# Proposed additions to %s" % target_label,
    ]
    if tree_file:
        header.append("# Source: %s" % tree_file)
    header.append("# Generated: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    if model_name:
        header.append("# Model: %s" % model_name)
    header.append("# Review before merging - find-fragment-candidates never "
                  "edits %s directly." % target_label)
    header.append("#")
    header.append("# Proposed additions (%d):" % len(classification.proposed))
    for decision in classification.proposed:
        why = ": " + decision.reason if decision.reason else ""
        header.append("#   + %s [%s -> %s]%s" % (
            decision.token, decision.category, decision.section, why))
    if classification.excluded:
        header.append("#")
        header.append("# Excluded (%d, NOT added):" % len(classification.excluded))
        for decision in classification.excluded:
            why = ": " + decision.reason if decision.reason else ""
            header.append("#   - %s [%s]%s" % (
                decision.token, decision.category, why))
    header.append("#")
    diff = build_diff(doc, classification.proposed, target_label=target_label)
    return "\n".join(header) + "\n" + diff


def _display_target(fragments_path: str, script_dir: str) -> str:
    try:
        rel = os.path.relpath(fragments_path, script_dir).replace(os.sep, "/")
    except ValueError:
        return fragments_path
    return rel if not rel.startswith("..") else fragments_path


def propose(rows: Sequence[tuple[int, str]],
            names: Sequence[str],
            *,
            tree_file: str = "",
            fragments_path: str = "",
            script_dir: str = "",
            models: Sequence[llmclient.Llm] | None = None,
            meter_file: str = "",
            stamp: str = "") -> ProposalResult | None:
    """Classify candidate tokens with a language model and write any proposed
    additions as a unified diff under ``logs/``, never editing
    ``data/fragments.txt``.

    Returns :class:`ProposalResult` when a model answered, or ``None`` when
    the switch is off, there are no new candidates, no model is available, or
    every model failed.
    """
    if not enabled():
        _detail("No model asked to classify fragment candidates: the %s "
                "setting is off" % SWITCH_VARIABLE)
        return None
    if not rows:
        return None

    script_dir = script_dir or commands.script_dir()
    fragments_path = fragments_path or os.path.join(
        script_dir, "data", "fragments.txt")
    doc = read_fragments(fragments_path)

    fresh = [(count, token) for count, token in rows
             if token.strip() and token.strip().lower() not in doc.existing]
    if not fresh:
        _detail("No model asked: all %d candidate(s) are already in %s"
                % (len(rows), _display_target(fragments_path, script_dir)))
        return None

    batch = fresh[:MAX_CANDIDATES]

    if models is None:
        models, why = llmroster.optional(llmroster.FRAGMENT_CLASSIFY)
        if not models:
            _detail("No model asked to classify fragment candidates: %s" % why)
            return None

    samples = sample_names(names, [token for _count, token in batch])
    request = prompt(batch, samples, doc)

    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    used_model = ""
    try:
        for llm in models:
            asker = llm.searching(PURPOSE) or llm
            try:
                with asker.serving(_detail) as bound:
                    with spans.timing():
                        answer = bound.ask(SYSTEM, request, tally)
                    used_model = bound.model or llm.name
                break
            except llmclient.LlmError as error:
                _log("No fragment classification from %s: %s" % (llm.name, error))
    finally:
        if tally.requests:
            _log(modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)

    if not answer:
        return None

    classification = parse_decisions(
        answer, [token for _count, token in batch], doc.existing)

    _log("Model (%s) classified %d fragment candidate(s): %d proposed, %d excluded"
         % (used_model,
            len(classification.proposed) + len(classification.excluded),
            len(classification.proposed),
            len(classification.excluded)))
    for decision in classification.proposed:
        why = ": " + decision.reason if decision.reason else ""
        _log("  + %s [%s -> %s]%s" % (
            decision.token, decision.category, decision.section, why))
    for decision in classification.excluded:
        why = ": " + decision.reason if decision.reason else ""
        _log("  - %s [%s, not added]%s" % (
            decision.token, decision.category, why))
    for note in classification.rejected:
        _detail("  ! ignored model item: %s" % note)

    target_label = _display_target(fragments_path, script_dir)
    proposal_path = ""
    if classification.proposed:
        stamp_text = stamp or time.strftime("%Y%m%d-%H%M%S")
        proposal_path = commands.logs_file(
            script_dir,
            "find-fragment-candidates-proposals-%s.diff" % stamp_text,
        )
        body = render_proposal(
            classification, doc,
            tree_file=tree_file,
            target_label=target_label,
            model_name=used_model,
        )
        with open(proposal_path, "w", encoding="utf-8") as handle:
            handle.write(body)
        _log('Wrote %d proposed fragment addition(s) to "%s" '
             '(review before merging into "%s").'
             % (len(classification.proposed), proposal_path, target_label))
    else:
        _log("No fragment additions proposed (nothing written under logs/).")

    return ProposalResult(
        classification=classification,
        proposal_path=proposal_path,
        model_name=used_model,
    )
