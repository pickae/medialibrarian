"""What language a book is in.

A TTS engine is TOLD what language its input is in; it does not detect one. The
question is asked of the book, in the order that costs least and is trusted
most: its METADATA (an epub states its language in the OPF package document,
Calibre reads the same field out of every other format), else its TEXT (the
book is converted to plain text and the text is scored).

The text fallback is two mechanisms, because languages come in two kinds: a
language written in a SCRIPT OF ITS OWN is settled by that script, which is
both cheaper and certain (each of the supported scripts identifies exactly one
supported language, and the kana test runs first because Japanese always
carries kana); everything in the Latin script is scored on STOP WORDS, the
share of the sampled words that are among a language's commonest.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable
from typing import Any

from medialib.lib.booktext import book_to_text
from medialib.lib.enums import lower_extension_of

__all__ = [
    "STOP_WORDS",
    "MIN_SCORE",
    "MIN_SCRIPT_SHARE",
    "SKIP_BYTES",
    "SAMPLE_BYTES",
    "book_language_code",
    "book_metadata_language",
    "detect_text_language",
    "book_text_language",
    "book_language",
]

# The stop-word table: the few dozen words that make up a large share of any
# text in each supported language, one entry per language, scalar on purpose -
# the same shape the bash keeps, because the words are what the scoring reads.
# Adding a language means adding its row here, its codes to _CODE_MAP, and the
# engine's own support for it.
STOP_WORDS = (
    "eng:the,and,of,to,that,is,it,was,for,with,this,but,his,are,from,have,not,you,they,what;"
    "deu:der,die,das,und,ist,nicht,sich,ein,eine,auch,aber,wurde,werden,oder,wenn,doch,schon,noch,nur,mehr;"
    "fra:les,des,est,une,que,qui,dans,pour,pas,plus,avec,mais,cette,elle,tout,être,aussi,comme,leur,sans;"
    "spa:los,las,que,una,por,con,para,como,pero,más,este,esta,sus,sobre,cuando,muy,todo,hasta,entre,porque;"
    "ita:che,non,per,con,sono,come,alla,dei,delle,questo,anche,quando,essere,tutto,dopo,senza,molto,perché,sul,nella;"
    "nld:het,een,van,zijn,niet,dat,maar,ook,worden,deze,naar,door,hij,zij,waren,werd,heeft,over,omdat,nog;"
    "por:uma,que,não,para,com,mais,como,mas,sua,seu,quando,muito,até,isso,pelo,pela,também,sobre,então,depois;"
    "pol:nie,się,jest,że,ale,jak,tak,tym,przez,tylko,jego,może,gdy,oraz,była,było,który,która,jeszcze,bardzo;"
    "ces:že,není,jsem,jsou,byl,byla,jako,ale,když,také,tak,jen,který,která,ještě,před,protože,může,aby,více;"
    "hun:hogy,nem,egy,van,meg,már,csak,volt,majd,még,ezt,ami,mint,ilyen,akkor,minden,lehet,vagy,azt,ott;"
    "tur:bir,için,ile,çok,daha,gibi,olarak,sonra,kadar,ancak,ama,değil,olan,şey,zaman,kendi,oldu,var,yok,onun"
)

# How sure the text fallback has to be before it names a language, as the share
# of the sampled words that are stop words of it. A text in the right language
# lands far above this (a fifth to a third of all words are stop words); a text
# in a language the table does not know scores near zero against all of them,
# and gets no answer rather than the least wrong one.
MIN_SCORE = 0.04
# The share of the sampled letters that has to be in a script of its own before
# that script decides the answer by itself. Well above the stray quotation, well
# below anything really written in it.
MIN_SCRIPT_SHARE = 0.15
# What is read of a book to judge it: how much is skipped first (the front of a
# book is a title page, a copyright notice and a table of contents - which in a
# translated book are routinely in a different language from the book itself),
# and how much is then sampled.
SKIP_BYTES = 4000
SAMPLE_BYTES = 300000

# Whatever a book states its language as, into one ISO 639-3 code. There is no
# single convention in the wild: an epub's dc:language is an IETF tag ("de",
# "de-DE", "pt-BR"), Calibre reports ISO 639-3 ("deu"), older tooling writes
# ISO 639-2/B ("ger", "fre", "dut", "cze"), and some files simply say "German".
# A region subtag is dropped rather than honoured - "pt-BR" and "pt-PT" are one
# voice's worth of difference, and the engine takes neither.
_CODE_MAP = {
    "ar": "ara", "ara": "ara", "arabic": "ara",
    "cs": "ces", "ces": "ces", "cze": "ces", "czech": "ces",
    "de": "deu", "deu": "deu", "ger": "deu", "german": "deu", "deutsch": "deu",
    "en": "eng", "eng": "eng", "english": "eng",
    "fr": "fra", "fra": "fra", "fre": "fra", "french": "fra",
    "hi": "hin", "hin": "hin", "hindi": "hin",
    "hu": "hun", "hun": "hun", "hungarian": "hun",
    "it": "ita", "ita": "ita", "italian": "ita",
    "ja": "jpn", "jpn": "jpn", "jp": "jpn", "japanese": "jpn",
    "ko": "kor", "kor": "kor", "korean": "kor",
    "nl": "nld", "nld": "nld", "dut": "nld", "dutch": "nld",
    "pl": "pol", "pol": "pol", "polish": "pol",
    "pt": "por", "por": "por", "portuguese": "por",
    "ru": "rus", "rus": "rus", "russian": "rus",
    "es": "spa", "spa": "spa", "esl": "spa", "spanish": "spa",
    "tr": "tur", "tur": "tur", "turkish": "tur",
    "zh": "zho", "zho": "zho", "chi": "zho", "cmn": "zho", "chinese": "zho",
}

# The table parsed the way the awk reads it: the languages in their table order
# (which is the tie order of the stop-word decision), and each stop word with
# the languages it belongs to - one word can belong to several, and each of
# them scores it. A word repeated in one row stays repeated: the awk scores it
# once per occurrence in the row.
_LANG_ORDER: list[str] = []
_STOP_LANGS: dict[str, list[str]] = {}
for _entry in STOP_WORDS.split(";"):
    _lang, _sep, _words = _entry.partition(":")
    _LANG_ORDER.append(_lang)
    for _word in _words.split(","):
        _STOP_LANGS.setdefault(_word, []).append(_lang)
del _entry, _lang, _sep, _words, _word

# A letter, the way [[:alpha:]] means it in the UTF-8 locale the bash runs
# under: the letter CATEGORIES of Unicode, as code-point ranges the scorer can
# walk without asking the classifier for every character of a 300 KB sample.
def _letter_class():
    code_points = [c for c in range(0x110000)
                   if unicodedata.category(chr(c))[0] == "L"]
    ranges = []
    lo = hi = code_points[0]
    for c in code_points[1:]:
        if c == hi + 1:
            hi = c
        else:
            ranges.append((lo, hi))
            lo = hi = c
    ranges.append((lo, hi))
    body = "".join(chr(lo) if lo == hi else f"{chr(lo)}-{chr(hi)}"
                   for lo, hi in ranges)
    return body

_LETTER_BODY = _letter_class()
_A_WORD = re.compile(f"[{_LETTER_BODY}]+")

# The scripts of their own, as the awk's literal character ranges: a range
# spans the code points between its ends, which is exactly "everything in this
# block" - except the awk's kana ends are the literal ぁ-ヿ, so the block's own
# first two characters (、 and 。) do NOT count, and are kept out here. The
# blocks are disjoint, so one walk counts them all.
_SCRIPT_BLOCKS = (
    (0x3042, 0x30FF, "jpn"),     # kana: Japanese always carries it, so it is
    (0xAC00, 0xD7A3, "kor"),     #   checked before the han block it shares with
    (0x4E00, 0x9FFF, "zho"),
    (0x0400, 0x04FF, "rus"),
    (0x0600, 0x06FF, "ara"),
    (0x0900, 0x09FF, "hin"),
)


def book_language_code(raw: str) -> str:
    """Whatever ``raw`` states a language as, as one ISO 639-3 code, or "".

    A code the table does not know names no language this can speak, which is
    the right thing to print: the caller would only learn of it later, from the
    engine refusing the book.
    """
    raw = raw.lower().replace("_", "-")
    raw = raw.split("-", 1)[0]
    raw = re.sub(r"[^a-z]", "", raw)
    if not raw:
        return ""
    return _CODE_MAP.get(raw, "")


def _first_opf_language(opf: str) -> str:
    """The first dc:language element of an OPF, the way the pipe reads it:
    the newlines out, the first ``<language...>value`` (with or without the
    dc: prefix), the value out of it."""
    opf = opf.replace("\r", "").replace("\n", "")
    match = re.search(r"<(dc:)?language[^>]*>[^<]*", opf)
    if not match:
        return ""
    value = match.group(0)
    return value[value.rfind(">") + 1:]


def _first_metadata_language(meta: str) -> str:
    """The first ``Languages:`` field of Calibre's output, as the awk reads it:
    the field after the first colon of the line, its leading whitespace off.
    Calibre lists several when a book claims several; the first is the one the
    text is actually in often enough, and no engine speaks two at once - the
    comma cut is the caller's, here rather than in the metadata function."""
    for line in meta.split("\n"):
        # [[:space:]] of the awk's match: every blank but the line break the
        # split already consumed.
        if re.match(r"^Languages?[ \t\v\f\r]*:", line):
            fields = line.split(":")
            return (fields[1] if len(fields) > 1 else "").lstrip(" \t")
    return ""


