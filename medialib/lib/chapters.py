"""The chapter helpers: what a concatenated file says about its parts.

A list of concatenated audio files becomes chapter marks, and the marks get
written into the finished file - and, on the transcode side, a source's own
chapters are read back out and re-attached to what was encoded from it.

  chapters_from_files    the concat list -> OGM CHAPTERnn= rows, a source's
                         own chapters kept and shifted to where it starts
  embed_chapters         the rows into the output file(s)
  extract_chapters       a source's chapters -> an OGM file
  attach_chapters       re-attach them after an encode

Chapter marks are carried as OGM-style "CHAPTERnn=" / "CHAPTERnnNAME=" rows,
and one writer, write_chapters, puts them into every format: mutagen writes them
into Opus and FLAC directly, mkvpropedit into Matroska, and anything else - an
m4b's chapter track, an mp3's ID3 chapter frames - is an ffmpeg stream copy
from an ffmetadata file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence

from medialib.lib import mutagentags, runlog, toolcapture
from medialib.lib.census import three_decimals
from medialib.lib.cleannamescollectively import clean_names_collectively
from medialib.lib.cleannamesindividually import clean_names_individually

__all__ = [
    "chapters_from_files",
    "embed_chapters",
    "extract_chapters",
    "attach_chapters",
    "attach_mp4_chapters",
    "carries_chapters",
    "CANNOT_HOLD",
    "SIDECAR_SUFFIX",
    "read_sidecar",
    "MATROSKA_SUFFIXES",
    "chapter_count",
    "duration_ms",
    "write_chapters",
]

Runner = Callable[..., "toolcapture.ToolRun"]

# The units the chapter rows are built from.
_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE

# The leading numeric slice glibc's strtod takes out of a duration string.
# The hex-float spelling strtod would also take (0x1p3) is not in this class -
# ffprobe never prints one, and it is declined rather than carry a second
# strtod, the way the census declines the 2^63 spellings.
_STRTOD_PREFIX = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")

# The leading word strtod takes out for the infinities and the not-a-number,
# case-blind and with the sign already off: the words it accepts, longest
# first so "infinity" is not answered as "inf" with a tail left behind.
_STRTOD_WORDS = ("nan", "infinity", "inf")

# The whitespace strtod skips before the number: the C isspace set.
_STRTOD_WS = " \t\n\v\f\r"

# The file the error line names: the one that does the formatting, and no
# line, because there is one place here rather than one per call site.
_SOURCE = os.path.abspath(__file__)

# The leading numeric slice of a chapter index.
_INT_PREFIX = re.compile(r"[+-]?\d+")


def _run(argv: Sequence[str], may_fail: bool = False) -> toolcapture.ToolRun:
    """The real runner: the tool on PATH, its output kept rather than shown.

    A failure is a warning with the tool's own words under it: chapters are
    metadata, and a step that did not complete leaves the book as it was.
    ``may_fail`` is for a call whose failure is an answer rather than a
    problem, and says nothing.
    """
    return toolcapture.run(
        list(argv), report=not may_fail,
        warning="chapters or title not written, so the book keeps what it had")


_log = runlog.log


def _remove_quiet(path: str) -> None:
    """A quiet removal: gone or never there, and both are a success."""
    try:
        os.remove(path)
    except OSError:
        pass


def _stem_of(name: str) -> str:
    """The shortest dot-led suffix stripped, or the whole name."""
    dot = name.rfind(".")
    return name[:dot] if dot >= 0 else name


def _c_div(a: int, b: int) -> int:
    """Division with truncation toward zero, not Python's floor."""
    quotient = a // b
    if a % b and (a < 0) != (b < 0):
        quotient += 1
    return quotient


def _c_rem(a: int, b: int) -> int:
    """Remainder with the C sign (the dividend's), not Python's."""
    remainder = a % b
    if remainder and (a < 0) != (b < 0):
        remainder -= b
    return remainder


