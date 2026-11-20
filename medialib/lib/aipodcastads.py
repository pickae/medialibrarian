"""Finding host-read ads, sponsor reads and self-promotion in RSS and site
podcasts, where yt-dlp's SponsorBlock does not reach.

YouTube profiles already carry SponsorBlock; ``rss*`` and ``site*`` feeds keep
whatever ads the show baked into its enclosure. When an episode has a
transcript - from ``ytdlp -i -C`` (embedded inside the converted audio) or a
sidecar beside the download - a language model (:data:`llmroster.PODCAST_ADS`)
reads the timestamped transcript and lists the ``{start, end, kind}`` stretches
of ads, sponsor reads and self-promotion.

By default those stretches are merged into the episode's chapters as
``[Sponsor]``, ``[Ad]`` and ``[Self-promo]`` marks so any player can skip them
without losing a single real chapter or re-encoding the audio. Cutting them out
of the file is a separate opt-in (:data:`CUT_VARIABLE`), strictly validated and
written atomically so the original is never damaged on failure.

Every model decision is logged in the run's output and counted through
:mod:`medialib.lib.modelmeter`.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from medialib.lib import (
    aichapters,
    chapters,
    cuechapters,
    llmclient,
    llmroster,
    modelmeter,
    podcastfeeds,
    runlog,
    safety,
    toolcapture,
    transcripttags,
)
from medialib.lib.enums import VIDEO_EXTENSIONS, shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "CUT_VARIABLE",
    "PURPOSE",
    "AD_KINDS",
    "KIND_LABELS",
    "MIN_SPAN_SECONDS",
    "MAX_SPAN_SECONDS",
    "MAX_EPISODE_SHARE",
    "AdSpan",
    "enabled",
    "cut_enabled",
    "eligible_profile",
    "parse_timestamp_ms",
    "normalize_kind",
    "parse_spans",
    "validate_spans",
    "prompt",
    "read_episode_transcript",
    "has_ad_chapters",
    "merge_chapters",
    "keep_segments",
    "shift_chapters_for_cut",
    "ffmpeg_cut_filter",
    "ffmpeg_cut_argv",
    "detect_ads",
    "cut_ads",
    "process_episode",
]

# Whether the model is asked for RSS/site podcast ad reads: on unless set off.
SWITCH_VARIABLE = "podcastAdsLlm"

# Opt-in switch to cut detected ad stretches out of the file instead of only
# marking them as skippable chapters. Always off by default.
CUT_VARIABLE = "podcastAdsCut"

# What the model's requests are counted as in modelmeter.
PURPOSE = "podcast-ads"

AD_KINDS = ("sponsor", "ad", "selfpromo")

KIND_LABELS = {
    "sponsor": "[Sponsor]",
    "ad": "[Ad]",
    "selfpromo": "[Self-promo]",
}

_KIND_ALIASES = {
    "sponsor": "sponsor",
    "ad": "ad",
    "selfpromo": "selfpromo",
    "self-promo": "selfpromo",
    "self_promo": "selfpromo",
}

# Sanity bounds for individual spans and total share of an episode.
MIN_SPAN_SECONDS = 5.0
MAX_SPAN_SECONDS = 600.0
MAX_EPISODE_SHARE = 0.50

# How long a passage line runs in the prompt sent to the model.
PASSAGE_SECONDS = 20

# Token budget sizing, matching aichapters.
CHARS_PER_TOKEN = 3
WINDOW_SHARE = 0.7

# Smallest content sliver worth keeping as its own chapter or cut segment.
_MIN_CHAPTER_SLIVER_MS = 1000
MIN_KEEP_SEGMENT_MS = 500
_CUT_DURATION_TOLERANCE_MS = 2000

_MS_PER_SECOND = 1000

_log = runlog.log
_detail = runlog.detail

SYSTEM = """You identify advertisement breaks in podcast episode transcripts.
You are given a timestamped transcript of one podcast episode and you answer
with JSON only, listing every stretch of:
- "sponsor": a host-read sponsor message or paid endorsement
- "ad": a dynamically inserted or pre-recorded advertisement
- "selfpromo": a promotional plug for the show's own merch, Patreon, paid
  membership, live tour, or sister shows

Rules:
1. Only include clear, unambiguous ad, sponsor or self-promotion stretches.
   Never mark ordinary episode discussion, news, interviews, or brief passing
   mentions of a product or brand.
