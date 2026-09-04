"""The chapter helpers: what a concatenated file says about its parts.

A list of concatenated audio files becomes chapter marks, and the marks get
written into the finished file - and, on the transcode side, a source's own
chapters are read back out and re-attached to what was encoded from it.

  chapters_from_files    the concat list -> OGM CHAPTERnn= rows
  embed_chapters         the rows into the output file(s)
  extract_chapters       a source's chapters -> an OGM file
  attach_chapters       re-attach them after an encode

Chapter marks are carried as OGM-style "CHAPTERnn=" / "CHAPTERnnNAME=" rows,
which is what every target format here accepts: mutagen writes them into Opus
and FLAC directly, while an mp3 or m4b takes the detour through a Matroska
because ffmpeg cannot write chapters into MP4 in place.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence

from medialib.lib import mutagentags
from medialib.lib.census import three_decimals
from medialib.lib.cleannamescollectively import clean_names_collectively
from medialib.lib.cleannamesindividually import clean_names_individually

__all__ = [
    "chapters_from_files",
    "embed_chapters",
    "extract_chapters",
    "attach_chapters",
]

Runner = Callable[..., "subprocess.CompletedProcess"]

# The units the chapter rows are built from, the way the shell names them.
_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE

# The leading numeric slice glibc's strtod takes out of a duration string:
# bash's printf '%.3f' parses exactly this prefix, whatever follows it. The
# hex-float spelling strtod would also take (0x1p3) is not in this class -
# ffprobe never prints one, and the port declines to parse it rather than
# carry a second strtod, the way the census declines the 2^63 spellings.
_STRTOD_PREFIX = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")

# The leading word strtod takes out for the infinities and the not-a-number,
# case-blind and with the sign already off: the words it accepts, longest
# first so "infinity" is not answered as "inf" with a tail left behind.
_STRTOD_WORDS = ("nan", "infinity", "inf")

# The whitespace strtod skips before the number: the C isspace set.
_STRTOD_WS = " \t\n\v\f\r"

# The file the error line names. The shell's printf named the script and the
# line it sat on; this names the file that does the formatting now, and no
# line, because there is one place here rather than one per call site.
_SOURCE = os.path.abspath(__file__)

# The leading numeric slice awk's `p[3] + 0` takes out of a chapter index.
_INT_PREFIX = re.compile(r"[+-]?\d+")


def _run(argv: Sequence[str], quiet: bool = False) -> subprocess.CompletedProcess:
    """The real runner: the tool on PATH.

    A call the shell version sends to /dev/null does the same here (quiet);
    the rest inherits the streams, the way the unredirected shell calls do.
    """
    if quiet:
        return subprocess.run(list(argv), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    return subprocess.run(list(argv))


def _log(message: str) -> None:
    """log: the one line the module prints, to stderr, the way the shell's log does."""
    if os.environ.get("LOG_TIMESTAMPS"):
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
    else:
        sys.stderr.write(f"==> {message}\n")


def _remove_quiet(path: str) -> None:
    """rm -rf on a file: gone or never there, and both are a success."""
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_status(path: str) -> int:
    """The same removal where the status is the answer: gone, or never there, is
    the success it is for rm too, and only something in the way is the failure.
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        return 0
    except OSError:
        return 1
    return 0


def _stem_of(name: str) -> str:
    """${name%.*}: the shortest dot-led suffix stripped, or the whole name."""
    dot = name.rfind(".")
    return name[:dot] if dot >= 0 else name


def _ram_temp() -> str:
    """mktemp on the RAM filesystem when the host has one, with the fall-back
    the shell takes when it does not. The shell's mktemp names its result
    tmp.<random> in whichever directory it used, and the recorded calls
    rewrite that name, so the port's temp is named the same way."""
    try:
        descriptor, path = tempfile.mkstemp(prefix="tmp.", dir="/dev/shm")
    except OSError:
        descriptor, path = tempfile.mkstemp(prefix="tmp.")
    os.close(descriptor)
    return path


def _c_div(a: int, b: int) -> int:
    """Shell arithmetic division: truncation toward zero, not Python's floor."""
    quotient = a // b
    if a % b and (a < 0) != (b < 0):
        quotient += 1
    return quotient


def _c_rem(a: int, b: int) -> int:
    """Shell arithmetic remainder: the C sign (the dividend's), not Python's."""
    remainder = a % b
    if remainder and (a < 0) != (b < 0):
        remainder -= b
    return remainder


