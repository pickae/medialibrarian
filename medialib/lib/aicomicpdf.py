"""Asking a vision model whether a borderline PDF is a comic book: the
fallback below :mod:`medialib.lib.comicpdf`'s single-image-per-page check.

The deterministic rule counts pages holding exactly one large image covering
most of the page. That catches scanned comics cheaply, but misses digitally
drawn comics (vector art, separate lettering layers) and tiled scans (multiple
strips per page), which come out with zero or too few matching pages.

So when the deterministic rule says "not a comic" AND the PDF either is near
the threshold or has zero matching pages at all, two or three representative
pages are rendered with ``pdftoppm`` and shown to a vision model
(:data:`llmroster.COMIC_PDF_JUDGE`, requiring ``vision`` and leaning
``light``). A PDF that already passed the deterministic rule is never sent to
a model.

Only a confident ``"comic"`` verdict overrides the deterministic result;
``"magazine"``, ``"text"``, ``"other"``, ``"unsure"``, unparseable output or
any error keeps the deterministic verdict (not a comic). Valid verdicts are
cached by each file's canonical path, size and modification time so
``convert-comics`` and ``content-census`` always agree without asking twice.

The ``comicPdfLlm`` setting turns this step off; ``comicPdfCache`` names the
verdict cache file. Every request is counted (:mod:`medialib.lib.modelmeter`).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from medialib.lib import (
    llmclient,
    llmroster,
    modelmeter,
    runlog,
    settings,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "CACHE_VARIABLE",
    "METER_VARIABLE",
    "PURPOSE",
    "KINDS",
    "Verdict",
    "enabled",
    "cache_path",
    "should_ask",
    "sample_pages",
    "prompt",
    "parse_verdict",
    "render_sample_pages",
    "read_cache",
    "write_cache",
    "judge",
]

# Whether a vision model is asked at all: on unless set to off.
SWITCH_VARIABLE = "comicPdfLlm"

# Where cached verdicts are stored, overriding the user cache directory.
CACHE_VARIABLE = "comicPdfCache"

# Run-internal variable carrying the modelmeter path into worker processes.
METER_VARIABLE = "COMIC_PDF_METER"

CACHE_NAME = "comic-pdf-verdicts.json"

# Label used in modelmeter rate lines.
PURPOSE = "comic-pdf"

# Long edge in pixels for sample pages rendered for the vision model.
RENDER_SCALE = 1024
FALLBACK_DPI = 120

# Percentage points below comicPdfMinPageShare still considered borderline
# (with default 90%, 50%..89% is borderline; 0 matching pages always qualifies).
BORDERLINE_MARGIN = 40

KINDS = ("comic", "magazine", "text", "other")
_ACCEPTED_KINDS = frozenset((*KINDS, "unsure"))

_CACHE_LOCK = threading.Lock()

_log = runlog.log
_detail = runlog.detail

Render = Callable[[str, Sequence[int], str], list[str]]

SYSTEM = """You classify PDF books for a personal media library. You are shown \
1 to 3 rendered pages from a PDF that did not pass a simple one-image-per-page \
scan check (which misses digitally drawn comics, vector comics and tiled \
scans) and decide what kind of book it is.

Classify the book as one of:
- "comic": a comic book, graphic novel, manga, webtoon or bande dessinee \
(sequential art panels, speech balloons, illustrated story pages).
- "magazine": a magazine, newspaper, zine or periodical with articles, \
columns and advertisements.
- "text": a text book, novel, manual, paper or reference document (even if it \
has diagrams, illustrations or a picture cover).
- "other": anything else (slides, photo album, art book without sequential \
panels, sheet music, forms, catalogues) or when you are not sure.

A wrong "comic" verdict turns a text document or magazine into a huge, \
unreadable image archive, so answer "comic" with "confident": true ONLY when \
the pages clearly show a comic book, graphic novel or manga.

Answer with JSON only:
{"kind": "comic" | "magazine" | "text" | "other", "confident": true | false, \
"why": "<one sentence>"}"""


@dataclass(frozen=True)
class Verdict:
    """One vision-model classification of a PDF."""

    kind: str
    comic: bool
    why: str = ""


def enabled() -> bool:
    """Whether a run asks a vision model about borderline PDFs."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def cache_path() -> str:
    """Where vision verdicts for PDFs are cached across commands."""
    explicit = os.environ.get(CACHE_VARIABLE, "").strip()
    if explicit:
        return explicit
    return os.path.join(settings.cache_home(), CACHE_NAME)


