"""The settings a user gives these commands: what each is called, what it is
for, which commands read it, and where a run finds it.

Every setting is an environment variable, and every command finds it in the
same two places: the environment the command was started in, and a settings
file - ``~/.config/medialib/settings``, or ``$XDG_CONFIG_HOME/medialib/settings``
where that is set. The environment wins, so a value given for one run
overrides the file without editing it.

The file is one ``name=value`` per line, the way a shell writes an
assignment, so a line copied out of a shell profile works as it is: an
``export`` in front is allowed, a value may be quoted, and a line starting
with ``#`` is a comment. It is read when a command starts, into that
command's environment, which is how a tool or a helper the command starts
gets the value too.

Three things are refused rather than guessed at. A file that holds a secret -
a password, a key, a token - and that another account on the machine can
read stops the run until it is made private, because a settings file is
exactly where those end up. A line that is not an assignment stops the run
too, naming the line. A name nothing here reads is only warned about, with
the setting it most looks like, because a typo is the likeliest reason for it
and a run that refused over it would be refusing over nothing.

:data:`SETTINGS` is the one list of the names. Nothing reads a setting the
list does not hold - the tests read every module and fail on one - and
`docs/settings.md` is written from it, so the page cannot fall behind either.
Variables the commands set for one another, in upper case, are not settings
and are not listed: nobody is meant to set them.
"""

from __future__ import annotations

import difflib
import os
import shlex
import stat
import sys
from typing import NamedTuple

__all__ = ["Setting", "SETTINGS", "INTERNAL", "SettingsError", "settings_file",
           "load", "read_file", "shell_value", "page"]

# Where a run is told to look instead, for the suite - which must never read
# the settings of the machine it runs on - and for anyone keeping two.
FILE_VARIABLE = "CLI_SETTINGS_FILE"


class Setting(NamedTuple):
    """One setting: its name, what it does, the commands that read it, its
    default as a user would describe it, and whether it is a secret."""

    name: str
    about: str
    commands: tuple
    default: str = "unset"
    secret: bool = False


class SettingsError(Exception):
    """The settings file cannot be used as it stands."""


_NARRATION = ("read-library",)
_COMICS = ("content-census", "convert-comics")
# Every command that may pick an ffmpeg build.
_FFMPEG = ("concat-audio", "content-census", "convert-and-concat", "convert-audio",
           "convert-video", "ingest-movies", "ingest-music", "read-library",
           "transcribe-audio", "ytdlp")
# Every command that keeps scratch in RAM.
_SCRATCH = ("concat-audio", "content-census", "content-census-bi", "convert-and-concat",
            "convert-audio", "convert-comics", "convert-images", "convert-video",
            "ingest-books", "ingest-movies", "ingest-music", "read-library",
            "transcribe-audio", "ytdlp")

