"""Asking a small language model for an e-book's language when deterministic
detection disagrees or cannot answer.

:mod:`medialib.lib.booklanguage` reads a book's declared metadata language and
scores its text by script and stop words. When both agree - or when text
detection is already confident and unambiguous - no model is asked. Only when
declared metadata and text detection disagree, when neither yields an answer
(such as languages outside the stop-word table), or when text detection is
borderline (such as mixed or bilingual prose), a capped ~2 KB excerpt together
with the book's title/filename and deterministic candidates is sent to the
roster's :data:`medialib.lib.llmroster.BOOK_LANGUAGE` task (which leans light).

Every answer is validated strictly against :mod:`medialib.lib.languages` and
``pycountry`` to a real ISO 639-3 code and must be confident; on ``"unsure"``,
low confidence, an invalid code or :class:`medialib.lib.llmclient.LlmError`,
the existing deterministic result is kept. The ``bookLanguageLlm`` setting
turns this check off; what the model was asked is logged and counted through
:mod:`medialib.lib.modelmeter`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import pycountry

from medialib.lib import (
    languages,
    llmclient,
    llmroster,
    modelmeter,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "MAX_SAMPLE_CHARS",
    "MAX_SAMPLE_BYTES",
    "SYSTEM",
    "enabled",
    "cap_sample",
    "normalize_language_code",
    "parse_answer",
    "needs_model",
    "prompt",
    "detect_language",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "bookLanguageLlm"

# What the model's requests are counted under in modelmeter lines.
PURPOSE = "language"

# Cap on the text excerpt sent to the model (~2 KB).
MAX_SAMPLE_CHARS = 2048
MAX_SAMPLE_BYTES = 2048

SYSTEM = """You identify the primary language of an e-book from a short text \
excerpt so a text-to-speech engine can read it in the right voice.

You are given a short excerpt (up to ~2 KB) from the book, along with its \
filename/title and what the book's metadata and a stop-word detector said (if \
anything). Metadata tags are often wrong or left at a tool's default (such as \
English on a non-English book), and the stop-word detector only covers a \
small set of languages and can be confused by bilingual text.

Judge from the actual prose of the excerpt:
1. Return the primary language as an ISO 639-3 three-letter code (for example \
"eng", "deu", "fra", "spa", "swe", "ell", "ukr", "jpn", "zho").
2. For bilingual or mixed-language books, pick the main prose language if one \
clearly dominates; otherwise answer "unsure".
3. Only report high confidence when you are sure. A wrong language is worse \
than falling back: if the excerpt is gibberish, too short, non-linguistic or \
genuinely ambiguous, answer "unsure" with low confidence.

