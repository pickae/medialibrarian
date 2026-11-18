"""The transcript a YouTube episode brings with it, kept as ``<episode>.srt``.

YouTube has captions for nearly every upload, made by its own speech
recognition where the uploader gave none. Fetched with the episode, they spare
the chapters the transcription: convert-audio reads a transcript beside a
source instead of running whisper, and writes it into the output as lyrics.

Which one is kept follows the languages the user speaks: the episode's own,
when it is one of them, and English when it is not. yt-dlp is asked for no
more than can be chosen from - the original-language track of each spoken
language (YouTube names it ``<code>-orig``) and the English ones - since
every track is a request to a provider that throttles the client asking too
much.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import NamedTuple

__all__ = ["ytdlp_args", "choose", "Choice"]

# What yt-dlp may have written a caption track as: the format asked for first,
# and the ones it falls back to.
_TRACK_SUFFIXES = (".vtt", ".srt", ".srv1", ".srv2", ".srv3", ".ttml", ".json3")
_READABLE = (".vtt", ".srt")

_ORIGINAL = "-orig"
_ENGLISH = "en"

_TIME = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_MARKUP = re.compile(r"<[^>]*>|\{\\[^}]*\}")


def ytdlp_args(spoken: Sequence[str]) -> list:
    """The yt-dlp arguments that fetch the tracks :func:`choose` picks from,
    for the ISO 639-1 codes in ``spoken``."""
    wanted = ["%s.*%s" % (code, _ORIGINAL) for code in spoken if code != _ENGLISH]
    wanted.append(_ENGLISH + ".*")
    return ["--write-subs", "--write-auto-subs", "--sub-format", "vtt/srt/best",
            "--sub-langs", ",".join(wanted), "--sleep-subtitles", "1"]


class Choice(NamedTuple):
    """The transcript kept for an episode: where, and in which track."""

    path: str
    language: str


def choose(episode: str, spoken: Sequence[str]) -> Choice | None:
    """The track kept for the episode at ``episode``, written as
    ``<stem>.srt``, and every track yt-dlp fetched for it removed - or None
    when none of them is one to keep.

    The episode's own language, when it is spoken, and English otherwise; in
    either language a track the uploader wrote over one made by speech
    recognition, where both are there.
    """
    tracks = _tracks(os.path.splitext(episode)[0])
    language = _pick(sorted(tracks), [code.lower() for code in spoken])
    kept = None
    if language and tracks[language].lower().endswith(_READABLE):
        cues = _plain_cues(_blocks(_read(tracks[language])))
        if cues:
            target = os.path.splitext(episode)[0] + ".srt"
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(_srt(cues))
            kept = Choice(target, language)
    for path in tracks.values():
        try:
            os.unlink(path)
        except OSError:
            pass
    return kept


def _tracks(stem: str) -> dict:
    """``{language: path}`` for the caption tracks beside ``stem``."""
    folder = os.path.dirname(stem) or "."
    base = os.path.basename(stem) + "."
    found: dict = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return found
    for name in names:
        if not name.startswith(base):
            continue
        language, _, suffix = name[len(base):].rpartition(".")
        if language and "." not in language \
                and "." + suffix.lower() in _TRACK_SUFFIXES:
            found[language] = os.path.join(folder, name)
    return found


def _code(language: str) -> str:
    return language.split("-")[0].lower()


def _pick(languages: Sequence[str], spoken: Sequence[str]) -> str:
    """Which of the fetched ``languages`` is kept."""
    for language in languages:
        if language.endswith(_ORIGINAL) and _code(language) in spoken:
            return _best_in(languages, _code(language))
    return _best_in(languages, _ENGLISH)


def _best_in(languages: Sequence[str], code: str) -> str:
    """The track in ``code`` worth most: one written by a person - the bare
    code, then a regional one - before the speech recognition's original."""
    ours = [language for language in languages if _code(language) == code]
    written = [language for language in ours if not language.endswith(_ORIGINAL)]
    exact = [language for language in written if language.lower() == code]
    ranked = exact + written + [language for language in ours if language not in written]
    return ranked[0] if ranked else ""


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().lstrip("﻿").replace("\r", "")
    except OSError:
        return ""


def _ms(hours, minutes, seconds, fraction) -> int:
    return ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 \
        + int(fraction.ljust(3, "0")[:3])


def _blocks(text: str) -> list:
    """``(start, end, lines)`` for each cue of an SRT or WebVTT text, the lines
    without their markup.

    A cue ends at an empty line only: YouTube's own captions open every cue
    with a line holding one space, which ends nothing.
    """
    blocks = []
    lines = text.split("\n")
    at = 0
    while at < len(lines):
        timing = _TIME.search(lines[at])
        at += 1
        if not timing:
            continue
        parts = timing.groups()
        said = []
        while at < len(lines) and lines[at] != "" and not _TIME.search(lines[at]):
            said.append(" ".join(_MARKUP.sub("", lines[at]).split()))
            at += 1
        blocks.append((_ms(*parts[:4]), _ms(*parts[4:]), said))
    return blocks


def _plain_cues(blocks) -> list:
    """The cues with each line said once.

    Speech-recognition captions roll: a cue repeats the line before it above
    its new one, and a cue a few milliseconds long holds only the repeat. Read
    as they are, every line would be in a transcript - and in the lyrics - two
    or three times over.
    """
    cues = []
    before: set = set()
    for start, end, lines in blocks:
        said = [line for line in lines if line]
        new = [line for line in said if line not in before]
        before = set(said)
        if new:
            cues.append((start, end, " ".join(new)))
    return cues


def _time(ms: int) -> str:
    ms = max(ms, 0)
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60,
                                    ms // 1000 % 60, ms % 1000)


def _srt(cues) -> str:
    return "".join("%d\n%s --> %s\n%s\n\n" % (number, _time(start), _time(end), text)
                   for number, (start, end, text) in enumerate(cues, 1))
