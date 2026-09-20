"""The job that turns a movie's commentary AUDIO tracks into the subtitle files
worth keeping.

Work out what language each commentary is in, and drain one flat queue of
transcription runs. The shared whisper settlement lives in
:mod:`medialib.lib.whisper`, the languages table and its lookups in
:mod:`medialib.lib.languages`, and the alignment in
:mod:`medialib.lib.subtitlefiles` - sourced once rather than re-implemented.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
import time

from medialib.lib import languages, plexnames
from medialib.lib import whisper as whisper_lib
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
    "COMMENTARY_NO_NAME",
    "commentary_prefix",
    "commentary_stem",
    "detect_commentary_language",
    "commentary_language",
    "transcribe_commentary",
    "export_commentary",
]

# How long a commentary stem ("<movie> <trackIndex> <trackName>") may get before
# the suffixes are appended to it. Every output derived from one stem has to fit
# the file name limit, so the stem is cut to leave room for the longest of them.
# Taken from the naming, which cuts this same stem at this same place when an id
# tag pushes one of these past the limit.
NAME_MAX_BYTES = plexnames.NAME_MAX_BYTES
COMMENTARY_SUFFIX_BYTES = plexnames.COMMENTARY_SUFFIX_BYTES
COMMENTARY_STEM_MAX_BYTES = plexnames.COMMENTARY_STEM_MAX_BYTES

# The word a nameless commentary is known by, in its file name and in the
# matching that asks of a film's commentaries which one a name names. The rare
# track that carries no name of its own is read as "null", and the empty
# string is no name at all; both become the word, so a nameless transcript is
# named after the word rather than "null".
COMMENTARY_NO_NAME = "Commentary"

# How long a commentary excerpt is, cut from the MIDDLE of the track (commentaries
# open on the film's music or silence, and whisper only looks at the first 30 s),
# and the detection probability under which a guess is no answer at all.
COMMENTARY_DETECT_SECONDS = 120
COMMENTARY_DETECT_MIN_PROBABILITY = 0.5

# The pause between landing a finished transcript and aligning it.
SYNC_SETTLE_SECONDS = 1

# whisper's "Detected language 'Dutch' with probability 0.987654" line.
_DETECT = re.compile(r"Detected language '([^']+)' with probability ([0-9.]+)")


def _run(args, **kwargs) -> subprocess.CompletedProcess:
    """A tool call run in silence: stderr and (where noted) stdout swallowed,
    stdin from /dev/null, an absent tool the call's own failure."""
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
    # An unreadable figure is zero.
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
    # an excerpt that cannot be made (or a missing ffmpeg) is a no-answer,
    # not an error
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
        # a failed probe drops its output too
        out = ran.stdout.decode("utf-8", "replace") if ran.returncode == 0 else ""
    except OSError:
        out = ""
    for path in (excerpt, excerpt[:-4] + ".txt"):
        if os.path.exists(path):
            os.remove(path)

    match = _DETECT.search(out)
    if not match:
        return ""
    # The comparison uses awk's numeric coercion, so a probability the parse
    # cannot read is its zero, not an error.
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
    ``update_tags`` stamps, not evidence), then whisper on a short excerpt.
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
    """Drop the shortest ``.*`` suffix, or nothing."""
    dot = path.rfind(".")
    return path[:dot] if dot != -1 else path


def commentary_prefix(movie_stem: str, track_id) -> str:
    """The part of a commentary output's name that no cut may reach: the film it
    belongs to, and the number of the track it is of.

    Everything after it is the track's own name, which is the one part a name too
    long for the filesystem is cut into - so this is what says "an output of this
    track", whatever length the rest of it ended up being.
    """
    return "%s %s " % (movie_stem, track_id)