Answer with JSON only, in this form:
{"language": "<ISO 639-3 code or \\"unsure\\">", \
"confidence": "high" | "low", \
"why": "<one short sentence>"}"""

# Codes ISO 639 defines for "no specific language", plus common model refusals
# that must never be mistaken for a real language.
_NON_LANGUAGES = frozenset((
    "", "null", "none", "und", "mis", "mul", "qaa", "zxx",
    "unsure", "unknown", "other", "mixed",
))

# Spellings in use for a language that no ISO list has (matching booklanguage.py).
_ALIASES = {"jp": "jpn", "esl": "spa"}

_CONFIDENT_WORDS = frozenset(("high", "certain", "confident", "sure", "very high"))

_log = runlog.log
_detail = runlog.detail


def enabled() -> bool:
    """Whether a run asks the model when deterministic language detection
    disagrees or cannot answer: on unless :data:`SWITCH_VARIABLE` says off."""
    import os

    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def cap_sample(text: str, max_chars: int = MAX_SAMPLE_CHARS,
               max_bytes: int = MAX_SAMPLE_BYTES) -> str:
    """``text`` trimmed and capped to at most ``max_chars`` characters and
    ``max_bytes`` UTF-8 bytes (~2 KB by default), keeping whole characters."""
    cleaned = text.strip()
    if not cleaned:
        return ""
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    encoded = cleaned.encode("utf-8")
    if len(encoded) > max_bytes:
        cleaned = encoded[:max_bytes].decode("utf-8", "ignore")
    return cleaned.strip()


def normalize_language_code(raw: str) -> str:
    """``raw`` normalised to one real ISO 639-3 language code via
    :mod:`medialib.lib.languages` and ``pycountry``, or ``""`` when it is
    empty, ``"unsure"``, a non-language/special tag (``und``, ``mis``,
    ``mul``, ``zxx``, ``qaa``-``qtz``), or not a known ISO language."""
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip().lower().replace("_", "-")
    if not cleaned:
        return ""
    primary = re.sub(r"[^a-z]", "", cleaned.split("-", 1)[0])
    if not primary or primary in _NON_LANGUAGES:
        return ""
    if not languages.is_real_language_tag(primary):
        return ""
    # ISO 639-2/3 reserves qaa..qtz for local use.
    if len(primary) == 3 and "qaa" <= primary <= "qtz":
        return ""
    mapped = _ALIASES.get(primary, primary)
    code = languages.code3_from_label(mapped)
    if code:
        return code
    if " " in cleaned:
        code = languages.code3_from_label(cleaned)
        if code:
            return code
    if len(mapped) == 3:
        entry = (pycountry.languages.get(alpha_3=mapped)
                 or pycountry.languages.get(bibliographic=mapped))
        if entry is not None and getattr(entry, "scope", "") != "S":
            return str(entry.alpha_3)
    return ""


def _is_confident(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value >= 0.7
    if isinstance(value, str):
        return value.strip().lower() in _CONFIDENT_WORDS
    return False


def parse_answer(answer: str) -> tuple[str, str]:
    """The validated ISO 639-3 code and reason from the model's reply, or
    ``("", why)`` when the reply is not JSON, is unsure, is not confident, or
    names no real ISO language."""
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return "", ""
    why = " ".join(str(found.get("why") or "").split())
    raw_lang = found.get("language")
    if not isinstance(raw_lang, str):
        return "", why
    if not _is_confident(found.get("confidence")):
        return "", why
    return normalize_language_code(raw_lang), why


def needs_model(metadata_language: str, text_language: str, *,
                text_ambiguous: bool = False,
                has_text: bool = True) -> str:
    """Why a language model should be asked, or ``""`` when deterministic
    detection already settled the language (or there is no text excerpt).

    Only asks when:
    - declared metadata language and text detection disagree (including when
      metadata names a language that text detection could not confirm on a
      non-empty excerpt); or
    - neither metadata nor text detection produced an answer; or
    - metadata is absent and text detection was borderline/ambiguous (for
      example mixed or bilingual prose).
    """
    if not has_text:
        return ""
    meta = (metadata_language or "").strip()
    text = (text_language or "").strip()
    if meta and text:
        if meta == text:
            return ""
        return "metadata (%s) disagrees with text (%s)" % (meta, text)
    if meta and not text:
        return "metadata (%s) was not confirmed by text" % meta
    if not meta and not text:
        return "neither metadata nor text detection answered"
    if text_ambiguous:
        return "text detection (%s) was borderline" % text
    return ""


def prompt(sample: str, *, title: str = "", metadata_language: str = "",
           text_language: str = "") -> str:
    """What the model is told about a book whose language is unclear or in
    dispute."""
    excerpt = cap_sample(sample)
    lines = [
        "Identify the primary language of this e-book.",
        "",
        "Book file/title: %s" % (title or "(unknown)"),
        "Declared metadata language: %s" % (metadata_language or "(none)"),
        "Stop-word / script detector candidate: %s" % (
            text_language or "(none / inconclusive)"),
        "",
        "Excerpt:",
        excerpt,
    ]
    return "\n".join(lines) + "\n"


def detect_language(
    sample: str,
    *,
    title: str = "",
    metadata_language: str = "",
    text_language: str = "",
    text_ambiguous: bool = False,
    fallback: str | None = None,
    models: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
) -> str:
    """The book's ISO 639-3 language code, asking a small language model ONLY
    when deterministic detection disagrees or cannot answer.

    Returns ``fallback`` (or ``metadata_language or text_language`` when
    ``fallback`` is omitted) whenever deterministic detection already settled
    the answer, the ``bookLanguageLlm`` setting is off, no model is available,
    or the model returns ``"unsure"``, low confidence, an invalid language
    code, or an :class:`llmclient.LlmError`.
    """
    deterministic = (fallback if fallback is not None
                     else (metadata_language or text_language))
    excerpt = cap_sample(sample)
    reason = needs_model(metadata_language, text_language,
                         text_ambiguous=text_ambiguous,
                         has_text=bool(excerpt))
    if not reason:
        return deterministic
    label = title or "book"
    if not enabled():
        _detail("    No model asked for book language (%s): the %s setting is off"
                % (label, SWITCH_VARIABLE))
        return deterministic
    if models is None:
        models, why = llmroster.optional(llmroster.BOOK_LANGUAGE)
        if not models:
            _detail("    No model asked for book language (%s): %s" % (label, why))
            return deterministic
    elif not models:
        return deterministic

    request = prompt(excerpt, title=title, metadata_language=metadata_language,
                     text_language=text_language)
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
                _log("    Book language not checked by %s (%s): %s"
                     % (llm.name, label, error))
    finally:
        if tally.requests:
            _log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)

    if not answer:
        _log("    Book language fallback (%s): keeping %s (%s)"
             % (label, deterministic or "unknown", reason))
        return deterministic
    code, why = parse_answer(answer)
    if not code:
        _log("    Book language model (%s) gave no confident valid ISO code "
             "for %s%s - keeping %s"
             % (used_model, label, ": " + why if why else "",
                deterministic or "unknown"))
        return deterministic
    _log("    Book language by %s for %s: %s (%s%s)"
         % (used_model, label, code, reason, "; " + why if why else ""))
    return code
