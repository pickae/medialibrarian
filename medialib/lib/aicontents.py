"""Asking a language model that can search the web for a book's table of
contents, where concat-audio named the joined book's chapters after its files
and the files only number them: ``01.mp3``, ``Track 02``, ``CD1 - 03`` come
out as chapters called "01", "02", which tells a listener nothing the chapter
list's own numbering does not.

A model that can search and read pages - Claude with its own search, any
other model through an agent harness - is told what the run knows of the
book, the same account the cover search gets (:func:`aicover.account`), and
the chapters as they stand: each one's start, length and name. It answers
with the book's own chapter titles, one a chapter, in order. Only an answer
with exactly as many titles as the book has chapters is taken: another count
means the files are not cut where the book's chapters are, and titles laid
on them anyway would name the wrong stretch. Otherwise the numbered names
stand.

The task is the roster's :data:`llmroster.CONTENTS_SEARCH`, which needs web
search: a model that cannot look the book up could only make titles up. The
:data:`SWITCH_VARIABLE` setting turns it off. What the model was asked is
counted (:mod:`medialib.lib.modelmeter`), said under the book, and recorded
for the run's total.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence

from medialib.lib import (
    aicover,
    chapters,
    formatting,
    llmclient,
    llmroster,
    modelmeter,
    onlinecover,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = ["SWITCH_VARIABLE", "PURPOSE", "enabled", "says_nothing", "numbered",
           "prompt", "titles", "renamed", "book_titles"]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "contentsSearchLlm"

# What the model's requests are for, which names a harness's sessions.
PURPOSE = "contents"

# A book is asked about when at least this share of its chapter names say
# nothing but a number: a book whose files are named "Prologue", "02" ...
# "41", "Epilogue" is a numbered book with two named ends.
NUMBERED_SHARE = 0.75

# Fewer chapters than this are no table of contents worth looking up.
MIN_CHAPTERS = 2

# The longest title taken; anything longer is a paragraph, not a title.
MAX_TITLE_CHARS = 200

# Words that only say which piece of the book a file is, in the languages the
# library's books come in, so "Track 03" or "CD2 Teil 4" says nothing more than
# "03" does.
_PIECE_WORDS = frozenset((
    "track", "tracks", "piste", "pista", "spur", "titel",
    "chapter", "chap", "ch", "chapitre", "kapitel", "hoofdstuk", "capitulo",
    "capítulo", "capitolo",
    "part", "pt", "partie", "teil", "deel", "parte",
    "cd", "disc", "disk", "side", "seite",
    "no", "nr", "num", "number", "nummer", "numero", "número", "of", "von", "van",
    "sur",
))

# A roman numeral, as written: "iv", "xii" - but not "mid" or "civil".
_ROMAN = re.compile(r"m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})")

# A run of letters or a run of digits: "CD1" is "cd" and "1".
_WORD = re.compile(r"[^\W\d_]+|\d+")

SYSTEM = """You name the chapters of an audiobook in a personal audio \
library. You can search the web and read pages. You are given what is known \
about one book whose chapters are named only by number, and you answer with \
the titles of its chapters from the book's own table of contents - in the \
language of the recording, as the edition it is read from gives them.

The chapters were cut where the book's files were joined, so they need not \
be the book's own chapters: an audiobook's files are often cut every so many \
minutes, a disc's tracks every few. Give titles only when the book's table \
of contents has exactly as many entries as the recording has chapters, in \
the same order, and their lengths are plausible for them - counting an \
opening (credits, a foreword, a prologue) and a closing (an epilogue, an \
afterword, credits) as entries where the recording plainly has them. Where \
you are not sure, give no titles: numbered chapters are better than wrong \
names.

Answer with JSON only, in this form:
{"chapters": ["the first chapter's title", "the second's", "..."], \
"source": "the page you found the table of contents on", \
"why": "one sentence on why these are this recording's chapters"}
{"chapters": []} when you found nothing you are sure of."""

_log = runlog.log
_detail = runlog.detail


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def says_nothing(name: str) -> bool:
    """Whether a chapter name is only a number: digits, a roman numeral and
    words that say which piece of the book it is, or nothing at all."""
    return all(word.isdigit() or word in _PIECE_WORDS or _ROMAN.fullmatch(word)
               for word in _WORD.findall(name.casefold()))


def numbered(names: Sequence[str]) -> bool:
    """Whether a book's chapter names are, nearly all of them, numbers."""
    if len(names) < MIN_CHAPTERS:
        return False
    return sum(map(says_nothing, names)) >= NUMBERED_SHARE * len(names)