2. Use timestamps directly from the transcript lines as HH:MM:SS (or seconds),
   with start strictly before end, and no overlapping stretches.
3. Answer with JSON only in this exact form:
   {"spans": [{"start": "00:05:10", "end": "00:06:30", "kind": "sponsor"}]}
   Return {"spans": []} when the episode has no ads, sponsor reads or
   self-promotion."""


@dataclass(frozen=True)
class AdSpan:
    """One validated ad, sponsor read or self-promotion stretch."""

    start_ms: int
    end_ms: int
    kind: str

    @property
    def start_seconds(self) -> float:
        return self.start_ms / _MS_PER_SECOND

    @property
    def end_seconds(self) -> float:
        return self.end_ms / _MS_PER_SECOND

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def label(self) -> str:
        return KIND_LABELS[self.kind]


def enabled() -> bool:
    """Whether podcast ad-read detection is enabled (on unless set off)."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def cut_enabled() -> bool:
    """Whether opt-in ad cutting is enabled (off unless explicitly set on)."""
    value = shell_lower(os.environ.get(CUT_VARIABLE, "off").strip())
    return value in ("on", "1", "yes", "true")


def eligible_profile(profile: str) -> bool:
    """True for non-SponsorBlock profiles (``rss*`` and ``site*``)."""
    return podcastfeeds.podcast_profile_provider(profile) in ("rss", "site")


# --- parsing and validating model spans -------------------------------------

_CLOCK_RE = re.compile(r"^(\d+):(\d{1,2})(?::(\d{1,2}))?(?:[.,](\d{1,3}))?$")
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


