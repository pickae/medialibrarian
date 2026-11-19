"""convert-video's -n: chapters for a video that has none, worked out by a
language model from what is said in it, and handed to the final mux.

What is said is read, where it can be, from a text subtitle that is the film's
own dialogue - not a commentary's, not the one for the deaf and hard of hearing,
not the forced lines (:data:`enums.SUBTITLE_ROLES`) - because that is only a
read of what the file already holds. Only a video with no such subtitle is
transcribed, from its soundtrack: an audio track that is no commentary, audio
description, score or other bonus audio (:data:`enums.AUDIO_ROLES`). That
transcript is muxed into the output as a subtitle of its own, in the same mux
that writes the chapters, so the finished file is written once.

Of several tracks that could serve, the first that works is taken: a subtitle
that turns out to hold a few lines rather than the dialogue, or a soundtrack
whisper hears no speech in, is passed over for the next.

It runs on a thread of its own beside the file's video encode and needs nothing
the encoder does. One file is chaptered at a time across the run, because
whisper fills the GPU on its own and a local model shares it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from typing import NamedTuple

from medialib.lib import (
    aichapters,
    audiotracks,
    chapterllm,
    chapters,
    languages,
    safety,
    subtitletracks,
    toolcapture,
)

__all__ = ["Candidate", "Deferred", "LATER", "Outcome", "candidates", "chapter",
           "model", "TRANSCRIPT_TITLE"]

# Where the work is done, under the file's own scratch folder, which the run
# removes with the rest once the file is finished.
SUBFOLDER = "chaptering"

# The name the transcript's subtitle track is given in the output.
TRANSCRIPT_TITLE = "Transcript"

# The fewest subtitle lines per minute of the video a track must hold to be read
# as its dialogue. A film's dialogue runs at ten or more; a subtitle that holds
# only the signs and foreign lines, and is not tagged as such, at one or two.
MIN_CUES_PER_MINUTE = 3


class Candidate(NamedTuple):
    """A track the chapters could be worked out from: ``kind`` is "subtitle"
    or "audio", ``position`` its 0-based place among the streams of that kind
    (ffmpeg's ``0:s:N`` and ``0:a:N``)."""

    kind: str
    position: int
    language: str
    name: str


class Outcome(NamedTuple):
    """What the mux is handed: the chapters as an ffmetadata file and as OGM
    rows ("" for none), and a transcript to add as a subtitle ("" when the
    words came from a subtitle the file already has) with its language and
    the output subtitle index it lands at - after every subtitle of the
    source. ``marks`` are the chapters themselves, (start ms, title), for a
    caller that writes them some other way than by a mux."""

    ffmetadata: str
    rows: str
    transcript: str
    language: str
    subtitle_index: int
    marks: tuple = ()


class Deferred:
    """What :func:`chapter` answers when only a soundtrack is left to chapter
    from and it was asked not to transcribe yet."""


LATER = Deferred()


def model(program: str, instructions: str, flag: str,
          log: Callable[[str], None]):
    """The chapter model the settings describe, given the user's own
    ``instructions`` for every request, or None when none is set up (which has
    been said). ``flag`` names the option that asked for it, for the line that
    says what is used."""
    llm = chapterllm.require(program)
    if llm is None:
        return None
    llm = llm.following(instructions)
    log("Chapters (%s): %s" % (flag, llm.describe()))
    return llm


def probe_streams(source: str) -> list[dict]:
    """The source's streams as ffprobe reports them, [] when it cannot."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name:stream_tags=title,language"
             ":stream_disposition=comment,forced,hearing_impaired,"
             "visual_impaired", "-of", "json", source],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        return list(json.loads(done.stdout.decode("utf-8", "surrogateescape")
                               or "{}").get("streams") or [])
    except (OSError, ValueError):
        return []


def candidates(streams: list[dict]) -> list[Candidate]:
    """The tracks to work the chapters out from, in the order they are tried:
    every dialogue text subtitle in the file's order, then every soundtrack."""
    subtitles: list[Candidate] = []
    audio: list[Candidate] = []
    subtitle_index = audio_index = 0
    for stream in streams:
        kind = stream.get("codec_type")
        tags = stream.get("tags") or {}
        flags = stream.get("disposition") or {}
        name = str(tags.get("title") or "")
        language = str(tags.get("language") or "")
        if kind == "subtitle":
            if (subtitletracks.subtitle_form(str(stream.get("codec_name") or ""))
                    == "text"
                    and subtitletracks.subtitle_role(
                        name, forced=bool(flags.get("forced")),
                        hearing_impaired=bool(flags.get("hearing_impaired")),
                        commentary=bool(flags.get("comment"))) == "full"):
                subtitles.append(Candidate("subtitle", subtitle_index,
                                           language, name))
            subtitle_index += 1
        elif kind == "audio":
            if audiotracks.is_main(
                    name, commentary=bool(flags.get("comment")),
                    visual_impaired=bool(flags.get("visual_impaired"))):
                audio.append(Candidate("audio", audio_index, language, name))
            audio_index += 1
    return subtitles + audio


def subtitle_count(streams: list[dict]) -> int:
    return sum(1 for stream in streams if stream.get("codec_type") == "subtitle")


def enough_dialogue(cues: list, duration_ms: int) -> bool:
    """Whether a transcript holds a film's dialogue rather than a few lines of
    it. An unknown duration asks only for something to read."""
    if not cues:
        return False
    if duration_ms <= 0:
        return True
    return len(cues) * 60000 >= MIN_CUES_PER_MINUTE * duration_ms


def _ffmpeg(argv: list, label: str) -> bool:
    return toolcapture.run(["ffmpeg", "-nostdin", "-hide_banner",
                            *toolcapture.ffmpeg_loglevel(), "-nostats", "-y",
                            *argv], label, report=False).ok


def _subtitle_cues(source: str, candidate: Candidate, work: str,
                   label: str, disk=contextlib.nullcontext) -> list:
    """The candidate subtitle read out as SRT, as transcript cues."""
    out = os.path.join(work, "subtitle%d.srt" % candidate.position)
    with disk():
        read = _ffmpeg(["-i", source, "-map", "0:s:%d" % candidate.position,
                        "-c:s", "srt", out], label)
    if not read:
        return []
    return aichapters.read_transcript(out)


def _transcribe(source: str, candidate: Candidate, work: str, label: str,
                script_dir: str, disk=contextlib.nullcontext) -> str:
    """The candidate soundtrack transcribed by transcribe-audio, as the path
    of its SRT, "" when nothing came of it.

    Lifted out first to the 16 kHz mono whisper resamples to anyway, so whisper
    reads a few dozen megabytes and not a film's lossless surround track.
    """
    stem = "audio%d" % candidate.position
    lifted = os.path.join(work, stem + ".flac")
    with disk():
        lifted_ok = _ffmpeg(["-i", source, "-map", "0:a:%d" % candidate.position,
                             "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
                             "-c:a", "flac", lifted], label)
    if not lifted_ok:
        return ""
    stage = os.path.join(work, "stage")
    transcripts = os.path.join(work, "transcripts")
    os.makedirs(stage, exist_ok=True)
    os.makedirs(transcripts, exist_ok=True)
    aichapters.transcribe([(stem + ".flac", lifted)], stage, transcripts,
                          script_dir)
    # whisper can exit 0 having written nothing, so what counts is the file.
    try:
        os.remove(lifted)
    except OSError:
        pass
    return aichapters.find_transcript(os.path.join(transcripts, stem))


def chapter(source: str, directory: str, llm, label: str, script_dir: str,
            log: Callable[[str], None], *, allow_audio: bool = True,
            disk=contextlib.nullcontext,
            gpu=contextlib.nullcontext) -> Outcome | None | Deferred:
    """Chapters for ``source``, worked out in ``directory``'s scratch, or None
    when it has chapters already, is too short for them, or nothing it holds
    could be read or heard.

    ``disk`` is called for a context that holds the source's disk's turn around
    each read of the source, and not around the model's questions; ``gpu``
    likewise holds the GPU's turn around the model's questions, for a model
    that runs on the same card as other work. Without
    ``allow_audio`` a source that has no dialogue subtitle to read is not
    transcribed but answered :data:`LATER`, for a caller that has whisper's
    turn to wait for."""
    if chapters.chapter_count(source) > 0:
        return None
    duration = chapters.duration_ms(source)
    if aichapters.too_short(duration):
        log("Too short for chapters: " + label)
        return None
    streams = probe_streams(source)
    found = candidates(streams)
    if not found:
        log("No dialogue subtitle or soundtrack to chapter from: " + label)
        return None

    work = os.path.join(directory, SUBFOLDER)
    os.makedirs(work, exist_ok=True)
    cues: list = []
    transcript = ""
    language = ""
    for candidate in found:
        if safety.abort_requested():
            return None
        what = "%s track %d%s" % (candidate.kind, candidate.position + 1,
                                  " (%s)" % candidate.name if candidate.name
                                  else "")
        if candidate.kind == "subtitle":
            cues = _subtitle_cues(source, candidate, work, label, disk)
            if enough_dialogue(cues, duration):
                log("Chaptering from the %s: %s" % (what, label))
                break
        else:
            if not allow_audio:
                return LATER
            log("Transcribing the %s for its chapters: %s" % (what, label))
            made = _transcribe(source, candidate, work, label, script_dir,
                               disk)
            cues = aichapters.read_transcript(made) if made else []
            if enough_dialogue(cues, duration):
                transcript = os.path.join(work, "transcript.srt")
                shutil.copyfile(made, transcript)
                language = (candidate.language
                            if languages.is_real_language_tag(candidate.language)
                            else "")
                break
        log("Passed over the %s, which holds too little dialogue: %s"
            % (what, label))
        cues = []
    if not cues:
        log("Nothing to chapter from: " + label)
        return None
    if safety.abort_requested():
        return None

    with gpu():
        marks = aichapters.generate(cues, llm, log=lambda line: log(
            "%s: %s" % (label, line.strip())))
    ffmetadata = rows_file = ""
    if marks:
        rows = aichapters.chapter_rows(marks)
        rows_file = os.path.join(work, "chapters.txt")
        with open(rows_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")
        ffmetadata = os.path.join(work, "chapters.ffmetadata")
        with open(ffmetadata, "w", encoding="utf-8") as handle:
            handle.write(chapters.ffmetadata_chapters(rows, "", duration))
        log("%d chapters: %s" % (len(marks), label))
    else:
        log("No chapters (the model gave no usable outline): " + label)
    if not marks and not transcript:
        return None
    return Outcome(ffmetadata, rows_file, transcript, language,
                   subtitle_count(streams), tuple(marks))