SETTINGS: tuple[Setting, ...] = (
    # --- logins and keys
    Setting("tmdbApiKey", "TheMovieDB API key, for naming films - and tagging their "
            "folders with an id, or matching them to Letterboxd's; without it the "
            "lookup is skipped", ("ingest-movies", "plex-letterboxd-sync"), secret=True),
    Setting("plexUrl", "The Plex server, e.g. `http://192.168.1.10:32400`",
            ("plex-letterboxd-sync",)),
    Setting("plexToken", "That server's X-Plex-Token", ("plex-letterboxd-sync",),
            secret=True),
    Setting("openSubtitlesUser", "OpenSubtitles.org login; with "
            "`openSubtitlesPassword`, subtitles are looked for there first",
            ("ingest-movies",), secret=True),
    Setting("openSubtitlesPassword", "OpenSubtitles.org password", ("ingest-movies",),
            secret=True),
    Setting("openSubtitlesComUser", "OpenSubtitles.com login - a separate service, "
            "asked second", ("ingest-movies",), secret=True),
    Setting("openSubtitlesComPassword", "OpenSubtitles.com password", ("ingest-movies",),
            secret=True),
    Setting("subDlApiKey", "API key of a free SubDL account, asked third for subtitles",
            ("ingest-movies",), secret=True),
    # --- where work is done
    Setting("ramScratchBase", "Where each run's scratch directories go",
            _SCRATCH, "`/dev/shm`, else the system temp directory"),
    Setting("ramScratchMaxPercent", "The largest share of the RAM filesystem, in "
            "percent, one intermediate file may take", _SCRATCH, "50"),
    Setting("ramScratchMinFreeBytes", "With this little free in RAM, new scratch "
            "goes to disk instead", _SCRATCH, "1073741824"),
    Setting("ramScratchDiskBase", "Where scratch goes on disk once RAM is nearly full",
            _SCRATCH, "the cache directory's `ramScratchOverflow`"),
    Setting("censusRamBase", "The census's scratch, ahead of `ramScratchBase`",
            ("content-census", "content-census-bi"), "`ramScratchBase`"),
    Setting("comicsRamBase", "convert-comics' scratch, ahead of `ramScratchBase`",
            ("convert-comics",), "`ramScratchBase`"),
    Setting("musicRamBase", "ingest-music's scratch, ahead of `ramScratchBase`",
            ("ingest-music",), "`ramScratchBase`"),
    Setting("readLibraryRamBase", "read-library's scratch, ahead of `ramScratchBase`",
            _NARRATION, "`ramScratchBase`"),
    # --- tools and sites
    Setting("ffmpegOverride", "The ffmpeg every run uses, by its path; a run refuses "
            "to start when it is not executable", _FFMPEG),
    Setting("ffmpegLadder", "The directories searched for ffmpeg after `PATH`, "
            "separated as `PATH` is - there for the test suite", _FFMPEG,
            "`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/opt/ffmpeg/bin`, "
            "`/usr/bin`"),
    Setting("imageGpuEncoder", "The GPU AVIF encoder (av1gpu) tried before "
            "ImageMagick, by name or path; empty to encode every image on the CPU",
            ("convert-images",), "`av1gpu`"),
    Setting("upscaleHome", "The VapourSynth environment `convert-video -u` upscales "
            "with", ("convert-video",), "`~/vapoursynth`"),
    Setting("upscaleModel", "The upscaling model: a path, or a file name under "
            "`<upscaleHome>/models`", ("convert-video",), "`2xLiveActionV1_SPAN_490000.onnx`"),
    Setting("chapterDbSite", "The chapter database's address - there for the test "
            "suite", ("ingest-movies",), "`https://chapterdb.plex.tv`"),
    Setting("dvdCompareSite", "The disc database's address - there for the test suite",
            ("ingest-movies",), "`http://www.dvdcompare.net/comparisons/`"),
    Setting("viewerCdnBase", "Where the census page loads its pivot-table engine "
            "from; a local copy makes the page fetch nothing", ("content-census-bi",),
            "`https://cdn.jsdelivr.net/npm`"),
    Setting("viewerPerspectiveVersion", "Which release of that engine the page loads",
            ("content-census-bi",), "5.3.0"),
    # --- comic PDFs
    Setting("comicPdfExtensions", "The file suffixes that may be comic PDFs, "
            "space-separated", ("content-census",), "pdf"),
    Setting("comicPdfMinCoverage", "How much of a page, in percent, its one image has "
            "to cover for it to be a comic page", _COMICS, "80"),
    Setting("comicPdfMinPixels", "How wide and high a page image has to be, in pixels",
            _COMICS, "400"),
    Setting("comicPdfMinPageShare", "How many of a PDF's pages, in percent, have to be "
            "comic pages for it to be a comic", _COMICS, "90"),
    Setting("comicPdfMinDpi", "The lowest resolution a comic PDF is rendered at",
            _COMICS, "150"),
    Setting("comicPdfMaxDpi", "The highest resolution a comic PDF is rendered at",
            _COMICS, "600"),
    Setting("comicPdfFallbackDpi", "The resolution used where the PDF's own cannot be "
            "read", _COMICS, "300"),
    Setting("comicPdfJpegQuality", "The JPEG quality comic PDF pages are rendered at",
            _COMICS, "92"),
    # --- reading books aloud
    Setting("narrationHome", "The ebook2audiobook checkout read-library drives; `-c` "
            "overrides it", _NARRATION, "`~/ebook2audiobook`"),
    Setting("narrationLanguage", "The language a book is read in when its own cannot "
            "be found", _NARRATION),
    Setting("narrationFormat", "The format the engine writes an audiobook in",
            _NARRATION, "m4b"),
    Setting("narrationChannel", "The engine's channel layout", _NARRATION, "mono"),
    Setting("narrationVramPerBookGB", "The GPU memory, in GB, each book read at once "
            "needs - which sets the default `-j`", _NARRATION, "5"),
    Setting("narrationEngineLanguages", "Which languages each engine speaks, as "
            "`engine:codes`", _NARRATION, "XTTS's seventeen"),
    Setting("narrationPythonMinMinor", "The oldest Python 3 minor version the "
            "engine's environment is built with", _NARRATION, "10"),
    Setting("narrationPythonMaxMinor", "The newest Python 3 minor version the "
            "engine's environment is built with", _NARRATION, "12"),
    Setting("narrationTorchcodecPairs", "Which torchcodec goes with which torch, for "
            "repairing a mismatch", _NARRATION, "2.9:0.9 2.10:0.10 2.11:0.11 2.13:0.15 2.14:0.17"),
    Setting("narrationSoundfileFallbackMark", "The text that marks the engine shim "
            "this package wrote", _NARRATION, "`_load_with_soundfile_fallback`"),
    Setting("narrationProgressTail", "How much of the end of the engine's log, in "
            "bytes, is read for its progress", _NARRATION, "65536"),
    Setting("narrationLosslessTolerance", "How far the engine's lossless copy may be "
            "from the book's length and still be found", _NARRATION, "0.02"),
    Setting("narrationVoiceFallbackNames", "The voice-sample names that mean \"any "
            "language\"", _NARRATION, "default any fallback other"),
    Setting("audiobookLosslessFilters", "The ffmpeg filters the lossless copy of an "
            "audiobook is made with; empty for none", _NARRATION,
            "`dynaudnorm=f=150:g=15,afftdn=nf=-70`"),
    Setting("audiobookLosslessCompression", "The FLAC compression level of that copy",
            _NARRATION, "5"),
    Setting("voiceSampleMaxSeconds", "A voice sample longer than this, in seconds, is "
            "cut down to a stretch of speech", _NARRATION, "60"),
    Setting("voiceSampleSearchSeconds", "How much of a long sample's middle, in "
            "seconds, is searched for speech", _NARRATION, "180"),
    Setting("voiceSampleSeconds", "How long the stretch of speech taken is, in seconds",
            _NARRATION, "45"),
    Setting("voiceSampleMinSeconds", "The shortest stretch still taken, in seconds",
            _NARRATION, "5"),
    Setting("voiceSampleMaxGap", "The longest pause, in seconds, inside that stretch",
            _NARRATION, "2.5"),
    Setting("voiceSilenceNoise", "What counts as silence when looking for it",
            _NARRATION, "-30dB"),
    Setting("voiceSilenceMinDur", "The shortest silence, in seconds, that counts",
            _NARRATION, "0.3"),
    Setting("voiceSampleRate", "The sample rate a prepared voice sample is written at",
            _NARRATION, "24000"),
    Setting("voiceSampleCodec", "The codec a prepared voice sample is written in",
            _NARRATION, "pcm_s16le"),
)