def parse_timestamp_ms(value: object) -> int | None:
    """Parse seconds (int/float/numeric string) or ``[H:]MM:SS[.mmm]`` into
    non-negative milliseconds, or None when invalid."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value) or value < 0:
            return None
        return round(value * _MS_PER_SECOND)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if _NUMBER_RE.match(text):
        seconds = float(text)
        return round(seconds * _MS_PER_SECOND) if math.isfinite(seconds) else None
    match = _CLOCK_RE.match(text)
    if not match:
        return None
    first, second, third, frac = match.groups()
    ms = int(frac.ljust(3, "0")[:3]) if frac else 0
    if third is not None:
        hours, minutes, secs = int(first), int(second), int(third)
        if minutes >= 60 or secs >= 60:
            return None
        return ((hours * 60 + minutes) * 60 + secs) * _MS_PER_SECOND + ms
    minutes, secs = int(first), int(second)
    if secs >= 60:
        return None
    return (minutes * 60 + secs) * _MS_PER_SECOND + ms


def normalize_kind(value: object) -> str | None:
    """Normalise a span kind to one of :data:`AD_KINDS`, or None."""
    if not isinstance(value, str):
        return None
    return _KIND_ALIASES.get(value.strip().lower())


def parse_spans(answer: str) -> list[dict[str, object]]:
    """Extract raw ``{start, end, kind}`` dicts from a model answer."""
    found = llmclient.first_json(answer)
    if isinstance(found, dict):
        for key in ("spans", "ads", "segments"):
            if key in found:
                found = found[key]
                break
        else:
            return []
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


def validate_spans(
    raw_spans: Sequence[object],
    duration_ms: int,
    *,
    min_span_seconds: float = MIN_SPAN_SECONDS,
    max_span_seconds: float = MAX_SPAN_SECONDS,
    max_episode_share: float = MAX_EPISODE_SHARE,
) -> tuple[list[AdSpan], str]:
    """Strictly validate raw ad spans against episode bounds and sanity guards.

    Returns ``(spans, reason)`` where ``reason`` is ``""`` when the result is
    accepted (including an empty list when there were no valid spans) and
    explains why the spans were rejected when a set-level guard tripped
    (overlapping spans or total span share exceeding ``max_episode_share``).
    """
    if duration_ms <= 0:
        return [], "unknown or zero episode duration"
    min_ms = round(min_span_seconds * _MS_PER_SECOND)
    max_ms = round(max_span_seconds * _MS_PER_SECOND)

    accepted: list[AdSpan] = []
    for item in raw_spans:
        if isinstance(item, AdSpan):
            start_ms, end_ms = item.start_ms, item.end_ms
            kind = normalize_kind(item.kind)
        elif isinstance(item, dict):
            start_ms_opt = parse_timestamp_ms(item.get("start"))
            end_ms_opt = parse_timestamp_ms(item.get("end"))
            kind = normalize_kind(item.get("kind"))
            if start_ms_opt is None or end_ms_opt is None:
                continue
            start_ms, end_ms = start_ms_opt, end_ms_opt
        else:
            continue
        if kind is None:
            continue
        if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
            continue
        length = end_ms - start_ms
        if length < min_ms or length > max_ms:
            continue
        accepted.append(AdSpan(start_ms, end_ms, kind))

    if not accepted:
        return [], ""

    accepted.sort(key=lambda span: (span.start_ms, span.end_ms))

    for prev, curr in zip(accepted, accepted[1:], strict=False):
        if curr.start_ms < prev.end_ms:
            return [], (
                "overlapping ad spans (%s-%s and %s-%s)"
                % (
                    aichapters.time_label(prev.start_ms),
                    aichapters.time_label(prev.end_ms),
                    aichapters.time_label(curr.start_ms),
                    aichapters.time_label(curr.end_ms),
                )
            )

    total_ms = sum(span.duration_ms for span in accepted)
    if total_ms > duration_ms * max_episode_share:
        return [], (
            "ad spans cover %.0f%% of the episode (limit %.0f%%)"
            % (100.0 * total_ms / duration_ms, 100.0 * max_episode_share)
        )

    return accepted, ""


# --- prompt construction & transcript reading -------------------------------

def _passage_lines(cues: Sequence[aichapters.Cue]) -> list[tuple[int, int, str]]:
    """Cues grouped into ``(start_ms, end_ms, text)`` passages."""
    grouped = aichapters.passages(cues, PASSAGE_SECONDS)
    return [
        (passage.start, max(passage.start, cues[passage.last].end), passage.text)
        for passage in grouped
    ]


def _render_lines(lines: Sequence[tuple[int, int, str]]) -> str:
    return "\n".join(
        "[%s - %s] %s" % (aichapters.time_label(start), aichapters.time_label(end), text)
        for start, end, text in lines
    )


def prompt(
    cues: Sequence[aichapters.Cue],
    duration_ms: int,
    title: str = "",
) -> str:
    """The user prompt sent to the model for one transcript (or window)."""
    return _prompt_for_lines(_passage_lines(cues), duration_ms, title=title)


def _prompt_for_lines(
    lines: Sequence[tuple[int, int, str]],
    duration_ms: int,
    *,
    title: str = "",
    part: tuple[int, int] | None = None,
) -> str:
    header = "Episode: %s\n" % title if title else ""
    part_note = (
        "This is part %d of %d of the transcript (%s to %s).\n"
        % (part[0], part[1], aichapters.time_label(lines[0][0]),
           aichapters.time_label(lines[-1][1]))
        if part and lines
        else ""
    )
    return (
        "%sTotal duration: %s\n%s"
        "Find every ad, sponsor read and self-promotion stretch in this "
        "podcast transcript. Answer with JSON only: "
        '{"spans": [{"start": "HH:MM:SS", "end": "HH:MM:SS", '
        '"kind": "sponsor"|"ad"|"selfpromo"}]}.\n\n'
        "Transcript:\n%s\n"
        % (header, aichapters.time_label(duration_ms), part_note, _render_lines(lines))
    )


def _budget(llm: llmclient.Llm) -> int:
    return max(int(llm.context_tokens * WINDOW_SHARE) - 1500, 1000)


def _tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def _windows(
    lines: Sequence[tuple[int, int, str]],
    budget: int,
) -> list[list[tuple[int, int, str]]]:
    windows: list[list[tuple[int, int, str]]] = [[]]
    used = 0
    for line in lines:
        cost = _tokens(line[2]) + 10
        if windows[-1] and used + cost > budget:
            windows.append([])
            used = 0
        windows[-1].append(line)
        used += cost
    return [window for window in windows if window]


_LRC_LINE = re.compile(r"^\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\](.*)$")
_SRT_TIME = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
_MARKUP = re.compile(r"<[^>]*>|\{\\[^}]*\}")


def _parse_lrc(text: str) -> list[aichapters.Cue]:
    timed: list[tuple[int, str]] = []
    for line in text.replace("\r", "").split("\n"):
        match = _LRC_LINE.match(line.strip())
        if match and match.group(4).strip():
            frac = (match.group(3) or "0").ljust(3, "0")[:3]
            start = (int(match.group(1)) * 60 + int(match.group(2))) * 1000 + int(frac)
            timed.append((start, match.group(4).strip()))
    timed.sort()
    return [
        aichapters.Cue(
            start,
            timed[index + 1][0] if index + 1 < len(timed) else start + 5000,
            words,
        )
        for index, (start, words) in enumerate(timed)
    ]


def _parse_srt(text: str) -> list[aichapters.Cue]:
    cues: list[aichapters.Cue] = []
    lines = text.replace("\r", "").split("\n")
    at = 0
    while at < len(lines):
        match = _SRT_TIME.search(lines[at])
        at += 1
        if not match:
            continue
        g = match.groups()
        start = ((int(g[0] or 0) * 60 + int(g[1])) * 60 + int(g[2])) * 1000 + int(
            g[3].ljust(3, "0")[:3]
        )
        end = ((int(g[4] or 0) * 60 + int(g[5])) * 60 + int(g[6])) * 1000 + int(
            g[7].ljust(3, "0")[:3]
        )
        words = []
        while at < len(lines) and lines[at].strip():
            cleaned = _MARKUP.sub("", lines[at]).strip()
            if cleaned:
                words.append(cleaned)
            at += 1
        if words:
            cues.append(aichapters.Cue(start, end, " ".join(words)))
    return sorted(cues, key=lambda cue: cue.start)


def _read_embedded_lyrics(media_path: str) -> list[aichapters.Cue]:
    lower = media_path.lower()
    if not lower.endswith((".opus", ".flac", ".ogg", ".oga")):
        return []
    try:
        import mutagen

        handle = mutagen.File(media_path)
    except Exception:
        return []
    if handle is None:
        return []
    values = handle.get(transcripttags.LYRICS_TAG) or handle.get(
        transcripttags.LYRICS_TAG.lower()
    )
    if not values:
        return []
    text = values[0] if isinstance(values, list) else str(values)
    return _parse_lrc(str(text))


def _read_embedded_subtitles(media_path: str) -> list[aichapters.Cue]:
    lower = media_path.lower()
    if not lower.endswith((".m4a", ".m4b", ".mp4", ".mkv", ".webm")):
        return []
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "quiet", "-i", media_path,
             "-map", "0:s:0", "-f", "srt", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    return _parse_srt(proc.stdout.decode("utf-8", "replace"))


def read_episode_transcript(media_path: str, source_path: str = "") -> list[aichapters.Cue]:
    """Read an episode's transcript from a sidecar file or embedded metadata."""
    for candidate in (media_path, source_path):
        if not candidate:
            continue
        stem = os.path.splitext(candidate)[0]
        sidecar = transcripttags.find_sidecar(stem) or aichapters.find_transcript(stem)
        if sidecar:
            cues = aichapters.read_transcript(sidecar)
            if cues:
                return cues
    return _read_embedded_lyrics(media_path) or _read_embedded_subtitles(media_path)