def prompt(input_path: str, output_name: str, tracks: Sequence[str],
           subject: onlinecover.Subject, rows: Sequence[tuple[int, str]],
           seconds: float,
           probe: Callable[[str], dict] = onlinecover.probe_tags) -> str:
    """What the model is told about the book joined from ``input_path`` and
    its chapters, ``rows`` of (start in milliseconds, name)."""
    ends = [start for start, _ in rows[1:]] + [int(seconds * 1000)]
    listed = []
    for number, ((start, name), end) in enumerate(zip(rows, ends, strict=True), 1):
        length = (" (%s long)" % formatting.fmt_clock(str((end - start) / 1000))
                  if end > start else "")
        listed.append("%d. at %s%s: %s" % (
            number, formatting.fmt_clock(str(start / 1000)), length,
            name or "(no name)"))
    sections = [
        "Name this book's chapters from its table of contents.",
        *aicover.account(input_path, output_name, tracks, subject, probe),
        "## Its chapters as they stand\n\nThe book has %d chapters, one where "
        "each file began, named from the file names:\n%s\n\nAnswer with "
        "exactly %d titles, or none." % (len(rows), "\n".join(listed), len(rows)),
    ]
    return "\n\n".join(sections) + "\n"


def titles(answer: str, count: int) -> tuple[list[str] | None, str]:
    """The titles the model answered with when there are exactly ``count``
    of them, each a line of text - else None - and why or where from."""
    found = llmclient.first_json(answer)
    why = ""
    if isinstance(found, dict):
        why = " ".join(str(found.get("source") or found.get("why") or "").split())
        found = found.get("chapters")
    if not isinstance(found, list) or not found:
        return None, "it found no table of contents it was sure of"
    names = [" ".join(str(item).split()) if isinstance(item, (str, int, float))
             else "" for item in found]
    if len(names) != count:
        return None, "it gave %d titles for %d chapters" % (len(names), count)
    if not all(names) or any(len(name) > MAX_TITLE_CHARS for name in names):
        return None, "not every one of its titles is a title"
    return names, why


def renamed(chapter_lines: Sequence[str], names: Sequence[str]) -> list[str]:
    """The OGM rows with their names, in order, replaced by ``names``."""
    pending = iter(names)
    lines = []
    for line in chapter_lines:
        key, _, _value = line.partition("=")
        if key.startswith("CHAPTER") and key.endswith("NAME"):
            line = "%s=%s" % (key, next(pending))
        lines.append(line)
    return lines


def book_titles(input_path: str, output_name: str,
                chapter_lines: Sequence[str], tracks: Sequence[str],
                joined: str, *, subject: onlinecover.Subject | None = None,
                seconds: float | None = None,
                searchers: Sequence[llmclient.Llm] | None = None,
                meter_file: str = "",
                probe: Callable[[str], dict] = onlinecover.probe_tags,
                ) -> list[str]:
    """The chapter rows of the book joined from ``input_path`` into
    ``joined``, named from the book's table of contents where its names are
    numbers and a model found titles for every one of them - else the rows
    as they were.

    ``searchers`` are the models that may search, best first, the roster's
    ladder unless given; ``seconds`` and ``subject`` are read off ``joined``
    and the tracks unless given. What the model was asked is said under the
    book and, with ``meter_file``, recorded there for the run."""
    rows = chapters._row_chapters(chapter_lines)
    if not numbered([name for _, name in rows]):
        return list(chapter_lines)
    if not enabled():
        _detail("    No model asked for the chapter names: the %s setting is off"
                % SWITCH_VARIABLE)
        return list(chapter_lines)
    if searchers is None:
        searchers, why = llmroster.optional(llmroster.CONTENTS_SEARCH)
        if not searchers:
            _detail("    No model asked for the chapter names: %s" % why)
            return list(chapter_lines)
    if seconds is None:
        seconds = onlinecover.media_seconds(joined) if joined else 0.0
    if subject is None:
        stereo = bool(joined) and onlinecover.media_channels(joined) >= 2
        subject = onlinecover.book_subject(input_path, tracks, seconds, stereo,
                                           probe)
    request = prompt(input_path, output_name, tracks, subject, rows, seconds,
                     probe)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    try:
        for llm in searchers:
            searcher = llm.searching(PURPOSE)
            if searcher is None:
                continue
            try:
                with spans.timing():
                    answer = searcher.ask(SYSTEM, request, tally)
                break
            except llmclient.LlmError as error:
                _log("    No chapter names from %s: %s" % (llm.name, error))
    finally:
        if tally.requests:
            _log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    if not answer:
        return list(chapter_lines)
    names, why = titles(answer, len(rows))
    if names is None:
        _log("    Chapters keep their numbers: %s" % why)
        return list(chapter_lines)
    _log("    Chapter names from the book's table of contents%s"
         % (": " + why if why else ""))
    return renamed(chapter_lines, names)