def _base_of(path: str) -> str:
    """The longest slash-led prefix stripped."""
    return path.rsplit("/", 1)[-1]


def _entry_path(entry: str) -> str:
    """The path a concat line carries: every "file " gone - all occurrences of
    the word, not just the first, so a name that holds the word still lands on
    the right file - and the pair of quotes AROUND the path gone. Only that
    pair: an apostrophe inside a name is part of the file, and the probe it
    feeds must reach the file it names, which is what keeps that file's chapter
    from collapsing to zero length."""
    body = entry.replace("file ", "")
    if body.startswith("'"):
        body = body[1:]
    if body.endswith("'"):
        body = body[:-1]
    return body


def _printf_f3(text: str) -> str:
    """A probe's duration string to three decimals.

    glibc's strtod skips the leading whitespace and takes the longest prefix
    it can parse - a number, or the inf and nan words with their sign - and
    whatever is left behind is reported: "printf: <arg>: invalid number" on
    stderr, the argument verbatim, and the formatted prefix all the same (zero
    the way it formats nothing when the prefix consumed nothing). The value it
    rounds is the nearest 80-bit float the prefix parses to, not the nearest
    double, so the rounding is the census' rule. The error line names the file
    it came from. A word that survives to the base-ten re-read below is the
    int() of a word, and that is where the run dies.
    """
    body = text.lstrip(_STRTOD_WS)
    match = _STRTOD_PREFIX.match(body)
    if match is not None:
        prefix = match.group(0)
        if body[len(prefix):]:
            _invalid_number(text)
        return three_decimals(prefix)
    rest = body
    if rest[:1] in ("+", "-"):
        rest = rest[1:]
    lowered = rest.lower()
    for word in _STRTOD_WORDS:
        if lowered.startswith(word):
            if rest[len(word):]:
                _invalid_number(text)
            return ("-" if body.startswith("-") else "") + word
    if body:
        _invalid_number(text)
    return "0.000"


def _invalid_number(text: str) -> None:
    """The error line for a value the formatting could not take: the file it
    was refused in, then the argument verbatim."""
    sys.stderr.write(f"{_SOURCE}: printf: {text}: invalid number\n")


def _duration_ms(text: str) -> int:
    """A probe's duration to integer milliseconds: three decimals, the dot
    stripped, and the whole thing re-read in base ten, which also drops the
    leading zeros the format leaves."""
    value = _printf_f3(text if text else "0")
    return int(value.replace(".", ""))


def _time_stamp(ms: int) -> str:
    """Flat milliseconds to the OGM stamp HH:MM:SS.mmm.

    The division and remainder chains truncate toward zero, not floor, so a
    length the arithmetic takes negative still lands where the zero-padded
    format puts it.
    """
    hours = _c_div(ms, _MS_PER_HOUR)
    minutes = _c_div(_c_rem(ms, _MS_PER_HOUR), _MS_PER_MINUTE)
    seconds = _c_div(_c_rem(_c_rem(ms, _MS_PER_HOUR), _MS_PER_MINUTE),
                     _MS_PER_SECOND)
    milliseconds = _c_rem(_c_rem(_c_rem(ms, _MS_PER_HOUR), _MS_PER_MINUTE),
                          _MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _clean_collectively(names: list[str],
                        prefixes: list[str]) -> tuple[list[str], list[str]]:
    """The collective pass, with the single-name guard: with one name there is
    nothing to compare it against, and the whole of it both leads and trails
    the set, so the pass would leave the chapter unnamed."""
    if len(names) > 1:
        return (clean_names_collectively(names),
                clean_names_collectively(prefixes))
    return list(names), list(prefixes)


def _chapter_name(prefix: str, clean_name: str) -> str:
    """The title a chapter gets: the name, the date kept in front of it, or
    the number when nothing else survived."""
    if not clean_name:
        return prefix
    if len(prefix) >= 8:
        return f"{prefix} {clean_name}"
    return clean_name


def _probe_duration(path: str) -> str:
    """The duration a probe of this path answers: ffprobe, quiet, one field -
    and its output with the trailing newlines gone, and nothing at all when
    the probe fails."""
    try:
        proc = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries",
                               "format=duration", "-of", "default=nk=1:nw=1",
                               path], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    return proc.stdout.decode("utf-8", "replace").rstrip("\n")