# --- merging ad spans into OGM chapters (default mode) ----------------------

def _parse_ogm_rows(chapter_rows: Sequence[str]) -> list[tuple[int, str]]:
    """Parse OGM CHAPTERnn=/CHAPTERnnNAME= rows into sorted (start_ms, title)."""
    starts: dict[str, int] = {}
    names: dict[str, str] = {}
    order: list[str] = []
    for raw in chapter_rows:
        line = raw.strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        upper = key.upper()
        if upper.startswith("CHAPTER") and upper.endswith("NAME"):
            ident = upper[:-4]
            names[ident] = value
        elif upper.startswith("CHAPTER"):
            ident = upper
            starts[ident] = chapters._ogm_milliseconds(value)
            order.append(ident)
    parsed = [(starts[ident], names.get(ident, "")) for ident in order if ident in starts]
    parsed.sort(key=lambda item: item[0])
    # Deduplicate identical timestamps, keeping the last title for that instant.
    deduped: list[tuple[int, str]] = []
    for start_ms, title in parsed:
        if deduped and deduped[-1][0] == start_ms:
            deduped[-1] = (start_ms, title)
        else:
            deduped.append((start_ms, title))
    return deduped


def has_ad_chapters(chapter_rows: Sequence[str]) -> bool:
    """Whether ``chapter_rows`` already contain an ad-read chapter mark."""
    labels = set(KIND_LABELS.values())
    return any(title in labels for _start, title in _parse_ogm_rows(chapter_rows))


