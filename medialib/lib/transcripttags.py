"""A transcript carried inside the audio it transcribes.

Which form it takes is the container's to decide, not a preference:

  Opus and FLAC                as synchronised lyrics - LRC, one ``[mm:ss.xx]``
                               line per cue - in the Vorbis comment ``LYRICS``,
                               which is where audio players look for text that
                               follows the playback. Ogg has no subtitle stream
                               ffmpeg can write, and a Matroska detour would
                               give up the Ogg file, so this is the one way in.
  MP4 (.m4a, .m4b)             as a timed-text subtitle track (``mov_text``),
                               which players show as subtitles.

Every other container is left as it is.
"""

from __future__ import annotations

import os
import re
import subprocess

from medialib.lib import aichapters, chapters, mutagentags, toolcapture

__all__ = ["LYRICS_TAG", "SIDECAR_SUFFIXES", "find_sidecar", "lrc_text",
           "embed_transcript", "has_transcript"]

LYRICS_TAG = "LYRICS"

# The sidecars a transcript is looked for in, in this order: the formats with
# a time on every line.
SIDECAR_SUFFIXES = (".srt", ".vtt", ".lrc")

# A sidecar carrying a language between the stem and its extension, the way
# whisper, yt-dlp and subtitle sites name them: show.en.srt, show.pt-BR.vtt.
_LANGUAGE = re.compile(r"\.[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?$")


def find_sidecar(stem: str) -> str:
    """The transcript sidecar of the file at ``stem`` - its path without its
    extension - or "" when there is none: ``<stem>.srt`` and its kin first,
    then one with a language code in front of the extension."""
    for suffix in SIDECAR_SUFFIXES:
        if os.path.isfile(stem + suffix) and os.path.getsize(stem + suffix) > 0:
            return stem + suffix
    folder = os.path.dirname(stem) or "."
    base = os.path.basename(stem)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return ""
    for suffix in SIDECAR_SUFFIXES:
        for name in names:
            if not name.lower().endswith(suffix) or not name.startswith(base + "."):
                continue
            middle = name[len(base):-len(suffix)]
            path = os.path.join(folder, name)
            if _LANGUAGE.fullmatch(middle) and os.path.getsize(path) > 0:
                return path
    return ""


def lrc_text(cues) -> str:
    """The cues as LRC: ``[mm:ss.xx]`` and the line, minutes past 99 written
    out in full rather than wrapped."""
    lines = []
    for cue in cues:
        hundredths = max(cue.start, 0) // 10
        lines.append("[%02d:%02d.%02d]%s" % (hundredths // 6000, hundredths // 100 % 60,
                                             hundredths % 100, " ".join(cue.text.split())))
    return "\n".join(lines) + "\n"


def embed_transcript(target: str, transcript: str, tmp_dir: str,
                     run=chapters._run) -> int:
    """The transcript written into ``target`` in place, in the form its
    container takes. 0 when it was written, 1 when it could not be or the
    container takes none."""
    cues = aichapters.read_transcript(transcript)
    if not cues:
        return 1
    if chapters._is_mp4(target):
        subtitles = os.path.join(tmp_dir, "transcript.srt")
        with open(subtitles, "w", encoding="utf-8") as handle:
            for number, cue in enumerate(cues, 1):
                handle.write("%d\n%s --> %s\n%s\n\n" % (
                    number, _srt_time(cue.start), _srt_time(cue.end), cue.text))
        staged = os.path.join(tmp_dir, "transcript" + os.path.splitext(target)[1])
        chapters._remove_quiet(staged)
        run(["ffmpeg", "-nostdin", "-hide_banner", *toolcapture.ffmpeg_loglevel(),
             "-y", "-i", target, "-i", subtitles, "-map", "0:a", "-map", "1",
             "-map_metadata", "0", "-map_chapters", "0", "-c:a", "copy",
             "-c:s", "mov_text", staged])
        if not os.path.isfile(staged) or os.path.getsize(staged) <= 0:
            chapters._remove_quiet(staged)
            return 1
        return chapters._move_over(staged, target)
    if not target.lower().endswith((".opus", ".flac")):
        return 1
    return mutagentags.set_tag(target, LYRICS_TAG, lrc_text(cues))


def has_transcript(target: str) -> bool:
    """Whether ``target`` carries a transcript already - or, for a container
    that takes none, True, since there is nothing to add."""
    if chapters._is_mp4(target):
        try:
            proc = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "s",
                                   "-show_entries", "stream=index", "-of", "csv=p=0",
                                   target], capture_output=True)
        except OSError:
            return True
        return bool(proc.stdout.strip())
    if not target.lower().endswith((".opus", ".flac")):
        return True
    return mutagentags.has_tag(target, LYRICS_TAG)


def _srt_time(ms: int) -> str:
    ms = max(ms, 0)
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60,
                                    ms // 1000 % 60, ms % 1000)
