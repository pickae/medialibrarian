"""ingest-music: a folder of freshly downloaded music, ingested into a clean
lossless library.

Every lossless source is re-encoded to a normalised FLAC, everything else is
copied across, large cover images become AVIF, stray videos are remuxed to MKV,
cue sheets give up their chapters into the flacs they describe, and beets and
`clean-folder-structure` finish the names off. The download tree itself is never
renamed.

Running the same download folder again adds only what is new, which takes some
doing: the last run ENDED by cleaning the library's names, so nothing in there is
called what this script called it. Three things survive that.

  * Every ingested flac records the download it came from, so a track already in
    the library is recognised whatever it is called now.
  * Those same records say which FOLDER a release lives in now, so this run's
    copies and cover art are written there rather than into a second folder named
    after the download.
  * A sidecar or a cover carries no such tag, so one the library already holds
    under a cleaned name is recognised by its content instead.

Without all three the library doubles on every run.
"""

import os
import re
import shutil
import subprocess
import sys
import time

from medialib import commands
from medialib.lib import (
    clioptions,
    cuechapters,
    enums,
    ffmpegselect,
    formatting,
    imagemagick,
    imagesizes,
    mutagentags,
    ramscratch,
    runlog,
    safety,
    statusline,
    tooldeps,
    workerpool,
)
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <downloadDir> <ingestDir> [opusCopyDir]

    downloadDir   directory holding the freshly downloaded music to ingest
    ingestDir     directory the cleaned/encoded library is written to
                  (created if it does not exist)
    opusCopyDir   directory the 120 kbps Opus copies are written to
                  (optional; defaults to the sibling folder "<ingestDir>opus")

Options:"""

OPT_SPEC = """
h |  | Print this help page.
j | <jobs> | Run up to <jobs> encoder processes in parallel
                  (default: the number of CPU cores).
"""

OPT_VARS = "j:jobs"
OPT_COLUMN = 18
OPT_LONG = "h:help j:jobs"

USAGE_TAIL = """

Dependencies:
    ffmpeg, mkvtoolnix, ImageMagick, rsync, fdupes, beets, and the mutagen
    package for this Python (chapter embedding). Optional: flock (numbers the
    progress lines). Also invokes the sibling commands convert-audio,
    clean-folder-structure and cue-to-chapters."""

# Cover-image conversion: only images of at least this size are re-encoded to
# AVIF (smaller ones are copied unchanged), and the AVIF encode settings.
COVER_MIN_BYTES = 1000000
COVER_AVIF_DEPTH = "10"
COVER_AVIF_QUALITY = "35"
COVER_AVIF_SPEED = "2"

# Flac parameters.
HEARING_THRESHOLD = 48000
EXPERIMENTAL_FLAC_LEVEL = "12"

# The Vorbis comment every ingested flac carries, holding the path (relative to
# the download folder) of the track it was made from. Upper case because that is
# the Vorbis-comment convention, and its own name because the two sides of that
# round trip must spell it identically.
INGEST_SOURCE_TAG = "INGESTSOURCE"

# ImageMagick spends about four threads on one image whatever we do.
IMAGE_THREADS_PER_CONVERSION = 4

# The Opus copies the run ends with.
OPUS_COPY_BITRATE = "120"

# One chapter is a CHAPTERnn= line plus its CHAPTERnnNAME= line, so a chapter
# list shorter than this holds no complete chapter.
LINES_PER_CHAPTER = 2

# The stray video containers remuxed into Matroska. Matched on the bare suffix
# the way the shell's `-name '*avi'` patterns are, which also catch a name
# ending in those letters without a dot.
STRAY_VIDEO_SUFFIXES = ("avi", "mp4", "m4v", "flv", "flv2", "m3u8", "mov",
                        "webm", "mpg")

_CHAPTER_START = re.compile(r"^CHAPTER[0-9]+=(.+)$")


def spec(program: str) -> "clioptions.Spec":
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
        tail=USAGE_TAIL,
    )


# --- the pure questions -------------------------------------------------------

def is_lossless_codec(codec: str) -> bool:
    """``isLosslessCodec``: is this codec, as ffprobe spells it, one of the
    lossless ones this script re-encodes?"""
    return enums.shell_lower(codec) in enums.LOSSLESS_CODECS


def chapter_time_ms(timestamp: str) -> int:
    """``chapterTimeMs``: an OGM "H:MM:SS.mmm" timestamp as whole milliseconds.

    The shell reads each field with a base-10 prefix, because "08" is not octal
    here, and computes with its own unit factors rather than the matching ones in
    the cue library - the value must not depend on an ambient global.
    """
    hours, _, rest = timestamp.partition(":")
    minutes, _, seconds_and_ms = rest.rpartition(":")
    seconds, _, milliseconds = seconds_and_ms.partition(".")
    if not milliseconds:
        milliseconds = seconds
    return (_base_ten(hours) * 3600000 + _base_ten(minutes) * 60000
            + _base_ten(seconds) * 1000 + _base_ten(milliseconds))


def _base_ten(field: str) -> int:
    """The shell's ``10#$field`` over one timestamp field."""
    try:
        return int(field, 10)
    except ValueError:
        return 0


def max_chapter_ms(chapter_lines: list) -> int:
    """``maxChapterMs``: the largest chapter START in a chapter list."""
    largest = 0
    for line in chapter_lines:
        found = _CHAPTER_START.match(line)
        if not found:
            continue
        largest = max(largest, chapter_time_ms(found.group(1)))
    return largest


def _one_level(directory: str, matches) -> list:
    """The files directly in ``directory`` whose name ``matches``, in the order
    ``find`` walks them."""
    try:
        with os.scandir(directory) as entries:
            found = [entry.path for entry in entries
                     if entry.is_file(follow_symlinks=False)
                     and matches(entry.name)]
    except OSError:
        return []
    return found


def flac_for_cue(cue: str) -> str:
    """``flacForCue``: the flac a cue sheet belongs to, or "" when undecidable.

    Its own name first; failing that, a folder holding exactly ONE flac and ONE
    cue leaves nothing to be ambiguous about.
    """
    directory = os.path.dirname(cue) or "."
    stem = enums.shell_lower(os.path.splitext(os.path.basename(cue))[0])

    sibling = _one_level(
        directory,
        lambda name: enums.shell_lower(name) == stem + ".flac")
    if sibling:
        return sorted(sibling)[0]

    flacs = _one_level(directory,
                       lambda name: enums.lower_extension_of(name) == "flac")
    cues = _one_level(directory,
                      lambda name: enums.lower_extension_of(name) == "cue")
    if len(flacs) == 1 and len(cues) == 1:
        return flacs[0]
    return ""


def has_twin_in(path: str, directory: str) -> bool:
    """``hasTwinIn``: does ``directory`` already hold, under some other name, a
    file with exactly these bytes?

    The other half of the problem the folder map solves. A run cleans the names
    of what it COPIED as well as of what it encoded, so the next run's copy of
    the same sidecar matches nothing by name and lands beside it as a second
    copy. A flac says what it came from; a rip log or a booklet scan carries no
    tag to say it, so what identifies it is what it IS.

    Size first and only then a comparison, so the read happens for the one
    candidate that could match rather than for every file in the folder.
    """
    try:
        size = os.stat(path).st_size
    except OSError:
        return False
    for candidate in _one_level(directory, lambda name: True):
        try:
            if os.path.samefile(candidate, path):
                continue
            if os.stat(candidate).st_size != size:
                continue
        except OSError:
            continue
        if _same_bytes(candidate, path):
            return True
    return False


