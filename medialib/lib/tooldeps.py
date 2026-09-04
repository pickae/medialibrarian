"""The external-tool preflight.

Every entry script drives half a dozen external binaries, and a missing one
would otherwise surface as whatever that particular call site does when its
tool is not there - fifteen minutes into a run, per file, with the input
already de-duplicated and renamed. The preflight turns all of that into one
refusal before any work starts, naming every tool that is missing at once:
all of them, in the order the caller gave them (the order the script would
have reached them in), each with what it is for and how to install it.

A spec is a command name (``"ffmpeg"``) or a set of alternatives written with
``|`` (``"7z|7zz|7za"``), satisfied by any one of them. The refusal is a
return value, not an exception: the shell's ``exit 1`` is
``sys.exit(require_tools(...))`` at the call site, and the message goes to
stderr so the stdout the run would have produced stays clean.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Sequence

from medialib.lib import hostos

__all__ = [
    "tool_present",
    "tool_note",
    "require_tools",
    "require_python_module",
]

# "<role>|<install hint>" for one command name: what the scripts use it for,
# and how to get it on a Debian/Ubuntu box (the platform the README's
# requirements are written for). ``_MACOS_HINTS`` below replaces the second
# half of the line on a Mac; the role is the same sentence on every host.
_TOOL_NOTES = {
    "ffmpeg": "audio/video de- and encoding|apt install ffmpeg",
    "ffprobe": "reading media durations, codecs and chapters|apt install ffmpeg",
    "mkvmerge": "muxing Matroska (chapters, track flags)|apt install mkvtoolnix",
    "mkvpropedit": "editing Matroska titles and track names in place|apt install mkvtoolnix",
    "mkvextract": "extracting cover attachments from Matroska|apt install mkvtoolnix",
    "mediainfo": "reading HDR/Dolby Vision stream properties|apt install mediainfo",
    "dovi_tool": "Dolby Vision RPU extraction and conversion"
                 "|https://github.com/quietvoid/dovi_tool (release binary)",
    "jq": "reading the JSON that ffprobe and mkvmerge print|apt install jq",
    "curl": "the TheMovieDB lookups|apt install curl",
    "rsync": "copying side-car files across trees|apt install rsync",
    "fdupes": "de-duplicating the input folder|apt install fdupes",
    "tree": "the before/after folder snapshots|apt install tree",
    "flock": "the progress counters shared by parallel workers|apt install util-linux",
    "unzip": "unpacking .cbz/.epub archives|apt install unzip",
    "zip": "packing .cbz/.epub archives|apt install zip",
    "unrar": "unpacking .cbr comics and .rar input folders|apt install unrar",
    "tar": "unpacking .tar input folders, compressed or not|apt install tar",
    "zstd": "the de-compression tar shells out to for .tar.zst|apt install zstd",
    "7z": "unpacking .cb7 comics|apt install p7zip-full",
    "7zz": "unpacking .cb7 comics|apt install p7zip-full",
    "7za": "unpacking .cb7 comics|apt install p7zip-full",
    "zpaq": "the -z archive of the converted text|apt install zpaq",
    "convert": "image conversion and downscaling (ImageMagick)|apt install imagemagick",
    "magick": "image conversion and downscaling (ImageMagick)|apt install imagemagick",
    "identify": "reading image dimensions (ImageMagick)|apt install imagemagick",
    "pdftoppm": "rasterising PDF pages|apt install poppler-utils",
    "pdfinfo": "reading PDF page geometry|apt install poppler-utils",
    "pdfimages": "listing the images a PDF page holds|apt install poppler-utils",
    "pdftotext": "extracting a PDF's text|apt install poppler-utils",
    "ebook-convert": "the e-book format conversions (Calibre)|apt install calibre",
    "gs": "shrinking the images in PDFs (Ghostscript)|apt install ghostscript",
    "beet": "tagging the imported music (beets)|apt install beets",
    "yt-dlp": "fetching the podcast audio"
              "|pipx install --suffix=-nightly --pip-args=--pre yt-dlp (or: apt install yt-dlp)",
    "AtomicParsley": "embedding cover art into m4a podcast episodes"
                     "|apt install atomicparsley",
    "duckdb": "building the census hypercubes"
              "|https://duckdb.org/docs/installation (single binary)",
    "ffsubsync": "aligning subtitles to the audio|pipx install ffsubsync",
    "pipx": "running whisper-ctranslate2 and subliminal|apt install pipx",
    "python3": "the mutagen tag/chapter helpers|apt install python3",
    "adb": "reaching the files on the attached phone|apt install android-tools-adb",
}

# What to say instead of "apt install ..." on a Mac, one entry per name in
# ``_TOOL_NOTES``. Homebrew is the package manager the rest of these tools come
# from there, and the four that ship WITH macOS say so rather than naming a
# formula that would reinstall something already present.
#
# Two of them are not a plain `brew install <name>`, and the reason is worth
# keeping: 7-Zip's Homebrew formula is called "sevenzip" and installs the
# binary as `7zz`, and unrar was dropped from homebrew-core over its licence,
# so the tap that still carries it is named in full.
#
# Kept complete by a test rather than by care - a tool added above with no line
# here would otherwise print an apt command to a Mac user.
_MACOS_HINTS = {
    "ffmpeg": "brew install ffmpeg",
    "ffprobe": "brew install ffmpeg",
    "mkvmerge": "brew install mkvtoolnix",
    "mkvpropedit": "brew install mkvtoolnix",
    "mkvextract": "brew install mkvtoolnix",
    "mediainfo": "brew install mediainfo",
    "dovi_tool": "brew install dovi_tool",
    "jq": "brew install jq",
    "curl": "ships with macOS",
    # The bundled rsync is 2.6.9 (or openrsync on 14+), and ingest-music reads
    # what a copy transferred - see _transfer_format in that command, which
    # falls back to the old spelling of the flag rather than failing here.
    "rsync": "ships with macOS (brew install rsync for a current one)",
    "fdupes": "brew install fdupes",
    "tree": "brew install tree",
    "flock": "not needed on macOS - the C library locks (brew install flock)",
    "unzip": "ships with macOS",
    "zip": "ships with macOS",
    "unrar": "brew install carlocab/personal/unrar",
    "tar": "ships with macOS",
    "zstd": "brew install zstd",
    "7z": "brew install sevenzip",
    "7zz": "brew install sevenzip",
    "7za": "brew install sevenzip",
    "zpaq": "brew install zpaq",
    "convert": "brew install imagemagick",
    "magick": "brew install imagemagick",
    "identify": "brew install imagemagick",
    "pdftoppm": "brew install poppler",
    "pdfinfo": "brew install poppler",
    "pdfimages": "brew install poppler",
    "pdftotext": "brew install poppler",
    "ebook-convert": "brew install --cask calibre",
    "gs": "brew install ghostscript",
    "beet": "brew install beets",
    "yt-dlp": "pipx install --suffix=-nightly --pip-args=--pre yt-dlp"
              " (or: brew install yt-dlp)",
    "AtomicParsley": "brew install atomicparsley",
    "duckdb": "brew install duckdb",
    "ffsubsync": "pipx install ffsubsync",
    "pipx": "brew install pipx",
    "python3": "brew install python",
    "adb": "brew install --cask android-platform-tools",
}

# The line a name with no entry falls through to, rather than to an empty one,
# so adding a tool to a caller's list can never produce a half-empty message.
_GENERIC_NOTE = 'required by this script|check your package manager for "%s"'
_MACOS_GENERIC_HINT = 'brew search "%s"'


def tool_present(spec: str) -> bool:
    """True when <spec> is satisfied: a command name, or a set of ``|``
    alternatives any one of which is present.

    The presence test is the shell's ``command -v``, and its two faces differ.
    A bare name is looked up on PATH, and bash's PATH scan does not test the
    executable bit - a non-executable file, and even a fifo, read as present -
    so the rule is: some PATH directory holds a non-directory entry by that
    name. A name with a slash is a direct path (the callers that resolve a
    binary themselves), present only when it is accessible for execution - a
    non-executable file reads as absent. An empty alternative (the middle of
    ``a||b``) never satisfies. Shell builtins are not on a PATH and are not
    present: this module is about tools a run would reach for, and a builtin
    is nothing a script would install.
    """
    for candidate in spec.split("|"):
        if os.sep in candidate:
            if os.access(candidate, os.X_OK):
                return True
            continue
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                # An empty PATH element is the current directory, the way the
                # shell treats it.
                directory = os.curdir
            try:
                info = os.stat(os.path.join(directory, candidate))
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                return True
    return False


def tool_note(tool: str, platform: str | None = None) -> str:
    """``<role>|<install hint>`` for one command name, the table's line for a
    known name and the generic one for everything else.

    The role is the same everywhere; the hint is the host's. ``platform`` is a
    ``sys.platform`` value to answer for instead of this one, which is how a
    case asks what a Mac would be told.
    """
    note = _TOOL_NOTES.get(tool, _GENERIC_NOTE % tool)
    if not hostos.is_macos(platform):
        return note
    role = note.split("|", 1)[0]
    return role + "|" + _MACOS_HINTS.get(tool, _MACOS_GENERIC_HINT % tool)


def _pad_bytes(text: str, width: int) -> str:
    """``printf %-*s``: the field is counted in BYTES, so a multibyte name
    earns fewer spaces than a character count would give."""
    return text + " " * max(0, width - len(text.encode("utf-8")))


def require_tools(what: str, specs: Sequence[str], *,
                  skip_preflight: bool = False, file=None) -> int:
    """Name every missing tool at once and return 1; return 0 silently when
    they are all there.

    <what> names the thing that cannot run ("the conversion", "simulation
    mode (-s)") and is quoted back to the user, so a script with a
    flag-conditional dependency can say which flag asked for it. All of the
    missing tools are reported, not just the first, and in the order the
    caller gave them; an alternatives spec is reported as the alternatives it
    is (``a or b``). The message goes to <file> (default: stderr) and the
    return value is the refusal - the call site exits on it.
    """
    if file is None:
        file = sys.stderr
    if skip_preflight:
        return 0
    missing = [spec for spec in specs if not tool_present(spec)]
    if not missing:
        return 0

    # Column widths from the longest entry actually being printed - in
    # CHARACTERS, the way ${#name} counts them - while the padding below is
    # printf's, in BYTES: a multibyte name that is the widest one pads nothing,
    # and the names beside it pad to its character width, not its byte width.
    names: list[str] = []
    roles: list[str] = []
    hints: list[str] = []
    width = 0
    role_width = 0
    for spec in missing:
        name = spec.replace("|", " or ")
        role, hint = tool_note(spec.split("|")[0]).split("|", 1)
        names.append(name)
        roles.append(role)
        hints.append(hint)
        width = max(width, len(name))
        role_width = max(role_width, len(role))

    text = "\n"
    if len(missing) == 1:
        text += "Cannot run {}: it needs a tool this machine does not have.\n\n".format(what)
    else:
        text += ("Cannot run {}: it needs {} tools this machine does not have.\n\n"
                 .format(what, len(missing)))
    for name, role, hint in zip(names, roles, hints, strict=True):
        text += "  {}  {}  {}\n".format(_pad_bytes(name, width),
                                        _pad_bytes(role, role_width), hint)
    text += "\nInstall the above (or put it on PATH) and run again. Nothing was changed.\n"
    file.write(text)
    return 1


def _module_hint(module: str, platform: str | None = None) -> str:
    """How to get one importable Python package on this host.

    A Mac gets pip alone: Homebrew carries almost none of these as formulae,
    and the distro package that is the first thing to reach for on Debian has
    no counterpart there.
    """
    if hostos.is_macos(platform):
        return "pip install {}".format(module)
    return "apt install python3-{}  (or: pip install {})".format(module, module)


def require_python_module(module: str, what: str, role: str = "used by this script", *,
                          skip_preflight: bool = False, file=None) -> int:
    """The same refusal for an importable Python package: the dependency is of
    exactly the same kind as a binary here.

    The module is imported rather than looked for on disk, on the same
    interpreter the helpers actually run on (a fresh process, the way
    ``pythonRun -c "import $module"`` does), because a package can be
    installed for a different interpreter than the one the run will use.

    That interpreter is ``sys.executable`` and not whatever ``python3`` PATH
    resolves to, so the callers do NOT ask ``require_tools`` for python3 first
    - one that is missing from PATH is not one this cannot run on. The
    exception is a caller that really does start a python off PATH
    (read-library, through the narration library's environment build), and it
    asks for its own.
    """
    if file is None:
        file = sys.stderr
    if skip_preflight:
        return 0
    try:
        ran = subprocess.run([sys.executable, "-c", "import " + module],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        ran = None
    if ran is not None and ran.returncode == 0:
        return 0
    text = "\n"
    text += 'Cannot run {}: the Python "{}" package is not importable.\n\n'.format(what, module)
    text += "  {}  {}\n".format(module, role)
    text += "  {}  {}\n".format(" " * len(module), _module_hint(module))
    text += "\nInstall it and run again. Nothing was changed.\n"
    file.write(text)
    return 1