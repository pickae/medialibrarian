"""The job that turns a movie's commentary AUDIO tracks into the subtitle files
worth keeping.

Work out what language each commentary is in, and drain one flat queue of
transcription runs. The shared whisper settlement lives in
:mod:`medialib.lib.whisper`, the languages table and its lookups in
:mod:`medialib.lib.languages`, and the alignment in
:mod:`medialib.lib.subtitlefiles` - sourced once rather than re-implemented.

The bash keeps its caller-provided helpers (``readTrackInfo``,
``isBonusFolder``, ``rename``, ``audioStreamIndex``) and the whisper
settlement as functions and environment globals. Here they are parameters: a
function takes what the caller would hand it, the way the bash says "expected
from the caller".
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
import time

from medialib.lib import languages
from medialib.lib.census import printf_f0
from medialib.lib.enums import shell_lower
from medialib.lib.formatting import awk_number
from medialib.lib.subtitlefiles import sync_subtitle

__all__ = [
    "NAME_MAX_BYTES",
    "COMMENTARY_SUFFIX_BYTES",
    "COMMENTARY_STEM_MAX_BYTES",
    "COMMENTARY_DETECT_SECONDS",
    "COMMENTARY_DETECT_MIN_PROBABILITY",
    "SYNC_SETTLE_SECONDS",
    "detect_commentary_language",
    "commentary_language",
    "transcribe_commentary",
    "export_commentary",
]

# How long a commentary stem ("<movie> <trackIndex> <trackName>") may get before
# the suffixes are appended to it. Every output derived from one stem has to fit
# the file name limit, so the stem is cut to leave room for the longest of them.
NAME_MAX_BYTES = 255
COMMENTARY_SUFFIX_BYTES = 8
COMMENTARY_STEM_MAX_BYTES = NAME_MAX_BYTES - COMMENTARY_SUFFIX_BYTES

# How long a commentary excerpt is, cut from the MIDDLE of the track (commentaries
# open on the film's music or silence, and whisper only looks at the first 30 s),
# and the detection probability under which a guess is no answer at all.
COMMENTARY_DETECT_SECONDS = 120
COMMENTARY_DETECT_MIN_PROBABILITY = 0.5

# The pause the bash took between landing a finished transcript and aligning it.
SYNC_SETTLE_SECONDS = 1

# whisper's "Detected language 'Dutch' with probability 0.987654" line.
_DETECT = re.compile(r"Detected language '([^']+)' with probability ([0-9.]+)")


def _run(args, **kwargs) -> subprocess.CompletedProcess:
    """A tool call with the bash's silence: stderr and (where noted) stdout
    swallowed, stdin from /dev/null, an absent tool the call's own failure."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("stderr", subprocess.DEVNULL)
    return subprocess.run(args, **kwargs)


def detect_commentary_language(mka: str, ram_root: str, whisper: dict,
                               log) -> str:
    """The language whisper hears in a commentary extract, or ``""``.

    whisper-ctranslate2 has no detect-only mode, so a short excerpt - cut from
    the MIDDLE of the track, decoded to the 16 kHz mono wav whisper resamples to
    anyway - is transcribed and everything but the "Detected language 'X' with
    probability Y" line is thrown away. The multilingual model does the
    listening (an English-only one would answer "English" to everything), and a
    detection whisper is not reasonably sure of counts as no answer at all.
    Prints the language NAME whisper reported, which is also a value its
    ``--language`` accepts.
    """
    excerpt = os.path.join(ram_root, "languageProbe.wav")
    dur_raw = ""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", mka],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        dur_raw = proc.stdout.decode("utf-8", "replace").rstrip("\n")
    except OSError:
        dur_raw = ""
    # printf '%.0f' with the bash's "|| dur=0": an unreadable figure is zero.
    try:
        dur = int(printf_f0(dur_raw if dur_raw else "0"))
    except ValueError:
        dur = 0
    start = 0
    if dur > COMMENTARY_DETECT_SECONDS * 2:
        start = (dur - COMMENTARY_DETECT_SECONDS) // 2

    if os.path.exists(excerpt):
        os.remove(excerpt)
    try:
        made = _run(["ffmpeg", "-y", "-loglevel", "error", "-nostats",
                     "-ss", str(start), "-t", str(COMMENTARY_DETECT_SECONDS),
                     "-i", mka, "-vn", "-ac", "1", "-ar", "16000",
                     "-c:a", "pcm_s16le", excerpt])
    except OSError:
        made = None
    # the bash's `|| return 0`: an excerpt that cannot be made (or a missing
    # ffmpeg) is a no-answer, not an error
    if made is None or made.returncode != 0:
        return ""
    try:
        ran = _run(["pipx", "run", "whisper-ctranslate2", excerpt,
                    "--output_dir", ram_root, "--model", whisper["modelMulti"],
                    "--task", "transcribe", "--output_format", "txt",
                    "--vad_filter", "True",
                    "--compute_type", whisper["computeType"],
                    "--device", whisper["device"],
                    "--threads", whisper["threads"]],
                   stdout=subprocess.PIPE)
        # the bash's `out=$(...) || out=""`: a failed probe drops its output too
        out = ran.stdout.decode("utf-8", "replace") if ran.returncode == 0 else ""
    except OSError:
        out = ""
    for path in (excerpt, excerpt[:-4] + ".txt"):
        if os.path.exists(path):
            os.remove(path)

    match = _DETECT.search(out)
    if not match:
        return ""
    # The bash's awk compares with its own numeric coercion, so a probability
    # the parse cannot read is the awk's zero, not an error.
    if awk_number(match.group(2)) < awk_number(str(COMMENTARY_DETECT_MIN_PROBABILITY)):
        return ""
    return match.group(1)