# Spelled like settings and not settings: what a command works out and hands
# to the commands, workers or engine it starts, overwriting whatever was
# there. Setting one changes nothing, so no page offers them.
INTERNAL = frozenset((
    "narrationPython", "narrationDevice", "narrationEnvReady", "narrationVerbose",
    "narrationVoiceMap", "narrationEngine", "censusVideoExtensions",
    "censusPageExtensions", "censusAllExtensions", "imageStatsFile",
    # The usage text a refusal prints after itself, which only a shell caller
    # ever set.
    "usage",
))

BY_NAME = {setting.name: setting for setting in SETTINGS}


def settings_file() -> str:
    """Where the settings file is, whether or not there is one."""
    named = os.environ.get(FILE_VARIABLE, "")
    if named:
        return named
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "medialib", "settings")


def read_file(path: str) -> dict[str, str]:
    """The assignments in a settings file, in its order.

    SettingsError for a line that is not one, naming the line.
    """
    found: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if text.startswith("export "):
                text = text[len("export "):].lstrip()
            name, sep, value = text.partition("=")
            if not sep or not name.isidentifier():
                raise SettingsError("%s, line %d: not a name=value line"
                                    % (path, number))
            try:
                found[name] = shell_value(value, {**os.environ, **found})
            except ValueError as error:
                raise SettingsError("%s, line %d: %s" % (path, number, error)) from error
    return found


