"""Asking a language model to spot advert cues in a subtitle when subcleaner
cannot clean it: either because subcleaner has no word list for the language,
or because subcleaner is not installed.

Where subcleaner has a profile for a language it always runs first, and this
module is never asked. Only when subcleaner refuses the language or is absent
are the subtitle's first :data:`HEAD_CUES` and last :data:`TAIL_CUES` cues
shown to a model from the :data:`llmroster.SUBTITLE_ADS` task. The model lists
only cue numbers from those shown cues that are ripper, sync, translator-plug,
or subtitle-site adverts. Any cue number outside the shown window, any non-int
value, or a response that asks to remove too many (or all) cues is refused,
leaving the subtitle untouched.

The ``subtitleAdsLlm`` setting turns this fallback off alone. What the model
was asked is counted (:mod:`medialib.lib.modelmeter`), logged, and recorded
for the run's total.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass

from medialib.lib import languages, llmclient, llmroster, modelmeter, runlog
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "HEAD_CUES",
    "TAIL_CUES",
    "MAX_REMOVALS",
    "MAX_REMOVAL_FRACTION",
    "MAX_CUE_CHARS",
    "SYSTEM",
    "Cue",
    "SubtitleFile",
    "Result",
    "enabled",
    "parse_subtitle",
    "read_subtitle",
    "render_subtitle",
    "write_subtitle",
    "sample_cues",
    "prompt",
    "max_removals",
    "parse_ad_cues",
    "metering",
    "clean_file",
    "clean",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "subtitleAdsLlm"

# What the model's requests are counted under in modelmeter.
PURPOSE = "sub-ads"

# How many cues from the opening and closing of the subtitle are shown.
HEAD_CUES = 20
TAIL_CUES = 20

# Strict sanity caps: never remove more than MAX_REMOVALS cues or more than
# MAX_REMOVAL_FRACTION of the subtitle, and never remove every cue in a file.
MAX_REMOVALS = 10
MAX_REMOVAL_FRACTION = 0.25

# Cap on each cue's text shown in the prompt so a malformed block cannot bloat
# the request.
MAX_CUE_CHARS = 300

# Matches SRT (HH:MM:SS,mmm --> HH:MM:SS,mmm) and WebVTT timing lines.
_TIMING_LINE = re.compile(
    r"^\s*(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3}\s*-->\s*(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3}"
)

SYSTEM = """You inspect the opening and closing cues of a film or TV subtitle \
file to find advertising and credit-spam cues added by subtitle sites, \
uploaders, rippers, encoders, or syncers - NOT part of the film or show.

Remove ONLY cues that are clearly one of:
1. Subtitle website plugs, URLs, or invitations to register, donate, download, \
or advertise (for example OpenSubtitles, Subscene, Addic7ed, Podnapisi, YIFY, \
or any website/domain).
2. Uploader, ripper, encoder, syncer, or fan-translator credits (for example \
"Synced and corrected by ...", "Subtitles by ...", "Translated by ...", \
"Ripped by ...", "Encoded by ...", Telegram/Discord/social handles).
3. Commercial spam, gambling, betting, VPN, or promotional banners injected \
into the subtitle file.

NEVER remove:
- Spoken dialogue, narration, song lyrics, sound descriptions ([music], \
(sighs)), on-screen signs, or title cards.
- The film or show's own opening or closing credits ("Directed by ...", \
"Starring ...", "Based on ...").
When in doubt, keep the cue: losing a real line is worse than leaving an advert.

List ONLY cue numbers from the cues shown to you. Never invent or guess cue \
numbers that were not shown.