def commentary_language(name: str, tag: str, mka: str, ram_root: str,
                        whisper: dict, log) -> tuple:
    """The language a commentary track is spoken in, as ``(spec, code)``.

    ``spec`` is what to hand whisper's ``--language``: a two-letter code for a
    language in the table, otherwise the English name whisper reported, empty
    when unknown. ``code`` is the ".xx.srt" suffix - the two-letter code, but
    ONLY for a table language; empty for anything else, which is exactly the
    test for "a native transcript is worth writing".

    Asked in order of cost and reliability: a language word in the track's own
    name, then the mkv language tag (``eng`` excepted - the default
    ``updateTags`` stamps, not evidence), then whisper on a short excerpt.
    """
    name = shell_lower(name)
    tag = shell_lower(tag)

    # 1. a language word in the track name
    for lang in languages.LANGUAGES:
        for keyword in lang.keywords:
            if keyword in name:
                return (lang.code2, lang.code2)

    # 2. the mkv language tag, unless it is the English default
    if tag not in ("eng", "en"):
        code2 = languages.code_from_tag(tag)
        if code2:
            return (code2, code2)

    # 3. ask whisper
    log("Detecting the language of: " + os.path.basename(mka))
    detected = detect_commentary_language(mka, ram_root, whisper, log)
    if not detected:
        return ("", "")
    spec = detected
    code = languages.code_from_name(detected)
    if code:
        spec = code
    return (spec, code)


def _strip_last_ext(path: str) -> str:
    """The bash's ``${path%.*}``: drop the shortest ``.*`` suffix, or nothing."""
    dot = path.rfind(".")
    return path[:dot] if dot != -1 else path


def _has_existing_output(opus: str, base: str) -> bool:
    """The bash's resume check: any output for this stem. "$base" is quoted so a
    glob character in the movie name is matched literally and only the
    ``.*.srt`` tail is a pattern; the ``.opus`` and the bare ``.srt`` are checked
    separately (the bare ``.srt`` is not matched by the ``.*.srt`` pattern)."""
    import glob
    if os.path.isfile(opus) or os.path.isfile(base + ".srt"):
        return True
    return any(os.path.isfile(match) for match in glob.glob(glob.escape(base) + ".*.srt"))


