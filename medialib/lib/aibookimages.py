"""Asking a vision model whether images inside an e-book are part of the book
or advertising / publisher promotional junk, during ``ingest-books -d``.

Without a vision model, ``ingest-books -d`` relies on two fixed filename
substrings (``teaser``, ``backadd``), which can drop a real illustration whose
name happens to match and miss advertisements with ordinary names. When a
vision model is available (the :data:`llmroster.BOOK_IMAGE_CHECK` task, which
needs ``vision`` and leans ``light``), candidate images inside the unpacked
e-book copy are shown to it:

* an image matching the junk-name substrings is shown to the model too, so a
  false positive that is actually part of the book is rescued and kept;
* an image is removed ONLY on an explicit, high-confidence ``"ad"`` verdict -
  ``"book"``, ``"unsure"``, medium/low confidence, unreadable JSON, or a model
  error always keeps the image;
* the book's own cover (declared in the ``.opf`` manifest or named
  ``cover.<ext>``) is never removed;
* when :data:`SWITCH_VARIABLE` (``bookJunkImagesLlm``) is ``off``, or no vision
  model is configured or able to see images, the run falls back cleanly to the
  existing junk-name substring check.

What the model was asked is counted (:mod:`medialib.lib.modelmeter`), logged,
and recorded for the run's total.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from medialib.lib import (
    formatting,
    imagecodecs,
    imagemagick,
    llmclient,
    llmroster,
    modelmeter,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "LOOK_GEOMETRY",
    "MAX_IMAGES_PER_BOOK",
    "VERDICTS",
    "CONFIDENCES",
    "SYSTEM",
    "Verdict",
    "enabled",
    "matches_junk_name",
    "prompt",
    "parse_verdict",
    "should_drop",
    "classify_image",
    "junk_images",
]

# Whether the vision model is asked at all: on unless set to off.
SWITCH_VARIABLE = "bookJunkImagesLlm"

# What the model's requests are counted as in modelmeter output.
PURPOSE = "images"

# Maximum dimensions of a picture sent to the vision model: sharp enough to
# read promotional text and tell a book illustration from an advert.
LOOK_GEOMETRY = "1024x1024"

# Cap on candidate images inspected per book so an image-heavy textbook does
# not trigger hundreds of vision calls. Substring matches are always inspected
# first; anything past the cap is kept untouched.
MAX_IMAGES_PER_BOOK = 64

VERDICTS = ("book", "ad", "unsure")
CONFIDENCES = ("high", "medium", "low")

SYSTEM = """You classify images extracted from an e-book to decide whether \
each picture is part of the book or advertising / publisher promotional junk.

Classify as "book" (KEEP):
- the book's own cover, title page, or half-title
- story illustrations, chapter headings, drop caps, ornaments, scene breaks
- maps, diagrams, charts, tables, equations, sheet music, photographs
- author portraits, publisher imprint/colophon logos on title or copyright pages

Classify as "ad" (JUNK) ONLY when the picture is plainly promotional or \
advertising material not part of the book's own content:
- cover thumbnails or promotional banners for OTHER books ("Also by...", \
"Read the next book", "Coming soon", "Sneak peek", "Preview", series adverts)
- newsletter signup cards, store / social-media / website call-to-action \
banners, QR codes selling other books, coupons, or third-party advertisements

Safety rule: when in doubt, or when the picture could plausibly belong to \
this book's own content, answer "book" or "unsure". Never guess "ad" on an \
illustration, decorative graphic, or unclear picture. Filenames can be \
misleading (a real illustration may contain "teaser" or "backadd", and an \
advert may have a generic name): judge primarily from what the picture shows, \
using the book title and filename only as context.

Answer with JSON only, in this exact form:
{"verdict": "book" | "ad" | "unsure", "confidence": "high" | "medium" | "low", \
"why": "<one short sentence>"}"""

_log = runlog.log
_detail = runlog.detail

Prepare = Callable[[str, str], str]


@dataclass(frozen=True)
class Verdict:
    """One parsed model judgement of an image inside an e-book."""

    verdict: str
    confidence: str
    why: str = ""

    @property
    def is_high_confidence_ad(self) -> bool:
        """Only an explicit, high-confidence advert verdict allows removal."""
        return self.verdict == "ad" and self.confidence == "high"


def enabled() -> bool:
    """Whether a run asks a vision model about e-book images: on unless off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def matches_junk_name(path: str, junk_substrings: Sequence[str]) -> bool:
    """Whether the file's basename matches any fixed junk-name substring."""
    lowered = os.path.basename(path).lower()
    return any(word.lower() in lowered for word in junk_substrings)