def _same_bytes(left: str, right: str) -> bool:
    """``cmp -s``, which answers no rather than raising for a file it cannot
    read."""
    try:
        with open(left, "rb") as one, open(right, "rb") as two:
            while True:
                block_one = one.read(65536)
                block_two = two.read(65536)
                if block_one != block_two:
                    return False
                if not block_one:
                    return True
    except OSError:
        return False


def delete_unneeded_cue(root: str, counters=None) -> int:
    """``deleteUnneededCue``: drop every cue sheet whose companion flac is not
    beside it.

    Only worth asking after the encoding and the chapter embedding have both run:
    the copy brings every cue across before a single flac has been encoded, so
    pruning them where the copy happens threw away every cue in the release -
    each one an orphan at that moment.
    """
    dropped = 0
    for parent, _dirs, names in os.walk(root):
        for name in sorted(names):
            if not name.endswith(".cue"):
                continue
            cue = os.path.join(parent, name)
            if os.path.isfile(os.path.splitext(cue)[0] + ".flac"):
                continue
            try:
                os.remove(cue)
            except OSError:
                continue
            dropped += 1
            if counters is not None:
                counters.bump("cuesDropped")
    return dropped


# --- writing where the library actually keeps things --------------------------
# Everything except the flacs is addressed by its path in the DOWNLOAD, and the
# flacs are what know better: each records the download it came from, so the
# folder a flac lives in IS the folder that download folder became.

def read_folder_map(path: str) -> dict:
    """The finished answer, as the workers read it: a download-relative folder to
    the folder in the library that holds what was ingested from it, listed only
    where the two differ."""
    try:
        with open(path, "rb") as handle:
            fields = handle.read().split(b"\0")
    except OSError:
        return {}
    pairs = {}
    for index in range(0, len(fields) - 1, 2):
        pairs[fields[index].decode("utf-8", "surrogateescape")] = \
            fields[index + 1].decode("utf-8", "surrogateescape")
    return pairs


def resolve_ingested_dir(wanted: str, folder_map: dict) -> str:
    """``resolveIngestedDir``: where this download folder's files belong, as a
    path relative to the library ("." for its root).

    The same path it was given whenever nothing is known about it, so an unmapped
    run behaves exactly as it did before.
    """
    return folder_map.get(wanted, wanted)


def ingest_path_for(relative: str, ingest_dir: str, folder_map: dict,
                    new_extension: str = "") -> str:
    """``ingestPathFor``: where one download file's output belongs in the library
    - its folder resolved as above, its name kept, or its extension replaced for
    the encoder's .flac and the cover's .avif.

    Checked, because this destination is WORKED OUT rather than walked to: the
    folder comes from a map learnt from a file the last run wrote, and every
    encode, copy and rename in the ingest lands where this says.
    """
    directory = os.path.dirname(relative) or "."
    name = os.path.basename(relative)
    if new_extension:
        name = os.path.splitext(name)[0] + "." + new_extension
    directory = resolve_ingested_dir(directory, folder_map)
    if directory == ".":
        target = os.path.join(ingest_dir, name)
    else:
        target = os.path.join(ingest_dir, directory, name)
    return safety.assert_within(ingest_dir, target, "ingest path")


def ingested_dir_path(relative: str, ingest_dir: str, folder_map: dict) -> str:
    """The library folder one download folder's files belong in, checked the same
    way and for the same reason."""
    return safety.assert_within(
        ingest_dir,
        os.path.join(ingest_dir, resolve_ingested_dir(relative, folder_map)),
        "ingest folder")


def build_ingested_folder_map(pairs_file: str, ingest_dir: str) -> tuple:
    """``buildIngestedFolderMap``: what each download folder became, from the
    direct evidence - the flacs ingested out of it and the folder they are in.

    A folder whose tracks are spread over SEVERAL library folders is recorded as
    ambiguous rather than resolved to one of them: someone has reorganised that
    album by hand, and which folder its rip log now belongs to is not this
    script's guess to make.
    """
    became: dict[str, str] = {}
    ambiguous: set[str] = set()
    try:
        with open(pairs_file, "rb") as handle:
            fields = handle.read().split(b"\0")
    except OSError:
        return became, ambiguous

    for index in range(0, len(fields) - 1, 2):
        source = fields[index].decode("utf-8", "surrogateescape")
        library = fields[index + 1].decode("utf-8", "surrogateescape")
        source_dir = os.path.dirname(source) or "."
        library_dir = os.path.dirname(library)
        # is_within and not startswith: a prefix with no separator under it
        # reads "/srv/musicExtra" as a folder inside "/srv/music".
        if safety.is_within(ingest_dir, library_dir):
            library_dir = os.path.relpath(library_dir, ingest_dir)
        library_dir = library_dir.lstrip("/") or "."
        if source_dir == library_dir:
            continue
        if source_dir in became and became[source_dir] != library_dir:
            ambiguous.add(source_dir)
        else:
            became[source_dir] = library_dir
    return became, ambiguous


def resolve_through_ancestors(wanted: str, became: dict, ambiguous: set) -> str:
    """``resolveThroughAncestors``: the same question for a folder that holds no
    flac of its own.

    A "Scans" subfolder of an album that WAS renamed belongs under the renamed
    album, so the nearest mapped ancestor decides and the path below it is
    carried across unchanged.
    """
    current, suffix = wanted, ""
    while True:
        if current in became and current not in ambiguous:
            base = became[current]
            if base == ".":
                base = ""
            return (base + suffix) or "."
        if current == ".":
            return wanted
        suffix = "/" + os.path.basename(current) + suffix
        current = os.path.dirname(current) or "."


def _download_folders(download_dir: str) -> list:
    """"." and every folder below the download tree, the way the shell's
    ``find -mindepth 1 -type d`` prints them: relative, parents first."""
    folders = ["."]
    for parent, dirs, _names in os.walk(download_dir):
        dirs.sort()
        for name in dirs:
            folders.append(os.path.relpath(os.path.join(parent, name),
                                           download_dir))
    return folders


def write_folder_map(download_dir: str, map_file: str, became: dict,
                     ambiguous: set) -> int:
    """``writeFolderMap``: the finished answer for EVERY folder in the download
    tree, so the workers only ever match a path rather than walk up it - and only
    the folders that resolve somewhere else, so an ordinary first run leaves the
    file empty and every path stays exactly what it was."""
    redirects = 0
    with open(map_file, "wb") as handle:
        if not became:
            return 0
        for relative in _download_folders(download_dir):
            resolved = resolve_through_ancestors(relative, became, ambiguous)
            if resolved == relative:
                continue
            handle.write(relative.encode("utf-8", "surrogateescape") + b"\0")
            handle.write(resolved.encode("utf-8", "surrogateescape") + b"\0")
            redirects += 1
            log('  "%s" is "%s" in the library by now - writing there'
                % (relative, resolved))
    return redirects


# --- what the run says while it works -----------------------------------------