def transcribe_commentary(record: str, whisper: dict, max_sync_offset: str,
                          quality: str, ram_root: str, log) -> None:
    """One queued transcription - a worker entry point.

    ``record`` is a whole queue record whose five ``\\x1f`` separated fields are
    the mka extract, the finished-srt destination, the task (transcribe or
    translate), the language, and every srt queued for this same extract
    (``\\x1e`` separated). Runs the whisper run when the srt is not already
    there, aligns it, and frees the extract from RAM once every sibling is on
    disk.
    """
    fields = record.split("\x1f")
    mka, srt, task = fields[0], fields[1], fields[2]
    lang = fields[3] if len(fields) > 3 else ""
    siblings = fields[4] if len(fields) > 4 else ""

    if not os.path.isfile(srt):
        # Anything whose SOURCE is not English runs on the multilingual model,
        # which also covers the translations.
        model = whisper["modelMulti"]
        if task == "transcribe" and lang == "en":
            model = whisper["model"]
        if task == "translate":
            log("Translating commentary into English (from {}, {}): {}".format(
                lang, model, os.path.basename(srt)))
        else:
            log("Transcribing commentary ({}, {}): {}".format(
                lang, model, os.path.basename(srt)))

        # whisper names its output after the input file, so every run gets an
        # output directory of its own: the two runs of one extract would
        # otherwise overwrite each other.
        out_dir = tempfile.mkdtemp(prefix="whisper.", dir=ram_root)
        mka_name = os.path.basename(mka)
        whisper_srt = os.path.join(out_dir, _strip_last_ext(mka_name) + ".srt")

        # "Hello." is only handed to runs whose OUTPUT is English, where it
        # nudges whisper into punctuated output.
        prompt_args = (["--initial_prompt", "Hello."]
                       if task == "translate" or lang == "en" else [])
        lang_args = ["--language", lang] if lang else []

        srt_dir = os.path.dirname(srt)
        if srt_dir:
            os.makedirs(srt_dir, exist_ok=True)
        try:
            ran = _run(["pipx", "run", "whisper-ctranslate2", mka,
                        "--output_dir", out_dir, "--model", model,
                        "--task", task, *lang_args,
                        "--compute_type", whisper["computeType"],
                        "--vad_filter", "True", *prompt_args,
                        "--output_format", "srt", "--device", whisper["device"],
                        "--threads", whisper["threads"]],
                       stdout=subprocess.DEVNULL)
            ran_ok = ran.returncode == 0
        except OSError:
            # a missing pipx is the bash's 127: a failed run, logged as such
            ran_ok = False
        if ran_ok and os.path.isfile(whisper_srt):
            shutil.move(whisper_srt, srt)
            time.sleep(SYNC_SETTLE_SECONDS)
            # A transcript that could not be synced is thrown out rather than
            # kept out of step: both ways of failing are the discard.
            status = sync_subtitle(mka, srt, max_sync_offset, max_sync_offset,
                                   quality)
            if status == 1:
                log("WARNING: transcript sync failed, discarding: {}".format(
                    os.path.basename(srt)))
                os.remove(srt)
            elif status == 2:
                log("WARNING: transcript sync rejected as low-quality, "
                    "discarding: {}".format(os.path.basename(srt)))
                os.remove(srt)
        else:
            log("WARNING: transcription failed: {}".format(
                os.path.basename(srt)))
        shutil.rmtree(out_dir, ignore_errors=True)

    # Free the extract from RAM as soon as no other queued run still needs it.
    # Whoever finishes last sees every sibling on disk; a run that failed leaves
    # its srt missing and its extract to the sweep.
    # An empty sibling list reads as zero fields in the bash, and zero fields
    # pass the check: the extract goes anyway.
    siblings_list = siblings.split("\x1e") if siblings else []
    if all(os.path.isfile(s) for s in siblings_list):
        os.remove(mka)


def _mkv_entries(top: str) -> list[str]:
    """``find <top> -name '*mkv' -print0``: every entry under ``top`` whose name
    ends the way the pattern does, spelled from ``top`` the way find spells it.

    In find's own order, which is the filesystem's: each directory's entries in
    readdir order, and a subdirectory descended into where it stands rather than
    after its siblings. The pattern carries no dot, so it is what the bash
    matched - any name ending in "mkv" - and it is tested against directories
    too, because find tests every entry it walks and not only the files.
    """
    found: list[str] = []

    def descend(dirpath: str) -> None:
        try:
            entries = list(os.scandir(dirpath))
        except OSError:
            return
        for entry in entries:
            if fnmatch.fnmatchcase(entry.name, "*mkv"):
                found.append(entry.path)
            if entry.is_dir(follow_symlinks=False):
                descend(entry.path)

    descend(top)
    return found