def commentary_stem(movie_stem: str, track_id, name: str) -> str:
    """The stem every output of one commentary track hangs off, cut to leave
    room for the suffixes hung on it.

    The cut takes the FILE NAME only and counts BYTES, because that is what the
    limit is: a film in a deeply nested folder would otherwise lose the end of
    its transcripts' names to the length of the path above them, and an accented
    title spends two bytes on a letter that a count of characters spends one on.
    """
    stem = commentary_prefix(movie_stem, track_id) + name
    directory, base = os.path.split(stem)
    base = plexnames.cut_to_bytes(base, COMMENTARY_STEM_MAX_BYTES)
    return os.path.join(directory, base) if directory else base


def _display_name(raw: str) -> str:
    """A commentary's own name, or the word a nameless one is known by.

    Asked of the raw track name before the cleaning that follows it: the absent
    name is "null" and the empty string is no name at all, and both are the
    word rather than a "null" spelled into the file name.
    """
    return raw if raw and raw != "null" else COMMENTARY_NO_NAME


def _existing_outputs(prefix: str) -> list:
    """The outputs of this track already on disk.

    Asked of the track and not of today's exact stem: the stem is cut at a
    different place once anything about the name above it changes - a folder
    renamed, an id tag added - and a check for the exact name would miss the
    transcript sitting right there and transcribe it a second time.
    """
    directory, start = os.path.split(prefix)
    directory = directory or "."
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return [os.path.join(directory, name) for name in names
            if name.startswith(start) and name.endswith((".opus", ".srt"))]


def _has_existing_output(prefix: str) -> bool:
    """The resume check: whether this TRACK already has an output of any kind."""
    return bool(_existing_outputs(prefix))


def _size_of(path: str) -> int:
    """The bytes a file holds, 0 where it cannot be read.

    The queue's estimate of how long a transcription of this extract will
    take: the queue sorts from the largest of its items to the smallest, and
    a transcription runs as long as the audio it transcribes.
    """
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remove_sidecars(prefix: str) -> None:
    """The track's transcripts, gone: the too-small sidecars a -c run discards
    before re-transcribing. Only the .srt is removed - the .opus this phase never
    writes to disk would belong to a run that did the opus pass, and is left
    alone."""
    for path in _existing_outputs(prefix):
        if path.endswith(".srt"):
            try:
                os.remove(path)
            except OSError:
                pass


# What a commentary's output puts after the film it belongs to: the track's
# number, then the track's own name - the same shape the cut keeps, asked here
# to read a name the check above does not match on its number.
_STALE_TAIL = re.compile(r"^([0-9]+) (\S.*)$", re.S)

# The language the transcription writes between a commentary's name and its
# extension, read the way the append reads it: two or three letters, and nothing
# a track name would end on.
_LANGUAGE_SUFFIX = re.compile(r"^\.[A-Za-z]{2,3}$")


def _named_part(tail: str) -> str:
    """The name a commentary output was named for, with whatever the
    transcription hung on the end taken off: the ".srt", and the language code
    an aligned transcript carries between the name and it. A transcript an
    older version wrote carries no language at all, and its name is the whole
    of what is left of it."""
    named, _extension = os.path.splitext(tail)
    head, language = os.path.splitext(named)
    return head if _LANGUAGE_SUFFIX.match(language) else named