def merge_chapters(
    existing_rows: Sequence[str],
    ad_spans: Sequence[AdSpan],
    duration_ms: int,
) -> list[str]:
    """Merge ``ad_spans`` into ``existing_rows`` as skippable chapters without
    losing real chapters.

    Real chapters split by a mid-roll ad resume cleanly after the ad with
    ``(cont.)``; a chapter whose start falls inside an ad begins right when
    the ad finishes so it is never lost; adjacent ads emit no empty chapter
    between them; and a post-roll ad reaching the end emits no trailing mark.
    """
    if not ad_spans:
        return list(existing_rows)

    existing = _parse_ogm_rows(existing_rows)
    had_real_chapters = bool(existing)
    if not existing:
        existing = [(0, "Episode")]
    elif existing[0][0] > _MIN_CHAPTER_SLIVER_MS:
        existing = [(0, "Episode"), *existing]

    end_bound = max(duration_ms, existing[-1][0], max(span.end_ms for span in ad_spans))

    # Build content intervals from the existing chapters.
    content_intervals: list[tuple[int, int, str, int]] = []
    for index, (start_ms, title) in enumerate(existing):
        stop_ms = existing[index + 1][0] if index + 1 < len(existing) else end_bound
        if stop_ms > start_ms:
            content_intervals.append((start_ms, stop_ms, title or "Chapter %02d" % (index + 1),
                                      index))

    sorted_ads = sorted(ad_spans, key=lambda span: (span.start_ms, span.end_ms))

    # Build the timeline partition: alternating content gaps and ad stretches.
    marks: list[tuple[int, str]] = []
    emitted_chapters: set[int] = set()

    def emit_content(gap_start: int, gap_end: int) -> None:
        if gap_end - gap_start < _MIN_CHAPTER_SLIVER_MS:
            return
        for ch_start, ch_end, title, ch_id in content_intervals:
            slice_start = max(gap_start, ch_start)
            slice_end = min(gap_end, ch_end)
            if slice_end - slice_start < _MIN_CHAPTER_SLIVER_MS:
                continue
            if ch_id in emitted_chapters and had_real_chapters:
                label = title if title.endswith("(cont.)") else "%s (cont.)" % title
            else:
                label = title
            emitted_chapters.add(ch_id)
            if marks and marks[-1][0] == slice_start:
                marks[-1] = (slice_start, label)
            else:
                marks.append((slice_start, label))

    cursor = 0
    for span in sorted_ads:
        if span.start_ms > cursor:
            emit_content(cursor, span.start_ms)
        marks.append((span.start_ms, span.label))
        cursor = max(cursor, span.end_ms)
    if cursor < end_bound:
        emit_content(cursor, end_bound)

    return aichapters.chapter_rows(marks)


# --- opt-in cut planning & safe atomic cutting ------------------------------

def keep_segments(
    ad_spans: Sequence[AdSpan],
    duration_ms: int,
    *,
    min_segment_ms: int = MIN_KEEP_SEGMENT_MS,
) -> list[tuple[int, int]]:
    """The ``(start_ms, end_ms)`` stretches of an episode to KEEP when cutting
    out ``ad_spans``."""
    if duration_ms <= 0:
        return []
    sorted_ads = sorted(ad_spans, key=lambda span: (span.start_ms, span.end_ms))
    kept: list[tuple[int, int]] = []
    cursor = 0
    for span in sorted_ads:
        seg_end = min(span.start_ms, duration_ms)
        if seg_end - cursor >= min_segment_ms:
            kept.append((cursor, seg_end))
        cursor = max(cursor, min(span.end_ms, duration_ms))
    if duration_ms - cursor >= min_segment_ms:
        kept.append((cursor, duration_ms))
    return kept