def _clean_titles(raw: Sequence[str]) -> list[str]:
    """The names a set of chapters gets, each decided by looking at the
    others: every raw name cleaned on its own, then the set cleaned together,
    then a name the common text was blocking cleaned on its own once more."""
    names: list[str] = []
    temp_prefixes: list[str] = []

    # First pass: the names and prefixes the individual pass splits out.
    for text in raw:
        prefix, name = clean_names_individually(text)
        temp_prefixes.append(prefix)
        names.append(name)

    clean_names, prefixes = _clean_collectively(names, temp_prefixes)

    # Second pass: one title per name - a name can depend on the others, so
    # none is final until the set has been looked at.
    titles: list[str] = []
    for clean_name, prefix in zip(clean_names, prefixes, strict=True):
        # The number a name still carries, blocked by the common text that is
        # now gone: removed by a second individual pass, but only when
        # something remains of the name.
        if not prefix:
            _, again = clean_names_individually(clean_name)
            if again:
                clean_name = again
        titles.append(_chapter_name(prefix, clean_name))
    return titles


def _clean_alone(text: str) -> str:
    """One name cleaned on its own, with nothing to compare it against."""
    return _chapter_name(*clean_names_individually(text))


def chapters_from_files(concat_list: Sequence[str],
                        probe: Callable[[str], str] = _probe_duration,
                        chapter_probe: Callable[[str], str] | None = None,
                        ) -> list[str]:
    """The concat list's OGM rows, in the list's own order.

    ``probe`` takes each entry's full path - the path, not the base name -
    and answers the probe's output with the trailing newlines gone.

    A file that carries two or more chapters of its own keeps them, each moved
    on by the length of every file before it, and their titles cleaned the way
    the file names are - but among that file's titles only. When its first
    chapter starts later than the file does, the stretch before it gets a
    chapter of its own, named after the file cleaned on its own - unless the
    file is the whole book, which then starts where its own chapters say. A
    file with one chapter or none gets one chapter named after the file, the
    file names cleaned among each other. ``chapter_probe`` answers a path's flat
    -show_chapters stream, ffprobe's unless a caller hands another.
    """
    if chapter_probe is None:
        chapter_probe = _flat_chapters
    paths = [_entry_path(entry) for entry in concat_list]
    stems = [_stem_of(_base_of(path)) for path in paths]
    file_titles = _clean_titles(stems)

    lines: list[str] = []
    accumulated = 0
    number = 0
    for path, stem, file_title in zip(paths, stems, file_titles, strict=True):
        duration = _duration_ms(probe(path).rstrip("\n"))
        inner = _inner_chapters(chapter_probe(path), duration)
        if inner:
            offsets = [offset for offset, _ in inner]
            titles = [clean or raw for (_, raw), clean in zip(
                inner, _clean_titles([raw for _, raw in inner]), strict=True)]
            if offsets[0] > 0 and len(paths) > 1:
                offsets.insert(0, 0)
                titles.insert(0, _clean_alone(stem) or file_title)
            marks = list(zip(offsets, titles, strict=True))
        else:
            marks = [(0, file_title)]
        for offset, title in marks:
            number += 1
            lines.append(f"CHAPTER{number:02d}="
                         f"{_time_stamp(accumulated + offset)}")
            lines.append(f"CHAPTER{number:02d}NAME={title}")
        accumulated += duration
    return lines


# A file's first chapter starting this close to the file's own start marks the
# start: a mark a few frames in is the muxer's rounding, not a stretch of the
# file before the first chapter.
_START_SLACK_MS = _MS_PER_SECOND