def book_metadata_language(src: str,
                           lower_extension: Callable[[str], str]
                           = lower_extension_of,
                           which: Callable[[str], str | None] = shutil.which,
                           run: Callable[..., Any] = subprocess.run) -> str:
    """The language the book states about itself, as an ISO 639-3 code, or "".

    An epub is read directly: its OPF package document is a file in the zip,
    and one unzip is cheaper (and needs less installed) than starting Calibre
    for a field that is sitting in plain XML. Every other format - and an epub
    whose OPF states nothing - goes through ebook-meta. Without either tool
    there is simply no answer, which is the right degradation for a step that
    only ever ADDS certainty.
    """
    raw = ""
    if lower_extension(src) == "epub" and which("unzip"):
        proc = run(["unzip", "-p", "--", src, "*.opf"], stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL)
        raw = _first_opf_language(proc.stdout.decode("utf-8", "replace"))
        raw = re.sub(r"[ \t\r\n\f\v]", "", raw)
    if not raw and which("ebook-meta"):
        proc = run(["ebook-meta", src], stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL)
        raw = _first_metadata_language(
            proc.stdout.decode("utf-8", "replace"))
        raw = raw.split(",", 1)[0]
        raw = re.sub(r"[ \t\r\n\f\v]", "", raw)
    if not raw:
        return ""
    return book_language_code(raw)