def shift_chapters_for_cut(
    existing_rows: Sequence[str],
    kept: Sequence[tuple[int, int]],
) -> list[str]:
    """Shift existing OGM chapters onto the shortened timeline produced by
    keeping ``kept`` segments."""
    existing = _parse_ogm_rows(existing_rows)
    if not existing or not kept:
        return []
    shifted: list[tuple[int, str]] = []
    for orig_ms, title in existing:
        new_ms: int | None = None
        offset = 0
        for seg_start, seg_end in kept:
            if orig_ms < seg_start:
                # Chapter started inside a cut stretch: snap to the start of
                # the next kept segment.
                new_ms = offset
                break
            if seg_start <= orig_ms < seg_end:
                new_ms = offset + (orig_ms - seg_start)
                break
            offset += seg_end - seg_start
        if new_ms is None:
            continue
        if shifted and shifted[-1][0] == new_ms:
            shifted[-1] = (new_ms, title)
        else:
            shifted.append((new_ms, title))
    return [
        row
        for number, (start, title) in enumerate(shifted, 1)
        for row in (cuechapters.time_row(number, start), cuechapters.name_row(number, title))
    ]


def _sec_str(ms: int) -> str:
    return "%.3f" % (ms / _MS_PER_SECOND)


def ffmpeg_cut_filter(
    kept: Sequence[tuple[int, int]],
    *,
    has_video: bool = False,
) -> str:
    """Build an ffmpeg ``-filter_complex`` graph that keeps ``kept`` segments
    and concatenates them seamlessly."""
    if not kept:
        raise ValueError("keep_segments must not be empty")
    parts: list[str] = []
    inputs: list[str] = []
    for index, (start_ms, end_ms) in enumerate(kept):
        start_s, end_s = _sec_str(start_ms), _sec_str(end_ms)
        if has_video:
            parts.append(
                "[0:v]trim=start=%s:end=%s,setpts=PTS-STARTPTS[v%d]"
                % (start_s, end_s, index)
            )
            parts.append(
                "[0:a]atrim=start=%s:end=%s,asetpts=PTS-STARTPTS[a%d]"
                % (start_s, end_s, index)
            )
            inputs.append("[v%d][a%d]" % (index, index))
        else:
            parts.append(
                "[0:a]atrim=start=%s:end=%s,asetpts=PTS-STARTPTS[a%d]"
                % (start_s, end_s, index)
            )
            inputs.append("[a%d]" % index)
    if has_video:
        parts.append("%sconcat=n=%d:v=1:a=1[vout][aout]" % ("".join(inputs), len(kept)))
    else:
        parts.append("%sconcat=n=%d:v=0:a=1[aout]" % ("".join(inputs), len(kept)))
    return ";".join(parts)


def _default_codec_args(path: str, has_video: bool) -> tuple[str, ...]:
    ext = os.path.splitext(path)[1].lower()
    audio = {
        ".opus": ("-c:a", "libopus"),
        ".ogg": ("-c:a", "libopus"),
        ".oga": ("-c:a", "libopus"),
        ".m4a": ("-c:a", "aac"),
        ".m4b": ("-c:a", "aac"),
        ".mp4": ("-c:a", "aac"),
        ".flac": ("-c:a", "flac"),
        ".mp3": ("-c:a", "libmp3lame"),
        ".mkv": ("-c:a", "libopus"),
        ".webm": ("-c:a", "libopus"),
    }.get(ext, ("-c:a", "libopus"))
    if has_video:
        return ("-c:v", "libx264", *audio)
    return audio


def ffmpeg_cut_argv(
    src: str,
    dst: str,
    kept: Sequence[tuple[int, int]],
    *,
    has_video: bool = False,
    codec_args: Sequence[str] = (),
) -> list[str]:
    """Build the full ffmpeg command line to write ``kept`` segments to ``dst``."""
    graph = ffmpeg_cut_filter(kept, has_video=has_video)
    maps = ["-map", "[vout]", "-map", "[aout]"] if has_video else ["-map", "[aout]"]
    codecs = list(codec_args) if codec_args else list(_default_codec_args(dst, has_video))
    return [
        "ffmpeg", "-nostdin", "-hide_banner", *toolcapture.ffmpeg_loglevel(),
        "-y", "-i", src, "-filter_complex", graph,
        *maps, "-map_metadata", "0", *codecs, dst,
    ]