def _inner_chapters(flat: str, duration: int) -> list[tuple[int, str]]:
    """A file's own chapters as (offset ms, raw title), or nothing when it has
    fewer than two - one chapter is the file itself, and the file's name says
    that as well. A start at or past the file's end, or not past the one
    before it, is dropped: the join has nothing there for it to mark."""
    marks: list[tuple[int, str]] = []
    for _, start, title in _flat_entries(flat):
        try:
            offset = max(0, round(float(start) * _MS_PER_SECOND))
        except ValueError:
            offset = 0
        if not marks and offset < _START_SLACK_MS:
            offset = 0
        if marks and offset <= marks[-1][0] or (duration
                                                and offset >= duration):
            continue
        marks.append((offset, title))
    return marks if len(marks) >= 2 else []


# The characters ffmetadata reads as syntax, which a title carrying one of them
# has to hand over escaped: the comment mark, the two that end a line's key and
# its value, and the escape itself.
_FFMETADATA_SYNTAX = "=;#\\\n"


def _ffmetadata_escape(text: str) -> str:
    return "".join("\\" + ch if ch in _FFMETADATA_SYNTAX else ch
                   for ch in text)


def _ogm_milliseconds(stamp: str) -> int:
    """HH:MM:SS.mmm back to milliseconds. A row this cannot read is 0, the way
    the rest of this module reads an unparseable duration."""
    hours, _, rest = stamp.partition(":")
    minutes, _, seconds = rest.partition(":")
    try:
        return (int(hours) * _MS_PER_HOUR + int(minutes) * _MS_PER_MINUTE
                + int(round(float(seconds) * _MS_PER_SECOND)))
    except ValueError:
        return 0


def ffmetadata_chapters(chapter_lines: Sequence[str], title: str,
                        end_ms: int) -> str:
    """The OGM rows as an ffmetadata document, plus the file's own title.

    Every chapter needs an END, which ffmpeg refuses to infer: each one ends
    where the next begins, and the last one at ``end_ms`` - the finished file's
    own duration, since nothing in the rows themselves says where it stops.
    """
    starts: list[int] = []
    names: list[str] = []
    for line in chapter_lines:
        key, _, value = line.partition("=")
        if key.endswith("NAME"):
            names.append(value)
        elif key.startswith("CHAPTER"):
            starts.append(_ogm_milliseconds(value))

    out = [";FFMETADATA1", "title=" + _ffmetadata_escape(title)]
    for index, start in enumerate(starts):
        stop = starts[index + 1] if index + 1 < len(starts) else end_ms
        # A last chapter the duration does not reach past would be zero-length
        # and dropped; one millisecond is under any player's resolution and
        # keeps it in the list.
        if stop <= start:
            stop = start + 1
        name = names[index] if index < len(names) else "Chapter %02d" % (
            index + 1)
        out += ["[CHAPTER]", "TIMEBASE=1/%d" % _MS_PER_SECOND,
                "START=%d" % start, "END=%d" % stop,
                "title=" + _ffmetadata_escape(name)]
    return "\n".join(out) + "\n"


def _unquote(value: str) -> str:
    """One leading quote and one trailing quote off."""
    if value.startswith('"'):
        value = value[1:]
    if value.endswith('"'):
        value = value[:-1]
    return value


def _flat_time(value: str) -> str:
    """A seconds string to HH:MM:SS.mmm; a value it cannot parse is zero."""
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    hours = int(seconds / 3600)
    seconds -= hours * 3600
    minutes = int(seconds / 60)
    seconds -= minutes * 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def _flat_entries(text: str) -> list[tuple[int, str, str]]:
    """The flat -show_chapters stream as (index, start, title) - the dotted
    chapter keys, the quotes off the values, in index order from the lowest
    index to the highest that carries a start time. An untitled chapter is
    "Chapter nn", by its index."""
    start: dict[int, str] = {}
    title: dict[int, str] = {}
    max_index = -1
    for line in text.split("\n"):
        cut = line.find("=")
        if cut < 0:
            continue
        parts = line[:cut].split(".")
        value = _unquote(line[cut + 1:])
        if len(parts) < 4 or parts[1] != "chapter":
            continue
        digits = _INT_PREFIX.match(parts[2])
        index = int(digits.group(0)) if digits else 0
        if parts[3] == "start_time":
            start[index] = value
            if index > max_index:
                max_index = index
        elif parts[3] == "tags" and len(parts) > 4 and parts[4] == "title":
            title[index] = value
    return [(index, start[index],
             title.get(index, f"Chapter {index + 1:02d}"))
            for index in range(max_index + 1) if index in start]


