"""Chapter marks for a recording of speech, worked out by a language model from
a timestamped transcript.

The model never writes a timestamp. Every line it is shown carries an id, it
answers with ids, and each id is turned back into the time the transcript
gives for it - so a mark can be misplaced by a judgement, but never by a
number the model invented or rounded.

The work is done in two passes, so that the chapters are a division of the
whole recording rather than a run of local guesses:

  outline   the whole transcript, read at once, as passages of
            :data:`PASSAGE_SECONDS` or so: the overall arc, a summary, and
            every chapter's start and title. A transcript too long for the
            model's window is summarised a window at a time first, and the
            outline is drawn from those summaries - so it still sees the whole
            recording, only at a coarser grain.
  refine    each chapter start looked at again, line by line, in the
            transcript around it, with the whole outline beside it: moved to
            the line where the new topic really begins, its title corrected
            where what is said there does not fit it, or dropped when it is
            not a change of topic after all.

The transcript is whatever a speech recogniser wrote, in any of the formats
:data:`TRANSCRIPT_SUFFIXES` lists, and the model is whatever
:mod:`medialib.lib.chapterllm` is set up to ask.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from medialib import commands
from medialib.lib import chapterllm, cuechapters

__all__ = ["TRANSCRIPT_SUFFIXES", "Cue", "read_transcript", "find_transcript",
           "Passage", "passages", "generate", "chapter_rows", "time_label",
           "DEFAULT_MIN_MINUTES", "DEFAULT_TYPICAL_MINUTES", "transcribe"]

# The transcript formats read, in the order a folder holding several of them is
# looked through: the two subtitle formats every recogniser can write,
# whisper's own two, then timed lyrics.
TRANSCRIPT_SUFFIXES = (".srt", ".vtt", ".tsv", ".json", ".lrc")

# How long a passage - one line of the outline's input - runs at least. Long
# enough that a three-hour recording is a few hundred lines, short enough that
# a chapter start lands within one passage of the real one before the refine
# pass places it exactly.
PASSAGE_SECONDS = 20

# The shortest chapter, and the length the model is steered towards, in
# minutes, unless the caller says otherwise.
DEFAULT_MIN_MINUTES = 2
DEFAULT_TYPICAL_MINUTES = 8

# How many passages either side of a chapter start the refine pass reads,
# line by line.
REFINE_PASSAGES = 3

# What a character costs in tokens, taken on the dear side: English runs near
# four characters a token, most other languages fewer.
CHARS_PER_TOKEN = 3

# The share of the model's window the transcript may fill. The rest is the
# instructions and the answer.
WINDOW_SHARE = 0.7

# The most times a transcript is summarised before the outline is drawn. Each
# time cuts it to a small fraction, so this is never reached in practice; it
# is here so a model that answers with summaries as long as what it read
# cannot loop.
MAX_SUMMARY_LEVELS = 4

# Before a chapter's first word, the mark is placed into the pause before it
# by up to this much, so a player jumping to it does not clip the first
# syllable.
LEAD_MS = 500

# The longest title kept.
MAX_TITLE = 100

_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND


@dataclass(frozen=True)
class Cue:
    """One timed line of a transcript."""

    start: int
    end: int
    text: str


# --- reading a transcript ---------------------------------------------------

_SUBTITLE_TIME = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_MARKUP = re.compile(r"<[^>]*>|\{\\[^}]*\}")


def _subtitle_ms(hours, minutes, seconds, fraction) -> int:
    return ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 \
        + int(fraction.ljust(3, "0")[:3])


def _read_subtitles(text: str) -> list[Cue]:
    """SRT and WebVTT alike: a timing line, then the text up to a blank line."""
    cues: list[Cue] = []
    lines = text.replace("\r", "").split("\n")
    at = 0
    while at < len(lines):
        timing = _SUBTITLE_TIME.search(lines[at])
        at += 1
        if not timing:
            continue
        parts = timing.groups()
        words = []
        while at < len(lines) and lines[at].strip():
            words.append(_MARKUP.sub("", lines[at]).strip())
            at += 1
        cues.append(Cue(_subtitle_ms(*parts[:4]), _subtitle_ms(*parts[4:]),
                        " ".join(word for word in words if word)))
    return cues


def _read_tsv(text: str) -> list[Cue]:
    """whisper's tsv: start and end in milliseconds, then the text."""
    cues = []
    for line in text.replace("\r", "").split("\n"):
        fields = line.split("\t", 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            cues.append(Cue(int(fields[0]), int(fields[1]), fields[2].strip()))
    return cues


def _read_json(text: str) -> list[Cue]:
    """whisper's json, which OpenAI-style transcription APIs answer with too:
    ``segments``, each with a start and end in seconds and its text."""
    try:
        document = json.loads(text)
    except ValueError:
        return []
    segments = document.get("segments") if isinstance(document, dict) else None
    cues = []
    for segment in segments or []:
        try:
            start = round(float(segment["start"]) * _MS_PER_SECOND)
            end = round(float(segment["end"]) * _MS_PER_SECOND)
        except (KeyError, TypeError, ValueError):
            continue
        cues.append(Cue(start, end, str(segment.get("text", "")).strip()))
    return cues


_LRC_LINE = re.compile(r"^\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\](.*)$")


def _read_lrc(text: str) -> list[Cue]:
    """An LRC file as cues, each ending where the next one starts."""
    timed = []
    for line in text.replace("\r", "").split("\n"):
        match = _LRC_LINE.match(line.strip())
        if match and match.group(4).strip():
            fraction = (match.group(3) or "0").ljust(3, "0")[:3]
            start = (int(match.group(1)) * 60 + int(match.group(2))) * 1000 \
                + int(fraction)
            timed.append((start, match.group(4).strip()))
    timed.sort()
    return [Cue(start, timed[index + 1][0] if index + 1 < len(timed)
                           else start + 5000, text)
            for index, (start, text) in enumerate(timed)]


def read_transcript(path: str) -> list[Cue]:
    """The cues of a transcript in one of :data:`TRANSCRIPT_SUFFIXES`, in time
    order and without the empty ones. Empty for a format this does not read or
    a file it cannot."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read().lstrip("﻿")
    except OSError:
        return []
    suffix = os.path.splitext(path)[1].lower()
    reader = {".tsv": _read_tsv, ".json": _read_json,
              ".lrc": _read_lrc}.get(suffix, _read_subtitles)
    cues = [cue for cue in reader(text) if cue.text]
    return sorted(cues, key=lambda cue: cue.start)


def find_transcript(stem: str) -> str:
    """The transcript beside ``stem`` - a path without its extension - in the
    first format there is one in, or "" when there is none."""
    for suffix in TRANSCRIPT_SUFFIXES:
        if os.path.isfile(stem + suffix) and os.path.getsize(stem + suffix) > 0:
            return stem + suffix
    return ""


# --- passages ---------------------------------------------------------------

@dataclass(frozen=True)
class Passage:
    """The cues ``first`` up to ``last``, inclusive, as one line."""

    first: int
    last: int
    start: int
    text: str


def passages(cues: Sequence[Cue], seconds: int = PASSAGE_SECONDS) -> list[Passage]:
    """The cues grouped into passages of at least ``seconds``, cut only between
    cues."""
    found: list[Passage] = []
    first = 0
    for index, cue in enumerate(cues):
        last = index == len(cues) - 1
        if last or cue.end - cues[first].start >= seconds * _MS_PER_SECOND:
            found.append(Passage(first, index, cues[first].start, " ".join(
                c.text for c in cues[first:index + 1])))
            first = index + 1
    return found


def time_label(ms: int) -> str:
    """H:MM:SS, the way the model is shown a time."""
    seconds = max(ms, 0) // _MS_PER_SECOND
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


# --- the model's answers ----------------------------------------------------

_SYSTEM = (
    "You divide recordings of speech - podcasts, interviews, lectures, "
    "audiobooks, talks - into chapters for a media player's chapter list. You "
    "are given a timestamped transcript in which every line has an id, and you "
    "answer with JSON only, naming lines by their ids.")

_TITLE_RULES = (
    "Titles are 2 to 8 words, specific to what is said (names, subjects, the "
    "question discussed), in the language that is spoken in the recording, "
    "with no chapter number, no word \"Chapter\" and no quotation marks. Leave out "
    "what every title would share - the show's name, its format - and say what "
    "sets this chapter apart. A greeting, a sponsor read or a sign-off may be "
    "titled as what it is.")


@dataclass
class _Line:
    """One line of what the outline is drawn from: a passage, or a summary of
    several, and the passage it starts at."""

    passage: int
    start: int
    text: str


def _render(lines: Sequence[_Line], prefix: str = "P") -> str:
    return "\n".join("[%s%d %s] %s" % (prefix, line.passage + 1,
                                        time_label(line.start), line.text)
                     for line in lines)


def _tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def _passage_id(value, count: int) -> int | None:
    """The passage index an answer's id names, or None for one that names
    none of them."""
    match = re.fullmatch(r"\s*P?(\d+)\s*", str(value), re.IGNORECASE)
    if not match:
        return None
    index = int(match.group(1)) - 1
    return index if 0 <= index < count else None


def _ask_json(llm, prompt: str, wanted: str, log) -> dict | None:
    """The model's answer as a dict holding ``wanted``, asked again once when
    the first answer is not one. None when neither was."""
    for attempt in range(2):
        try:
            answer = chapterllm.first_json(llm.ask(_SYSTEM, prompt))
        except chapterllm.LlmError as error:
            log("    the model gave no answer: %s" % error)
            return None
        if isinstance(answer, list) and wanted in ("chapters", "sections"):
            answer = {wanted: answer}
        if isinstance(answer, dict) and wanted in answer:
            return answer
        if not attempt:
            log("    the model's answer was not the JSON asked for, asking again")
    return None


def _budget(llm) -> int:
    """How many tokens of transcript one request may carry."""
    return max(int(llm.context_tokens * WINDOW_SHARE) - 1500, 1000)


def _windows(lines: Sequence[_Line], budget: int) -> list[list[_Line]]:
    """The lines cut into consecutive runs that each fit the budget."""
    windows: list[list[_Line]] = [[]]
    used = 0
    for line in lines:
        cost = _tokens(line.text) + 6
        if windows[-1] and used + cost > budget:
            windows.append([])
            used = 0
        windows[-1].append(line)
        used += cost
    return windows


def _summarise(lines: Sequence[_Line], llm, duration: int, count: int,
               log) -> list[_Line]:
    """The lines summarised a window at a time, one summary per stretch on a
    single topic. A window the model gives no usable answer for is kept as a
    single line holding the start of its text, so the outline still knows the
    stretch is there."""
    windows = _windows(lines, _budget(llm))
    per_window = max(2, round(count / len(windows) * 2))
    log("    the transcript is longer than the model's window: summarising it "
        "in %d parts first" % len(windows))

    def one(numbered):
        number, window = numbered
        prompt = (
            "Below is part %d of %d of the transcript of a recording that runs "
            "%s in all; this part covers %s to %s. Each line is a passage: its "
            "id, the time it starts, and what is said.\n\n"
            "List the topics this part goes through, in order, as sections: "
            "where each section starts (a passage id) and two or three sentences "
            "on what is said in it, with the names and subjects that come up. "
            "Start a new section wherever the topic clearly changes - about %d "
            "sections for this part, fewer if it stays on one subject. The first "
            "section starts at %s.\n\n"
            'Answer with JSON only: {"sections": [{"start": "<passage id>", '
            '"summary": "<what is said>"}, ...]}\n\nTranscript:\n%s\n'
            % (number + 1, len(windows), time_label(duration),
               time_label(window[0].start),
               time_label(window[-1].start), per_window,
               "P%d" % (window[0].passage + 1), _render(window)))
        answer = _ask_json(llm, prompt, "sections", log)
        by_passage = {line.passage: line for line in window}
        found = []
        for section in (answer or {}).get("sections") or []:
            if not isinstance(section, dict):
                continue
            index = _passage_id(section.get("start"), 1 << 30)
            summary = str(section.get("summary") or "").strip()
            if index is not None and index in by_passage and summary:
                found.append(_Line(index, by_passage[index].start, summary))
        if not found:
            first = window[0]
            return [_Line(first.passage, first.start,
                          " ".join(line.text for line in window)[:600])]
        found.sort(key=lambda line: line.passage)
        if found[0].passage != window[0].passage:
            found[0] = _Line(window[0].passage, window[0].start, found[0].text)
        return found

    with ThreadPoolExecutor(max_workers=llm.jobs) as pool:
        parts = list(pool.map(one, enumerate(windows)))
    return [line for part in parts for line in part]


@dataclass
class _Mark:
    passage: int
    title: str


def _outline(lines: Sequence[_Line], summarised: bool, total: int, llm,
             duration: int, typical_minutes: int, min_minutes: int,
             log) -> tuple[str, list[_Mark]] | None:
    """The first pass: the whole recording divided at once. None when the
    model gave no usable outline."""
    what = ("a section of the recording, already summarised: its id, the time "
            "it starts, and what is said in it"
            if summarised else
            "a passage: its id, the time it starts, and what is said")
    prompt = (
        "Below is the transcript of a recording that runs %s. Each line is %s.\n\n"
        "Divide the whole recording into chapters, the way a careful editor "
        "would for a chapter list:\n"
        "- Read all of it first and think about it as a whole: its overall arc, "
        "recurring segments, and where the big transitions are.\n"
        "- A chapter starts where the recording turns to a new topic, segment or "
        "story - not at every change of speaker or every new question.\n"
        "- Let the content decide how many. As a rough guide a chapter runs "
        "about %d minutes, but where the recording has more real segments than "
        "that, each still gets its own chapter, and where it stays on one subject "
        "longer, so does the chapter. No chapter shorter than %d minutes.\n"
        "- A recording can be nested - several episodes or programmes in a row, "
        "each with its own segments, or parts made of chapters. Then give one flat "
        "list: a chapter where each episode or part begins, and within it a "
        "chapter for each of its main segments. Number the episodes or parts in "
        "the order they come and start the title of the chapter that opens one "
        "with its number (e.g. \"Ep. 2: <first segment>\"). Never let one "
        "chapter run across the start of an episode or part, and never merge two "
        "of its segments into one chapter. If an episode or part comes twice, "
        "chapter the repeat like any other.\n"
        "- The first chapter starts at %s.\n"
        "- %s\n\n"
        'Answer with JSON only: {"summary": "<two or three sentences on what the '
        'whole recording is about>", "chapters": [{"start": "<line id>", '
        '"title": "<title>"}, ...]}\n\nTranscript:\n%s\n'
        % (time_label(duration), what, typical_minutes, min_minutes,
           "P%d" % (lines[0].passage + 1), _TITLE_RULES, _render(lines)))
    answer = _ask_json(llm, prompt, "chapters", log)
    if answer is None:
        return None
    marks: dict[int, _Mark] = {}
    for chapter in answer.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        index = _passage_id(chapter.get("start"), total)
        title = clean_title(chapter.get("title"))
        if index is not None and title and index not in marks:
            marks[index] = _Mark(index, title)
    known = [line.passage for line in lines]
    kept = sorted((mark for mark in marks.values() if known[0] <= mark.passage),
                  key=lambda mark: mark.passage)
    if not kept:
        return None
    kept[0].passage = known[0]
    return str(answer.get("summary") or "").strip(), kept


def _refine(cues: Sequence[Cue], found: Sequence[Passage], marks: list[_Mark],
            summary: str, llm, duration: int, log) -> list[tuple[int, str]]:
    """The second pass: every chapter start after the first looked at again in
    the transcript around it. Comes back as (cue index, title), the first
    chapter on the first cue; a start the model answers nothing usable for
    stays where the outline put it."""
    listing = [(found[mark.passage].start, mark.title) for mark in marks]

    def one(position: int):
        mark = marks[position]
        here = found[mark.passage]
        low = found[max(mark.passage - REFINE_PASSAGES, 0)].first
        high = found[min(mark.passage + REFINE_PASSAGES, len(found) - 1)].last
        window = "\n".join("[L%d %s] %s" % (index + 1, time_label(cues[index].start),
                                            cues[index].text)
                           for index in range(low, high + 1))
        outline = "\n".join("%s %s%s" % (time_label(start), title,
                                         "   <-- this one" if index == position else "")
                            for index, (start, title) in enumerate(listing))
        prompt = (
            "A recording that runs %s is being divided into chapters.%s\n\n"
            "The chapter list so far:\n%s\n\n"
            "Below is the transcript around where the chapter \"%s\" is placed "
            "to start (%s), line by line, each with its id and start time. The "
            "chapter before it is \"%s\".\n\n"
            "Find the line where \"%s\" really begins: the first line that "
            "belongs to it, such as the sentence that introduces the new topic "
            "or leads over to it - not a line in the middle of it. A chapter that "
            "opens an episode or part begins where that episode or part opens, "
            "not where its first segment does. If the title does not fit what is "
            "said there, give a better one, keeping an episode or part number it "
            "starts with. %s If this is not a change of topic at all, answer null "
            "as the start.\n\n"
            'Answer with JSON only: {"start": "<line id, or null>", '
            '"title": "<title>"}\n\nTranscript:\n%s\n'
            % (time_label(duration),
               " What it is: " + summary if summary else "",
               outline, mark.title, time_label(here.start),
               marks[position - 1].title, mark.title, _TITLE_RULES, window))
        answer = _ask_json(llm, prompt, "start", log)
        if answer is None:
            return here.first, mark.title
        title = clean_title(answer.get("title")) or mark.title
        start = answer.get("start")
        if start is None or str(start).strip().lower() in ("", "null", "none"):
            return None
        match = re.fullmatch(r"\s*L?(\d+)\s*", str(start), re.IGNORECASE)
        index = int(match.group(1)) - 1 if match else -1
        if not low <= index <= high:
            index = here.first
        return index, title

    with ThreadPoolExecutor(max_workers=llm.jobs) as pool:
        refined = list(pool.map(one, range(1, len(marks))))
    dropped = refined.count(None)
    if dropped:
        log("    %d chapter start(s) dropped on a second look" % dropped)
    return [(0, marks[0].title)] + [mark for mark in refined if mark is not None]


def clean_title(value) -> str:
    """A title as a chapter list shows it: one line, no surrounding quotes, no
    number or "Chapter n" in front, at most :data:`MAX_TITLE` characters."""
    if not isinstance(value, str):
        return ""
    title = " ".join(value.split())
    title = title.strip("\"'“”‘’«» ")
    title = re.sub(r"^(?:chapter|chapitre|kapitel|hoofdstuk|cap[ií]tulo)\s*\d*\s*[:.\-–]\s*",
                   "", title, flags=re.IGNORECASE)
    title = re.sub(r"^#?\d{1,2}[.):\-–]\s+", "", title)
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE].rsplit(" ", 1)[0].rstrip(",;:-–") + "…"
    return title


def _place(cues: Sequence[Cue], index: int) -> int:
    """Where a chapter starting on this cue is marked: into the pause before
    its first word by up to :data:`LEAD_MS`, never before the previous cue
    ends."""
    if index <= 0:
        return 0
    gap = cues[index].start - cues[index - 1].end
    return max(cues[index].start - min(LEAD_MS, max(gap, 0) // 2), 0)


def _settle(cues: Sequence[Cue], chosen: Sequence[tuple[int, str]],
            duration: int, min_ms: int) -> list[tuple[int, str]]:
    """The marks in time order, at their places, with every one that would
    leave a chapter shorter than ``min_ms`` taken out - the later of two that
    sit too close, and a last one too near the end."""
    marks: list[tuple[int, str]] = []
    for index, title in sorted(chosen, key=lambda pair: pair[0]):
        at = _place(cues, index)
        if not marks:
            marks.append((0, title))
        elif at - marks[-1][0] >= min_ms:
            marks.append((at, title))
    while len(marks) > 1 and duration - marks[-1][0] < min_ms:
        marks.pop()
    return marks if len(marks) > 1 else []


def too_short(duration: int, min_minutes: int = DEFAULT_MIN_MINUTES) -> bool:
    """Whether a recording of ``duration`` ms is too short for two chapters -
    asked before it is transcribed, since :func:`generate` would answer
    nothing for it whatever its transcript said. An unknown length (0) is
    not too short."""
    return 0 < duration < 2 * min_minutes * _MS_PER_MINUTE


def generate(cues: Sequence[Cue], llm, *, min_minutes: int = DEFAULT_MIN_MINUTES,
             typical_minutes: int = DEFAULT_TYPICAL_MINUTES,
             log: Callable[[str], None] = lambda line: None) -> list[tuple[int, str]]:
    """The chapters of a recording, as (start ms, title), the first at 0.

    Empty when there is nothing to divide - a recording too short for two
    chapters, or one the model gave no usable outline for.
    """
    if not cues:
        return []
    duration = max(cue.end for cue in cues)
    min_ms = min_minutes * _MS_PER_MINUTE
    if duration < 2 * min_ms:
        return []
    found = passages(cues)
    count = max(2, round(duration / (typical_minutes * _MS_PER_MINUTE)))

    lines = [_Line(index, passage.start, passage.text)
             for index, passage in enumerate(found)]
    summarised = False
    for _ in range(MAX_SUMMARY_LEVELS):
        if sum(_tokens(line.text) + 6 for line in lines) <= _budget(llm):
            break
        shorter = _summarise(lines, llm, duration, count, log)
        if len(shorter) >= len(lines):
            break
        lines, summarised = shorter, True

    outline = _outline(lines, summarised, len(found), llm, duration,
                       typical_minutes, min_minutes, log)
    if outline is None:
        return []
    summary, marks = outline
    log("    outline: %d chapter(s); placing each start" % len(marks))
    chosen = _refine(cues, found, marks, summary, llm, duration, log)
    return _settle(cues, chosen, duration, min_ms)


def chapter_rows(marks: Sequence[tuple[int, str]]) -> list[str]:
    """The marks as OGM rows, CHAPTERnn= and CHAPTERnnNAME= in turn."""
    rows = []
    for number, (start, title) in enumerate(marks, 1):
        rows.append(cuechapters.time_row(number, start))
        rows.append(cuechapters.name_row(number, title))
    return rows


def transcribe(items: Sequence[tuple[str, str]], stage_root: str,
               transcript_root: str, script_dir: str = "") -> int:
    """Transcripts for ``items`` - (relative path, audio file) pairs - made by
    transcribe-audio, the first phase as that command already does it, each
    at ``<transcript_root>/<relative stem>.srt``.

    transcribe-audio takes a folder, so the files are linked into
    ``stage_root`` under their relative paths and that folder is handed over:
    only what still needs a transcript is transcribed, wherever it lives.
    Returns the command's own exit status.
    """
    for relative, audio in items:
        link = os.path.join(stage_root, relative)
        os.makedirs(os.path.dirname(link) or stage_root, exist_ok=True)
        if not os.path.lexists(link):
            os.symlink(os.path.abspath(audio), link)
    # Started from the package's own directory, which ``python -m`` puts on
    # the child's path: a caller may have moved into its input folder, and
    # from there a package that is not installed cannot be imported.
    return commands.run_command("transcribe-audio",
                                ["-f", "srt", os.path.abspath(stage_root),
                                 os.path.abspath(transcript_root)],
                                script_dir=script_dir,
                                cwd=commands.package_root()).returncode