def shell_value(text: str, environ) -> str:
    """The value a shell gives ``name=<text>``.

    Read the way bash reads it, because the lines are copied out of a shell
    profile: what is single-quoted is taken as it is; what is double-quoted or
    not quoted at all has ``$NAME`` and ``${NAME}`` replaced from ``environ``
    and a backslash escaping the character after it; a ``~`` it starts with is
    the home directory; and the value ends at the first space not inside
    quotes, after which only a ``#`` comment may follow. ValueError for a
    quote left open, or for more than one word.
    """
    out: list[str] = []
    at, quote = 0, ""
    if text.startswith("~") and (len(text) == 1 or text[1] == "/"):
        out.append(os.path.expanduser("~"))
        at = 1
    while at < len(text):
        char = text[at]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                out.append(char)
        elif char == "\\" and at + 1 < len(text) and (
                not quote or text[at + 1] in '$`"\\'):
            out.append(text[at + 1])
            at += 1
        elif char == "$" and _variable(text, at + 1)[0]:
            name, length = _variable(text, at + 1)
            out.append(environ.get(name, ""))
            at += length
        elif quote == '"':
            if char == '"':
                quote = ""
            else:
                out.append(char)
        elif char in "'\"":
            quote = char
        elif char.isspace():
            rest = text[at:].strip()
            if rest and not rest.startswith("#"):
                raise ValueError("a value with spaces has to be quoted")
            break
        else:
            out.append(char)
        at += 1
    if quote:
        raise ValueError("a %s quote is never closed" % quote)
    return "".join(out)


def _variable(text: str, at: int) -> tuple[str, int]:
    """The name a ``$`` just before ``at`` refers to, and how many characters
    after the ``$`` it takes - ("", 0) where it refers to none."""
    if text[at:at + 1] == "{":
        end = text.find("}", at)
        name = text[at + 1:end] if end > 0 else ""
        return (name, end - at + 1) if name.isidentifier() else ("", 0)
    end = at
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    name = text[at:end]
    return (name, end - at) if name.isidentifier() else ("", 0)


def _readable_by_others(path: str) -> bool:
    """Whether another account can read the file. Windows keeps no such
    bits in a file's mode, and is not asked."""
    if os.name == "nt":
        return False
    return bool(os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def load(stream=None) -> None:
    """Read the settings file into this process's environment, under what
    the environment already says. Nothing to do where there is no file.

    The first thing every command does. A file that cannot be used ends the
    run: SystemExit, after saying why.
    """
    stream = sys.stderr if stream is None else stream
    path = settings_file()
    if not os.path.isfile(path):
        return
    try:
        values = read_file(path)
        secrets = [name for name in values
                   if name in BY_NAME and BY_NAME[name].secret]
        if secrets and _readable_by_others(path):
            raise SettingsError(
                "%s holds %s and other accounts on this machine can read it - "
                "make it private first: chmod 600 %s"
                % (path, ", ".join(secrets), shlex.quote(path)))
    except (OSError, SettingsError) as error:
        stream.write("==> ERROR: %s\n" % error)
        raise SystemExit(1) from error
    for name, value in values.items():
        if name not in BY_NAME:
            near = difflib.get_close_matches(name, BY_NAME, n=1)
            stream.write("==> WARNING: %s sets %s, which no command reads%s\n"
                         % (path, name, " - did you mean %s?" % near[0] if near else ""))
        os.environ.setdefault(name, value)


def page() -> str:
    """docs/settings.md, as the registry says it."""
    lines = [
        "# Settings",
        "",
        "Every setting is an environment variable. A command finds it in the",
        "environment it was started in, or in the settings file",
        "`~/.config/medialib/settings` (`$XDG_CONFIG_HOME/medialib/settings` where",
        "that is set) - one `name=value` per line, the way a shell profile writes",
        "it, `export` and quotes allowed. The environment wins, so one run can",
        "override the file without editing it.",
        "",
        "A file that holds a secret (marked below) must be readable by you alone -",
        "`chmod 600` - or every command refuses to start. A name in the file that",
        "no command reads is warned about.",
        "",
        "This page is written from `medialib/lib/settings.py`; the tests fail if",
        "the two disagree.",
        "",
        "| Setting | What it does | Default | Read by |",
        "| --- | --- | --- | --- |",
    ]
    for setting in sorted(SETTINGS, key=lambda one: one.name.lower()):
        lines.append("| `%s`%s | %s | %s | %s |" % (
            setting.name, " (secret)" if setting.secret else "",
            setting.about.replace("|", "\\|"), setting.default.replace("|", "\\|"),
            ", ".join("`%s`" % command for command in setting.commands)))
    return "\n".join(lines) + "\n"