def _renumber_stale_transcripts(file_stem: str, track_id, name: str,
                                siblings: list, same_commentary_name,
                                log) -> bool:
    """The near miss the resume check cannot see: the transcript is beside the
    film, of the right film and the right commentary, but numbered for a track
    that no longer stands where the run that wrote it numbered it - the film's
    track layout changed under the name, or an older run numbered tracks a
    different way.

    Asked of the film and the track the way the check above asks of them: the
    film is the stem no cut may reach, and the commentary the name, which may
    be the one the file was written under or one of the film's commentary
    names cut short - a cut file name is the front of the name it was cut
    from. The caller's ``same_commentary_name`` is the rule that says whether
    a name names this commentary, the one the append uses to match a
    transcript to its track, and a name it answers no for is left for the
    track it is of rather than spent on the one it is not.

    Renumbered to the track's own number, the file is what the check above
    would have skipped on, and is asked the same questions it would have asked:
    a -c run still discards one too small to be the film's real transcript.
    The rest of the name is left as it was written, and the srt itself
    untouched - what is spent is the rename, not the transcription.

    Whether a single transcript came home renumbered.
    """
    directory, stem_base = os.path.split(file_stem)
    directory = directory or "."
    try:
        entries = os.listdir(directory)
    except OSError:
        return False

    found = []
    for entry in entries:
        if not (entry.startswith(stem_base + " ")
                and entry.endswith(".srt")):
            continue
        tail = _STALE_TAIL.match(entry[len(stem_base) + 1:])
        if not tail or int(tail.group(1)) == track_id:
            continue
        # The number is the one thing the rule may not agree on; the name is
        # asked of the film's commentaries the way the append asks it.
        if not same_commentary_name(_named_part(tail.group(2)), name, siblings):
            continue
        found.append((entry, tail.group(2)))
    if not found:
        return False

    # Two copies under one name are one transcript twice over: the one that
    # lost least of the name to the cut is what the append would keep, and it
    # takes the number, the rest dropped rather than left to disagree about
    # which copy is the transcript.
    found.sort(key=lambda pair: len(pair[1]), reverse=True)
    kept = 0
    for entry, rest in found:
        target = "{} {} {}".format(stem_base, track_id, rest)
        if os.path.exists(os.path.join(directory, target)):
            log("WARNING: dropping a second transcript of the same "
                "commentary: {}".format(entry))
            try:
                os.remove(os.path.join(directory, entry))
            except OSError:
                pass
            continue
        try:
            os.rename(os.path.join(directory, entry),
                      os.path.join(directory, target))
        except OSError:
            continue
        kept += 1
        log("Renumbered the track's transcript from a stale track number "
            "(track {}): {} -> {}".format(track_id, entry, target))
    return kept > 0


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
        # Nought slots is the unbatched path. The line flags ride with the
        # batch because batching segments on the voice detection alone, which
        # writes one cue per speech run.
        slots = whisper.get("batchSlots", 0)
        batch_args = (["--batched", "True", "--batch_size", str(slots),
                       "--word_timestamps", "True",
                       "--max_line_width", str(whisper_lib.WHISPER_LINE_WIDTH),
                       "--max_line_count", str(whisper_lib.WHISPER_LINE_COUNT)]
                      if slots else [])

        srt_dir = os.path.dirname(srt)
        if srt_dir:
            os.makedirs(srt_dir, exist_ok=True)
        try:
            ran = _run(["pipx", "run", "whisper-ctranslate2", mka,
                        "--output_dir", out_dir, "--model", model,
                        "--task", task, *lang_args,
                        "--compute_type", whisper["computeType"],
                        "--vad_filter", "True", *prompt_args, *batch_args,
                        "--output_format", "srt", "--device", whisper["device"],
                        "--threads", whisper["threads"]],
                       stdout=subprocess.DEVNULL)
            ran_ok = ran.returncode == 0
        except OSError:
            # a missing pipx is a failed run, logged as such
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
    # An empty sibling list reads as zero fields, and zero fields pass the
    # check: the extract goes anyway.
    siblings_list = siblings.split("\x1e") if siblings else []
    if all(os.path.isfile(s) for s in siblings_list):
        # The last run of an extract frees it, and two runs of one extract can
        # finish at the same time: both see every sibling on disk, and the one
        # that loses the removal is not a failure but the extract already free.
        try:
            os.remove(mka)
        except OSError:
            pass