def _chapters_from_flat(text: str) -> list[str]:
    """The flat -show_chapters stream to OGM rows, numbered by the source's
    own chapter index."""
    lines: list[str] = []
    for index, start, title in _flat_entries(text):
        number = f"{index + 1:02d}"
        lines.append(f"CHAPTER{number}={_flat_time(start)}")
        lines.append(f"CHAPTER{number}NAME={title}")
    return lines


def _flat_chapters(src: str) -> str:
    """The source's flat chapter stream: ffprobe, stderr to /dev/null. A
    source the probe cannot read answers nothing."""
    try:
        proc = subprocess.run(["ffprobe", "-v", "quiet", "-show_chapters",
                               "-of", "flat", src],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return proc.stdout.decode("utf-8", "replace")
    except OSError:
        return ""


def extract_chapters(src: str, out: str,
                     probe: Callable[[str], str] = _flat_chapters) -> int:
    """The source's chapters (if any) into <out> as OGM rows.

    Non-zero - leaving <out> empty - when the source has no chapters, so the
    caller can skip the embed.
    """
    lines = _chapters_from_flat(probe(src))
    with open(out, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
    return 0 if lines else 1


def attach_chapters(src: str, target: str, tmp_dir: str, script_dir: str,
                    run: Runner = _run,
                    probe: Callable[[str], str] = _flat_chapters) -> int:
    """Re-attach the source's chapters (if any) to a produced file.

    libopus does not carry chapters through, so this runs both for a fresh
    single-file encode and for a re-concatenated set of chunks - the
    source's chapters win, written straight in with mutagen.

    An MP4 target takes the other route below: it has nowhere to keep a
    Vorbis-comment row, so there is nothing for mutagen to write.
    """
    if _is_mp4(target):
        return attach_mp4_chapters(src, target, tmp_dir, run)
    chapters = f"{tmp_dir}/chapters.ogm"
    if extract_chapters(src, chapters, probe) == 0:
        mutagentags.embed_chapters(target, chapters, force=True)
    return 0


def _is_mp4(path: str) -> bool:
    """Whether this target's chapters are a TRACK rather than a tag.

    The list is `mutagentags.MP4_SUFFIXES`, the same one the cover half reads:
    the two halves of that split have to agree about which files they mean, or
    a container would take its cover through mutagen and its chapters nowhere.
    """
    return path.lower().endswith(mutagentags.MP4_SUFFIXES)


def attach_mp4_chapters(src: str, target: str, tmp_dir: str,
                        run: Runner = _run) -> int:
    """The source's chapters into an MP4 target, copied across by ffmpeg rather
    than written as a tag.

    MP4 keeps chapters as a chapter TRACK, which mutagen cannot write and which
    ffmpeg's `-map_chapters` copies from one input to another in a single pass.
    So the source goes in as a second input, purely to be read for its chapters,
    while the target's own audio is stream-copied - no encoder in the path, so
    the xhe-aac the external encoder just produced is carried over sample for
    sample.

    In place is not possible: ffmpeg cannot read and write one file at once, so
    the pass writes into <tmp_dir> and the result is moved over the target. A
    failed or empty pass leaves the target exactly as it was - chapters missing
    rather than the audio lost, which is the right way round for a step that is
    only metadata.
    """
    staged = os.path.join(tmp_dir, "chapters" + os.path.splitext(target)[1])
    _remove_quiet(staged)
    try:
        run(["ffmpeg", "-nostdin", "-hide_banner",
             *toolcapture.ffmpeg_loglevel(), "-y",
             "-i", target, "-i", src, "-map", "0:a:0", "-map_chapters", "1",
             "-c", "copy", staged])
    except OSError:
        _remove_quiet(staged)
        return 1
    if not os.path.isfile(staged) or os.path.getsize(staged) <= 0:
        _remove_quiet(staged)
        return 1
    return _move_over(staged, target)


def _move_over(src: str, dst: str) -> int:
    """<src> onto <dst>, across filesystems: the staging file lives in RAM and
    the target does not, so a rename alone would fail with EXDEV."""
    try:
        shutil.move(src, dst)
        return 0
    except OSError:
        _remove_quiet(src)
        return 1

def duration_ms(path: str) -> int:
    """How long a file plays, in milliseconds, by ffprobe's reading of it -
    0 when it cannot be read."""
    try:
        return _duration_ms(_probe_duration(path))
    except ValueError:
        return 0


def chapter_count(path: str,
                  probe: Callable[[str], str] = _flat_chapters) -> int:
    """How many chapters a file carries, by ffprobe's reading of it."""
    return len(_flat_entries(probe(path)))


# The containers that keep their chapters where a tool can rewrite them without
# copying the file: Vorbis comments, written by mutagen, and Matroska's chapter
# element, written by mkvpropedit.
_VORBIS_SUFFIXES = (".opus", ".flac", ".ogg", ".oga")
MATROSKA_SUFFIXES = (".mkv", ".mka", ".mk3d", ".webm")

# How far a chapter start read back may sit from the row it was written from:
# a container keeps its own timebase, and the row is in milliseconds.
_START_TOLERANCE_MS = 50

# How much shorter or longer a stream copy may come out before it is not put
# over the file it was copied from.
_COPY_DURATION_TOLERANCE_MS = 1000

# What a chapter file beside a recording is called: generate-chapters writes
# them, and convert-audio writes one into the output of the recording beside it.
SIDECAR_SUFFIX = ".chapters.txt"

# What write_chapters answers for a file whose container has no place for
# chapters - raw AAC, WAV - which it leaves as it was.
CANNOT_HOLD = 2


def _row_chapters(chapter_lines: Sequence[str]) -> list[tuple[int, str]]:
    """The OGM rows as (start in milliseconds, title) pairs."""
    starts: list[int] = []
    names: list[str] = []
    for line in chapter_lines:
        key, _, value = line.partition("=")
        if key.endswith("NAME"):
            names.append(value)
        elif key.startswith("CHAPTER"):
            starts.append(_ogm_milliseconds(value))
    return [(start, names[index] if index < len(names) else "")
            for index, start in enumerate(starts)]


def carries_chapters(path: str, chapter_lines: Sequence[str],
                     probe: Callable[[str], str] = _flat_chapters) -> bool:
    """Whether a file already holds these chapters: as many, with the same
    titles, each starting where its row says."""
    wanted = _row_chapters(chapter_lines)
    held = _flat_entries(probe(path))
    if len(held) != len(wanted):
        return False
    for (start, name), (_index, held_start, held_name) in zip(wanted, held,
                                                              strict=True):
        try:
            held_ms = float(held_start) * _MS_PER_SECOND
        except ValueError:
            return False
        if held_name != name or abs(held_ms - start) > _START_TOLERANCE_MS:
            return False
    return True


def write_chapters(target: str, chapter_lines: Sequence[str], tmp_dir: str,
                   run: Runner = _run,
                   probe: Callable[[str], str] = _flat_chapters,
                   book_title: str | None = None) -> int:
    """The rows written into a finished file IN PLACE, replacing whatever
    chapters it had and keeping every other tag.

    A Vorbis-comment file takes them with mutagen, and a Matroska file with
    mkvpropedit, neither copying the file. Anything else - an MP4's chapter
    track, an MP3's ID3 chapter frames - is a stream copy by ffmpeg into
    ``tmp_dir``, moved over the target only once it reads back with every
    chapter and as long as the target. A container that cannot hold chapters
    is left as it was, and answered with :data:`CANNOT_HOLD` rather than 1.

    ``book_title`` is for a book this program has just joined: the title is
    set to it, and a stream copy keeps only the audio and that title rather
    than whatever the join carried over, so a cover stream cannot displace the
    chapter track and the cover added later is the only one.
    """
    rows = os.path.join(tmp_dir, "generated.chapters.txt")
    with open(rows, "w", encoding="utf-8") as handle:
        handle.write("\n".join(chapter_lines) + "\n")
    lower = target.lower()
    if lower.endswith(_VORBIS_SUFFIXES):
        status = mutagentags.embed_chapters(target, rows, book_title or "",
                                            force=True)
        _remove_quiet(rows)
        return status
    if lower.endswith(MATROSKA_SUFFIXES):
        title = ["--edit", "info", "--set", f"title={book_title}"] \
            if book_title is not None else []
        run(["mkvpropedit", "--quiet", target, "--chapters", rows, *title])
        _remove_quiet(rows)
        return 0 if carries_chapters(target, chapter_lines, probe) else 1
    _remove_quiet(rows)
    duration = duration_ms(target)
    metadata_file = os.path.join(tmp_dir, "generated.ffmetadata")
    with open(metadata_file, "w", encoding="utf-8") as handle:
        handle.write(ffmetadata_chapters(chapter_lines, book_title or "",
                                         duration))
    staged = os.path.join(tmp_dir, "generated" + os.path.splitext(target)[1])
    _remove_quiet(staged)
    book = book_title is not None
    result = run(["ffmpeg", "-nostdin", "-hide_banner",
                  *toolcapture.ffmpeg_loglevel(), "-y", "-i", target,
                  "-i", metadata_file, "-map", "0:a" if book else "0",
                  "-map_metadata", "1" if book else "0", "-map_chapters", "1",
                  "-c", "copy", staged])
    _remove_quiet(metadata_file)
    if getattr(result, "returncode", 0) != 0 or not os.path.isfile(staged) \
            or os.path.getsize(staged) <= 0 \
            or abs(duration_ms(staged) - duration) > _COPY_DURATION_TOLERANCE_MS:
        _remove_quiet(staged)
        return 1
    if not carries_chapters(staged, chapter_lines, probe):
        # A whole copy holding no chapters at all is a container without a
        # place for them, as against one that kept some of them wrong.
        cannot = chapter_lines and not _flat_entries(probe(staged))
        _remove_quiet(staged)
        return CANNOT_HOLD if cannot else 1
    return _move_over(staged, target)


def embed_chapters(chapter_file: str, chapter_lines: Sequence[str],
                   ram_dir: str, run: Runner = _run,
                   probe: Callable[[str], str] = _flat_chapters) -> int:
    """The rows written into whichever of the stem's output files exists,
    titled after the stem: :func:`write_chapters` with the book's title.

    A list of fewer than two chapters is no chapters at all, so only the title
    goes in.
    """
    stem = _stem_of(chapter_file)
    title = _stem_of(os.path.basename(chapter_file))
    # One of the four should exist; the opus-before-flac order is the priority.
    for extension in ("opus", "mp3", "m4b", "flac"):
        audio_file = f"{stem}.{extension}"
        if os.path.isfile(audio_file):
            rows = list(chapter_lines) if len(chapter_lines) >= 4 else []
            return write_chapters(audio_file, rows, ram_dir, run, probe,
                                  book_title=title)
    return 0


def read_sidecar(stem: str) -> list[str]:
    """The chapter rows of ``<stem>.chapters.txt``, or none when there is no
    such file or it holds no chapter."""
    try:
        with open(stem + SIDECAR_SUFFIX, encoding="utf-8") as handle:
            rows = [line.strip() for line in handle if line.strip()]
    except (OSError, UnicodeDecodeError):
        return []
    return rows if _row_chapters(rows) else []