def should_ask(pages: int, good: int, min_page_share: int) -> bool:
    """Whether a PDF with ``good`` single-image pages out of ``pages`` should
    be shown to a vision model.

    Never asks when the PDF cannot be read (``pages <= 0``) or already passed
    the deterministic threshold. Asks only when ``good == 0`` (digitally drawn
    or tiled comics) or when the share is near ``min_page_share``.
    """
    if pages <= 0:
        return False
    if good * 100 >= pages * min_page_share:
        return False
    if good == 0:
        return True
    floor = max(min_page_share - BORDERLINE_MARGIN, min_page_share // 2)
    return good * 100 >= pages * floor


def sample_pages(pages: int) -> list[int]:
    """1 to 3 representative 1-based page numbers from a ``pages``-page PDF:
    the cover and two interior pages past front matter."""
    if pages <= 0:
        return []
    if pages <= 3:
        return list(range(1, pages + 1))
    early = max(2, pages // 4)
    mid = min(pages, max(early + 1, (pages * 3) // 5))
    return sorted({1, early, mid})


def prompt(pdf: str, pages: int, good: int,
           page_numbers: Sequence[int]) -> str:
    """What the vision model is told alongside the rendered pages."""
    shown = ", ".join(str(number) for number in page_numbers)
    return (
        "File name: %s\n"
        "Total pages: %d (%d matched the single-full-page-image scan check)\n"
        "The %d picture(s) above are page(s) %s of this PDF, in order.\n\n"
        "Look at the rendered pages. Is this PDF a comic book / graphic novel "
        "/ manga, a magazine, a text book / document, or something else?\n"
        'Answer with JSON only: {"kind": "comic" | "magazine" | "text" | '
        '"other", "confident": true | false, "why": "<one sentence>"}.'
        % (os.path.basename(pdf), pages, good, len(page_numbers), shown)
    )


def _is_confident(parsed: dict) -> bool:
    if "confident" in parsed:
        raw = parsed["confident"]
        if isinstance(raw, bool):
            confident = raw
        elif isinstance(raw, int | float):
            confident = bool(raw)
        elif isinstance(raw, str):
            confident = raw.strip().lower() in ("true", "yes", "1", "high")
        else:
            confident = False
    else:
        confident = True
    level = parsed.get("confidence")
    if isinstance(level, str) and level.strip().lower() in (
        "low", "unsure", "uncertain", "no", "false"
    ):
        confident = False
    return confident


def parse_verdict(answer: str) -> Verdict | None:
    """Strictly parse the model's JSON answer.

    Accepts ``comic`` ONLY when the model says ``"comic"`` confidently.
    Returns None for unparseable output or an unknown kind so transient
    garbage is never cached.
    """
    parsed = llmclient.first_json(answer)
    if not isinstance(parsed, dict):
        return None
    kind = str(parsed.get("kind") or "").strip().lower()
    if kind not in _ACCEPTED_KINDS:
        return None
    why = " ".join(str(parsed.get("why") or "").split())[:200]
    if kind == "comic":
        if _is_confident(parsed):
            return Verdict(kind="comic", comic=True, why=why)
        return Verdict(kind="unsure", comic=False, why=why)
    return Verdict(kind=kind, comic=False, why=why)


def _render_one(pdf: str, page: int, dest: str) -> str | None:
    """Render one page of ``pdf`` to JPEG in ``dest`` via ``pdftoppm``.

    Returns the JPEG path, "" when the page did not render, or None when
    ``pdftoppm`` itself is missing or unrunnable.
    """
    prefix = os.path.join(dest, "page-%d" % page)
    expected = prefix + ".jpg"
    for extra in (["-scale-to", str(RENDER_SCALE)],
                  ["-r", str(FALLBACK_DPI)]):
        try:
            subprocess.run(
                ["pdftoppm", "-jpeg", "-f", str(page), "-l", str(page),
                 *extra, "-singlefile", pdf, prefix],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=120,
            )
        except (FileNotFoundError, PermissionError):
            return None
        except (OSError, subprocess.SubprocessError):
            continue
        if os.path.isfile(expected) and os.path.getsize(expected) > 0:
            return expected
    return ""


def render_sample_pages(pdf: str, page_numbers: Sequence[int],
                        dest: str) -> list[str]:
    """Render ``page_numbers`` from ``pdf`` into ``dest`` as JPEGs."""
    pictures: list[str] = []
    for page in page_numbers:
        rendered = _render_one(pdf, page, dest)
        if rendered is None:
            return []
        if rendered:
            pictures.append(rendered)
    return pictures


def _file_key(pdf: str) -> tuple[str, int, int] | None:
    """``(canonical_path, size, mtime_ns)`` for ``pdf``, or None if missing."""
    try:
        stat = os.stat(pdf)
    except OSError:
        return None
    return os.path.realpath(pdf), stat.st_size, stat.st_mtime_ns


def _load_entries(target: str) -> dict[str, dict]:
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and isinstance(value, dict)}


def read_cache(pdf: str, path: str = "") -> Verdict | None:
    """The cached verdict for ``pdf`` when its size and mtime still match."""
    key = _file_key(pdf)
    if key is None:
        return None
    real_path, size, mtime_ns = key
    target = path or cache_path()
    with _CACHE_LOCK:
        entry = _load_entries(target).get(real_path)
    if not entry:
        return None
    if entry.get("size") != size or entry.get("mtime_ns") != mtime_ns:
        return None
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind not in _ACCEPTED_KINDS:
        return None
    comic = bool(entry.get("comic")) and kind == "comic"
    why_raw = entry.get("why")
    why = why_raw if isinstance(why_raw, str) else ""
    return Verdict(kind=kind, comic=comic, why=why)


def write_cache(pdf: str, verdict: Verdict, path: str = "") -> None:
    """Record ``verdict`` for ``pdf`` keyed by canonical path, size and mtime.

    Never raises: losing a cache write must not fail a conversion or census.
    """
    key = _file_key(pdf)
    if key is None:
        return
    real_path, size, mtime_ns = key
    target = path or cache_path()
    parent = os.path.dirname(os.path.abspath(target))
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:
        return
    record = {
        "size": size,
        "mtime_ns": mtime_ns,
        "kind": verdict.kind,
        "comic": verdict.comic,
        "why": verdict.why,
    }
    lock_path = target + ".lock"
    with _CACHE_LOCK:
        try:
            with open(lock_path, "a+", encoding="utf-8") as lock_handle, \
                    runlog.take_lock(lock_handle):
                entries = _load_entries(target)
                entries[real_path] = record
                fd, partial = tempfile.mkstemp(prefix=".comic-pdf-cache.",
                                               dir=parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(entries, handle, sort_keys=True)
                    os.replace(partial, target)
                except OSError:
                    try:
                        os.remove(partial)
                    except OSError:
                        pass
        except OSError:
            return


def judge(pdf: str, pages: int, good: int, *,
          lookers: Sequence[llmclient.Llm] | None = None,
          meter_file: str = "",
          cache_file: str = "",
          render: Render | None = None) -> Verdict | None:
    """Ask a vision model whether ``pdf`` is a comic book, caching the verdict.

    Returns a :class:`Verdict` when cached or freshly judged, or None when
    disabled, no vision model is available, sample pages could not be rendered,
    or the model failed / gave an unusable answer.
    """
    name = os.path.basename(pdf)
    if not enabled():
        _detail('  No vision model asked for "%s": the %s setting is off'
                % (name, SWITCH_VARIABLE))
        return None

    cached = read_cache(pdf, cache_file)
    if cached is not None:
        _detail('  Cached vision verdict for "%s": %s' % (name, cached.kind))
        return cached

    if lookers is None:
        lookers, why_none = llmroster.optional(llmroster.COMIC_PDF_JUDGE)
        if not lookers:
            _detail('  No vision model asked for "%s": %s' % (name, why_none))
            return None

    page_numbers = sample_pages(pages)
    if not page_numbers:
        return None

    renderer = render or render_sample_pages
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    with tempfile.TemporaryDirectory(prefix="comicPdfJudge.") as work:
        pictures = renderer(pdf, page_numbers, work)
        if not pictures:
            _detail('  No vision model asked for "%s": sample pages could not '
                    "be rendered" % name)
            return None
        user_prompt = prompt(pdf, pages, good, page_numbers[:len(pictures)])
        try:
            for llm in lookers:
                try:
                    with llm.serving(_detail) as bound:
                        if not bound.sees_images():
                            _detail("  %s does not see pictures"
                                    % (bound.model or llm.name))
                            continue
                        with spans.timing():
                            answer = bound.ask(SYSTEM, user_prompt, tally,
                                               images=pictures)
                        break
                except llmclient.LlmError as error:
                    _log('  No vision verdict for "%s" from %s: %s'
                         % (name, llm.name, error))
        finally:
            if meter_file and tally.requests:
                modelmeter.record(meter_file, spans, tally)

    if not answer:
        return None

    verdict = parse_verdict(answer)
    if verdict is None:
        _log('  Vision verdict for "%s" could not be read; keeping '
             "deterministic verdict" % name)
        return None

    write_cache(pdf, verdict, cache_file)
    reason = ": " + verdict.why if verdict.why else ""
    rate = modelmeter.rate_line(PURPOSE, spans.seconds, tally)
    if verdict.comic:
        _log('  Vision judged "%s" as comic%s (%s)' % (name, reason, rate))
    else:
        _log('  Vision judged "%s" as %s (not a comic)%s (%s)'
             % (name, verdict.kind, reason, rate))
    return verdict