def prompt(relative_path: str, book_name: str = "", *, title: str = "",
           author: str = "", matched_substring: bool = False,
           size_bytes: int | None = None) -> str:
    """What the vision model is told alongside one e-book image."""
    lines = ["Classify the e-book image shown above as part of the book or advertising."]
    context: list[str] = []
    if book_name:
        context.append("E-book file: %s" % book_name)
    if title:
        context.append("Book title: %s" % title)
    if author:
        context.append("Author: %s" % author)
    size_part = " (%s)" % formatting.fmt_bytes(size_bytes) if size_bytes is not None else ""
    context.append("Image path inside e-book: %s%s" % (relative_path, size_part))
    if matched_substring:
        context.append(
            "Note: its filename matched a junk-name heuristic, which can be a "
            "false positive - judge from the picture itself.")
    lines.append("\n".join(context))
    lines.append(
        'Answer with JSON only: {"verdict": "book" | "ad" | "unsure", '
        '"confidence": "high" | "medium" | "low", "why": "<one short sentence>"}')
    return "\n\n".join(lines) + "\n"


def parse_verdict(answer: str) -> Verdict | None:
    """Strict JSON parse of the model's answer. Returns None on anything
    malformed, missing required fields, or outside the allowed values."""
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return None
    raw_verdict = found.get("verdict")
    raw_confidence = found.get("confidence")
    if not isinstance(raw_verdict, str) or not isinstance(raw_confidence, str):
        return None
    verdict = raw_verdict.strip().lower()
    confidence = raw_confidence.strip().lower()
    if verdict not in VERDICTS or confidence not in CONFIDENCES:
        return None
    why = " ".join(str(found.get("why") or "").split())
    return Verdict(verdict=verdict, confidence=confidence, why=why)


def should_drop(verdict: Verdict | None) -> bool:
    """Whether ``verdict`` authorises deleting the image. Always False unless
    the model returned a valid, high-confidence ``"ad"`` verdict."""
    return verdict is not None and verdict.is_high_confidence_ad


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _opf_metadata(directory: str) -> tuple[str, str, set[str]]:
    """Best-effort ``(title, author, cover_paths)`` from any ``.opf`` package
    document under ``directory``. Malformed or absent files yield empty values."""
    title = ""
    author = ""
    covers: set[str] = set()
    for parent, _dirs, names in os.walk(directory):
        for name in sorted(names):
            if not name.lower().endswith(".opf"):
                continue
            opf_path = os.path.join(parent, name)
            try:
                tree = ET.parse(opf_path)
            except (ET.ParseError, OSError, ValueError):
                continue
            root = tree.getroot()
            cover_ids: set[str] = set()
            items_by_id: dict[str, str] = {}
            for element in root.iter():
                tag = element.tag.rsplit("}", 1)[-1].lower()
                if tag == "title" and not title:
                    text = " ".join((element.text or "").split())
                    if text:
                        title = text
                elif tag == "creator" and not author:
                    text = " ".join((element.text or "").split())
                    if text:
                        author = text
                elif tag == "meta":
                    if (element.attrib.get("name") or "").strip().lower() == "cover":
                        content = (element.attrib.get("content") or "").strip()
                        if content:
                            cover_ids.add(content)
                elif tag == "item":
                    href = (element.attrib.get("href") or "").strip()
                    if not href:
                        continue
                    item_id = (element.attrib.get("id") or "").strip()
                    resolved = _norm_path(
                        os.path.join(parent, urllib.parse.unquote(href)))
                    if item_id:
                        items_by_id[item_id] = resolved
                    props = (element.attrib.get("properties") or "").split()
                    if "cover-image" in props:
                        covers.add(resolved)
            for cover_id in cover_ids:
                if cover_id in items_by_id:
                    covers.add(items_by_id[cover_id])
            if title or author or covers:
                break
    return title, author, covers


def _is_cover_path(path: str, opf_covers: set[str],
                   junk_substrings: Sequence[str]) -> bool:
    """Whether ``path`` is the book's own cover: declared in the ``.opf``
    manifest, or named ``cover.<ext>`` (unless explicitly matched by a custom
    junk substring)."""
    if _norm_path(path) in opf_covers:
        return True
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    return stem == "cover" and not matches_junk_name(path, junk_substrings)