def _mkv_entries(top: str) -> list[str]:
    """Every entry under ``top`` whose name ends in "mkv", spelled from
    ``top``.

    In the filesystem's own order: each directory's entries in readdir order,
    and a subdirectory descended into where it stands rather than after its
    siblings. The pattern carries no dot - any name ending in "mkv" - and it
    is tested against directories too, not only the files.
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
                      whisper: dict, log, drain_queue,
                      max_sync_offset: str, quality: str,
                      discard_existing=None, same_commentary_name=None,
                      orphans=None, unfixed=None) -> None:
    """Extract every commentary, and hand the queue to the drain to run.

    ``read_track_info`` is the caller's track reader, returning the six
    per-track arrays for a movie;
    ``is_bonus_folder``, ``rename`` and ``audio_stream_index`` the caller's
    helpers. ``drain_queue`` is handed the queue itself - the generator below -
    and runs it: the walk prepares one commentary at a time - the resume check,
    the extract, the language, the wanted subtitles - and yields ``(record,
    size)`` for every transcription it queues, the size being the extract the
    transcription runs on, so the queue it is fed can keep the longest of them
    to the end. The preparation is a job of its own and runs beside the
    transcription rather than in front of it, which is why the queue is a
    producer and not a list. ``discard_existing``, when given, is the caller's
    verdict on a track that already has an output: True discards the sidecar
    and transcribes for real, and absent an existing output is the resume that
    skips the track. ``same_commentary_name``, when given, is the caller's rule
    for whether a name names a commentary, and it is what lets a transcript an
    older run numbered for a track that no longer stands where it numbered it
    be renumbered to the track's own number rather than transcribed a second
    time. ``orphans``, when given, is a list the walk fills with a transcript
    that names a track the film no longer numbers a commentary for; the -c run
    is the one that hands it one, so it can leave the list in a file.
    ``unfixed``, when given, is a list the walk fills with a movie whose name
    carries no dot before its extension: the film's stem cannot be read off a
    name like that, and a transcript written from it would land without a
    directory, so the film is left untranscribed and handed over to be
    reported.
    Everything else is this run's own: the queue spans every movie and every
    language wanted, so the workers stay busy to the last record.
    """
    try:
        os.chdir(directory)
    except OSError:
        return

    def prepare():
        for file in _mkv_entries("."):
            dir_name = os.path.dirname(file)
            # a commentary is a film's, so bonus material is passed over
            if is_bonus_folder(dir_name):
                continue
            # and so is the "(old)" copy an improved remux kept: its commentary
            # is the same audio as its living sibling's, already being
            # transcribed.
            if plexnames.is_kept_copy(file):
                continue
            # A name that carries no dot before its extension has no stem to
            # read the transcript's name from: built from the empty stem it
            # would be written without a directory, in the library's root
            # rather than the film's folder. The film is left untranscribed and
            # handed over to be reported.
            if not os.path.isfile(file):
                continue
            if not os.path.basename(file).endswith(".mkv"):
                log("WARNING: no dot before the extension in the movie's "
                    "name, left untranscribed: " + file)
                if unfixed is not None:
                    unfixed.append(file)
                continue
            (names, _codecs, _channels, comments, types, langs) = \
                read_track_info(file)
            # What every commentary of the film is called, which is what says
            # whether a name that lost its number still points at one of them
            # and at no other.
            commentary_names = [
                rename(_display_name(n).replace("/", "").replace("&", "and"))
                for n, c, t in zip(names, comments, types, strict=True)
                if "audio" in t and (c == "true"
                                     or languages.is_commentary_name(n))]
            # A transcript that names a track number the film no longer numbers
            # a commentary for is one the film has outlived: reported, not
            # transcribed and not silently kept. The walk only asks when the
            # run wants the list.
            if orphans is not None:
                movie_base = os.path.basename(_strip_last_ext(file))
                start = movie_base + " "
                commentary_numbers = {
                    i - 1 for i in range(1, len(comments) + 1)
                    if "audio" in types[i - 1] and (
                        comments[i - 1] == "true"
                        or languages.is_commentary_name(names[i - 1]))}
                try:
                    entries = os.listdir(dir_name)
                except OSError:
                    entries = []
                for entry in entries:
                    if not (entry.startswith(start)
                            and entry.endswith((".srt", ".opus"))):
                        continue
                    tail = _STALE_TAIL.match(entry[len(start):])
                    if not tail or int(tail.group(1)) in commentary_numbers:
                        continue
                    orphans.append(os.path.join(dir_name, entry))
            for i in range(1, len(comments) + 1):
                comment = comments[i - 1]
                type_ = types[i - 1]

                # only audio tracks that are identified as commentaries - by
                # their flag or, for a file that only says it there, by their
                # name.
                if "audio" not in type_:
                    continue
                if comment != "true" and \
                        not languages.is_commentary_name(names[i - 1]):
                    continue

                # determine name of file to export; a nameless track is the
                # rare one, and the "null" it carries would be spelled into
                # the name as-is
                name = _display_name(names[i - 1])
                name = name.replace("/", "").replace("&", "and")
                name = rename(name)
                file_stem = _strip_last_ext(file)
                prefix = commentary_prefix(file_stem, i - 1)
                base = commentary_stem(file_stem, i - 1, name)
                # the large temp audio extract goes to RAM, mirroring the
                # absolute disk path so the outputs end up next to the movie.
                # Both halves are kept - os.path.join would drop the first at
                # an absolute part.
                mka = "{}/{}/{}.mka".format(ram_root,
                                            os.path.realpath(directory),
                                            base[2:]
                                            if base.startswith("./")
                                            else base)

                # Script resume: a track that already has ANY output is
                # skipped before the extract. A transcript an older run
                # numbered for a track that no longer stands there is
                # renumbered to the track's own number first, so the check
                # finds it rather than spending a second transcription on the
                # same commentary.
                existing = _has_existing_output(prefix)
                if not existing and same_commentary_name is not None:
                    existing = _renumber_stale_transcripts(
                        file_stem, i - 1, name, commentary_names,
                        same_commentary_name, log)
                if existing:
                    if (discard_existing is None
                            or not discard_existing(prefix, file)):
                        continue
                    # The sidecar is too small to be this film's real
                    # transcript - the one a run wrote before it could tell a
                    # commentary's language, which forced a non-English
                    # commentary through the English model. Discard it and
                    # transcribe for real.
                    log("Discarding too-small commentary sidecar, "
                        "re-transcribing (track {}) of: {}".format(i - 1,
                                                                   file))
                    _remove_sidecars(prefix)

                # mkvtools index the whole matroska while ffmpeg indexes each
                # track type separately and from zero
                log("Extracting commentary track {}: {}".format(i - 1, file))
                os.makedirs(os.path.dirname(mka), exist_ok=True)
                index = audio_stream_index(i, types)
                # this ffmpeg's stderr is left on the script's stderr
                try:
                    made = subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-nostats",
                         "-i", file, "-vn",
                         "-map", "0:a:{}".format(index),
                         "-acodec", "copy", mka],
                        stdin=subprocess.DEVNULL)
                    made_ok = made.returncode == 0
                except OSError:
                    # a missing ffmpeg is a failed extract
                    made_ok = False
                if not made_ok:
                    log("WARNING: commentary extract failed (track {}): {}"
                        .format(i - 1, file))
                    if os.path.exists(mka):
                        os.remove(mka)
                    continue

                # what language it is in decides which subtitles are wanted
                spec, code = commentary_language(name, langs[i - 1], mka,
                                                 ram_root, whisper, log)
                if not spec:
                    # neither the file nor whisper could tell: English is both
                    # the likeliest answer and what this script assumed before
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

                # the size the queue sorts on: the extract's bytes, and the
                # transcription's length runs with them
                size = _size_of(mka)
                # the sibling list all runs of this extract share
                siblings = "\x1e".join(job_srt)
                for j in range(len(job_srt)):
                    log("Queued: {} {} -> {}".format(
                        job_task[j], job_lang[j], os.path.basename(job_srt[j])))
                    record = "\x1f".join([mka, job_srt[j], job_task[j],
                                          job_lang[j], siblings])
                    yield (record, size)

    drain_queue(prepare())

    # Extracts whose transcription never finished are left behind by the
    # workers, so sweep tmpfs clean here.
    for dirpath, _dirnames, filenames in os.walk(ram_root):
        for fn in filenames:
            if fn.endswith(".mka"):
                os.remove(os.path.join(dirpath, fn))