class Counters:
    """The shared counters, the per-item lines and the live status row.

    All three are files rather than variables, because the encode and cover
    phases run as worker PROCESSES whose tallies would otherwise die with them.
    The queue position is per PHASE: this script's phases count different things
    (tracks, images, cue sheets, videos), and one counter running across all of
    them would put a track and a cover in the same numbering.
    """

    NAMES = ("current", "encoded", "upToDate", "notLossless", "failed",
             "coverAvif", "coverCopied", "coverAlreadyThere",
             "copiesAlreadyThere", "chaptersEmbedded", "chaptersSkipped",
             "chaptersFailed", "videosRemuxed", "videosFailed", "discsSplit",
             "alacRenamed", "cuesDropped", "folderRedirects")

    def __init__(self, counter_dir: str, run_start_epoch: int) -> None:
        self.counter_dir = counter_dir
        self.run_start_epoch = run_start_epoch
        self.progress_file = os.path.join(counter_dir, "current")
        self.duration_file = os.path.join(counter_dir, "duration")
        self.lock_file = os.path.join(counter_dir, "lock")
        self.label = ""
        self.unit = ""
        self.total = 0
        self.shows_speed = False

    def create(self) -> None:
        """The tallies, up front, so the closing report can be asked for figures
        from a phase a Ctrl+C stopped the run before."""
        for name in self.NAMES:
            self._write(os.path.join(self.counter_dir, name), "0")
        self._write(self.duration_file, "0\n")

    @staticmethod
    def _write(path: str, text: str) -> None:
        with open(path, "w") as handle:
            handle.write(text)

    def _read(self, path: str, default: str = "0") -> str:
        try:
            with open(path) as handle:
                return handle.read().strip() or default
        except OSError:
            return default

    def start_phase(self, label: str, unit: str, total: int,
                    shows_speed: bool = False) -> None:
        self.label, self.unit = label, unit
        self.total, self.shows_speed = total, shows_speed
        self._write(self.progress_file, "0")
        statusline.start_status_monitor(self.lock_file, self.status_text)

    def end_phase(self) -> None:
        """Stop refreshing the row and leave it behind as this phase's last
        word."""
        statusline.stop_status_monitor()

    def progress(self, line: str) -> None:
        """One counted line, printed under the same lock as the row it displaces.

        On stderr, next to the log lines and the row rather than on stdout: this
        run's account of itself is one interleaved narrative, and splitting it
        over two streams would put the headings on the terminal and the items in
        the file.

        The position counts items STARTED, so an encode announces itself before
        the minutes it takes rather than after them.
        """
        with open(self.lock_file, "w") as lock, runlog.take_lock(lock):
            current = self._bump_position()
            statusline.clear_status()
            sys.stderr.write("%s%s\n" % (
                runlog.counted_prefix(current, self.total), line))
            sys.stderr.flush()
            statusline.repin_status(self.status_text)

    def advance(self) -> None:
        """Bump the position WITHOUT printing, for a phase whose items are too
        many and too quick to deserve a line each - the provenance scan is one
        ffprobe per flac in a library that can hold tens of thousands."""
        with open(self.lock_file, "w") as lock, runlog.take_lock(lock):
            self._bump_position()

    def note(self, line: str) -> None:
        """One UNCOUNTED line about an item that has already announced itself -
        the warning a failed encode has to give afterwards. Indented, because by
        then the line it belongs to can be several items up the screen."""
        with open(self.lock_file, "w") as lock, runlog.take_lock(lock):
            statusline.clear_status()
            sys.stderr.write("    %s\n" % line)
            sys.stderr.flush()
            statusline.repin_status(self.status_text)

    def _bump_position(self) -> int:
        try:
            current = int(self._read(self.progress_file)) + 1
        except ValueError:
            current = 1
        self._write(self.progress_file, str(current))
        return current

    def bump(self, name: str, amount: int = 1) -> None:
        """Add to one of the tallies. Its own lock per counter, so a worker
        recording an outcome never waits on the console lock."""
        path = os.path.join(self.counter_dir, name)
        with open(path + ".lock", "w") as lock, runlog.take_lock(lock):
            try:
                value = int(self._read(path)) + amount
            except ValueError:
                value = amount
            self._write(path, str(value))

    def tally_duration(self, add) -> None:
        """Encoded audio-seconds, tallied as tracks finish rather than probed at
        the end, so a re-run that skipped 90 of 100 already-ingested tracks
        reports the speed of the work IT did."""
        if not add:
            return
        with open(self.duration_file + ".lock", "w") as lock, \
                runlog.take_lock(lock):
            current = formatting.awk_number(self._read(self.duration_file))
            self._write(self.duration_file,
                        "%.3f\n" % (current + formatting.awk_number(add)))

    def value(self, name: str) -> int:
        """One tally, for the closing report. A counter file a phase never
        reached reads as 0: the report is named above every point the run can be
        stopped, so it can be asked for figures from before the phase that
        produces them."""
        try:
            return int(self._read(os.path.join(self.counter_dir, name)))
        except ValueError:
            return 0

    def status_text(self) -> str:
        """ONE line saying how the RUN is doing, pinned under the per-item lines.

        Elapsed is measured from the start of the run, not of the phase, so the
        figure converges on the one the closing report divides by instead of
        dropping at the end. The encoded-audio and speed-up fields are shown only
        where they mean something: during the cover, chapter and remux phases
        nothing is being encoded, and a speed-up frozen at whatever the encoder
        last reached would read as a claim about work that is not happening.

        No ETA, deliberately: the items left are tracks and albums of unknown
        length, so extrapolating from a position in the queue would be inventing
        a number.
        """
        try:
            current = int(self._read(self.progress_file))
        except ValueError:
            current = 0
        elapsed = max(0, int(time.time()) - self.run_start_epoch)

        # Without flock the counter can lose increments, so the row says how much
        # work there is without claiming a position in it.
        if runlog.have_flock():
            position = "%d/%d %s" % (current, self.total, self.unit)
        else:
            position = "%d %s" % (self.total, self.unit)

        if not self.shows_speed:
            return "  %s %s: elapsed %s" % (self.label, position,
                                            formatting.fmt_clock(elapsed))

        with open(self.duration_file + ".lock", "w") as lock, \
                runlog.take_lock(lock):
            duration = formatting.awk_number(self._read(self.duration_file))
        speed = formatting.fmt_ratio(
            "%.6f" % (duration / elapsed) if elapsed > 0 else "0")
        return "  %s %s: elapsed %s  encoded %s  %sx realtime" % (
            self.label, position, formatting.fmt_clock(elapsed),
            formatting.fmt_clock("%.3f" % duration), speed)


# --- the work -----------------------------------------------------------------

