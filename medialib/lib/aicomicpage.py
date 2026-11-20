"""Checking whether a very small comic page is truly blank before
:mod:`medialib.cli.convert_comics` drops it.

A comic page whose AVIF encode comes out below ``MIN_PAGE_SIZE`` is usually an
empty page, but a sparse title page, a single line of text, a credits notice or
a faint pencil sketch can encode that small too. When a vision model is
available (the :data:`llmroster.COMIC_PAGE_CHECK` task, needing ``vision`` and
leaning ``light``), a downsized JPEG of that candidate page is shown to it.

The model can only keep a page, never drop one above the size threshold:

* Only pages already below ``MIN_PAGE_SIZE`` are ever passed here.
* When :data:`SWITCH_VARIABLE` is off or no vision model is configured,
  :func:`is_blank` returns ``None`` quietly so the caller keeps its existing
  size-only rule.
* When a vision model is available, :func:`is_blank` returns ``True`` only on a
  confident ``"blank"`` verdict. On ``"content"``, low or missing confidence,
  ambiguity, malformed JSON, an image that cannot be prepared, or
  :class:`llmclient.LlmError`, it returns ``False`` so the page is kept.

What the model was asked is counted (:mod:`medialib.lib.modelmeter`) and
recorded for the run's closing total when ``meter_file`` is given.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence

from medialib.lib import (
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
    "SYSTEM",
    "enabled",
    "prompt",
    "parse_verdict",
    "is_blank",
]

# Whether the vision check is asked at all: on unless set to off.
SWITCH_VARIABLE = "comicEmptyPageLlm"

# What the model's requests are counted under in modelmeter.
PURPOSE = "page"

# Maximum size of the JPEG shown to the vision model: enough to read sparse
# lettering or a faint sketch while keeping vision token usage modest.
LOOK_GEOMETRY = "1024x1024"

MIN_NUMERIC_CONFIDENCE = 0.9
_HIGH_CONFIDENCES = frozenset(("high", "certain", "sure"))

SYSTEM = (
    "You inspect candidate empty pages from a comic book whose encoded file "
    "size is very small, and decide whether the page is truly blank or holds "
    "intentional content.\n\n"
    'Answer "blank" ONLY when the page is completely empty: a uniform solid '
    "colour, plain paper texture, faint scanner noise, bleed-through or an "
    "empty flyleaf with no readable text or intentional art.\n"
    'Answer "content" for ANY intentional mark, however sparse: a title page, '
    "chapter heading, a single line of text, a page number, a copyright or "
    "credits line, a small logo or emblem, a panel border, or a faint pencil "
    "drawing.\n\n"
    "Err strongly on the side of keeping: a lost page is worse than an extra "
    'blank page. Give "confidence": "high" with "blank" only when you are '
    "certain nothing intentional is on the page.\n\n"
    "Answer with JSON only:\n"
    '{"verdict": "blank" | "content", '
    '"confidence": "high" | "medium" | "low", '
    '"why": "<short phrase>"}'
)

_log = runlog.log
_detail = runlog.detail


def enabled() -> bool:
    """Whether a run asks a vision model to check small pages: on unless the
    setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def prompt(page_name: str = "", book_name: str = "") -> str:
    """What the vision model is asked about the page image shown to it."""
    where = ""
    if book_name and page_name:
        where = " (%s from %s)" % (page_name, book_name)
    elif page_name:
        where = " (%s)" % page_name
    return (
        "This comic page%s encoded to a very small file size and is about to "
        "be dropped as empty. Look closely at the image: is it truly blank, "
        "or does it contain any intentional text, lettering, logo, sketch or "
        "artwork?\n\n"
        'Answer with JSON only: {"verdict": "blank" | "content", '
        '"confidence": "high" | "medium" | "low", "why": "<short phrase>"}'
        % where
    )