def detect_text_language(text: str) -> str:
    """The ISO 639-3 code ``text`` is most likely written in, or "".

    A script of its own settles its language outright (the kana test first,
    because Japanese text always carries kana and would otherwise read as
    Chinese); the Latin script is scored on stop words, and the winner has to
    clear MIN_SCORE, so a language not in the table gets no answer rather than
    the closest of the wrong ones.
    """
    kana = hangul = han = cyrillic = arabic = devanagari = letters = 0
    word_count = 0
    score: dict[str, int] = {}
    for line in text.lower().split("\n"):
        # The script census and the word split in one walk: a letter opens or
        # continues a word, and each of the script blocks is disjoint from the
        # others, so the blocks count as the word forms.
        word = []
        for ch in line:
            if ch.isalpha():
                letters += 1
                word.append(ch)
                cp = ord(ch)
                if 0x3042 <= cp <= 0x30FF:
                    kana += 1
                elif 0xAC00 <= cp <= 0xD7A3:
                    hangul += 1
                elif 0x4E00 <= cp <= 0x9FFF:
                    han += 1
                elif 0x0400 <= cp <= 0x04FF:
                    cyrillic += 1
                elif 0x0600 <= cp <= 0x06FF:
                    arabic += 1
                elif 0x0900 <= cp <= 0x09FF:
                    devanagari += 1
            elif word:
                word_count += 1
                hits = _STOP_LANGS.get("".join(word))
                if hits:
                    for lang in hits:
                        score[lang] = score.get(lang, 0) + 1
                word = []
        if word:
            word_count += 1
            hits = _STOP_LANGS.get("".join(word))
            if hits:
                for lang in hits:
                    score[lang] = score.get(lang, 0) + 1
    if letters:
        threshold = letters * MIN_SCRIPT_SHARE
        if kana >= threshold:
            return "jpn"
        if hangul >= threshold:
            return "kor"
        if han >= threshold:
            return "zho"
        if cyrillic >= threshold:
            return "rus"
        if arabic >= threshold:
            return "ara"
        if devanagari >= threshold:
            return "hin"
    if word_count <= 0:
        return ""
    best = ""
    best_score = 0.0
    for lang in _LANG_ORDER:
        value = score.get(lang, 0) / word_count
        if value > best_score:
            best_score = value
            best = lang
    if best and best_score >= MIN_SCORE:
        return best
    return ""