class Run:
    """One run's settings, and the work its queue jobs do."""

    # Declared, not defaulted: the settings dict supplies every one, so a name
    # it does not carry is still an AttributeError at the read.
    download_dir: str
    ingest_dir: str
    script_dir: str
    counters: "Counters"
    skips: safety.RunSkipLog
    # Where each download folder's tracks actually landed, filled in as the
    # library is read.
    folder_map: dict
    lossless_total: int
    # A number, not None: DEFAULT_TIER is a row of the table by construction.
    cover_max_edge: str
    ingested_sources: str
    ingested_pairs: str
    ram_base: str
    # The run's clocks, each unset until the phase it times has begun.
    run_start: float
    run_end: float | None
    encode_start: float | None
    encode_end: float | None

    def __init__(self, **settings) -> None:
        self.__dict__.update(settings)

    # --- the library as it stands --------------------------------------------

    def read_ingest_source(self, flac: str) -> None:
        """``readIngestSource``: what one flac already in the library was made
        from, read back out of the tag the encoder wrote into it.

        A flac without the tag - ingested before it was recorded, or added by
        hand - contributes nothing and falls back to the check by name. The
        appends are serialised: a lost record would only cost a needless
        re-encode, but a half-written one could match the wrong track.
        """
        source = _probe(["ffprobe", "-v", "quiet", "-show_entries",
                         "format_tags=" + INGEST_SOURCE_TAG,
                         "-of", "default=nk=1:nw=1", flac]).strip()
        if source:
            with open(self.ingested_sources + ".lock", "w") as lock, \
                    runlog.take_lock(lock):
                with open(self.ingested_sources, "ab") as handle:
                    handle.write(_bytes(source) + b"\0")
                with open(self.ingested_pairs, "ab") as handle:
                    handle.write(_bytes(source) + b"\0" + _bytes(flac) + b"\0")
        self.counters.advance()

    # --- encoding -------------------------------------------------------------

    def encode(self, relative: str) -> None:
        """One lossless track re-encoded into a normalised FLAC: 16-bit samples,
        capped at 48 kHz, any embedded cover re-encoded as MJPEG and scaled.

        Every track leaves exactly ONE counted line behind, on all four paths
        through here - encoded, already up to date, not lossless after all, or
        failed - so the phase counter reaches its total and the closing
        categories add up to it.
        """
        output = ingest_path_for(relative, self.ingest_dir, self.folder_map,
                                 "flac")
        source = os.path.join(self.download_dir, relative)

        # Resume, on PROVENANCE rather than on names: this is what survives the
        # ingested copy being renamed by the end of the last run. First, and
        # before the probes, so a re-run over a finished library costs one lookup
        # per track instead of four ffprobe processes.
        if self._already_ingested(relative):
            self.counters.bump("upToDate")
            self.counters.progress("Skip (already ingested): " + relative)
            return

        input_duration = _duration_seconds(source)
        output_duration = _duration_seconds(output)
        codec, rate = _codec_and_rate(source)

        if not is_lossless_codec(codec):
            # A lossless extension over something else - a wav holding mp3 data,
            # an ape that is really a tag dump. Not encoded, and the copy
            # excludes those extensions, so it is not carried across either.
            # Worth a line for exactly that reason: it is the one input a run
            # silently leaves behind.
            self.counters.bump("notLossless")
            self.counters.progress("Skip (not lossless, %s): %s"
                                   % (codec, relative))
            return

        # And the same check by name, for tracks ingested before provenance was
        # recorded: no tag to go by, but a flac of at least the source's length
        # under the expected name is still an ingested track.
        if os.path.isfile(output) and input_duration <= output_duration:
            self.counters.bump("upToDate")
            self.counters.progress("Skip (already ingested): " + relative)
            return

        # Ultrasound music files are useless.
        rate = min(rate, HEARING_THRESHOLD) if rate else HEARING_THRESHOLD

        # Announced before the encode, not after it: a single track is minutes of
        # work, and a line that only appears once it is over says nothing about
        # what the run is doing right now.
        self.counters.progress("Encoding: " + relative)

        scale = ("scale='min(%s,iw)':min'(%s,ih)'"
                 ":force_original_aspect_ratio=decrease,format=yuvj420p"
                 % (self.cover_max_edge, self.cover_max_edge))
        argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-y", "-i", source, "-map", "0:a:0", "-map", "0:v:0?",
                "-filter:v", scale, "-c:v", "mjpeg", "-c:a", "flac",
                "-sample_fmt", "s16", "-ar", str(rate),
                "-compression_level", EXPERIMENTAL_FLAC_LEVEL,
                # The provenance the resume check above reads. It survives what
                # happens to the file afterwards - the mutagen chapter write,
                # beets' tag write, and any rename - because all three edit tags
                # they know and leave the rest of the comment block alone.
                "-metadata", "%s=%s" % (INGEST_SOURCE_TAG, relative),
                "-f", "flac", output]
        if _run(argv) == 0 and _copy_mtime(source, output):
            self.counters.bump("encoded")
            # Only counted when the encode landed, so the figure stays the rate
            # of real work.
            self.counters.tally_duration(input_duration)
        else:
            self.counters.bump("failed")
            self.counters.note("WARNING: ffmpeg could not encode this track, "
                               "no flac written: " + relative)

    def _already_ingested(self, relative: str) -> bool:
        """The shell's ``grep -qzxF``: one whole NUL-terminated record equal to
        this path."""
        try:
            with open(self.ingested_sources, "rb") as handle:
                records = handle.read().split(b"\0")
        except OSError:
            return False
        return _bytes(relative) in records

    # --- cover art ------------------------------------------------------------

    def convert_image(self, relative: str) -> None:
        """One cover image to AVIF when it is large enough to be worth it and no
        output exists yet; otherwise copied across unchanged. A failed conversion
        falls back to a plain copy so no art is lost.

        One counted line per image, saying which of the two happened - the AVIF
        encode is seconds of work per file at this speed preset, so a folder of
        scans is a phase with a visible position in it rather than a pause.
        """
        source = os.path.join(self.download_dir, relative)
        output = ingest_path_for(relative, self.ingest_dir, self.folder_map,
                                 "avif")
        copy = ingest_path_for(relative, self.ingest_dir, self.folder_map)
        cover_dir = os.path.dirname(copy)
        try:
            size = os.stat(source).st_size
        except OSError:
            size = 0

        if size < COVER_MIN_BYTES:
            # Unless the library already holds these very bytes. It usually holds
            # them under another name: a cover copied by an earlier run is
            # "folder.png" by now, so a check by name alone would copy it a
            # second time on every run.
            if os.path.isfile(copy) or has_twin_in(source, cover_dir):
                self.counters.bump("coverAlreadyThere")
                self.counters.progress("Cover already in the library: "
                                       + relative)
            else:
                self.counters.bump("coverCopied")
                self.counters.progress("Cover copied as it is: " + relative)
                _copy(source, copy)
            return

        if os.path.isfile(output):
            self.counters.bump("coverAlreadyThere")
            self.counters.progress("Cover already converted: " + relative)
            return

        self.counters.progress("Cover to AVIF (%s): %s"
                               % (formatting.fmt_bytes(size), relative))
        argv = imagemagick.convert_argv(
            ["-format", "avif", "-depth", COVER_AVIF_DEPTH,
             "-quality", COVER_AVIF_QUALITY,
             "-define", "heic:speed=" + COVER_AVIF_SPEED, source, output])
        # Counted by what the image ENDED UP as, not by what was attempted: the
        # fall-back copy is a copy, and a tally that called it an AVIF would say
        # the library holds art it does not.
        if _run(argv) == 0:
            # The encode is a pure function of the source and these settings, so
            # an AVIF the library already holds under a cleaned name is byte for
            # byte this one: keep theirs, drop ours. The encode itself cannot be
            # skipped that way - what it will produce is only known once it has.
            if has_twin_in(output, cover_dir):
                _remove(output)
                self.counters.bump("coverAlreadyThere")
            else:
                self.counters.bump("coverAvif")
        else:
            self.counters.bump("coverCopied")
            self.counters.note("WARNING: the AVIF encode failed, copying the "
                               "image as it is: " + relative)
            _copy(source, copy)

    # --- the serial phases ----------------------------------------------------

    def mux_video(self, files: list) -> None:
        """``muxVideo``: stray video files remuxed into Matroska, replacing the
        original on success so the library holds a single video format."""
        for path in files:
            relative = _relative_to(path, self.ingest_dir)
            self.counters.progress("Remuxing to Matroska: " + relative)
            target = os.path.splitext(path)[0] + ".mkv"
            if _run(["mkvmerge", "-q", "-o", target, path]) == 0 \
                    and _remove_tree(path):
                self.counters.bump("videosRemuxed")
            else:
                self.counters.bump("videosFailed")
                self.counters.note("WARNING: mkvmerge could not remux this "
                                   "file, left as it is: " + relative)

    def embed_cue_chapters(self, cues: list) -> None:
        """``embedCueChapters``: the chapters a cue sheet describes, written into
        the flac it belongs to.

        Every cue leaves one counted line behind, including the four ways it can
        be declined, so a release whose cue names a disc that never got copied
        does not come out without chapters and without a word about it.
        """
        for cue in cues:
            relative = _relative_to(cue, self.ingest_dir)

            flac = flac_for_cue(cue)
            if not flac:
                self.counters.bump("chaptersSkipped")
                self.counters.progress(
                    "Skip (no flac this cue could belong to): " + relative)
                continue

            lines = cuechapters.chapters_from_cue(cue)
            if len(lines) < LINES_PER_CHAPTER:
                self.counters.bump("chaptersSkipped")
                self.counters.progress("Skip (no chapter in the cue): "
                                       + relative)
                continue

            duration_ms = _duration_ms(flac)
            if duration_ms <= 0:
                self.counters.bump("chaptersSkipped")
                self.counters.progress("Skip (flac reports no duration): "
                                       + relative)
                continue
            # Never write a chapter set that starts at or after the real
            # duration: that cue describes a different disc.
            if max_chapter_ms(lines) >= duration_ms:
                self.counters.bump("chaptersSkipped")
                self.counters.progress(
                    "Skip (chapters start past the flac's end, wrong disc): "
                    + relative)
                continue

            title = os.path.splitext(os.path.basename(flac))[0]
            self.counters.progress("Chapters (%d): %s"
                                   % (len(lines) // LINES_PER_CHAPTER,
                                      relative))
            chapter_file, _status = ramscratch.ram_scratch_file(
                "ingestMusic.chapters")
            with open(chapter_file, "w") as handle:
                handle.write("\n".join(lines) + "\n")
            # --force: the cue is what the release says its chapters are.
            written = mutagentags.embed_chapters(flac, chapter_file, title,
                                                 force=True) == 0
            if written:
                self.counters.bump("chaptersEmbedded")
            else:
                self.counters.bump("chaptersFailed")
                self.counters.note(
                    "WARNING: the chapter marks could not be written into "
                    + _relative_to(flac, self.ingest_dir))
            _remove(chapter_file)

    def rename_alac(self, root: str) -> None:
        """``renameAlac``: Apple Music files carry .m4a whether their codec is
        lossy AAC or lossless ALAC, so the lossless ones are renamed up front and
        the later copy hands them to the encoder instead of carrying them across
        as if they were lossy.

        One line per RENAME rather than per m4a examined: a lossy m4a is the
        ordinary case and is meant to be copied, while a file that changed its
        extension before the encoder ever saw it is the kind of thing a run
        should say out loud.
        """
        for path in _find_files(root, lambda name:
                                enums.lower_extension_of(name) == "m4a"):
            codec, _rate = _codec_and_rate(path)
            if codec != "alac":
                continue
            target = os.path.splitext(path)[0] + ".flac"
            if safety.safe_rename(path, target, self.skips):
                self.counters.bump("alacRenamed")
                log("  Lossless m4a (ALAC) renamed for the encoder: "
                    + _relative_to(path, self.download_dir))

    def split_disc_images(self, root: str) -> None:
        """``splittableImagesToSubfolders``: a folder holding several CD images
        (image + CUE per disc) gets one subfolder per disc, so downstream tools
        treat them as separate albums.

        Redundant CUE sidecars are dropped and byte-identical duplicates removed
        first.
        """
        # What a cue sheet's disc image can be: any of the lossless containers,
        # plus the ALAC .m4a - this runs BEFORE renameAlac, so an ALAC image
        # still wears the extension it arrived with.
        image_extensions = list(enums.LOSSLESS_AUDIO_EXTENSIONS) + ["m4a"]

        for path in _find_files(root, lambda name: name.endswith(".ape.cue")
                                or name.endswith(".wav.cue")):
            _remove(path)

        # Silenced, like every other fdupes call in this repo: it prints a
        # paragraph per duplicate set it deletes, before the run has said what it
        # is doing at all.
        log("  De-duplicating byte-identical files (fdupes)")
        _run(["fdupes", "-q", "-rdN", root])

        for path in _find_files(root, lambda name: name.endswith(".flac.cue")):
            _force_rename(path, os.path.splitext(os.path.splitext(path)[0])[0]
                          + ".cue")

        for cue in _find_files(root, lambda name:
                               enums.lower_extension_of(name) == "cue"):
            folder = os.path.dirname(cue)
            cue_count = len(_one_level(
                folder, lambda name: name.endswith(".cue")))
            stem = os.path.splitext(cue)[0]
            has_image = any(os.path.isfile(stem + "." + extension)
                            for extension in image_extensions)
            if cue_count < 2 or not has_image:
                continue
            for duplicate in _one_level(
                    folder,
                    lambda name: enums.lower_extension_of(name) == "cue"):
                new_folder = os.path.splitext(duplicate)[0]
                os.makedirs(new_folder, exist_ok=True)
                _move_no_clobber(duplicate, new_folder)
                for extension in ["log"] + image_extensions:
                    companion = os.path.splitext(duplicate)[0] + "." + extension
                    if os.path.isfile(companion):
                        _move_no_clobber(companion, new_folder)
                self.counters.bump("discsSplit")
                log("  Disc moved into a folder of its own: "
                    + _relative_to(new_folder, self.download_dir))


