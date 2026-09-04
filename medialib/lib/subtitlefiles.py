"""The subtitle sidecar helpers.

Everything that gets a movie its ``.xx.srt`` files and keeps them in sync: a
``*Subs`` folder pre-existing one level down is lifted up, sidecars whose name
names a language are renamed to the ``<movie>.<xx>.srt`` convention, a fetched
sidecar is aligned to the audio, and a subtitle that cannot be aligned is
thrown out rather than kept out of step. ``sync_subtitle`` is the one function
the parallel commentary workers run (bash exports it for them), so its three
outcomes - synced, died, refused - are reported as a status rather than an
exception: ffsubsync exits 0 both when it applied an alignment and when its
quality check refused one, so the verdict comes from the log file, never from
stderr (which rich hard-wraps wherever COLUMNS is unset).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable

from medialib.lib import languages
from medialib.lib.safety import SkipLog

__all__ = [
    "move_subs",
    "rename_subs",
    "sync_subtitle",
    "download_srt",
    "download_subs",
]

# The folder names downloadSubs leaves alone: the extras that live beside a
# movie, matched as a substring of the movie folder's path, the way bash's
# ``$dir == *"Featurettes"*`` does.
EXTRAS_WORDS = ("Featurettes", "Other", "Scenes", "Interviews",
                "Shorts", "Trailers", "Extras")


def _move(source: str, destination: str) -> None:
    """``mv -- source destination || true``, with mv's destination rule: a
    destination that is a directory receives the source under its own name, and
    the kernel's rename then decides what an existing name of that shape allows
    - a file is replaced, a folder replaces an empty folder of its own name,
    and a file onto a folder or a folder onto a non-empty one is left where it
    is rather than failing the run."""
    if os.path.isdir(destination):
        destination = os.path.join(destination, os.path.basename(source))
    try:
        os.rename(source, destination)
    except OSError:
        pass


def move_subs(directory: str) -> None:
    """Lift the content of every ``*Subs`` folder exactly one level down.

    bash's ``find -maxdepth 2 -mindepth 2 -type d -name '*Subs'``: the folder
    is two levels below the one handed in (a movie's own ``Subs``), the name
    match is case-sensitive, and an entry whose name is taken at the level
    above (by a non-empty folder, or by a file when the entry is one) is left
    where it is rather than failing the run. The match is ``find -P -type d``:
    a symlink is a link, so neither a linked movie folder is descended into
    nor a linked ``*Subs`` lifted, while a link INSIDE the lifted folder moves
    as a link.
    """
    for entry in os.listdir(directory):
        movie_dir = os.path.join(directory, entry)
        if os.path.islink(movie_dir) or not os.path.isdir(movie_dir):
            continue
        for subs_name in os.listdir(movie_dir):
            subs_dir = os.path.join(movie_dir, subs_name)
            if os.path.islink(subs_dir) \
                    or not os.path.isdir(subs_dir) \
                    or not subs_name.endswith("Subs"):
                continue
            for name in os.listdir(subs_dir):
                # the folder itself, the way `mv -- "$entry" ../` hands mv a
                # directory and lets its destination rule name the entry in it
                _move(os.path.join(subs_dir, name), movie_dir)


def rename_subs(directory: str, skip_log: SkipLog | None = None) -> None:
    """Rename sidecars whose name names a language to ``<movie>.<xx>.srt``.

    For every movie folder and every language of the table, a sidecar matching
    ``*<SubWord>.srt`` case-insensitively (bash's ``-iname``) is renamed to the
    movie's own name with the language's code2. A sidecar nested one level down
    still names the movie it belongs to. A target that already exists is
    skipped - and the skip is recorded, the way ``recordSafetySkip`` does -
    rather than overwriting the subtitle that got there first. The walk is
    ``find -P``: a linked movie folder is not a movie, a linked sidecar is not
    a file, and a linked subfolder is not descended into.
    """
    for movie in os.listdir(directory):
        movie_dir = os.path.join(directory, movie)
        if os.path.islink(movie_dir) or not os.path.isdir(movie_dir):
            continue
        # The shell's order: one find per language, the way the safety-skip
        # report it produces reads - a movie's skips come back language by
        # language, not file by file.
        for row in languages.LANGUAGES:
            suffix = row.sub_word.lower() + ".srt"
            target = "{}/{}.{}".format(movie, movie, row.code2) + ".srt"
            for dirpath, _dirnames, filenames in os.walk(movie_dir):
                for filename in filenames:
                    if not filename.lower().endswith(suffix):
                        continue
                    source = os.path.join(dirpath, filename)
                    if os.path.islink(source):
                        continue
                    # The two spellings bash compares: the find output keeps
                    # the "./" the walk started from, the target is built from
                    # it with the "./" stripped.
                    subtitle = "./" + os.path.relpath(source, directory)
                    if os.path.exists(os.path.join(directory, target)) \
                            and subtitle != target:
                        if skip_log is not None:
                            skip_log.record(subtitle, target)
                        continue
                    _move(source, os.path.join(directory, target))


def sync_subtitle(reference: str, srt: str, max_offset: str,
                  quality_offset: str, quality: str) -> int:
    """Align one subtitle to its reference, and say which of three things
    happened: 0 the subtitle was synced, 1 ffsubsync died outright (bad
    arguments, a missing dependency), 2 ffsubsync refused the alignment as too
    poor to trust and left the subtitle unmodified.

    Telling 2 from 0 needs the log file read, because ffsubsync exits 0 for
    both. The log is read from ``--log-dir-path`` and not from stderr, because
    stderr is rendered by rich, which hard-wraps to 80 columns whenever COLUMNS
    is unset and thereby splits the very message being matched across lines.
    """
    quality_args = []
    if quality == "yes":
        quality_args = ["--skip-sync-on-low-quality",
                        "--quality-max-offset-seconds", quality_offset]
    # mktemp -d honours TMPDIR; the explicit dir keeps this call on the same
    # directory without consulting tempfile's cached resolution of it
    try:
        log_dir = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
    except OSError:
        return 1
    try:
        try:
            ran = subprocess.run(
                ["ffsubsync", reference, "-i", srt, "-o", srt,
                 "--max-offset-seconds", max_offset, *quality_args,
                 "--log-dir-path", log_dir],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return 1
        if ran.returncode != 0:
            return 1
        try:
            with open(os.path.join(log_dir, "ffsubsync.log"),
                      encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
        except OSError:
            return 0
        return 2 if "low-quality alignment" in log_text else 0
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)


def download_srt(file: str, language_code: str, user: str, password: str,
                 max_sync_offset: str, max_sync_quality_offset: str,
                 ffsubsync_quality: str,
                 log: Callable[[str], None]) -> None:
    """Fetch one missing subtitle for one movie and one language.

    A sidecar that already exists is left in place - that is the resume
    check, and it looks for exactly the name the deletions below remove, so
    the next run re-downloads what this one threw out. Without credentials
    the download is skipped cleanly. A sidecar some provider labelled ``.srt``
    but that ffprobe names as another format is converted to real SubRip first,
    and a subtitle that cannot be synced is discarded rather than kept out of
    step - whether ffsubsync failed outright or refused the alignment it found.
    """
    dot = file.rfind(".")
    stem = file[:dot] if dot != -1 else file
    srt = "{}.{}.srt".format(stem, language_code)
    if os.path.isfile(srt):
        return
    if not user or not password:
        log("WARNING: openSubtitlesUser/openSubtitlesPassword not set, "
            "skipping subtitle download")
        return
    log("Downloading {} subtitles: {}".format(language_code, file))
    try:
        subprocess.run(
            ["pipx", "run", "subliminal", "--opensubtitles", user, password,
             "download", "-p", "opensubtitles", "-l", language_code, file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    if not os.path.isfile(srt):
        return

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", srt],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        sub_codec = probe.stdout.decode("utf-8", "replace").rstrip("\n")
    except OSError:
        sub_codec = ""
    if sub_codec and sub_codec != "subrip":
        log("Converting {} subtitle from {} to subrip: {}".format(
            language_code, sub_codec, file))
        converted = (srt[:-len(".srt")] + ".converted.srt"
                     if srt.endswith(".srt") else srt + ".converted.srt")
        try:
            made = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-nostats",
                 "-i", srt, converted],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            ok = made.returncode == 0
        except OSError:
            ok = False
        if ok:
            os.replace(converted, srt)
        else:
            try:
                os.remove(converted)
            except OSError:
                pass

    log("Syncing {} subtitles: {}".format(language_code, file))
    status = sync_subtitle(file, srt, max_sync_offset,
                           max_sync_quality_offset, ffsubsync_quality)
    if status == 1:
        log("WARNING: subtitle sync failed ({}), discarding: {}".format(
            language_code, file))
        try:
            os.remove(srt)
        except OSError:
            pass
    elif status == 2:
        log("WARNING: subtitle sync rejected as low-quality ({}), "
            "discarding: {}".format(language_code, file))
        try:
            os.remove(srt)
        except OSError:
            pass


def download_subs(directory: str, user: str, password: str,
                  max_sync_offset: str, max_sync_quality_offset: str,
                  ffsubsync_quality: str,
                  log: Callable[[str], None]) -> None:
    """Do the one-language download for every movie and every language.

    A movie is anything named ``*mkv`` (case-sensitively) anywhere under the
    tree, and one is NOT a movie when its folder's path carries one of the
    extras words - Featurettes, Other, Scenes, Interviews, Shorts, Trailers,
    Extras - the folders that hold the material a film comes with. Sequential
    by design: rapid-fire downloads get throttled.
    """
    def spell(rel):
        # the way find spells a path under the start point it was given: a bare
        # "." writes "./name", a trailing slash is not doubled, and anything
        # else joins with a single slash
        if directory == ".":
            return "./" + rel
        if directory.endswith("/"):
            return directory + rel
        return directory + "/" + rel

    def walk(base, rel_prefix):
        for entry in os.scandir(base):
            rel = rel_prefix + entry.name
            if entry.name.endswith("mkv"):
                yield spell(rel)
            if entry.is_dir(follow_symlinks=False):
                yield from walk(entry.path, rel + "/")

    for movie in walk(directory, ""):
        slash = movie.rfind("/")
        folder = movie[:slash] if slash != -1 else movie
        if any(word in folder for word in EXTRAS_WORDS):
            continue
        for row in languages.LANGUAGES:
            download_srt(movie, row.code2, user, password, max_sync_offset,
                         max_sync_quality_offset, ffsubsync_quality, log)