def export_commentary(directory: str, read_track_info, is_bonus_folder,
                      rename, audio_stream_index, ram_root: str,
                      whisper: dict, whisper_jobs: int, log, drain_queue,
                      max_sync_offset: str, quality: str) -> None:
    """Extract every commentary and drain the queue.

    ``read_track_info`` is the caller's track reader (the bash's
    ``readTrackInfo``), returning the six per-track arrays for a movie;
    ``is_bonus_folder``, ``rename`` and ``audio_stream_index`` the caller's
    helpers; ``drain_queue`` stands in for the bash's ``WHISPER_XARGS`` worker
    drain (the comparison runs the workers one record at a time through
    :func:`transcribe_commentary`). Everything else is this run's own: the flat
    queue spans every movie and every language wanted, so the workers stay busy
    to the last record.
    """
    try:
        os.chdir(directory)
    except OSError:
        return
    files = _mkv_entries(".")
    queue = os.path.join(ram_root, "commentaryQueue")
    with open(queue, "w", encoding="ascii") as handle:
        handle.write("")

    queue_records = []
    for file in files:
        dir_name = os.path.dirname(file)
        # a commentary is a film's, so bonus material is passed over
        if is_bonus_folder(dir_name):
            continue
        (names, _codecs, _channels, comments, types, langs) = \
            read_track_info(file)
        for i in range(1, len(comments) + 1):
            comment = comments[i - 1]
            type_ = types[i - 1]

            # only audio tracks that are identified as commentaries - by their
            # flag or, for a file that only says it there, by their name.
            if "audio" not in type_:
                continue
            if comment != "true" and \
                    not languages.is_commentary_name(names[i - 1]):
                continue

            # determine name of file to export
            name = names[i - 1].replace("/", "").replace("&", "and")
            name = rename(name)
            base = "{} {} {}".format(_strip_last_ext(file), i - 1, name)
            # cut to leave room for the suffixes put on it below
            base = base[:COMMENTARY_STEM_MAX_BYTES]
            # final outputs stay on disk next to the movie
            opus = base + ".opus"
            # the large temp audio extract goes to RAM, mirroring the absolute
            # disk path so the outputs end up next to the movie. The bash builds
            # this as the literal string "$ramRoot/$(pwd -P)/...", keeping BOTH
            # halves - os.path.join would drop the first at an absolute part.
            mka = "{}/{}/{}.mka".format(ram_root,
                                        os.path.realpath(directory),
                                        base[2:] if base.startswith("./") else base)

            # Script resume: a track that already has ANY output is skipped
            # before the extract.
            if _has_existing_output(opus, base):
                continue

            # mkvtools index the whole matroska while ffmpeg indexes each track
            # type separately and from zero
            log("Extracting commentary track {}: {}".format(i - 1, file))
            os.makedirs(os.path.dirname(mka), exist_ok=True)
            index = audio_stream_index(i, types)
            # the bash leaves this ffmpeg's stderr on the script's stderr
            try:
                made = subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                                       "-nostats", "-i", file, "-vn",
                                       "-map", "0:a:{}".format(index),
                                       "-acodec", "copy", mka],
                                      stdin=subprocess.DEVNULL)
                made_ok = made.returncode == 0
            except OSError:
                # a missing ffmpeg is the bash's 127: a failed extract
                made_ok = False
            if not made_ok:
                log("WARNING: commentary extract failed (track {}): {}".format(
                    i - 1, file))
                if os.path.exists(mka):
                    os.remove(mka)
                continue

            # what language it is in decides which subtitles are wanted
            spec, code = commentary_language(name, langs[i - 1], mka,
                                             ram_root, whisper, log)
            if not spec:
                # neither the file nor whisper could tell: English is both the
                # likeliest answer and what this script assumed before
                log("WARNING: could not tell the language of commentary "
                    "track {}, assuming English: {}".format(i - 1, file))
                spec, code = "en", "en"

            # the three cases of the table at the top of this section
            job_task = []
            job_lang = []
            job_srt = []
            if code == "en":
                job_task.append("transcribe")
                job_lang.append(spec)
                job_srt.append(base + ".en.srt")
            else:
                if code:
                    job_task.append("transcribe")
                    job_lang.append(spec)
                    job_srt.append("{}.{}.srt".format(base, code))
                job_task.append("translate")
                job_lang.append(spec)
                job_srt.append(base + ".en.srt")

            # the sibling list all runs of this extract share
            siblings = "\x1e".join(job_srt)
            for j in range(len(job_srt)):
                log("Queued: {} {} -> {}".format(
                    job_task[j], job_lang[j], os.path.basename(job_srt[j])))
                record = "\x1f".join([mka, job_srt[j], job_task[j],
                                      job_lang[j], siblings])
                queue_records.append(record)
                with open(queue, "ab") as handle:
                    handle.write(record.encode("utf-8") + b"\0")

    if queue_records:
        log("Transcribing {} queued commentary subtitle(s) on {} worker(s)"
            .format(len(queue_records), whisper_jobs))
        drain_queue(queue_records, queue)

    # Extracts whose transcription never finished are left behind by the
    # workers, so sweep tmpfs clean here.
    for dirpath, _dirnames, filenames in os.walk(ram_root):
        for fn in filenames:
            if fn.endswith(".mka"):
                os.remove(os.path.join(dirpath, fn))