def _base_of(path: str) -> str:
    """${path##*/}: the longest slash-led prefix stripped."""
    return path.rsplit("/", 1)[-1]


def _entry_path(entry: str) -> str:
    """The path a concat line carries: every "file " and every quote gone,
    the way the shell strips them - all occurrences of both, not just the
    first, so a name that holds the words still lands where the shell lands."""
    return entry.replace("file ", "").replace("'", "")


def _printf_f3(text: str) -> str:
    """bash's printf '%.3f' on a probe's duration string.

    glibc's strtod skips the leading whitespace and takes the longest prefix
    it can parse - a number, or the inf and nan words with their sign - and
    bash reports whatever is left behind: "printf: <arg>: invalid number" on
    stderr, the argument verbatim, and the formatted prefix all the same (zero
    the way it formats nothing when the prefix consumed nothing). The value it
    rounds is the nearest 80-bit float the prefix parses to, not the nearest
    double, so the rounding is the census' rule. The error line names the file
    it came from, the way the shell's did. A word that survives to the base-ten
    re-read below is the shell's 10# arithmetic error that kills its run, and
    the int() of a word is the port's.
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
    """A probe's duration to integer milliseconds: the shell idiom of
    printf '%.3f', the dot stripped, and the whole thing re-read in base
    ten - 10# of which also drops the leading zeros the format leaves."""
    value = _printf_f3(text if text else "0")
    return int(value.replace(".", ""))


def _time_stamp(ms: int) -> str:
    """Flat milliseconds to the OGM stamp HH:MM:SS.mmm.

    The division and remainder chains are the shell's - truncation toward
    zero, not floor - so a length the arithmetic takes negative still
    lands where the shell's printf %02d would print it.
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
    """The collective pass, with the shell's single-name guard: with one name
    there is nothing to compare it against, and the whole of it both leads
    and trails the set, so the pass would leave the chapter unnamed."""
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
    """The duration a probe of this path answers: the ffprobe the shell runs,
    quiet, one field - and its output as the command substitution saw it,
    the trailing newlines gone, and nothing at all when the probe fails, the
    way the shell's || true leaves the substitution empty."""
    try:
        proc = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries",
                               "format=duration", "-of", "default=nk=1:nw=1",
                               path], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    return proc.stdout.decode("utf-8", "replace").rstrip("\n")


def chapters_from_files(concat_list: Sequence[str],
                        probe: Callable[[str], str] = _probe_duration) -> list[str]:
    """The concat list's OGM rows, in the list's own order.

    ``probe`` takes each entry's full path - the path, not the base name,
    the way the shell hands the file to the probe - and answers the probe's
    output as the command substitution saw it, trailing newlines gone.
    """
    lines: list[str] = []
    names: list[str] = []
    temp_prefixes: list[str] = []

    # First pass: the names and prefixes the individual pass splits out.
    for entry in concat_list:
        stem = _stem_of(_base_of(_entry_path(entry)))
        prefix, name = clean_names_individually(stem)
        temp_prefixes.append(prefix)
        names.append(name)

    clean_names, prefixes = _clean_collectively(names, temp_prefixes)

    # Second pass: one chapter per entry - a chapter's name can depend on the
    # other chapters, so the rows cannot be written while the names are made.
    accumulated = 0
    for index, entry in enumerate(concat_list):
        clean_name = clean_names[index]
        prefix = prefixes[index]

        # The number a name still carries, blocked by the common text that is
        # now gone: removed by a second individual pass, but only when
        # something remains of the name.
        if not prefix:
            _, again = clean_names_individually(clean_name)
            if again:
                clean_name = again

        chapter_name = _chapter_name(prefix, clean_name)
        number = index + 1
        lines.append(f"CHAPTER{number:02d}={_time_stamp(accumulated)}")
        lines.append(f"CHAPTER{number:02d}NAME={chapter_name}")

        raw = probe(_entry_path(entry))
        accumulated += _duration_ms(raw.rstrip("\n"))
    return lines


