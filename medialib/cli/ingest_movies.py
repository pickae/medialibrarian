"""ingest-movies: a folder of downloaded films, ingested into a Plex-shaped
library.

Loose movies go into one folder each, names are cleaned until they stop
changing, bonus material is sorted into the folders Plex recognises, each film is
looked up on TMDb for its IMDb id, subtitles are moved, renamed, downloaded and
aligned, the mkv tags and track flags are refreshed from the track layout,
lossless audio is transcoded to opus, commentary tracks are extracted and
transcribed, and finally every film's main movie is remuxed once into an improved
copy with the original kept beside it as "<name> (old).mkv".

The improvement is the interesting half, and it is one mkvmerge call: each
lossless track that was converted swaps for its opus in place, excessive audio
and image-subtitle tracks are dropped, at most one surround tier survives per
language, the commentary transcripts are appended as subtitle tracks, and a
dual-layer Dolby Vision profile 7 is normalised to single-layer 8.1 - or, when
the video turns out to carry no Dolby Vision behind the container's claim, that
claim is dropped.
"""

import os
import re
import shutil
import subprocess
import sys

from medialib import commands
from medialib.lib import (
    bitrates,
    cleannamesindividually,
    clioptions,
    commentarytranscription,
    enums,
    languages,
    objectaudio,
    safety,
)
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <movieDir>

    movieDir      directory holding the movies to ingest

    Options
    -------"""

OPT_SPEC = """
f | <file> | read the name fragments to remove from <file> (one per line,
                  '#' comments and blank lines ignored) instead of from
                  data/fragments.txt beside this script. Without it that file is
                  used when it is there, and names are cleaned without any fragment
                  removal when it is not."""

OPT_VARS = "f:fragmentsOverride"
OPT_COLUMN = 18
OPT_LONG = "f:fragments"

USAGE_TAIL = """

    Default behavior
    ----------------
    sorts loose movie files into one subfolder each (Plex layout)
    cleans up folder, movie and subtitle names
    sorts bonus material into the subfolders Plex recognizes (see below)

    IMDb ids (Plex/Jellyfin naming)
    -------------------------------
    looks each movie up on TheMovieDB (TMDb) by its cleaned name and year
    appends the \"{imdb-ttXXXXXXX}\" id tag to the folder, movie and subtitles
    only when TMDb returns a single exact title+year match (~99% certainty)

    Languages & subtitles
    ---------------------
    six languages are supported out of the box (en, de, fr, nl, es, it)
    renames pre-existing subtitles to the .xx.srt convention
    downloads missing subtitles and embeds the language tags

    Tags & commentary
    ------------------
    reads the track layout once and refreshes the mkv tags from it
    transcodes regular audio to opus at a per-channel target bitrate
    extracts commentary tracks, transcribes them and archives them
    takes a track for a commentary by its mkv flag or by its name, in any of the
    supported languages (\"Audiokommentar\" counts as much as \"Audio Commentary\")
    reads each commentary's language from its track name or mkv language tag, and
    lets whisper detect it when neither says. English gives one \"<name>.en.srt\",
    another supported language a native \"<name>.xx.srt\" plus a translated
    \"<name>.en.srt\", an unsupported one only the translated \"<name>.en.srt\"
    queues every wanted transcription of every movie in ONE queue, spread over all
    transcription workers
    transcribes on the GPU when whisper can use it, picking the largest model
    the free VRAM fits, and falls back to all CPU threads on base.en if not

    Dolby Vision
    ------------
    normalises dual-layer profile 7 to single-layer profile 8.1, so DV survives
    on hardware that cannot read the enhancement layer (metadata only, the video
    is never re-encoded, needs dovi_tool)
    leaves profile 5, profile 8.x, HDR10, HDR10+ and SDR untouched (a profile 5
    base layer is not HDR10 and cannot be converted without re-encoding)
    corrects a Dolby Vision level that overstates the video, whatever the profile:
    players check the level against their hardware before decoding and refuse a
    file that asks for more than they have (container metadata only, in place)

    RAM usage
    ---------
    all large temporary media (extracts, remux copies, Dolby Vision streams)
    live in tmpfs
    only the final outputs (.mkv, .srt, .opus) are written to disk

    Dependencies
    ------------
    Required: ffmpeg, mkvtoolnix, mediainfo (and curl, once tmdbApiKey is set).
    Optional, each skipped with a warning when absent: pipx + ffsubsync (together
    they enable subtitle downloading and commentary transcription - without either,
    BOTH are skipped, since neither subtitle is worth keeping unaligned), dovi_tool
    (the Dolby Vision normalisation), flock (numbers the progress lines)"""

# The smallest a real movie file can be: an .mkv under this holds seconds of
# video, so the cleanup treats it as one of the sample clips that ship next to a
# release rather than as a movie.
MIN_MOVIE_BYTES = 1000 * 1024

# Subtitle syncing is governed by two different numbers, which ffsubsync keeps
# apart: the search window it looks for an offset IN, and the offset it is still
# willing to BELIEVE. An offset outside the window does not make ffsubsync give
# up - it returns the best scoring offset inside it, a wrong sync that looks
# clean - so the window stays wide and the quality threshold is what rejects an
# implausible result.
MAX_SYNC_OFFSET = "600"
# A downloaded subtitle is often cut for a different release, so tens of seconds
# can be legitimate; past a minute it is far likelier to be the wrong release.
MAX_SYNC_QUALITY_OFFSET = "60"
# A whisper transcript is made FROM the audio it is synced against, so its true
# offset is ~0: one tight number serves as both window and credibility limit.
MAX_WHISPER_SYNC_OFFSET = "5"

# Plex bonus-material categories and the keywords that identify them, in priority
# order - the first matching row wins.
# https://support.plex.tv/articles/local-files-for-trailers-and-extras/
BONUS_CATEGORIES = (
    ("Shorts", ("short",)),
    ("Behind The Scenes", ("behind the scene", "behind-the-scene", "making of",
                           "making-of", "inside", "revealed", "filming",
                           "backstage")),
    ("Deleted Scenes", ("deleted scene", "delete scene", "extended scene",
                        "alternate")),
    ("Interviews", ("interview", "conversation", "talk", "chat", "discuss",
                    "q&a", "press conference", "panel")),
    ("Trailers", ("trailer", "teaser", "spot", "promo")),
    ("Scenes", ("test", "blooper", "outtake", "gag", "reel")),
    ("Other", ("review",)),
)

# The folder names that hold bonus material rather than a film: every category
# above, plus Featurettes - the folder extras land in before they are sorted.
# DERIVED, because a category added above but missing here would have its folder
# treated as a film folder and each featurette in it remuxed as a feature.
BONUS_FOLDER_NAMES = ("Featurettes",) + tuple(
    name for name, _keywords in BONUS_CATEGORIES)

# The image-based subtitle codecs, as mkvmerge reports them.
IMAGE_SUB_CODECS = ("S_HDMV/PGS", "S_VOBSUB", "S_DVBSUB")

# The junk a release ships with, deleted before anything else runs.
JUNK_EXTENSIONS = ("txt", "nfo", "exe", "DOC", "sfv")
SAMPLE_NAMES = ("sample.mkv", "Sample.mkv", "SAMPLE.mkv")

# The field separator the track reads join on, which cannot appear in a track
# name.
UNIT = "\x1f"

_YEAR = re.compile(r"[1-2][0-9][0-9][0-9]")
_TWO_LETTER = re.compile(r"^[a-z][a-z]$")


def spec(program: str) -> "clioptions.Spec":
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
        tail=USAGE_TAIL,
        no_args_with_credits=False,
        no_args_stream="stderr",
    )


# --- the pure questions -------------------------------------------------------

def is_bonus_folder(path: str) -> bool:
    """``isBonusFolder``: does this folder hold bonus material rather than a
    film? The guard every per-movie phase opens with.

    Matched on the folder's own last component, by SUFFIX, so a disc's "Movie
    Featurettes" counts as much as a plain "Featurettes". Two ways in: a name
    ending in "extras" in any case - the one spelling common enough in the wild
    to catch before anything has been renamed - or one ending in a category name
    in the case Plex spells it, which is also the case this script writes.
    """
    name = os.path.basename(path.rstrip("/"))
    if enums.shell_lower(name).endswith("extras"):
        return True
    return any(name.endswith(candidate) for candidate in BONUS_FOLDER_NAMES)


def rename(name: str, fragments_file: str = "") -> str:
    """``rename``: the naming rules, for folders, movies and commentary tracks.

    The generic first pass is the shared cleaner; everything after it is this
    script's own - dots and underscores to spaces, an empty bracket pair dropped,
    whitespace collapsed, and a trailing year put in parentheses.
    """
    # Separate any directory part so the cleaner only sees the actual name, then
    # recombine, so the movie-specific rules below act on a full path.
    directory, _slash, base = name.rpartition("/")
    directory = directory + "/" if _slash else ""

    prefix, cleaned = cleannamesindividually.clean_names_individually(
        base, fragments_file or None)
    name = directory + (prefix + " " + cleaned if prefix else cleaned)

    if name[:2] == "./":
        name = "./" + name[2:].replace(".", " ")
    else:
        name = name.replace(".", " ")
    name = name.replace("_", " ")

    # Drop an empty trailing bracket pair, in either spelling: a film whose year
    # was removed by an earlier pass is otherwise left with a bare "()" hanging
    # off it. Each spelling is compared against a slice of its OWN length, since
    # one shared four-character slice can never equal the shorter of the two.
    # The removal leaves the separating space for the trim below.
    if name[-4:] == " ( )":
        name = name[:-3]
    elif name[-3:] == " ()":
        name = name[:-2]

    # Collapse whitespace runs and trim, which is what word-splitting on the
    # default IFS does.
    name = " ".join(name.split())

    trailing = name[-4:]
    if _YEAR.search(trailing):
        name = name[:-4] + "(" + trailing + ")"
    return name


def is_image_sub_codec(codec: str) -> bool:
    """``isImageSubCodec``: PGS (Blu-ray), VobSub (DVD) or DVBSub
    (broadcast)."""
    return (codec or "").upper() in IMAGE_SUB_CODECS


def is_lossless_track_codec(codec: str) -> bool:
    """``isLosslessTrackCodec``: is this Matroska codec_id one of the lossless
    ones transcoded to opus?

    Matched as a SUBSTRING, because mkvmerge wraps the codec in a prefix and
    sometimes a suffix - "A_TRUEHD", "A_PCM/INT/LIT" - so the enum lists the
    codec itself and this decides how it is found in the id.
    """
    folded = enums.shell_lower(codec)
    return any(candidate in folded
               for candidate in enums.LOSSLESS_TRACK_CODECS)


def audio_stream_index(track: int, types: list) -> int:
    """``audioStreamIndex``: an mkvmerge track number as the 0-based audio-stream
    index ffmpeg's ``-map 0:a:N`` expects.

    mkvmerge uses a single index running across every track while ffmpeg numbers
    each type separately, so the audio index of a track is the number of audio
    tracks before it - however the types are interleaved.
    """
    return sum(1 for position in range(1, track)
               if "audio" in (types[position - 1] if position - 1 < len(types)
                              else ""))


def bonus_category_for(name: str) -> str:
    """Which Plex bonus folder an extra belongs in, or "" for one that stays put.

    The table's order is the priority: the first matching category wins.
    """
    folded = enums.shell_lower(name)
    for subfolder, keywords in BONUS_CATEGORIES:
        if any(keyword in folded for keyword in keywords):
            return subfolder
    return ""


def language_allowed(language: str, primary: str) -> bool:
    """Is this track's language one the improved copy keeps?

    The first audio track's language and English, plus the ones there is nothing
    to judge - empty, "null", undetermined - and no-linguistic-content (zxx),
    which is an isolated score or effects rather than a second soundtrack.
    """
    folded = enums.shell_lower(language)
    return folded in ("", "null", "und", "zxx", "eng", "en", primary)


# --- reading a file's tracks --------------------------------------------------

class Track:
    """One track of a Matroska file, as mkvmerge reports it."""

    FIELDS = ("id", "type", "codec", "channels", "language", "name",
              "commentary", "default", "forced")

    # FIELDS is what __init__ sets; these declare what it sets them TO. Every
    # one is text, because mkvmerge's JSON is read as the shell reads it and an
    # absent field is the empty string rather than a missing attribute.
    id: str
    type: str
    codec: str
    channels: str
    language: str
    name: str
    commentary: str
    default: str
    forced: str

    def __init__(self, **fields) -> None:
        for field in self.FIELDS:
            setattr(self, field, fields.get(field, ""))
        self.objects = ""
        self.action = "keep"
        self.opus = ""

    @property
    def is_audio(self) -> bool:
        return "audio" in self.type

    @property
    def is_video(self) -> bool:
        return "video" in self.type

    @property
    def is_subtitle(self) -> bool:
        return "subtitle" in self.type

    @property
    def is_commentary(self) -> bool:
        return (self.commentary == "true"
                or languages.is_commentary_name(self.name))


def _identify(movie: str) -> list:
    """``mkvmerge -J``, read into tracks.

    mkvmerge exits 1 on non-fatal WARNINGS while still printing valid JSON, and
    the shell guards every call for that reason: an unguarded non-zero there
    aborts the whole ingest mid-phase. A genuine parse failure yields no tracks,
    which every caller already treats as nothing to do.
    """
    import json
    try:
        done = subprocess.run(["mkvmerge", "-J", movie],
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
        document = json.loads(done.stdout.decode("utf-8", "surrogateescape"))
    except (OSError, ValueError):
        return []
    if not isinstance(document, dict):
        return []

    tracks = []
    for entry in document.get("tracks") or []:
        if not isinstance(entry, dict):
            continue
        properties = entry.get("properties") or {}
        tracks.append(Track(
            id=_as_text(entry.get("id")),
            type=_as_text(entry.get("type")),
            codec=_as_text(properties.get("codec_id")),
            channels=_as_text(properties.get("audio_channels")),
            language=_as_text(properties.get("language")),
            name=_as_text(properties.get("track_name")),
            commentary=_as_text(properties.get("flag_commentary")),
            default=_as_text(properties.get("default_track")),
            forced=_as_text(properties.get("forced_track"))))
    return tracks


def _as_text(value) -> str:
    """jq's ``tostring``, which renders a null-valued property as the literal
    "null" - what every comparison in the shell is written against."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def read_track_info(movie: str) -> tuple:
    """``readTrackInfo``: the six per-track arrays, in mkvmerge's order.

    The shape the commentary library expects from its caller.
    """
    tracks = _identify(movie)
    return ([t.name for t in tracks], [t.codec for t in tracks],
            [t.channels for t in tracks], [t.commentary for t in tracks],
            [t.type for t in tracks], [t.language for t in tracks])


def _object_flags(movie: str, tracks: list) -> None:
    """Which audio tracks carry object-based metadata (Dolby Atmos / DTS:X).

    One mediainfo pass gives each track's commercial name and object count in the
    same file order as the mkvmerge list, so the a-th audio track here is the
    a-th one mediainfo reports. A mediainfo that cannot read the file leaves
    every flag empty and the ladder deduplicates on codec and channels alone.
    """
    import json
    rows = []
    try:
        done = subprocess.run(["mediainfo", "--Output=JSON", movie],
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
        document = json.loads(done.stdout.decode("utf-8", "surrogateescape"))
        for entry in (document.get("media") or {}).get("track") or []:
            if not isinstance(entry, dict):
                continue
            if enums.shell_lower(_as_text(entry.get("@type"))) != "audio":
                continue
            extra = entry.get("extra") or {}
            rows.append((entry.get("Format_Commercial_IfAny") or "-",
                         _as_text(extra.get("NumberOfDynamicObjects") or "-")))
    except (OSError, ValueError, AttributeError):
        rows = []

    audio_index = 0
    for track in tracks:
        if not track.is_audio:
            continue
        commercial, objects = rows[audio_index] if audio_index < len(rows) \
            else ("", "")
        track.objects = objectaudio.audio_object_flag(commercial, objects,
                                                      track.name)
        audio_index += 1


# --- the phases ---------------------------------------------------------------

def cleanup(root: str) -> None:
    """The junk a release ships with, and the folders left empty by removing
    it."""
    for parent, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(parent, name)
            extension = name.rpartition(".")[2] if "." in name else ""
            if extension in JUNK_EXTENSIONS or name in SAMPLE_NAMES:
                _remove(path)
                continue
            # The sample clips are not always NAMED sample: an .mkv this small
            # holds seconds of video, so it is one of those rather than a movie,
            # whatever it is called.
            if name.endswith(".mkv") and _size_of(path) < MIN_MOVIE_BYTES:
                _remove(path)

    for parent, dirs, _names in os.walk(root):
        for name in dirs:
            if "unwanted" in name:
                shutil.rmtree(os.path.join(parent, name), ignore_errors=True)

    _remove_empty_below(root)


def _remove_empty_below(root: str) -> None:
    """``find -mindepth 1 -type d -empty -delete``: never the folder we were
    pointed at, only empties inside it."""
    for parent, dirs, _names in os.walk(root, topdown=False):
        for name in dirs:
            path = os.path.join(parent, name)
            try:
                if not os.listdir(path):
                    os.rmdir(path)
            except OSError:
                continue


def movies_into_subfolders(root: str) -> None:
    """``moviesIntoSubfolders``: a movie that arrived as a loose file gets a
    folder of its own, which is the layout Plex wants."""
    for name in sorted(_names_in(root)):
        path = os.path.join(root, name)
        if not (os.path.isfile(path) and name.endswith(".mkv")):
            continue
        folder = os.path.join(root, os.path.splitext(name)[0])
        try:
            os.mkdir(folder)
            os.rename(path, os.path.join(folder, name))
        except OSError:
            continue


def rename_folders(root: str, fragments_file: str, skips) -> int:
    """``renameFolders``: each top-level folder renamed once, answering how many
    actually moved so the caller can repeat until the names stabilise."""
    renamed = 0
    for name in sorted(_names_in(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        new_name = rename(name, fragments_file)
        if new_name == name:
            continue
        target = os.path.join(root, new_name)
        if os.path.exists(target):
            skips.record(path, target)
            continue
        try:
            os.rename(path, target)
            renamed += 1
        except OSError:
            continue
    return renamed


def rename_movies(root: str, fragments_file: str, skips) -> int:
    """``renameMovies``: each movie renamed once, carrying its subtitles along.

    A bare ".srt" belongs to the English track - the assumption the whole
    subtitle convention rests on.
    """
    renamed = 0
    for movie in _movies_one_level_down(root):
        directory = os.path.dirname(movie)
        base = os.path.splitext(os.path.basename(movie))[0]
        new_base = rename(base, fragments_file)
        new_movie = os.path.join(directory, new_base + ".mkv")

        if new_movie != movie:
            if os.path.exists(new_movie):
                skips.record(movie, new_movie)
                new_movie = movie
            else:
                try:
                    os.rename(movie, new_movie)
                    renamed += 1
                except OSError:
                    new_movie = movie

        old_stem = os.path.join(directory, base)
        plain = old_stem + ".srt"
        if os.path.isfile(plain):
            english = old_stem + ".en.srt"
            if os.path.exists(english):
                skips.record(plain, english)
            else:
                _rename_quiet(plain, english)

        new_stem = os.path.splitext(new_movie)[0]
        for row in languages.LANGUAGES:
            source = "%s.%s.srt" % (old_stem, row.code2)
            target = "%s.%s.srt" % (new_stem, row.code2)
            if not os.path.isfile(source) or source == target:
                continue
            if os.path.exists(target):
                skips.record(source, target)
            else:
                _rename_quiet(source, target)
    return renamed


def extras_into_subfolders(root: str, skips) -> None:
    """``extrasIntoSubfolders``: bonus material sorted into the folders Plex
    recognises.

    Only a sub-folder INSIDE a movie folder holds bonus material, and what tells
    one apart from a FILM called "Bonus" is its parent: an extras folder's parent
    is the movie folder and holds the film itself, while a movie folder's parent
    is a library or box-set folder and holds no film directly. That also keeps a
    box set working, which a fixed depth would not.
    """
    root = root.rstrip("/")
    for folder in _folders_below(root, min_depth=2):
        parent = os.path.dirname(folder)
        if not any(enums.lower_extension_of(name) == "mkv"
                   and os.path.isfile(os.path.join(parent, name))
                   for name in _names_in(parent)):
            continue

        folded = enums.shell_lower(folder)
        if folded.endswith(("extras", "specials", "bonus")):
            target = os.path.join(parent, "Featurettes")
            if folder != target:
                if os.path.exists(target):
                    skips.record(folder, target)
                    continue
                try:
                    os.rename(folder, target)
                except OSError:
                    continue
                folder = target

        if not folder.endswith("Featurettes"):
            continue

        # Nested folders are flattened into Featurettes first, so the
        # classification below sees one level of files.
        for nested in _files_below(folder, min_depth=2):
            destination = safety.unique_suffix_path(
                os.path.join(folder, os.path.basename(nested)))
            _rename_quiet(nested, destination)
        _remove_empty_below(folder)

        for name in sorted(_names_in(folder)):
            subfolder = bonus_category_for(name)
            if not subfolder:
                continue
            category_dir = os.path.join(parent, subfolder)
            os.makedirs(category_dir, exist_ok=True)
            destination = safety.unique_suffix_path(
                os.path.join(category_dir, name))
            _rename_quiet(os.path.join(folder, name), destination)


def mkv_mux(root: str, ram_root: str) -> None:
    """``mkvMux``: every non-Matroska video remuxed into one."""
    for path in _files_below(root, matches=lambda name:
                             enums.lower_extension_of(name)
                             in enums.SOURCE_VIDEO_EXTENSIONS):
        relative = os.path.relpath(path, root)
        log("Muxing into Matroska: ./" + relative)
        as_mkv = os.path.splitext(path)[0] + ".mkv"
        _rename_quiet(path, as_mkv)
        target = os.path.join(ram_root, os.path.splitext(relative)[0]
                              + " (compressed).mkv")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if _run(["mkvmerge", "-q", "-o", target, as_mkv]) == 0:
            _remove(as_mkv)
            _rename_quiet(target, as_mkv)


def update_tags(root: str) -> None:
    """``updateTags``: the mkv title and every track's flags, refreshed from the
    track layout in ONE mkvpropedit call per file.

    Each mkvpropedit call rewrites the file header, so batching turns dozens of
    rewrites into one.
    """
    for movie in _files_below(root, matches=lambda name:
                              name.endswith("mkv")):
        directory = os.path.dirname(movie)
        if is_bonus_folder(directory):
            continue

        log("Tagging tracks: ./" + os.path.relpath(movie, root))

        # Remembered so that adjusting the tags does not count as editing the
        # file.
        try:
            original = os.stat(movie).st_mtime
        except OSError:
            original = None

        title = os.path.splitext(os.path.basename(movie))[0]
        # Done before reading the track info, so the names read back reflect the
        # freshly written video-track name.
        if _run(["mkvpropedit", movie, "--edit", "info", "--set",
                 "title=" + title, "--edit", "track:v1", "--set",
                 "name=" + title]) >= 2:
            log("WARNING: mkvpropedit could not set title/video-track name: "
                + movie)

        tracks = _identify(movie)
        arguments = _tag_arguments(tracks)
        if arguments:
            failure = _run_capture(["mkvpropedit", movie] + arguments)
            if failure:
                log("WARNING: mkvpropedit could not apply all track flags: %s "
                    ":: %s" % (movie, failure.replace("\n", " | ")))

        if original is not None:
            try:
                os.utime(movie, (original, original))
            except OSError:
                pass


def _tag_arguments(tracks: list) -> list:
    """One ``--edit`` scope per track, bundling every ``--set`` for it together.

    Always the same numeric selector: addressing a track as ``track:$i`` here and
    as ``track:v1`` there makes mkvpropedit warn that the two resolve to the same
    track and exit non-zero, so every movie logged a spurious failure even though
    the tags were written.
    """
    # The default audio track is the first non-commentary audio track, falling
    # back to the first audio track when every one of them is a commentary.
    first_video = first_audio = default_audio = None
    for position, track in enumerate(tracks, start=1):
        if first_video is None and track.is_video:
            first_video = position
        if first_audio is None and track.is_audio:
            first_audio = position
        if default_audio is None and track.is_audio and not track.is_commentary:
            default_audio = position
    if default_audio is None:
        default_audio = first_audio

    arguments = []
    for position, track in enumerate(tracks, start=1):
        sets = ["--set", "flag-default=%d"
                % (1 if position in (first_video, default_audio) else 0)]

        folded = enums.shell_lower(track.name)
        if languages.is_commentary_name(track.name):
            sets += ["--set", "flag-commentary=1"]
        if "forced" in folded:
            sets += ["--set", "flag-forced=1"]
        if "sdh" in folded:
            sets += ["--set", "flag-hearing-impaired=1"]

        matched = False
        for row in languages.LANGUAGES:
            for keyword in row.keywords:
                if keyword in folded:
                    sets += ["--set", "language=" + row.code3]
                    matched = True
                    break

        # A commentary track whose name names no language is defaulted to
        # English, which most commentaries are - but ONLY when the file says
        # nothing itself. Overwriting a real tag here both mislabels the track
        # and destroys the only cheap hint the transcription has about which
        # language to work in.
        if (not matched and not languages.is_real_language_tag(track.language)
                and track.is_commentary):
            sets += ["--set", "language=eng"]

        if "isolated score" in folded:
            sets += ["--set", "language=zxx"]

        arguments += ["--edit", "track:%d" % position] + sets
    return arguments


def check_audio_tracks(movie: str, root: str) -> None:
    """``checkAudioTracks``: every lossless track transcoded to opus, at the
    per-channel target bitrate."""
    safety.trap_worker_abort()
    if is_bonus_folder(os.path.dirname(movie)):
        return

    tracks = _identify(movie)
    types = [track.type for track in tracks]
    file_duration = None

    for position, track in enumerate(tracks, start=1):
        if not is_lossless_track_codec(track.codec):
            continue
        # mkvtools index the whole matroska while ffmpeg indexes each track type
        # separately and from zero.
        index = audio_stream_index(position, types)
        bitrate = bitrates.audio_bitrate(track.channels, "normal")
        if not bitrate:
            continue

        # 3 and 4 channels are the only counts that need an explicit layout:
        # ffmpeg's own defaults for them (2.1 / 4.0) are layouts libopus refuses.
        # Forcing the layout only renames channels when the source carries no
        # LFE, so the source is checked: a 2.1 / 3.1 source - or one the probe
        # cannot read - would have its LFE remapped onto a regular speaker and is
        # kept lossless.
        layout = bitrates.audio_opus_layout(track.channels)
        if layout:
            source_layout = _probe([
                "ffprobe", "-v", "quiet", "-select_streams", "a:%d" % index,
                "-show_entries", "stream=channel_layout",
                "-of", "default=nk=1:nw=1", movie]).strip()
            if (track.channels, source_layout) not in (
                    ("3", "3.0"), ("4", "4.0"), ("4", "quad")):
                continue

        opus = "%s_%d.opus" % (os.path.splitext(movie)[0], position - 1)
        opus_duration = _duration_of(opus)
        # The source duration is the same for every track, so probe it once.
        if file_duration is None:
            file_duration = _duration_of(movie)

        # Resume: only when the output is missing or incomplete.
        if os.path.isfile(opus) and file_duration <= opus_duration:
            continue

        layout_args = (["-channel_layout", layout] if layout
                       else ["-ac", track.channels])
        log("Transcoding audio track %d to opus (%sk, %sch): %s"
            % (position - 1, bitrate, track.channels,
               "./" + os.path.relpath(movie, root)))
        if _run(["ffmpeg", "-y", "-loglevel", "error", "-nostats",
                 "-i", movie, "-vn", "-map", "0:a:%d" % index,
                 "-c:a", "libopus", "-b:a", bitrate + "k"]
                + layout_args + [opus]) != 0:
            log("WARNING: opus transcode failed (track %d): %s"
                % (position - 1, movie))


# --- the improved copy --------------------------------------------------------

def gather_commentary_transcripts(base: str, tracks: list) -> list:
    """``gatherCommentaryTranscripts``: the transcripts to append as commentary
    subtitle tracks, as (srt, language, title) triples.

    A transcript the movie already carries as a subtitle track - same name and
    language, one a previous improvement appended - is left out, or every run
    would pile another identical track on top of what the last one left.
    """
    found = []
    for track in tracks:
        if not (track.is_audio and track.is_commentary):
            continue
        name = track.name.replace("/", "").replace("&", "and")
        stem = "%s %s %s" % (base, track.id, rename(name))
        # Cut exactly as the transcription cut it when it wrote these, or this
        # would look for a name that was never written.
        stem = stem[:commentarytranscription.COMMENTARY_STEM_MAX_BYTES]

        # Gathered before any is muxed, so a track with more than one can have
        # the language put into their titles.
        transcripts = []
        directory = os.path.dirname(stem) or "."
        prefix = os.path.basename(stem) + "."
        for candidate in sorted(_names_in(directory)):
            if not (candidate.startswith(prefix)
                    and candidate.endswith(".srt")):
                continue
            suffix = candidate[:-len(".srt")].rpartition(".")[2]
            # Exactly what the transcription appends - never a
            # "<stem>.something else.srt" that happens to sit there.
            if not _TWO_LETTER.match(suffix):
                continue
            transcripts.append((os.path.join(directory, candidate), suffix))
        # One written by an older version carries no language suffix at all;
        # those were always English.
        if os.path.isfile(stem + ".srt"):
            transcripts.append((stem + ".srt", "eng"))

        for srt, language in transcripts:
            title = track.name
            if not title or title == "null":
                title = "Commentary"
            if len(transcripts) > 1:
                title = "%s (%s)" % (title, language.upper())
            if _already_a_subtitle(tracks, title, language):
                log("  Commentary transcript already a subtitle track in the "
                    "file, not appending it again: " + title)
                continue
            found.append((srt, language, title))
    return found


def _already_a_subtitle(tracks: list, title: str, language: str) -> bool:
    return any(track.is_subtitle and track.name == title
               and languages.same_language_tag(track.language, language)
               for track in tracks)


def apply_surround_ladder(tracks: list) -> list:
    """The surround ladder, per language: one winner - the best available tier -
    and every other ladder track of that language dropped.

    Candidates are the lossy compatibility tracks that survived the decisions
    before this. Opus tracks are not on the ladder, and neither are commentary
    tracks: a commentary is bonus material rather than a second soundtrack. Ties
    keep the first track, except that a default track beats a non-default one.
    """
    best: dict[str, int] = {}
    winner: dict[str, int] = {}
    winner_default: dict[str, bool] = {}
    scores: dict[int, int] = {}
    for position, track in enumerate(tracks):
        if not (track.is_audio and track.action == "keep"):
            continue
        if not track.codec.upper().startswith(("A_EAC3", "A_AC3")):
            continue
        if track.is_commentary:
            continue
        score = objectaudio.audio_ladder_score(track.codec, track.channels,
                                               track.objects)
        if not score:
            continue
        scores[position] = int(score)
        language = enums.shell_lower(track.language) or "und"
        is_default = track.default == "true"
        if (language not in best or int(score) > best[language]
                or (int(score) == best[language] and is_default
                    and not winner_default[language])):
            best[language] = int(score)
            winner[language] = position
            winner_default[language] = is_default

    dropped = []
    for position, points in scores.items():
        track = tracks[position]
        language = enums.shell_lower(track.language) or "und"
        if points < best[language] or winner[language] != position:
            track.action = "drop"
            dropped.append((position, winner[language]))
    return dropped


def decide_actions(tracks: list, base: str) -> bool:
    """keep, drop or swap, per track; True when anything changed.

    A track "was converted to opus" is decided by the presence of its opus file
    rather than by re-deriving losslessness, which is what makes every exception
    fall out for free: a 3/4-channel source whose layout carries an LFE, a bed
    above the libopus ceiling and a non-lossless commentary all simply have no
    opus, so they stay as they are.
    """
    primary = ""
    for track in tracks:
        if track.is_audio:
            primary = enums.shell_lower(track.language)
            break

    changed = False
    for track in tracks:
        if track.is_audio:
            # Excessive language: neither the first audio track's nor English.
            # Commentary tracks are exempt - a commentary is bonus material
            # rather than a second soundtrack, its language is often the only one
            # it comes in, and its transcript is being kept either way.
            if (not language_allowed(track.language, primary)
                    and not track.is_commentary):
                track.action = "drop"
            else:
                opus = "%s_%s.opus" % (base, track.id)
                if os.path.isfile(opus):
                    if "flac" in enums.shell_lower(track.codec) \
                            and track.channels == "1":
                        # FLAC 1.0: barely worth it, and some TVs choke on opus
                        # mono, so the lossless stays even though an opus exists.
                        track.action = "keep"
                    else:
                        track.action = "swap"
                        track.opus = opus
                else:
                    track.action = "keep"
        elif track.is_subtitle:
            if is_image_sub_codec(track.codec) \
                    and not language_allowed(track.language, primary):
                track.action = "drop"
        if track.action != "keep":
            changed = True
    return changed


def _duration_of(path: str) -> int:
    text = _probe(["ffprobe", "-v", "quiet", "-show_entries",
                   "format=duration", "-of", "default=nk=1:nw=1", path])
    from medialib.lib import formatting
    return int(round(formatting.awk_number(text.strip() or 0)))


# --- the small shells ---------------------------------------------------------

def _run(argv: list) -> int:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return 127
    return done.returncode


def _run_capture(argv: list) -> str:
    """The tool's own output, when it failed for real. mkvpropedit exits 1 on
    non-fatal warnings while still writing the tags, so only 2 and above is a
    failure worth a word."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
    except OSError:
        return "mkvpropedit not found"
    if done.returncode >= 2:
        return done.stdout.decode("utf-8", "surrogateescape").strip()
    return ""


def _probe(argv: list) -> str:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    return done.stdout.decode("utf-8", "surrogateescape")


def _size_of(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _rename_quiet(source: str, target: str) -> None:
    """``mv``: a rename where that works, a copy where it cannot.

    The scratch is a tmpfs and the library is on disk, so the move that puts an
    improved copy in its original's place crosses a file system - which
    ``os.replace`` refuses and ``mv`` does not.
    """
    try:
        os.replace(source, target)
        return
    except OSError as error:
        import errno
        if error.errno != errno.EXDEV:
            return
    try:
        shutil.move(source, target)
    except (OSError, shutil.Error):
        pass


def _names_in(directory: str) -> list:
    try:
        return os.listdir(directory)
    except OSError:
        return []


def _folders_below(root: str, min_depth: int = 1) -> list:
    found = []
    for parent, dirs, _names in os.walk(root):
        dirs.sort()
        for name in dirs:
            path = os.path.join(parent, name)
            depth = len(os.path.relpath(path, root).split(os.sep))
            if depth >= min_depth:
                found.append(path)
    return found


def _files_below(root: str, matches=None, min_depth: int = 1) -> list:
    found = []
    for parent, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(parent, name)
            depth = len(os.path.relpath(path, root).split(os.sep))
            if depth < min_depth:
                continue
            if matches is None or matches(name):
                found.append(path)
    return found


def _movies_one_level_down(root: str) -> list:
    """``find -maxdepth 2 -mindepth 2 -name '*.mkv'``: the films in the folders
    directly below the library root."""
    found = []
    for name in sorted(_names_in(root)):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        for entry in sorted(_names_in(folder)):
            path = os.path.join(folder, entry)
            if entry.endswith(".mkv") and os.path.isfile(path):
                found.append(path)
    return found


if __name__ == "__main__":
    from medialib.cli import ingest_movies_run
    raise SystemExit(ingest_movies_run.main(
        sys.argv[1:],
        commands.program_name(ingest_movies_run.__name__),
        commands.script_dir()))