# --- the small shells this run runs out to ------------------------------------

def _bytes(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _run(argv: list) -> int:
    """One tool, its own output silenced the way the shell silences it. A tool
    that is not there is the shell's 127 rather than an exception."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return 127
    return done.returncode


def _probe(argv: list) -> str:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    return done.stdout.decode("utf-8", "surrogateescape")


def _duration_seconds(path: str) -> int:
    """The shell's ``ffprobe -show_format | sed -n 's/duration=//p' | xargs
    printf %.0f``: the duration rounded to whole seconds, 0 for a file it cannot
    read."""
    text = _probe(["ffprobe", "-i", path, "-show_format", "-v", "quiet"])
    for line in text.splitlines():
        if line.startswith("duration="):
            return int(round(formatting.awk_number(line[len("duration="):])))
    return 0


def _duration_ms(path: str) -> int:
    """``durationMs``: the duration in whole milliseconds, 0 on failure."""
    text = _probe(["ffprobe", "-v", "quiet", "-show_entries",
                   "format=duration", "-of", "default=nk=1:nw=1", path])
    return int(formatting.awk_number(text.strip() or 0) * 1000)


def _codec_and_rate(path: str) -> tuple:
    """The first stream's codec name and sample rate, as the shell reads them
    back through jq - an absent field answering the literal "null"."""
    import json
    text = _probe(["ffprobe", "-loglevel", "0", "-print_format", "json",
                   "-show_format", "-show_streams", path])
    try:
        document = json.loads(text)
    except ValueError:
        return "null", 0
    if not isinstance(document, dict):
        return "null", 0
    streams = document.get("streams") or []
    if not streams or not isinstance(streams[0], dict):
        return "null", 0
    codec = streams[0].get("codec_name")
    rate = streams[0].get("sample_rate")
    return ("null" if codec is None else str(codec),
            int(formatting.awk_number(rate)) if rate is not None else 0)


def _copy_mtime(source: str, target: str) -> bool:
    """``touch -r``: the encoded flac keeps its source's modification time."""
    try:
        stamp = os.stat(source)
        os.utime(target, (stamp.st_atime, stamp.st_mtime))
    except OSError:
        return False
    return True


def _copy(source: str, target: str) -> None:
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        shutil.copy(source, target)
    except OSError:
        pass


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_tree(path: str) -> bool:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError:
        return False
    return True


def _force_rename(source: str, target: str) -> None:
    """``mv -f``, which is deliberately not the safe rename: these two names are
    the same cue sheet and the shell overwrites."""
    try:
        os.replace(source, target)
    except OSError:
        pass


def _move_no_clobber(source: str, folder: str) -> None:
    """``mv -n``: into the folder, unless something of that name is there."""
    target = os.path.join(folder, os.path.basename(source))
    if os.path.exists(target):
        return
    try:
        os.replace(source, target)
    except OSError:
        pass


def _relative_to(path: str, root: str) -> str:
    """The shell's ``${path#"$root/"}``, which leaves a path that does not start
    there exactly as it is."""
    prefix = root.rstrip("/") + "/"
    return path[len(prefix):] if path.startswith(prefix) else path


def _find_files(root: str, matches) -> list:
    """Every file below ``root`` whose name matches, deterministically ordered."""
    found = []
    for parent, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(parent, name)
            if os.path.isfile(path) and matches(name):
                found.append(path)
    return found


def _find_relative(root: str, matches) -> list:
    """The same, as paths relative to ``root``."""
    return [os.path.relpath(path, root) for path in _find_files(root, matches)]


# --- the closing report -------------------------------------------------------

def _stat_row(label: str, value) -> None:
    sys.stdout.write("%-20s %s\n" % (label + ":", value))


def _stat_row_if_any(label: str, value) -> None:
    """Only when there is something to say, so an ordinary run of album rips does
    not carry rows of zeroes about ALAC files it never saw."""
    if isinstance(value, int) and value > 0:
        _stat_row(label, value)


def footer(state) -> None:
    counters = state.counters
    tracks = state.lossless_total
    encoded = counters.value("encoded")
    covers = (counters.value("coverAvif") + counters.value("coverCopied")
              + counters.value("coverAlreadyThere"))
    # Each group's own total, so its indented rows always have a heading above
    # them: a run whose only cue sheet was skipped would otherwise print a lone
    # "skipped: 1" under the covers, belonging to nothing.
    cues = (counters.value("chaptersEmbedded")
            + counters.value("chaptersSkipped")
            + counters.value("chaptersFailed"))
    videos = counters.value("videosRemuxed") + counters.value("videosFailed")
    audio_seconds = formatting.awk_number(
        counters._read(counters.duration_file))

    total_seconds = (state.run_end or time.time()) - state.run_start
    # An encode phase that started but never finished is timed up to NOW, not to
    # its own missing end: a run stopped an hour into the encoding did that hour
    # of work, and falling back to the phase's start would report it as having
    # taken no time at an infinite speed-up.
    if state.encode_start:
        encode_seconds = (state.encode_end or time.time()) - state.encode_start
    else:
        encode_seconds = 0

    sys.stdout.write("\nStats\n=====\n")
    _stat_row("Lossless tracks", tracks)
    _stat_row_if_any("  encoded", encoded)
    _stat_row_if_any("  up to date", counters.value("upToDate"))
    _stat_row_if_any("  not lossless", counters.value("notLossless"))
    _stat_row_if_any("  failed", counters.value("failed"))
    _stat_row_if_any("Cover images", covers)
    _stat_row_if_any("  to AVIF", counters.value("coverAvif"))
    _stat_row_if_any("  copied unchanged", counters.value("coverCopied"))
    _stat_row_if_any("  already there", counters.value("coverAlreadyThere"))
    _stat_row_if_any("Cue sheets", cues)
    _stat_row_if_any("  embedded", counters.value("chaptersEmbedded"))
    _stat_row_if_any("  skipped", counters.value("chaptersSkipped"))
    _stat_row_if_any("  failed", counters.value("chaptersFailed"))
    _stat_row_if_any("  dropped, no flac", counters.value("cuesDropped"))
    _stat_row_if_any("Video files", videos)
    _stat_row_if_any("  remuxed to MKV", counters.value("videosRemuxed"))
    _stat_row_if_any("  failed", counters.value("videosFailed"))
    _stat_row_if_any("Discs split off", counters.value("discsSplit"))
    _stat_row_if_any("ALAC renamed", counters.value("alacRenamed"))
    _stat_row_if_any("Folders re-homed", counters.value("folderRedirects"))
    _stat_row_if_any("Already in library",
                     counters.value("copiesAlreadyThere"))
    _stat_row("Total time", "%s s (%s)" % (
        "%.2f" % total_seconds, formatting.fmt_hms("%.2f" % total_seconds)))

    # Only once there has been an encoding phase to time: everything below
    # divides by the work the encoder did, and a run stopped during the copy did
    # none.
    if encode_seconds > 0:
        _stat_row("Encoding time", "%s s (%s)" % (
            "%.2f" % encode_seconds,
            formatting.fmt_hms("%.2f" % encode_seconds)))
        _stat_row("Audio encoded", "%d s (%s)" % (
            round(audio_seconds), formatting.fmt_hms(audio_seconds)))
        # Per track and the speed-up both divide by what was ENCODED, not by what
        # was found: a re-run that skipped 90 of 100 tracks did the work of ten,
        # and dividing by a hundred would report a machine ten times faster than
        # it is.
        if encoded > 0:
            _stat_row("Time per track",
                      "%.2f s" % (encode_seconds / encoded))
            _stat_row("Real-time speedup", "%sx" % formatting.fmt_ratio(
                "%.6f" % (audio_seconds / encode_seconds)))

    safety.report_safety_skips()


# --- the run ------------------------------------------------------------------

def _in_worker(state, method: str, item) -> None:
    """One job, in a worker PROCESS. The interrupt handling is installed here
    rather than in the work, because at width 1 that same work runs in the run's
    own process - where the worker's handler would replace the run's. The scratch
    base is adopted for the same kind of reason: a worker that settled its own
    would leave a second run directory behind."""
    safety.trap_worker_abort()
    ramscratch.adopt_ram_base(getattr(state, "ram_base", ""))
    getattr(state, method)(item)


def _run_pool(state, method: str, items: list, jobs: int) -> None:
    if jobs <= 1:
        for item in items:
            if safety.abort_requested():
                return
            getattr(state, method)(item)
        return

    workerpool.run(items, jobs, _in_worker, lambda item: (state, method, item))


def main(argv: list, program: str = "ingest-music",
         script_dir: str = "") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        sys.stderr.write(clioptions.usage_error_text(declaration,
                                                     error.message))
        return 1

    if clioptions.args_out_of_range(len(result.positionals), 2, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    script_dir = script_dir or commands.script_dir()

    jobs = int(result.values["jobs"] or runlog.cpu_count())
    download_dir = result.positionals[0]
    ingest_dir = result.positionals[1].rstrip("/")
    # The Opus copies land in an optional third argument, defaulting to a sibling
    # folder next to the lossless library.
    opus_copy_dir = (result.positionals[2] if len(result.positionals) > 2
                     else ingest_dir + "opus")

    # This is a long run - every lossless track re-encoded, then beets over the
    # whole library, then a second library in Opus - so its log lines carry a
    # wall-clock stamp. Exported, so the child scripts this one ends with stamp
    # their own the same way and the console reads as one run rather than two.
    os.environ["LOG_TIMESTAMPS"] = "1"

    if not os.path.isdir(download_dir):
        sys.stdout.write(clioptions.missing_dir_text(declaration,
                                                     download_dir))
        return 1

    # Three folders, so three ways to nest one inside another, and all three
    # break the same way: the FLAC library written into the download folder would
    # be ingested again by the next run, and so would the opus copies. Checked
    # before the ingest folder is created, so a refused run leaves nothing
    # behind.
    if safety.require_separate_output(download_dir, ingest_dir):
        return 1
    if safety.require_separate_output(download_dir, opus_copy_dir):
        return 1
    if safety.require_separate_output(ingest_dir, opus_copy_dir):
        return 1

    # Which of this machine's ffmpeg builds does the re-encoding - inherited by
    # the convert-audio child, and settled before the preflight below asks
    # whether PATH can reach one.
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()

    # This script's own tools plus everything convert-audio needs, because that
    # runs as a child right at the end: the point of a preflight is lost if the
    # last phase of a long ingest is where a missing encoder finally shows up.
    # beets is what makes this an INGEST rather than a copy, so its absence is a
    # refusal and not a warning.
    if tooldeps.require_tools(program, [
            "ffmpeg", "ffprobe", "rsync", "fdupes", "beet",
            imagemagick.CONVERT_SPEC,
            "mkvmerge", "mkvpropedit", "mkvextract"]):
        return 1
    if tooldeps.require_python_module(
            "mutagen", program,
            "writes the cue-sheet chapter marks into the ingested FLACs"):
        return 1
    runlog.warn_uncounted_progress()

    # Unlike the other wrappers this one ingests a whole download folder rather
    # than one file type, so any file at all is something to do. Only a folder
    # holding no file whatsoever has nothing for it.
    if not _find_files(download_dir, lambda name: True):
        return safety.fail_no_relevant_input(
            download_dir, "the files of a freshly downloaded music release")

    os.makedirs(ingest_dir, exist_ok=True)

    statusline.init_status_line()

    # This run's counters live in a RAM-backed directory together with the
    # safety-skip log and the interrupt flag: they are written once per item by
    # worker processes, so they have to be files every process can see, and they
    # are scratch nobody wants left behind.
    ramscratch.init_ram_base(os.environ.get("musicRamBase", ""))
    counter_dir, status = ramscratch.ram_scratch_dir("ingestMusic.counters")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([counter_dir])

    safety.init_safety_log(os.path.join(counter_dir, "safetySkips.log"))
    skips = safety.RunSkipLog()
    safety.init_abort_flag(os.path.join(counter_dir, "abortRequested"))
    safety.trap_run_abort()

    try:
        return _ingest(program, script_dir, download_dir, ingest_dir,
                       opus_copy_dir, counter_dir, skips, jobs)
    finally:
        statusline.stop_status_monitor()
        ramscratch.run_exit_cleanup()


def _ingest(program: str, script_dir: str, download_dir: str, ingest_dir: str,
            opus_copy_dir: str, counter_dir: str, skips, jobs: int) -> int:
    # Stamped here rather than at the top of the run proper, because the closing
    # report divides by it and the pretreatment below - de-duplicating, splitting
    # discs, copying - is already part of what the run cost.
    run_start = time.time()

    counters = Counters(counter_dir, int(run_start))
    counters.create()

    for name in ("ingestedSources", "ingestedPairs", "folderMap"):
        with open(os.path.join(counter_dir, name), "wb"):
            pass

    cover_tier = imagesizes.DEFAULT_TIER
    state = Run(
        download_dir=download_dir, ingest_dir=ingest_dir,
        script_dir=script_dir, counters=counters, skips=skips,
        folder_map={}, lossless_total=0,
        cover_max_edge=imagesizes.height(cover_tier),
        ingested_sources=os.path.join(counter_dir, "ingestedSources"),
        ingested_pairs=os.path.join(counter_dir, "ingestedPairs"),
        ram_base=ramscratch.ram_base(),
        run_start=run_start, run_end=None,
        encode_start=None, encode_end=None)
    safety.set_run_footer(lambda: footer(state))

    # Put CD images that need splitting in separate folders if needed.
    log("Splitting multi-disc CUE images into subfolders")
    state.split_disc_images(download_dir)

    # Before anything reads the tree: an ALAC .m4a is a lossless source wearing a
    # lossy extension, and renaming it now is what puts it in the encoder's queue
    # below instead of in the plain copy.
    state.rename_alac(download_dir)

    # The encoder's queue, collected into a list first, and the SAME list both
    # counted and dispatched - a denominator from a second walk could disagree
    # with it over a file that arrived, left, or was renamed in between.
    # Collected before the copy, because what the library already holds from
    # these very tracks is also what says where this release lives in it.
    lossless_tracks = _find_relative(
        download_dir,
        lambda name: enums.lower_extension_of(name)
        in enums.LOSSLESS_AUDIO_EXTENSIONS)
    state.lossless_total = len(lossless_tracks)

    # What the library already holds, and under what names, read out of the flacs
    # themselves. Inside this branch because it walks the whole library to answer
    # a question a run of nothing but lossy downloads never asks.
    if lossless_tracks:
        library_flacs = _find_files(
            ingest_dir,
            lambda name: enums.lower_extension_of(name) == "flac")
        if library_flacs:
            log("Reading what the %d flac(s) already in the library were "
                "made from" % len(library_flacs))
            counters.start_phase("reading the library", "flacs",
                                 len(library_flacs))
            _run_pool(state, "read_ingest_source", library_flacs, jobs)
            counters.end_phase()
            safety.exit_if_aborted()
            log("  %d of them name the download they came from"
                % _record_count(state.ingested_sources))
            became, ambiguous = build_ingested_folder_map(
                state.ingested_pairs, ingest_dir)
            redirects = write_folder_map(
                download_dir, os.path.join(counter_dir, "folderMap"),
                became, ambiguous)
            counters.bump("folderRedirects", redirects)
            state.folder_map = read_folder_map(
                os.path.join(counter_dir, "folderMap"))

    # The target folders - each one where the library actually keeps it, which is
    # the same path it has in the download unless a previous run's name cleaning
    # moved it.
    for relative in _download_folders(download_dir)[1:]:
        os.makedirs(
            ingested_dir_path(relative, ingest_dir, state.folder_map),
            exist_ok=True)

    _copy_the_rest(state, download_dir, ingest_dir, counters)

    if lossless_tracks:
        log("Encoding %d lossless track(s) to FLAC (up to %d in parallel)"
            % (state.lossless_total, jobs))
        # Only stamped where there is encoding to time, so the closing report can
        # tell a run that encoded nothing from one that was stopped while
        # encoding.
        state.encode_start = time.time()
        counters.start_phase("encoding", "tracks", state.lossless_total, True)
        _run_pool(state, "encode", lossless_tracks, jobs)
        counters.end_phase()
        safety.exit_if_aborted()
        state.encode_end = time.time()
    else:
        log("No lossless source to encode - everything here is already lossy")

    # Cover-image conversion is CPU-heavier per file, so it runs roughly
    # cores/4 at once instead of one per core. Its own number, so the encoder
    # jobs (and the -j the user passed) stay intact.
    image_jobs = runlog.jobs_per_core(IMAGE_THREADS_PER_CONVERSION)
    cover_images = _find_relative(
        download_dir,
        lambda name: enums.lower_extension_of(name)
        in enums.COVER_IMAGE_EXTENSIONS)
    if cover_images:
        log("Converting %d cover image(s), the large ones to AVIF (up to %d "
            "in parallel)" % (len(cover_images), image_jobs))
        counters.start_phase("converting", "images", len(cover_images))
        _run_pool(state, "convert_image", cover_images, image_jobs)
        counters.end_phase()
        safety.exit_if_aborted()
    else:
        log("No cover image to convert")

    stray_videos = _find_files(
        ingest_dir,
        lambda name: any(name.endswith(suffix)
                         for suffix in STRAY_VIDEO_SUFFIXES))
    if stray_videos:
        log("Remuxing %d stray video file(s) to MKV" % len(stray_videos))
        counters.start_phase("remuxing", "videos", len(stray_videos))
        state.mux_video(stray_videos)
        counters.end_phase()

    # The chapters, while cue and flac still share a base name - before beets and
    # the name cleaning rename them.
    cue_sheets = _find_files(
        ingest_dir, lambda name: enums.lower_extension_of(name) == "cue")
    if cue_sheets:
        log("Embedding the chapters of %d cue sheet(s) into the flacs they "
            "describe" % len(cue_sheets))
        counters.start_phase("embedding chapters from", "cues",
                             len(cue_sheets))
        state.embed_cue_chapters(cue_sheets)
        counters.end_phase()

    # Only NOW are the leftover cue sheets dropped: the copy brings every cue
    # across before a single flac has been encoded, so pruning them where the
    # copy happens threw away every cue in the release - each one an orphan at
    # that moment. After the encoding and the embedding, the cues that are still
    # companionless really are.
    delete_unneeded_cue(ingest_dir, counters)

    # Tag and organise with beets, against the OUTPUT so the original input is
    # never renamed. The config travels with the package instead of a personal
    # ~/.config/beets, and beets runs from inside the library so its
    # "directory: ." resolves there.
    log("Tagging and organising with beets")
    beets_log = os.path.join(script_dir, "logs", "beets.log")
    os.makedirs(os.path.dirname(beets_log), exist_ok=True)
    _run_in(ingest_dir, ["beet", "-c",
                         os.path.join(commands.config_dir(), "beets.yaml"),
                         "import", "-Ciq", "-l", beets_log, "."])

    # Final name and structure cleanup, applied to the output only. It also
    # prunes any folders left empty in the ingest tree, so no separate prune is
    # needed here.
    log("Cleaning ingested folder and file names")
    _run_child("clean-folder-structure", [ingest_dir], script_dir)

    # The Opus copies of the finished lossless library, reusing convert-audio:
    # its -c also carries the cover art and other sidecar files across, and music
    # tracks fall under its long-file split threshold so they are encoded whole.
    log('Creating %s kbps Opus copies in "%s"'
        % (OPUS_COPY_BITRATE, opus_copy_dir))
    _run_child("convert-audio",
               ["-cb", OPUS_COPY_BITRATE, "-j", jobs, "-c",
                ingest_dir, opus_copy_dir], script_dir)

    state.run_end = time.time()
    safety.print_run_footer()
    return 0


def _copy_the_rest(state, download_dir: str, ingest_dir: str,
                   counters) -> None:
    """Everything that is not a lossless source, an image or a rip-checker
    leftover, copied into the library.

    Two paths on purpose. With nothing to redirect - a first ingest, or a library
    whose names still match the download's - it is the single recursive rsync it
    always was. Once the map holds something, the tree is copied folder by folder
    instead, each into the folder the library keeps it in: -d rather than -r so
    each rsync moves that one folder's files and leaves its subfolders to their
    own turn, since a subfolder can resolve somewhere its parent does not.
    """
    log("Copying non-lossless files into the ingest tree")
    # The lossless sources are excluded from the same enum the encoder's queue
    # was built from, so a format cannot end up queued for encoding AND copied
    # across as-is.
    excludes = ["--exclude=*." + extension
                for extension in enums.LOSSLESS_AUDIO_EXTENSIONS]
    excludes += ["--exclude=accurip.txt", "--exclude=auCDtect.txt*",
                 "--exclude=*.accurip", "--exclude=foo_dr*",
                 "--exclude=*.unwanted*", "--exclude=*.jpeg",
                 "--exclude=*.jpg", "--exclude=*.png", "--exclude=*.svg",
                 "--exclude=*.tiff", "--exclude=*.tif", "--exclude=*.bmp",
                 "--exclude=*.webp*"]

    if not state.folder_map:
        _run(["rsync", "-rm", "--quiet", "--size-only"] + excludes
             + [download_dir + "/", ingest_dir])
        return

    # The flag that names what was actually transferred, which is what the twin
    # check below has to look at: rsync's own comparison is by name and size, so
    # it transfers again exactly those files the last run's name cleaning
    # renamed. Settled once here rather than per folder - which spelling this
    # rsync takes cannot change mid-pass.
    naming = _transfer_format()
    for relative in _download_folders(download_dir):
        copy_to = ingested_dir_path(relative, ingest_dir, state.folder_map)
        os.makedirs(copy_to, exist_ok=True)
        transferred = _probe(
            ["rsync", "-d", "--size-only", naming] + excludes
            + [os.path.join(download_dir, relative) + "/", copy_to + "/"])
        for name in transferred.splitlines():
            # Directories come through as "name/", and are not files to compare.
            if not name or name.endswith("/"):
                continue
            copied = os.path.join(copy_to, name)
            if not os.path.isfile(copied):
                continue
            if has_twin_in(copied, copy_to):
                _remove(copied)
                counters.bump("copiesAlreadyThere")


def _transfer_format() -> str:
    """The flag that makes rsync print one line per transferred item, in the
    spelling the rsync on PATH understands.

    ``--out-format`` is rsync 3.0 and later, which is every Linux
    distribution's. macOS is the reason there is a second rung: it ships rsync
    2.6.9, where the same flag is spelled ``--log-format``, and openrsync on
    14 and later carries that spelling too. An rsync that took neither would
    exit without copying, so the fallback is to the OLD name rather than to no
    flag at all - the caller reads the output, and a copy with nothing to read
    would silently stop de-duplicating.

    Asked by reading ``rsync --help``, which both spellings appear in, rather
    than by parsing a version number: a distribution patch and a rewrite
    (openrsync) both make the number a poor proxy for what the flags are.

    Asked once per copying PASS and held in a local by the caller, rather than
    cached in the module: one extra process for a run that copies hundreds of
    folders is nothing, and module state that outlives a run is the thing a
    test then has to remember to clear.
    """
    return ("--out-format=%n" if "--out-format" in _rsync_help()
            else "--log-format=%n")


def _rsync_help() -> str:
    """``rsync --help``, or "" when it cannot be run at all - which reads as
    the modern spelling, because that is what the preflight asked for and what
    every host this has ever run on has."""
    try:
        done = subprocess.run(["rsync", "--help"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
    except OSError:
        return "--out-format"
    return done.stdout.decode("utf-8", "replace")


def _record_count(path: str) -> int:
    """The shell's ``grep -zc ''``: how many NUL-terminated records a file
    holds."""
    try:
        with open(path, "rb") as handle:
            return handle.read().count(b"\0")
    except OSError:
        return 0


def _run_in(directory: str, argv: list) -> int:
    try:
        return subprocess.run(argv, cwd=directory).returncode
    except OSError:
        return 127


def _run_child(command: str, argv: list, script_dir: str) -> int:
    """A sibling command, its output left exactly where it goes: the run's console
    is one narrative and the child's lines belong in it."""
    try:
        return commands.run_command(command, argv,
                                    script_dir=script_dir).returncode
    except OSError:
        return 127


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, commands.program_name(__spec__.name),
                commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