def embed_chapters(chapter_file: str, chapter_lines: Sequence[str],
                   ram_dir: str, script_dir: str, have_mkvtoolnix: bool,
                   run: Runner = _run) -> int:
    """Write the rows into whichever of the stem's output files exists."""
    stem = _stem_of(chapter_file)
    opus_file = f"{stem}.opus"
    mp3_file = f"{stem}.mp3"
    m4b_file = f"{stem}.m4b"
    flac_file = f"{stem}.flac"
    # The intermediary mka detour, kept in RAM rather than on the SSD.
    mka_file = f"{ram_dir}/{os.path.basename(stem)}.mka"
    title = _stem_of(os.path.basename(chapter_file))

    # One of the four should exist; every step below acts on that single
    # file, and the opus-before-flac order is the one the shell checks.
    audio_file = ""
    for candidate in (opus_file, mp3_file, m4b_file, flac_file):
        if os.path.isfile(candidate):
            audio_file = candidate
            break

    # FLAC and Opus keep chapters as Vorbis comments, so the already-OGM rows
    # go straight in with mutagen - no Matroska detour. --force: the file was
    # assembled here, so this list is its truth.
    if audio_file in (flac_file, opus_file):
        chapter_temp = ""
        if len(chapter_lines) >= 4:
            chapter_temp = _ram_temp()
            with open(chapter_temp, "w", encoding="utf-8") as handle:
                handle.write("\n".join(chapter_lines) + "\n")
        mutagentags.embed_chapters(audio_file, chapter_temp or "/dev/null",
                                   title, force=True)
        if chapter_temp:
            _remove_quiet(chapter_temp)
        return 0

    # The mp3 and m4b path cannot run without mkvtoolnix: neither format
    # carries Vorbis-comment chapters. Unset means absent, and outside a run
    # the safe answer is to skip, not die.
    if not have_mkvtoolnix:
        _log("    chapters and title not embedded: mkvtoolnix is not installed "
             "(MP3 and m4b need it)")
        return 0

    # The detour over mka, but only if there is more than one chapter:
    # mkvmerge needs a seekable chapters file, so the list is serialized to
    # a RAM-backed temporary (removed right after the call).
    if len(chapter_lines) >= 4:
        chapter_temp = _ram_temp()
        with open(chapter_temp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(chapter_lines) + "\n")
        if audio_file:
            run(["mkvmerge", "--quiet", audio_file, "--chapters",
                 chapter_temp, "-o", mka_file])
        _remove_quiet(chapter_temp)
    elif audio_file:
        run(["mkvmerge", "--quiet", audio_file, "-o", mka_file])

    # Set the title.
    run(["mkvpropedit", "--quiet", mka_file, "--edit", "info",
         "--set", f"title={title}", "--edit", "track:1",
         "--set", f"name={title}"])

    # Re-extract the audio from the mka, now with chapters: the m4b is
    # overwritten in place, so it is cleared before the re-mux.
    if audio_file:
        if audio_file == m4b_file:
            _remove_quiet(m4b_file)
        run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", mka_file, "-codec", "copy", audio_file])
    return _remove_status(mka_file)


def _unquote(value: str) -> str:
    """The awk's two subs: one leading quote and one trailing quote off."""
    if value.startswith('"'):
        value = value[1:]
    if value.endswith('"'):
        value = value[:-1]
    return value


def _flat_time(value: str) -> str:
    """The awk's fmt: a seconds string to HH:MM:SS.mmm through the C
    library's float, the way awk's sprintf %f parses it - a value it cannot
    parse is zero, the way awk's non-numeric is."""
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    hours = int(seconds / 3600)
    seconds -= hours * 3600
    minutes = int(seconds / 60)
    seconds -= minutes * 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def _chapters_from_flat(text: str) -> list[str]:
    """The flat -show_chapters stream to OGM rows: the shell's awk, line
    for line - the dotted chapter keys, the quotes off the values, the
    rows in index order from the lowest index to the highest that carries
    a start time."""
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
    lines: list[str] = []
    for index in range(max_index + 1):
        if index not in start:
            continue
        number = f"{index + 1:02d}"
        lines.append(f"CHAPTER{number}={_flat_time(start[index])}")
        lines.append(f"CHAPTER{number}NAME={title.get(index, 'Chapter ' + number)}")
    return lines


def _flat_chapters(src: str) -> str:
    """The source's flat chapter stream: the ffprobe the shell runs, stderr
    to /dev/null. A source the probe cannot read answers nothing, the way
    the pipeline's left side does."""
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

    Non-zero - leaving <out> empty, the way the shell's redirect leaves it
    - when the source has no chapters, so the caller can skip the embed.
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
    """
    chapters = f"{tmp_dir}/chapters.ogm"
    if extract_chapters(src, chapters, probe) == 0:
        mutagentags.embed_chapters(target, chapters, force=True)
    return 0