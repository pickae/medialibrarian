"""The subtitle sidecar helpers.

Everything that gets a movie its ``.xx.srt`` files and keeps them in sync: a
``*Subs`` folder pre-existing one level down is lifted up, sidecars whose name
names a language are renamed to the ``<movie>.<xx>.srt`` convention, a fetched
sidecar is aligned to the film - to the timings of a picture subtitle the film
carries where it has one, to its audio where it has none - and a subtitle that
cannot be aligned is thrown out rather than kept out of step. ``sync_subtitle`` is the one function
the parallel commentary workers run, so its three outcomes - synced, died,
refused - are reported as a status rather than an exception: ffsubsync exits 0
both when it applied an alignment and when its quality check refused one, so
the verdict comes from the log file, never from stderr (which rich hard-wraps
wherever COLUMNS is unset).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable

from medialib import helpers
from medialib.lib import languages, plexnames, safety, tmdblookup, treewalk
from medialib.lib.safety import SkipLog

__all__ = [
    "move_subs",
    "rename_subs",
    "ffsubsync_python",
    "can_measure_confidence",
    "sync_subtitle",
    "TimingReference",
    "film_imdb_id",
    "download_srt",
    "subtitle_movies",
    "download_subs",
    "check_srt",
    "check_subs",
]

# The folder names download_subs leaves alone: the extras that live beside a
# movie, matched as a substring of the movie folder's path.
EXTRAS_WORDS = ("Featurettes", "Other", "Scenes", "Interviews",
                "Shorts", "Trailers", "Extras")

# How far an alignment's best offset must stand out from every offset more than
# two seconds away from it - in spreads of the agreement across the search
# window, see medialib/helpers/ffsubsync_confidence.py - to be believed. Over a
# library's worth of pairings another film's subtitle never measured above 0.7,
# and a film's own - out of step by two minutes, or at another frame rate - never
# below 1.25. A subtitle for a different CUT measures low as well, rightly: its
# scenes sit at two offsets, and no one shift puts both in step.
MIN_SYNC_CONFIDENCE = 1.0

# The subtitle codecs drawn as pictures, which ffsubsync cannot read and so
# replaces with the audio's voice detection. A film that carries one carries
# the exact on-screen times of its dialogue, so those times are lifted out and
# handed to ffsubsync as a text subtitle of their own. Voice detection hears
# singing over an orchestra as noise, so for a musical the picture subtitle is
# the only reference there is; for anything else it is still the better one.
# A text subtitle the film carries needs none of this: ffsubsync reads it
# itself before it ever listens to the audio.
_PICTURE_CODECS = ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle")

# The fewest cues a picture subtitle needs to stand in for the dialogue. A
# forced track that is only the signs and the odd foreign line has a few
# dozen, and so few cues line up with almost any shift.
MIN_REFERENCE_CUES = 100

# A Blu-ray subtitle takes a line off the screen with a packet of its own that
# carries no picture, about 30 bytes where one with a picture runs to thousands.
_PGS_CLEAR_BYTES = 50

_CONFIDENCE = re.compile(r"^alignment confidence: (-?[0-9.]+)$", re.M)
_OFFSET = re.compile(r"^offset seconds: (-?[0-9.]+)$", re.M)
# How far off the best fit was, in the line ffsubsync refuses it with.
_REFUSED_OFFSET = re.compile(r"\|offset\| ([0-9.]+)s")

# The two id tags a Plex name carries, read for the number in them.
_IMDB_TAG = re.compile(r"\{imdb-(tt[0-9]+)\}", re.I)
_TMDB_TAG = re.compile(r"\{tmdb-([0-9]+)\}", re.I)


def _move(source: str, destination: str) -> None:
    """A destination that is a directory receives the source under its own
    name, and the kernel's rename then decides what an existing name of that
    shape allows - a file is replaced, a folder replaces an empty folder of
    its own name, and a file onto a folder or a folder onto a non-empty one is
    left where it is rather than failing the run."""
    if os.path.isdir(destination):
        destination = os.path.join(destination, os.path.basename(source))
    if safety.would_hide(destination):
        return
    try:
        os.rename(source, destination)
    except OSError:
        pass


def move_subs(directory: str) -> None:
    """Lift the content of every ``*Subs`` folder exactly one level down.

    The folder is two levels below the one handed in (a movie's own ``Subs``),
    the name match is case-sensitive, and an entry whose name is taken at the
    level above (by a non-empty folder, or by a file when the entry is one) is
    left where it is rather than failing the run. A symlink is a link, so
    neither a linked movie folder is descended into nor a linked ``*Subs``
    lifted, while a link INSIDE the lifted folder moves as a link.
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
                # the folder itself: _move's destination rule names the entry
                # in it
                _move(os.path.join(subs_dir, name), movie_dir)


def rename_subs(directory: str, skip_log: SkipLog | None = None) -> None:
    """Rename sidecars whose name names a language to ``<movie>.<xx>.srt``.

    For every movie folder and every language of the table, a sidecar matching
    ``*<SubWord>.srt`` case-insensitively is renamed to the movie's own name
    with the language's code2. A sidecar nested one level down still names the
    movie it belongs to. A target that already exists is skipped - and the
    skip is recorded - rather than overwriting the subtitle that got there
    first. A linked movie folder is not a movie, a linked sidecar is not a
    file, and a linked subfolder is not descended into.
    """
    for movie in os.listdir(directory):
        movie_dir = os.path.join(directory, movie)
        if os.path.islink(movie_dir) or not os.path.isdir(movie_dir):
            continue
        # One pass per language: the safety-skip report it produces reads a
        # movie's skips language by language, not file by file.
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
                    # The two spellings compared: subtitle keeps the "./" the
                    # walk started from; the target is the same path without
                    # it.
                    subtitle = "./" + os.path.relpath(source, directory)
                    if os.path.exists(os.path.join(directory, target)) \
                            and subtitle != target:
                        if skip_log is not None:
                            skip_log.record(subtitle, target)
                        continue
                    _move(source, os.path.join(directory, target))


def ffsubsync_python() -> list | None:
    """The interpreter the installed ffsubsync runs under, as the argv that
    starts it, or None when there is no ffsubsync or it cannot be told.

    Its own script's ``#!`` line says, for a pipx or pip install alike; a
    script whose ``#!`` is a shell wrapper (pip writes one for a path too long
    for the kernel) still has its venv's ``python`` beside it.
    """
    script = shutil.which("ffsubsync")
    if not script:
        return None
    try:
        with open(script, "rb") as handle:
            first = handle.readline().decode("utf-8", "replace").strip()
    except OSError:
        return None
    if first.startswith("#!"):
        argv = first[2:].split()
        if argv and "python" in os.path.basename(argv[-1]):
            return argv
    beside = os.path.join(os.path.dirname(os.path.realpath(script)), "python")
    return [beside] if os.access(beside, os.X_OK) else None


def can_measure_confidence() -> bool:
    """Whether the installed ffsubsync is one the confidence helper can watch."""
    python = ffsubsync_python()
    if python is None:
        return False
    try:
        probe = subprocess.run(
            [*python, helpers.path_of("ffsubsync_confidence.py"), "--probe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return probe.returncode == 0


def sync_subtitle(reference: str, srt: str, max_offset: str,
                  quality_offset: str, quality: str,
                  measured: dict | None = None) -> int:
    """Align one subtitle to its reference, and say which of three things
    happened: 0 the subtitle was synced, 1 ffsubsync died outright (bad
    arguments, a missing dependency) or caught its own failure, 2 the
    alignment was refused as too poor to trust.

    ``quality`` is what the installed ffsubsync can be asked, settled once per
    run. "confidence": run through the confidence helper, and refuse an
    alignment that does not stand out from every other offset by
    :data:`MIN_SYNC_CONFIDENCE` - which is what tells the film's own subtitle
    from another film's, where no offset limit can; ffsubsync's own check still
    refuses the absurd, but with the whole search window as its offset limit,
    since a right subtitle found far away is still right. "yes": ffsubsync's
    own check alone, with ``quality_offset`` as the furthest offset believed.
    Anything else: no check at all.

    ``measured``, when handed one, is given what the log says of the
    alignment: its ``offset`` in seconds - or, for one ffsubsync refused, the
    ``distance`` of its best fit - and its ``confidence``, each only where the
    log has it.

    Telling 2 from 0 needs the log file read, because ffsubsync exits 0 for
    both. The log is read from ``--log-dir-path`` and not from stderr, because
    stderr is rendered by rich, which hard-wraps to 80 columns whenever COLUMNS
    is unset and thereby splits the very message being matched across lines.
    """
    command = ["ffsubsync"]
    quality_args = []
    if quality == "confidence":
        python = ffsubsync_python()
        if python is None:
            return 1
        command = [*python, helpers.path_of("ffsubsync_confidence.py")]
        quality_args = ["--skip-sync-on-low-quality",
                        "--quality-max-offset-seconds", max_offset]
    elif quality == "yes":
        quality_args = ["--skip-sync-on-low-quality",
                        "--quality-max-offset-seconds", quality_offset]
    # The explicit dir honours TMPDIR without consulting tempfile's cached
    # resolution of it
    try:
        log_dir = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
    except OSError:
        return 1
    try:
        try:
            ran = subprocess.run(
                [*command, reference, "-i", srt, "-o", srt,
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
            return 1 if quality == "confidence" else 0
        measured_confidence = _CONFIDENCE.search(log_text)
        if measured is not None:
            offset = _OFFSET.search(log_text)
            refused = _REFUSED_OFFSET.search(log_text)
            if offset:
                measured["offset"] = float(offset.group(1))
            elif refused:
                measured["distance"] = float(refused.group(1))
            if measured_confidence:
                measured["confidence"] = float(measured_confidence.group(1))
        if "low-quality alignment" in log_text:
            return 2
        if quality != "confidence":
            return 0
        if not measured_confidence:
            return 1
        return (0 if float(measured_confidence.group(1)) >= MIN_SYNC_CONFIDENCE
                else 2)
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)


def _probe(args: list, file: str) -> dict:
    """ffprobe's JSON answer about a file, or an empty one for a probe that
    failed. The file is named from the root: a relative name whose first
    folder reads ``Name: ...`` is taken by ffprobe for a URL of protocol
    "Name"."""
    try:
        ran = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json",
                              os.path.abspath(file)],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return json.loads(ran.stdout.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return {}


def _frames(stream: dict) -> int:
    """The packet count mkvmerge's statistics tags give a stream, 0 unknown."""
    for key, value in stream.get("tags", {}).items():
        if key.upper().startswith("NUMBER_OF_FRAMES"):
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def _reference_stream(film: str) -> dict | None:
    """The picture subtitle a film's timings are taken from: the one with the
    most packets that is not a commentary's, and not a forced one while there
    is another - a film in a foreign language flags its whole subtitle forced,
    and a forced track that is only the signs is caught by its few cues. None
    when the film has none, or when it carries a text subtitle, which ffsubsync
    reads on its own."""
    streams = _probe(["-select_streams", "s",
                      "-show_entries", "stream=index,codec_name"
                      ":stream_disposition=forced:stream_tags"], film
                     ).get("streams", [])
    if any(s.get("codec_name") not in _PICTURE_CODECS for s in streams):
        return None
    usable = [s for s in streams
              if not languages.is_commentary_name(
                  s.get("tags", {}).get("title", ""))]
    if not usable:
        return None
    # max keeps the first of equals, the stream the film lists first
    return max(usable, key=lambda s: (
        not s.get("disposition", {}).get("forced"), _frames(s)))


def _reference_cues(film: str, stream: dict) -> list:
    """When each line of a picture subtitle is on screen, as (start, end)
    seconds. A line ends where the packet says it does, or - a Blu-ray's, whose
    packets say nothing - where the next packet clears it or replaces it."""
    packets = []
    for packet in _probe(["-select_streams", str(stream.get("index")),
                          "-show_entries", "packet=pts_time,duration_time,size"],
                         film).get("packets", []):
        try:
            start = float(packet["pts_time"])
            size = int(packet.get("size", 0))
        except (KeyError, ValueError):
            continue
        try:
            # "N/A" on a Blu-ray's
            duration = float(packet.get("duration_time", ""))
        except ValueError:
            duration = 0.0
        packets.append((start, duration, size))
    packets.sort()
    pgs = stream.get("codec_name") == "hdmv_pgs_subtitle"
    cues = []
    for number, (start, duration, size) in enumerate(packets):
        if pgs and size <= _PGS_CLEAR_BYTES:
            continue
        if duration > 0:
            cues.append((start, start + duration))
        elif number + 1 < len(packets):
            cues.append((start, packets[number + 1][0]))
    return cues


def _srt_time(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    return "%02d:%02d:%02d,%03d" % (millis // 3600000, millis // 60000 % 60,
                                    millis // 1000 % 60, millis % 1000)


class TimingReference:
    """The timings of a film's own picture subtitle, written out as a SubRip
    file ffsubsync can align against - read off the film the first time a
    subtitle asks for them, since a film nothing is downloaded for should not
    be read through for them. ``path`` is None for a film without one."""

    def __init__(self, film: str) -> None:
        self._film = film
        self._scratch: str | None = None
        self._path: str | None = None
        self._read = False

    def path(self) -> str | None:
        if self._read:
            return self._path
        self._read = True
        stream = _reference_stream(self._film)
        if stream is None:
            return None
        cues = _reference_cues(self._film, stream)
        if len(cues) < MIN_REFERENCE_CUES:
            return None
        try:
            self._scratch = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
            path = os.path.join(self._scratch, "reference.srt")
            with open(path, "w", encoding="utf-8") as handle:
                for number, (start, end) in enumerate(cues, 1):
                    handle.write("%d\n%s --> %s\n-\n\n" % (
                        number, _srt_time(start), _srt_time(end)))
        except OSError:
            return None
        self._path = path
        return path

    def close(self) -> None:
        if self._scratch:
            shutil.rmtree(self._scratch, ignore_errors=True)
        self._scratch = self._path = None

    def __enter__(self) -> TimingReference:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _sync_to_film(file: str, srt: str, timing: str | None, max_offset: str,
                  quality_offset: str, quality: str,
                  measured: dict | None = None) -> int:
    """:func:`sync_subtitle`'s status for a subtitle aligned to its film: to
    the film's picture-subtitle ``timing`` first where there is one, and to its
    audio where there is none or that alignment was refused or failed. The
    first try runs on a copy, so a refusal - after which ffsubsync may already
    have rewritten the file - hands the audio the subtitle as it came.

    ``measured`` is given :func:`sync_subtitle`'s measurement of the alignment
    that decided, and what it was aligned ``against``."""
    if measured is None:
        measured = {}
    if timing:
        try:
            scratch = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        except OSError:
            scratch = None
        if scratch:
            try:
                copy = os.path.join(scratch, os.path.basename(srt))
                shutil.copyfile(srt, copy)
                if sync_subtitle(timing, copy, max_offset, quality_offset,
                                 quality, measured) == 0:
                    shutil.copyfile(copy, srt)
                    measured["against"] = "the film's subtitle track"
                    return 0
            except OSError:
                pass
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
    measured.clear()
    measured["against"] = "the audio"
    return sync_subtitle(file, srt, max_offset, quality_offset, quality,
                         measured)


def _said(language_code: str, text: str) -> str:
    """One subtitle's line under its film's header: the language, and what
    became of it."""
    name = next((row.sub_word for row in languages.LANGUAGES
                 if row.code2 == language_code), language_code)
    return "  {}: {}".format(name, text)


def _alignment(step: str, measured: dict) -> str:
    """"in step" or "out of step", with what the alignment was made against
    and what it came to where ffsubsync said: "in step with the audio
    (shifted +5.0 s, confidence 3.20)"."""
    if measured.get("against"):
        step += " with " + measured["against"]
    detail = []
    if "offset" in measured:
        detail.append("shifted {:+.1f} s".format(measured["offset"]))
    elif "distance" in measured:
        detail.append("best fit {:.1f} s off".format(measured["distance"]))
    if "confidence" in measured:
        detail.append("confidence {:.2f}".format(measured["confidence"]))
    return step + (" ({})".format(", ".join(detail)) if detail else "")


def film_imdb_id(file: str) -> str:
    """The IMDb id a film's name carries, or "" when it carries none.

    Read off the file's own name first and its folder's after it, since the
    tagging names both. A TMDb tag is looked up for the IMDb id it stands for,
    which is the only id OpenSubtitles is searched by.
    """
    for name in (os.path.basename(file),
                 os.path.basename(os.path.dirname(os.path.abspath(file)))):
        imdb = _IMDB_TAG.search(name)
        if imdb:
            return imdb.group(1).lower()
        tmdb = _TMDB_TAG.search(name)
        if tmdb:
            return tmdblookup.imdb_of_tmdb(tmdb.group(1))
    return ""


def _sidecar(file: str, language_code: str) -> str:
    """The ``<movie>.<xx>.srt`` a film's subtitle in one language is named."""
    dot = file.rfind(".")
    stem = file[:dot] if dot != -1 else file
    return "{}.{}.srt".format(stem, language_code)


def _to_subrip(srt: str, language_code: str, file: str,
               log: Callable[[str], None]) -> None:
    """A sidecar some provider labelled ``.srt`` but that ffprobe names as
    another format, converted to real SubRip in place. One that cannot be
    converted is left as it was."""
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
        log(_said(language_code,
                  "converting from {} to SubRip".format(sub_codec)))
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


def download_srt(file: str, language_code: str, user: str, password: str,
                 max_sync_offset: str, max_sync_quality_offset: str,
                 ffsubsync_quality: str,
                 log: Callable[[str], None],
                 timing: TimingReference | None = None) -> None:
    """Fetch one missing subtitle for one movie and one language.

    A sidecar that already exists is left in place - that is the resume
    check, and it looks for exactly the name the deletions below remove, so
    the next run re-downloads what this one threw out. Without credentials
    the download is skipped cleanly. A film whose name carries its id is
    searched for BY that id, and only a subtitle filed under that film is
    taken; one without is searched for by the title its name reads as. A
    sidecar some provider labelled ``.srt`` but that ffprobe names as another
    format is converted to real SubRip first, and a subtitle that cannot be
    synced is discarded rather than kept out of step - whether ffsubsync failed
    outright or the alignment it found was refused. It is synced to ``timing``,
    the film's own picture subtitle, where the film has one (see
    :func:`_sync_to_film`); one is read off the film here when none is handed in.
    """
    srt = _sidecar(file, language_code)
    if os.path.isfile(srt):
        return
    if not user or not password:
        log("WARNING: openSubtitlesUser/openSubtitlesPassword not set, "
            "skipping subtitle download")
        return
    # The credentials travel in the environment, where argv would show them to
    # every account on the machine.
    env = dict(os.environ, openSubtitlesUser=user,
               openSubtitlesPassword=password)
    try:
        subprocess.run(
            ["pipx", "run", helpers.path_of("subliminal_download.py"),
             language_code, film_imdb_id(file), file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except OSError:
        pass
    if not os.path.isfile(srt):
        log(_said(language_code, "none found to download"))
        return

    _to_subrip(srt, language_code, file, log)

    measured: dict = {}
    if timing is None:
        with TimingReference(file) as own:
            status = _sync_to_film(file, srt, own.path(), max_sync_offset,
                                   max_sync_quality_offset, ffsubsync_quality,
                                   measured)
    else:
        status = _sync_to_film(file, srt, timing.path(), max_sync_offset,
                               max_sync_quality_offset, ffsubsync_quality,
                               measured)
    if status == 0:
        log(_said(language_code, "downloaded, {} - synced and kept".format(
            _alignment("in step", measured))))
        return
    if status == 1:
        log(_said(language_code, "WARNING: downloaded, but could not be "
                  "synced - thrown out"))
    else:
        log(_said(language_code, "downloaded, {} - thrown out".format(
            _alignment("out of step", measured))))
    try:
        os.remove(srt)
    except OSError:
        pass


def subtitle_movies(directory: str) -> list:
    """Every movie under the tree that gets subtitles of its own.

    A movie is anything named ``*mkv`` (case-sensitively) anywhere under the
    tree, and one is NOT a movie when its folder's path carries one of the
    extras words - Featurettes, Other, Scenes, Interviews, Shorts, Trailers,
    Extras - the folders that hold the material a film comes with, nor when it
    is the "(old)" copy an improved remux kept: its living sibling is getting
    these same subtitles.
    """
    def spell(rel):
        # how a path under the start point is spelled: a bare "." writes
        # "./name", a trailing slash is not doubled, and anything else joins
        # with a single slash
        if directory == ".":
            return "./" + rel
        if directory.endswith("/"):
            return directory + rel
        return directory + "/" + rel

    movies = []
    for entry in treewalk.entries_below(directory):
        if not entry.name.endswith("mkv"):
            continue
        movie = spell(os.path.relpath(entry.path, directory)
                      .replace(os.sep, "/"))
        slash = movie.rfind("/")
        folder = movie[:slash] if slash != -1 else movie
        if any(word in folder for word in EXTRAS_WORDS):
            continue
        if plexnames.is_kept_copy(movie):
            continue
        movies.append(movie)
    # by name, folder by folder, so a walk that counts its films counts them
    # in an order that can be followed
    return sorted(movies, key=lambda movie: movie.split("/"))


def download_subs(directory: str, user: str, password: str,
                  max_sync_offset: str, max_sync_quality_offset: str,
                  ffsubsync_quality: str,
                  log: Callable[[str], None]) -> None:
    """Do the one-language download for every movie of
    :func:`subtitle_movies` and every language. Sequential by design:
    rapid-fire downloads get throttled.

    Without credentials nothing can be downloaded, which is said once for the
    folder - with how many subtitles that leaves missing - rather than once per
    film and language, and not at all where none is missing.
    """
    if not user or not password:
        missing = sum(1 for movie in subtitle_movies(directory)
                      for row in languages.LANGUAGES
                      if not os.path.isfile(_sidecar(movie, row.code2)))
        if missing:
            log("WARNING: openSubtitlesUser/openSubtitlesPassword not set, "
                "skipping subtitle download ({} missing)".format(missing))
        return
    movies = subtitle_movies(directory)
    for index, movie in enumerate(movies, start=1):
        log(_movie_header(index, len(movies), directory, movie))
        with TimingReference(movie) as timing:
            _download_missing(movie, user, password, max_sync_offset,
                              max_sync_quality_offset, ffsubsync_quality, log,
                              timing)


def _movie_header(index: int, total: int, directory: str, movie: str) -> str:
    """The line each film of a folder's subtitle walk opens with: where it
    stands in the folder, so a film nothing is done to is still named."""
    return "[{}/{}] {}".format(index, total, os.path.relpath(movie, directory))


def _download_missing(movie: str, user: str, password: str,
                      max_sync_offset: str, max_sync_quality_offset: str,
                      ffsubsync_quality: str, log: Callable[[str], None],
                      timing: TimingReference) -> None:
    for row in languages.LANGUAGES:
        download_srt(movie, row.code2, user, password, max_sync_offset,
                     max_sync_quality_offset, ffsubsync_quality, log, timing)


def check_srt(file: str, srt: str, language_code: str, max_sync_offset: str,
              max_sync_quality_offset: str, ffsubsync_quality: str,
              write: bool, log: Callable[[str], None],
              timing: TimingReference | None = None) -> str:
    """Judge one subtitle already beside its film by the alignment a download
    is judged by, and say what it came to: "kept", "discarded" or "untested".

    With ``write`` the verdict is carried out the way :func:`download_srt`
    carries it out - converted to SubRip, synced in place, and thrown out when
    the alignment is refused. Without it the same test runs on a copy, and the
    sidecar is not touched. One ffsubsync could not align at all is left alone
    either way: unlike a download it may be a subtitle that cannot be fetched
    again, and a tool that failed says nothing about whether it is in step.
    """
    if timing is None:
        with TimingReference(file) as own:
            return check_srt(file, srt, language_code, max_sync_offset,
                             max_sync_quality_offset, ffsubsync_quality,
                             write, log, own)
    measured: dict = {}
    if write:
        _to_subrip(srt, language_code, file, log)
        status = _sync_to_film(file, srt, timing.path(), max_sync_offset,
                               max_sync_quality_offset, ffsubsync_quality,
                               measured)
    else:
        status = _test_copy(file, srt, language_code, max_sync_offset,
                            max_sync_quality_offset, ffsubsync_quality,
                            timing.path(), measured)

    if status == 1:
        log(_said(language_code, "WARNING: could not be tested - left alone"))
        return "untested"
    if status == 2:
        log(_said(language_code, "{} - {}".format(
            _alignment("out of step", measured),
            "thrown out" if write else "would be thrown out")))
        if write:
            try:
                os.remove(srt)
            except OSError:
                pass
        return "discarded"
    log(_said(language_code, "{} - {}".format(
        _alignment("in step", measured),
        "synced and kept" if write else "would be kept")))
    return "kept"


def _test_copy(file: str, srt: str, language_code: str, max_sync_offset: str,
               max_sync_quality_offset: str, ffsubsync_quality: str,
               reference: str | None, measured: dict) -> int:
    """:func:`_sync_to_film`'s status for a copy of the sidecar, made the way
    the real one would be synced - converted to SubRip first - so the dry run
    gives the verdict the real run would, and leaves the sidecar as it was."""
    try:
        scratch = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
    except OSError:
        return 1
    try:
        copy = os.path.join(scratch, os.path.basename(srt))
        try:
            shutil.copyfile(srt, copy)
        except OSError:
            return 1
        _to_subrip(copy, language_code, file, lambda _line: None)
        return _sync_to_film(file, copy, reference, max_sync_offset,
                             max_sync_quality_offset, ffsubsync_quality,
                             measured)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check_subs(directory: str, max_sync_offset: str,
               max_sync_quality_offset: str, ffsubsync_quality: str,
               write: bool, log: Callable[[str], None],
               credentials: tuple | None = None) -> dict:
    """:func:`check_srt` for every ``<movie>.<xx>.srt`` beside every movie of
    :func:`subtitle_movies`, in every language of the table - the sidecars a
    download would have written, and so never a commentary transcript, whose
    name carries the track number after the movie's.

    With ``credentials`` - OpenSubtitles' (user, password) - each film then has
    what is missing downloaded before the walk goes on to the next, so one
    thrown out is fetched again straight away.

    Returns the sidecars by verdict: ``{"kept": [...], "discarded": [...],
    "untested": [...]}``.
    """
    verdicts: dict = {"kept": [], "discarded": [], "untested": []}
    movies = subtitle_movies(directory)
    for index, movie in enumerate(movies, start=1):
        log(_movie_header(index, len(movies), directory, movie))
        with TimingReference(movie) as timing:
            for row in languages.LANGUAGES:
                srt = _sidecar(movie, row.code2)
                if os.path.islink(srt) or not os.path.isfile(srt):
                    continue
                verdict = check_srt(movie, srt, row.code2, max_sync_offset,
                                    max_sync_quality_offset, ffsubsync_quality,
                                    write, log, timing)
                verdicts[verdict].append(srt)
            if credentials:
                user, password = credentials
                _download_missing(movie, user, password, max_sync_offset,
                                  max_sync_quality_offset, ffsubsync_quality,
                                  log, timing)
    return verdicts