Answer with JSON only, in this form:
{"ads": [1, 2], "why": "one short sentence naming what the removed cues are"}
Use {"ads": [], "why": ""} when none of the shown cues are adverts."""

_detail = runlog.detail
_meter_file: str = ""


@dataclass(frozen=True)
class Cue:
    """One timed subtitle cue, numbered sequentially from 1."""

    number: int
    timing: str
    text: str
    identifier: str = ""


@dataclass(frozen=True)
class SubtitleFile:
    """A parsed SRT or WebVTT file, keeping non-cue blocks and format details
    so removing selected cues leaves everything else intact."""

    kind: str  # "srt" or "vtt"
    blocks: tuple[Cue | str, ...]
    encoding: str = "utf-8"
    newline: str = "\n"

    @property
    def cues(self) -> list[Cue]:
        return [block for block in self.blocks if isinstance(block, Cue)]


@dataclass(frozen=True)
class Result:
    """Outcome of asking a model to clean one subtitle file."""

    removed: int
    asked: bool
    why: str = ""


def enabled() -> bool:
    """Whether a run asks the model when subcleaner cannot clean a language:
    on unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


@contextlib.contextmanager
def metering(path: str) -> Iterator[None]:
    """Record every subtitle-advert model request made inside this block to
    ``path`` (:func:`modelmeter.record`) when a call does not pass its own
    ``meter_file``."""
    global _meter_file
    previous = _meter_file
    _meter_file = path
    try:
        yield
    finally:
        _meter_file = previous


def _decode_bytes(raw: bytes) -> tuple[str, str]:
    """Decode subtitle bytes while remembering the encoding to write back."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def parse_subtitle(text: str, *, encoding: str = "utf-8") -> SubtitleFile | None:
    """Parse an SRT or WebVTT string into a :class:`SubtitleFile`, or None when
    it has no valid timed cues (so callers never clobber unknown or corrupt
    files)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped = normalised.lstrip("\ufeff").strip()
    if not stripped:
        return None
    raw_blocks = re.split(r"\n[ \t]*\n+", stripped)
    first_line = raw_blocks[0].splitlines()[0].strip() if raw_blocks else ""
    kind = "vtt" if first_line.startswith("WEBVTT") else "srt"

    blocks: list[Cue | str] = []
    cue_number = 0
    for index, raw_block in enumerate(raw_blocks):
        lines = raw_block.splitlines()
        if not lines:
            continue
        if kind == "vtt" and index == 0:
            blocks.append(raw_block)
            continue
        timing_at: int | None = None
        if _TIMING_LINE.match(lines[0]):
            timing_at = 0
        elif len(lines) >= 2 and _TIMING_LINE.match(lines[1]):
            if kind == "vtt" or lines[0].strip().isdigit():
                timing_at = 1
        if timing_at is None:
            # Keep WebVTT NOTE/STYLE/REGION blocks verbatim; in SRT, a stray
            # non-cue block is also kept intact rather than dropped.
            blocks.append(raw_block)
            continue
        cue_number += 1
        identifier = lines[0] if timing_at == 1 else ""
        timing = lines[timing_at]
        body = "\n".join(lines[timing_at + 1:])
        blocks.append(Cue(number=cue_number, timing=timing, text=body,
                          identifier=identifier))

    if cue_number == 0:
        return None
    return SubtitleFile(kind=kind, blocks=tuple(blocks), encoding=encoding,
                        newline=newline)