def _is_direct_picture(path: str) -> bool:
    """Whether ``path`` is a non-empty JPEG, PNG, GIF or WebP file accepted
    directly by :mod:`medialib.lib.llmclient`."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return False
    return (head.startswith(b"\xff\xd8\xff")
            or head.startswith(b"\x89PNG")
            or head[:4] == b"GIF8"
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _for_looking(path: str, dest: str) -> str:
    """``path`` as a JPEG no larger than :data:`LOOK_GEOMETRY`, which every
    vision API accepts (including rasterising SVG or shrinking huge scans),
    written to ``dest``; ``path`` itself where conversion fails."""
    try:
        subprocess.run(imagemagick.convert_argv(
            ["-quiet", imagecodecs.coder_prefixed(path) + "[0]",
             "-resize", LOOK_GEOMETRY + ">", "-quality", "85", dest]),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return path
    return dest if _is_direct_picture(dest) else path


def classify_image(path: str, relative_path: str, bound: llmclient.Llm, *,
                   book_name: str = "", title: str = "", author: str = "",
                   matched_substring: bool = False,
                   tally: modelmeter.Tally | None = None,
                   spans: modelmeter.Spans | None = None,
                   prepare: Prepare | None = None,
                   scratch_dir: str = "") -> Verdict | None:
    """Ask ``bound`` whether one e-book image at ``path`` is book content or
    advertising. Logs every decision and returns ``None`` on any error or
    unreadable verdict (which keeps the image)."""
    prepare = prepare or _for_looking
    try:
        size_bytes: int | None = os.path.getsize(path)
    except OSError:
        size_bytes = None
    request = prompt(relative_path, book_name, title=title, author=author,
                     matched_substring=matched_substring, size_bytes=size_bytes)
    temp_ctx = (contextlib.nullcontext(scratch_dir) if scratch_dir
                else tempfile.TemporaryDirectory(prefix="aibookimg."))
    with temp_ctx as work:
        dest = os.path.join(work, "look.jpg")
        picture = prepare(path, dest)
        try:
            timing = spans.timing() if spans is not None else contextlib.nullcontext()
            with timing:
                answer = bound.ask(SYSTEM, request, tally, images=[picture])
        except llmclient.LlmError as error:
            _log("    Book image %s: model error (%s) -> keep" % (relative_path, error))
            return None
        finally:
            if picture != path:
                _remove(picture)
    verdict = parse_verdict(answer)
    if verdict is None:
        _log("    Book image %s: unreadable verdict -> keep" % relative_path)
        return None
    reason = " - " + verdict.why if verdict.why else ""
    if should_drop(verdict):
        _log("    Book image %s: ad (high confidence) -> drop%s"
             % (relative_path, reason))
    elif matched_substring:
        _log("    Book image %s: %s (%s confidence) -> keep "
             "(rescued from junk name match)%s"
             % (relative_path, verdict.verdict, verdict.confidence, reason))
    else:
        _log("    Book image %s: %s (%s confidence) -> keep%s"
             % (relative_path, verdict.verdict, verdict.confidence, reason))
    return verdict


def junk_images(directory: str, image_paths: Sequence[str],
                junk_substrings: Sequence[str], *, book_name: str = "",
                lookers: Sequence[llmclient.Llm] | None = None,
                meter_file: str = "",
                prepare: Prepare | None = None) -> list[str]:
    """Which images in ``image_paths`` (inside unpacked e-book ``directory``)
    should be deleted under ``ingest-books -d``.

    When :func:`enabled` is False, or no vision model is configured or able to
    see images, falls back cleanly to ``junk_substrings``. Otherwise shows each
    non-cover candidate image (substring matches first, up to
    :data:`MAX_IMAGES_PER_BOOK`) to the first available vision model and
    returns only those classified as high-confidence ``"ad"``. Any ``LlmError``
    keeps the affected image(s).
    """
    if not image_paths:
        return []
    fallback = [path for path in image_paths
                if matches_junk_name(path, junk_substrings)]
    if not enabled():
        _detail("    Book images not checked by a model: the %s setting is off"
                % SWITCH_VARIABLE)
        return fallback
    if lookers is None:
        lookers, why_none = llmroster.optional(llmroster.BOOK_IMAGE_CHECK)
        if not lookers:
            _detail("    Book images not checked by a model: %s" % why_none)
            return fallback
    if not lookers:
        return fallback

    title, author, opf_covers = _opf_metadata(directory)
    non_cover = [path for path in image_paths
                 if not _is_cover_path(path, opf_covers, junk_substrings)]
    if not non_cover:
        return []
    # Prioritise junk-name matches so false positives are always inspected even
    # in an image-heavy book that exceeds MAX_IMAGES_PER_BOOK.
    matched_first = [path for path in non_cover
                     if matches_junk_name(path, junk_substrings)]
    others = [path for path in non_cover
              if not matches_junk_name(path, junk_substrings)]
    candidates = (matched_first + others)[:MAX_IMAGES_PER_BOOK]

    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    dropped: list[str] = []
    checked = False
    had_error = False
    try:
        with tempfile.TemporaryDirectory(prefix="aibookimg.") as scratch_dir:
            for llm in lookers:
                try:
                    with llm.serving(_detail) as bound:
                        if not bound.sees_images():
                            _detail("    %s does not see pictures"
                                    % (bound.model or llm.name))
                            continue
                        checked = True
                        for path in candidates:
                            rel = os.path.relpath(path, directory).replace(os.sep, "/")
                            verdict = classify_image(
                                path, rel, bound,
                                book_name=book_name,
                                title=title,
                                author=author,
                                matched_substring=matches_junk_name(
                                    path, junk_substrings),
                                tally=tally,
                                spans=spans,
                                prepare=prepare,
                                scratch_dir=scratch_dir,
                            )
                            if should_drop(verdict):
                                dropped.append(path)
                        break
                except llmclient.LlmError as error:
                    had_error = True
                    _log("    Book images not checked by %s: %s"
                         % (llm.name, error))
    finally:
        if tally.requests:
            _log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file and tally.requests:
            modelmeter.record(meter_file, spans, tally)

    if not checked:
        return [] if had_error else fallback
    return dropped