def parse_verdict(answer: str) -> tuple[bool, str]:
    """``(confident_blank, reason)`` parsed from the model's answer.

    Returns ``(True, why)`` ONLY for an explicit ``"blank"`` verdict with high
    confidence. Anything else - ``"content"``, low or medium confidence,
    missing confidence, unexpected verdict, malformed or missing JSON - returns
    ``(False, reason)`` so the page is kept.
    """
    verdict_obj = llmclient.first_json(answer)
    if not isinstance(verdict_obj, dict):
        return False, "unparseable model answer"
    verdict = str(verdict_obj.get("verdict") or "").strip().lower()
    why = " ".join(str(verdict_obj.get("why") or "").split())
    if verdict == "content":
        return False, why or "page has content"
    if verdict != "blank":
        return False, why or ("unclear verdict %r" % (verdict_obj.get("verdict"),))
    confidence = verdict_obj.get("confidence")
    if isinstance(confidence, str):
        confident = confidence.strip().lower() in _HIGH_CONFIDENCES
    elif isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        confident = float(confidence) >= MIN_NUMERIC_CONFIDENCE
    else:
        confident = False
    if not confident:
        detail = (
            "not confident enough to drop (%r)" % (confidence,)
            if confidence is not None
            else "confidence not stated"
        )
        return False, ("%s: %s" % (detail, why)) if why else detail
    return True, why or "blank page"


def is_blank(
    path: str,
    *,
    book_name: str = "",
    page_name: str = "",
    models: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
    prepare: Callable[[str, str], str] | None = None,
) -> bool | None:
    """Whether the small comic page at ``path`` is confirmed blank.

    Returns ``None`` when the check is turned off (:data:`SWITCH_VARIABLE`) or
    no vision model is available (caller falls back to its size-only rule),
    ``True`` only when a vision model confidently judges the page blank, and
    ``False`` when the model sees content, is unsure, returns unparseable
    output, or fails (page should be kept).
    """
    prepare = prepare or _for_looking
    if not enabled():
        _detail("    Small page not checked by a model: the %s setting is off"
                % SWITCH_VARIABLE)
        return None
    if models is None:
        models, why = llmroster.optional(llmroster.COMIC_PAGE_CHECK)
        if not models:
            _detail("    Small page not checked by a model: %s" % why)
            return None
    elif not models:
        return None

    label = (
        "%s: %s" % (book_name, page_name)
        if book_name and page_name
        else page_name or os.path.basename(path)
    )
    dest = path + ".look.jpg"
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    attempted = False
    picture = ""
    try:
        for llm in models:
            try:
                with llm.serving(_detail) as bound:
                    # The server may hold another model than the roster says.
                    if not bound.sees_images():
                        _detail("    %s does not see pictures"
                                % (bound.model or llm.name))
                        continue
                    if not picture:
                        picture = prepare(path, dest)
                        if not picture:
                            _log("%s: kept small page (could not render image "
                                 "for vision model)" % label)
                            return False
                    attempted = True
                    with spans.timing():
                        answer = bound.ask(
                            SYSTEM,
                            prompt(page_name or os.path.basename(path),
                                   book_name),
                            tally,
                            images=[picture],
                        )
                    break
            except llmclient.LlmError as error:
                attempted = True
                _log("%s: small page check failed on %s: %s"
                     % (label, llm.name, error))
    finally:
        if dest != path:
            _remove(dest)
        if picture and picture not in (path, dest):
            _remove(picture)
        if tally.requests:
            _detail("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file and tally.requests:
            modelmeter.record(meter_file, spans, tally)

    if not attempted:
        return None
    if not answer:
        _log("%s: kept small page (vision model did not answer)" % label)
        return False
    blank, why = parse_verdict(answer)
    if blank:
        _log("%s: dropped empty (confirmed blank by vision model: %s)"
             % (label, why))
        return True
    _log("%s: kept small page (%s)" % (label, why))
    return False


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _is_jpeg(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(3).startswith(b"\xff\xd8\xff")
    except OSError:
        return False


def _is_vision_image(path: str) -> bool:
    """Whether ``path`` is already one of the four formats vision APIs accept."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return False
    return (
        head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"\x89PNG")
        or head[:4] == b"GIF8"
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )


def _for_looking(path: str, dest: str) -> str:
    """``path`` as a JPEG no larger than :data:`LOOK_GEOMETRY`, which every
    vision API takes - an AVIF, JXL or oversized scan is neither - at ``dest``;
    falls back to ``path`` if it is already JPEG, PNG, GIF or WebP, else ""."""
    try:
        subprocess.run(
            imagemagick.convert_argv([
                "-quiet",
                imagecodecs.coder_prefixed(path) + "[0]",
                "-resize",
                LOOK_GEOMETRY + ">",
                "-quality",
                "85",
                dest,
            ]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    if _is_jpeg(dest):
        return dest
    return path if _is_vision_image(path) else ""