def book_text_language(src: str, ram_base: str | None = None,
                       book_to_text_fn: Callable[[str, str], int]
                       = book_to_text) -> str:
    """What the book's own text reads like, as an ISO 639-3 code, or "".

    The book is converted to plain text and a sample of that text is scored.
    The text never touches the disk for long: it is written into the RAM
    scratch and removed again on the way out. Without a converter there is
    simply no answer - and WHICH converter is the bookToText decision, not
    this one's, so the guard asks for the tool that would actually run.
    """
    if ram_base is None:
        ram_base = os.environ.get("TMPDIR", "/tmp")
    if src.lower().endswith(".pdf"):
        if not (shutil.which("pdftotext") or shutil.which("ebook-convert")):
            return ""
    elif not shutil.which("ebook-convert"):
        return ""

    scratch = tempfile.mkdtemp(prefix="bookLanguage.", dir=ram_base)
    try:
        text_path = f"{scratch}/book.txt"
        code = ""
        if book_to_text_fn(src, text_path) == 0:
            try:
                with open(text_path, "rb") as handle:
                    data = handle.read()
            except OSError:
                # [[ -s $text ]]: a converter that finished without a file is
                # an empty answer, not an error.
                data = b""
            if data:
                # tail -c +SKIP is the sample past the front matter; a book
                # shorter than the skip reads from the top instead.
                sample = data[SKIP_BYTES - 1:][:SAMPLE_BYTES]
                code = detect_text_language(
                    sample.decode("utf-8", "replace"))
                if not code:
                    code = detect_text_language(
                        data[:SAMPLE_BYTES].decode("utf-8", "replace"))
        return code
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def book_language(src: str, ram_base: str | None = None) -> str:
    """The book's language as one ISO 639-3 code: what it says about itself,
    else what its text reads like, else nothing at all. The caller decides
    what to do with "nothing" - it is a real answer here, and better than a
    confident guess that sends a whole book to be read in the wrong voice.
    """
    code = book_metadata_language(src)
    if not code:
        code = book_text_language(src, ram_base=ram_base)
    return code