def _is_video_path(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return ext in VIDEO_EXTENSIONS


def _atomic_replace(staged: str, target: str) -> int:
    """Atomically replace ``target`` with ``staged``, preserving mtime even
    when ``staged`` lives on another filesystem (e.g. RAM scratch)."""
    parent = os.path.dirname(os.path.abspath(target)) or "."
    try:
        orig_stat = os.stat(target)
    except OSError:
        orig_stat = None
    fd, sibling = tempfile.mkstemp(prefix=".adcut.", suffix=os.path.splitext(target)[1],
                                   dir=parent)
    os.close(fd)
    try:
        shutil.copyfile(staged, sibling)
        if orig_stat is not None:
            try:
                os.utime(sibling, (orig_stat.st_atime, orig_stat.st_mtime))
            except OSError:
                pass
        os.replace(sibling, target)
        return 0
    except OSError:
        try:
            os.remove(sibling)
        except OSError:
            pass
        return 1


def cut_ads(
    media_path: str,
    ad_spans: Sequence[AdSpan],
    duration_ms: int,
    *,
    existing_rows: Sequence[str] = (),
    tmp_dir: str = "",
    has_video: bool | None = None,
    run: chapters.Runner = chapters._run,
    duration_probe: Callable[[str], int] = chapters.duration_ms,
    chapter_probe: Callable[[str], str] = chapters._flat_chapters,
) -> int:
    """Cut validated ``ad_spans`` out of ``media_path`` via a staged ffmpeg
    pass and atomic replacement. Returns 0 on success, non-zero on refusal or
    failure (leaving ``media_path`` untouched)."""
    if safety.abort_requested():
        return 1
    valid, reason = validate_spans(ad_spans, duration_ms)
    if reason or not valid:
        return 1
    kept = keep_segments(valid, duration_ms)
    if not kept:
        return 1

    video = _is_video_path(media_path) if has_video is None else has_video
    own_tmp = not tmp_dir
    work_dir = tmp_dir or tempfile.mkdtemp(prefix="adcut.")
    os.makedirs(work_dir, exist_ok=True)
    ext = os.path.splitext(media_path)[1]
    fd, staged = tempfile.mkstemp(prefix="adcut.", suffix=ext, dir=work_dir)
    os.close(fd)
    try:
        argv = ffmpeg_cut_argv(media_path, staged, kept, has_video=video)
        result = run(argv)
        if getattr(result, "returncode", 0) != 0:
            return 1
        if not os.path.isfile(staged) or os.path.getsize(staged) <= 0:
            return 1
        expected_ms = sum(stop - start for start, stop in kept)
        actual_ms = duration_probe(staged)
        if actual_ms > 0 and abs(actual_ms - expected_ms) > _CUT_DURATION_TOLERANCE_MS:
            return 1
        if existing_rows:
            shifted = shift_chapters_for_cut(existing_rows, kept)
            if shifted:
                chapters.write_chapters(staged, shifted, work_dir, run=run,
                                        probe=chapter_probe)
        return _atomic_replace(staged, media_path)
    finally:
        if own_tmp:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            try:
                os.remove(staged)
            except OSError:
                pass


# --- model invocation & per-episode orchestration ---------------------------

def _format_spans(ad_spans: Sequence[AdSpan]) -> str:
    return ", ".join(
        "%s-%s %s" % (
            aichapters.time_label(span.start_ms),
            aichapters.time_label(span.end_ms),
            span.kind,
        )
        for span in ad_spans
    )


def detect_ads(
    cues: Sequence[aichapters.Cue],
    duration_ms: int,
    *,
    title: str = "",
    models: Sequence[llmclient.Llm] | None = None,
    meter_file: str = "",
    log: Callable[[str], None] = _log,
) -> tuple[list[AdSpan], str]:
    """Ask the language model for ad, sponsor and self-promotion spans in a
    transcript, validate strictly, log the decision and record modelmeter."""
    if not enabled():
        _detail("    No model asked for podcast ads: the %s setting is off" % SWITCH_VARIABLE)
        return [], "disabled"
    if not cues:
        return [], "no transcript"
    if duration_ms <= 0:
        return [], "unknown duration"
    if safety.abort_requested():
        return [], "aborted"

    if models is None:
        models, why = llmroster.optional(llmroster.PODCAST_ADS)
        if not models:
            _detail("    No model asked for podcast ads: %s" % why)
            return [], why

    lines = _passage_lines(cues)
    if not lines:
        return [], "no transcript"

    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    raw_spans: list[dict[str, object]] = []
    answered = False
    label = title or "episode"

    try:
        for llm in models:
            if safety.abort_requested():
                return [], "aborted"
            windows = _windows(lines, _budget(llm))
            window_spans: list[dict[str, object]] = []
            try:
                with llm.serving(_detail) as bound:
                    for index, window in enumerate(windows, 1):
                        if safety.abort_requested():
                            return [], "aborted"
                        part = (index, len(windows)) if len(windows) > 1 else None
                        user_prompt = _prompt_for_lines(
                            window, duration_ms, title=title, part=part
                        )
                        with spans.timing():
                            reply = bound.ask(SYSTEM, user_prompt, tally)
                        window_spans.extend(parse_spans(reply))
                raw_spans = window_spans
                answered = True
                break
            except llmclient.LlmError as error:
                log("    %s: no podcast ad answer from %s: %s" % (label, llm.name, error))
    finally:
        if tally.requests:
            log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file and tally.requests:
            modelmeter.record(meter_file, spans, tally)

    if not answered:
        return [], "no model answered"

    valid, rejection = validate_spans(raw_spans, duration_ms)
    if rejection:
        log("    %s: ad spans rejected (%s) - left unchanged" % (label, rejection))
        return [], rejection
    if not valid:
        log("    %s: no ad reads found" % label)
        return [], ""

    log("    %s: %d ad stretch(es) found: %s" % (label, len(valid), _format_spans(valid)))
    return valid, ""


def process_episode(
    media_path: str,
    profile: str,
    *,
    source_path: str = "",
    transcript_path: str = "",
    models: Sequence[llmclient.Llm] | None = None,
    cut: bool | None = None,
    meter_file: str = "",
    tmp_dir: str = "",
    log: Callable[[str], None] = _log,
    run: chapters.Runner = chapters._run,
    duration_probe: Callable[[str], int] = chapters.duration_ms,
    chapter_probe: Callable[[str], str] = chapters._flat_chapters,
) -> str:
    """Detect and mark (or, with opt-in, cut) ad reads for one episode.

    Returns one of ``"marked"``, ``"cut"``, ``"none"``, ``"skipped"``,
    ``"failed"``.
    """
    name = os.path.basename(media_path)
    if not eligible_profile(profile):
        _detail("    %s: profile %s uses SponsorBlock, skipping ad-read pass" % (name, profile))
        return "skipped"
    if not enabled():
        _detail("    %s: %s is off" % (name, SWITCH_VARIABLE))
        return "skipped"
    if safety.abort_requested():
        return "skipped"

    want_cut = cut_enabled() if cut is None else cut
    existing_rows = chapters._chapters_from_flat(chapter_probe(media_path))
    if not want_cut and has_ad_chapters(existing_rows):
        _detail("    %s: already carries ad-read chapters" % name)
        return "skipped"

    cues = (
        aichapters.read_transcript(transcript_path)
        if transcript_path
        else read_episode_transcript(media_path, source_path)
    )
    if not cues:
        _detail("    %s: no transcript, skipping ad-read pass" % name)
        return "skipped"

    dur = duration_probe(media_path)
    if dur <= 0:
        _detail("    %s: unknown duration, skipping ad-read pass" % name)
        return "skipped"

    ad_spans, _reason = detect_ads(
        cues, dur, title=name, models=models, meter_file=meter_file, log=log
    )
    if not ad_spans:
        return "none"

    if want_cut:
        status = cut_ads(
            media_path, ad_spans, dur, existing_rows=existing_rows, tmp_dir=tmp_dir,
            run=run, duration_probe=duration_probe, chapter_probe=chapter_probe,
        )
        if status == 0:
            removed = sum(span.duration_ms for span in ad_spans)
            log("    %s: cut %d ad stretch(es) (%s removed)"
                % (name, len(ad_spans), aichapters.time_label(removed)))
            return "cut"
        log("    %s: could not cut ad stretches - left unchanged" % name)
        return "failed"

    merged = merge_chapters(existing_rows, ad_spans, dur)
    own_tmp = not tmp_dir
    work_dir = tmp_dir or tempfile.mkdtemp(prefix="adchapters.")
    try:
        status = chapters.write_chapters(
            media_path, merged, work_dir, run=run, probe=chapter_probe
        )
    finally:
        if own_tmp:
            shutil.rmtree(work_dir, ignore_errors=True)
    if status == 0:
        log("    %s: marked %d ad stretch(es) as chapters (%s)"
            % (name, len(ad_spans), _format_spans(ad_spans)))
        return "marked"
    log("    %s: could not write ad-read chapters - left unchanged" % name)
    return "failed"