def read_subtitle(path: str) -> SubtitleFile | None:
    """Read and parse ``path`` as SRT or WebVTT; None on read or parse failure."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    text, encoding = _decode_bytes(raw)
    return parse_subtitle(text, encoding=encoding)


def render_subtitle(sub: SubtitleFile, remove: Collection[int]) -> str:
    """Render ``sub`` with only cue numbers in ``remove`` omitted, renumbering
    SRT cues cleanly from 1 and preserving WebVTT headers and cue identifiers."""
    drop = set(remove)
    rendered: list[str] = []
    next_number = 0
    for block in sub.blocks:
        if not isinstance(block, Cue):
            rendered.append(block)
            continue
        if block.number in drop:
            continue
        next_number += 1
        if sub.kind == "srt":
            lines = [str(next_number), block.timing]
            if block.text:
                lines.append(block.text)
            rendered.append("\n".join(lines))
        else:
            lines = []
            if block.identifier:
                lines.append(block.identifier)
            lines.append(block.timing)
            if block.text:
                lines.append(block.text)
            rendered.append("\n".join(lines))
    out = "\n\n".join(rendered) + "\n"
    if sub.newline != "\n":
        out = out.replace("\n", sub.newline)
    return out


def write_subtitle(path: str, sub: SubtitleFile, remove: Collection[int]) -> bool:
    """Atomically write ``sub`` without the cues in ``remove`` to ``path``.
    Never leaves a half-written file behind; returns False on write failure."""
    rendered = render_subtitle(sub, remove)
    try:
        data = rendered.encode(sub.encoding)
    except UnicodeEncodeError:
        data = rendered.encode("utf-8")
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    tmp_path = ""
    try:
        mode = os.stat(target).st_mode & 0o777
    except OSError:
        mode = None
    try:
        descriptor, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".subtitleads.", suffix=".tmp")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
        if mode is not None:
            try:
                os.chmod(tmp_path, mode)
            except OSError:
                pass
        os.replace(tmp_path, target)
        return True
    except OSError:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def sample_cues(cues: Sequence[Cue], head: int = HEAD_CUES,
                tail: int = TAIL_CUES) -> tuple[list[Cue], list[Cue]]:
    """The first ``head`` and last ``tail`` cues, with no duplicates when the
    file is short enough that the two windows meet or overlap.

    When ``len(cues) <= head + tail``, returns ``(list(cues), [])`` so every
    cue appears once.
    """
    if len(cues) <= head + tail:
        return list(cues), []
    return list(cues[:head]), list(cues[-tail:])


def _language_label(language_code: str) -> str:
    code = (language_code or "").strip()
    if not code:
        return "unknown"
    info = languages.language(code)
    if info is not None:
        return "%s (%s)" % (info.sub_word, code)
    return code


def _format_cue(cue: Cue) -> str:
    text = " / ".join(line.strip() for line in cue.text.splitlines() if line.strip())
    if len(text) > MAX_CUE_CHARS:
        text = text[:MAX_CUE_CHARS] + "..."
    return "[%d] %s\n%s" % (cue.number, cue.timing.strip(), text or "(empty)")


def prompt(head_cues: Sequence[Cue], tail_cues: Sequence[Cue],
           language_code: str, total_cues: int) -> str:
    """Build the user prompt showing only the sampled head and tail cues."""
    sections = [
        "Identify advert cues in this subtitle file.",
        "Language: %s\nTotal cues in file: %d" % (
            _language_label(language_code), total_cues),
    ]
    if not tail_cues:
        sections.append("## All %d cues\n\n%s" % (
            len(head_cues),
            "\n\n".join(_format_cue(cue) for cue in head_cues)))
    else:
        omitted = max(0, total_cues - len(head_cues) - len(tail_cues))
        sections.append("## First %d cues\n\n%s" % (
            len(head_cues),
            "\n\n".join(_format_cue(cue) for cue in head_cues)))
        sections.append("(... %d middle cues omitted ...)" % omitted)
        sections.append("## Last %d cues\n\n%s" % (
            len(tail_cues),
            "\n\n".join(_format_cue(cue) for cue in tail_cues)))
    return "\n\n".join(sections) + "\n"


def max_removals(total_cues: int) -> int:
    """The strict upper bound on how many cues may be removed from a subtitle
    with ``total_cues`` cues. Never allows removing all cues."""
    if total_cues <= 1:
        return 0
    by_fraction = max(1, int(total_cues * MAX_REMOVAL_FRACTION))
    return min(MAX_REMOVALS, by_fraction, total_cues - 1)


def parse_ad_cues(answer: str, allowed: Collection[int],
                  total_cues: int) -> tuple[list[int], str, str]:
    """Strictly parse and guard the model's JSON answer.

    Returns ``(cue_numbers, why, refusal)``:
    - ``cue_numbers``: sorted unique valid cue numbers strictly inside
      ``allowed`` (rejects non-int, ``bool``, and any number outside
      ``allowed``).
    - ``why``: the model's brief explanation, whitespace-normalised.
    - ``refusal``: non-empty when the response tried to remove all cues or more
      than :func:`max_removals` allows, in which case ``cue_numbers`` is ``[]``.
    """
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return [], "", "answer held no JSON object"
    raw_ads = found.get("ads")
    if not isinstance(raw_ads, list):
        return [], "", "answer JSON had no 'ads' list"
    why = " ".join(str(found.get("why") or "").split())

    allowed_set = set(allowed)
    kept: list[int] = []
    seen: set[int] = set()
    for item in raw_ads:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if item not in allowed_set or item in seen:
            continue
        seen.add(item)
        kept.append(item)
    kept.sort()

    if not kept:
        return [], why, ""

    if total_cues <= 1 or len(kept) >= total_cues:
        return [], why, ("model suggested removing all %d of %d cues - refused"
                         % (len(kept), total_cues))
    cap = max_removals(total_cues)
    if len(kept) > cap:
        return [], why, ("model suggested removing %d of %d cues (cap %d) - "
                         "refused" % (len(kept), total_cues, cap))
    return kept, why, ""


def clean_file(srt: str, language_code: str, log: Callable[[str], None], *,
               models: Sequence[llmclient.Llm] | None = None,
               meter_file: str = "") -> Result:
    """Ask a language model to spot and remove advert cues from ``srt`` in
    place, returning a :class:`Result` with how many cues went and whether a
    model actually inspected the file."""
    if not enabled():
        _detail("    No model asked for subtitle adverts: the %s setting is off"
                % SWITCH_VARIABLE)
        return Result(0, False)

    if models is None:
        models, why_none = llmroster.optional(llmroster.SUBTITLE_ADS)
        if not models:
            _detail("    No model asked for subtitle adverts: %s" % why_none)
            return Result(0, False)

    sub = read_subtitle(srt)
    if sub is None:
        return Result(0, False)
    cues = sub.cues
    if len(cues) < 2:
        return Result(0, False)

    head_cues, tail_cues = sample_cues(cues)
    allowed = {cue.number for cue in head_cues} | {cue.number for cue in tail_cues}
    request = prompt(head_cues, tail_cues, language_code, len(cues))

    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    used_model = ""
    target_meter = meter_file or _meter_file
    try:
        for llm in models:
            try:
                with llm.serving(_detail) as bound:
                    with spans.timing():
                        answer = bound.ask(SYSTEM, request, tally)
                    used_model = bound.model or llm.name
                break
            except llmclient.LlmError as error:
                log("subtitle advert model (%s) failed: %s" % (llm.name, error))
    finally:
        if tally.requests:
            _detail("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if target_meter:
            modelmeter.record(target_meter, spans, tally)

    if not answer:
        return Result(0, False)

    to_remove, why, refusal = parse_ad_cues(answer, allowed, len(cues))
    if refusal:
        log("WARNING: model (%s) %s - left untouched" % (used_model, refusal))
        return Result(0, True, why)
    if not to_remove:
        log("model (%s) found no advert cues%s" % (
            used_model, ": " + why if why else ""))
        return Result(0, True, why)
    if not write_subtitle(srt, sub, to_remove):
        log("WARNING: model (%s) found %d advert cue%s, but could not write "
            "the cleaned subtitle - left untouched" % (
                used_model, len(to_remove), "" if len(to_remove) == 1 else "s"))
        return Result(0, True, why)
    log("model (%s) removed %d advert cue%s%s" % (
        used_model, len(to_remove), "" if len(to_remove) == 1 else "s",
        ": " + why if why else ""))
    return Result(len(to_remove), True, why)


def clean(srt: str, language_code: str, log: Callable[[str], None], *,
          models: Sequence[llmclient.Llm] | None = None,
          meter_file: str = "") -> int:
    """Take advert cues identified by a language model out of ``srt`` in place,
    and return how many cues went: 0 when none were found, the setting is off,
    or no model was available."""
    return clean_file(srt, language_code, log, models=models,
                      meter_file=meter_file).removed
