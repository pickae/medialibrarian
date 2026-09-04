"""The tidy-up a finished download needs.

A yt-dlp run does not leave one file per episode. It leaves the episode plus
whatever its arguments asked to be written beside it - the thumbnail it could not
embed, the description, the metadata sidecar, the subtitles it converted - and,
when a download was interrupted, the half of a fragment it was in the middle of.

This module is told WHICH files a run produced, so it removes the sidecars of
THOSE episodes rather than every ``.jpg`` under the library root: from a
"find -name '*.jpg' -delete", a leftover thumbnail and a folder's cover art are
indistinguishable.

The functions return their result rather than setting globals, the way the shell
version sets ``CLEANED_PATH`` / ``CLEANED_SIDECARS`` / ``CLEANED_REMUXED`` /
``SWEPT_PARTIALS`` / ``PRUNED_FOLDERS``; a return value, not an exit status, is
how a Python caller learns what happened.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import subprocess
from collections.abc import Callable, Sequence

from medialib.lib.enums import VIDEO_EXTENSIONS, lower_extension_of
from medialib.lib.safety import SkipLog, is_empty_folder, lower_case_ending

__all__ = [
    "DOWNLOAD_SIDECARS",
    "DOWNLOAD_PARTIALS",
    "DOWNLOAD_AUDIO_KEPT",
    "download_cleanup_tools",
    "clean_downloaded_file",
    "sweep_partial_downloads",
    "prune_empty_folders",
]

# What sits beside a finished episode and is not wanted once it is finished: the
# thumbnail (embedded, so the loose copy is only what the embedding step read),
# the description and the metadata json (attached into the Matroska), and the
# converted subtitles (muxed in; --compat-options no-keep-subs asks yt-dlp to
# delete them, which it does not always manage when a run is interrupted).
DOWNLOAD_SIDECARS: tuple[str, ...] = (
    "webp", "jpg", "jpeg", "png", "description", "info.json", "json",
    "vtt", "srt", "ass", "lrc",
)

# What an interrupted download leaves: none of these is ever part of a finished
# episode, whatever it sits next to, which is why they can be swept from a whole
# tree while the sidecars above cannot.
DOWNLOAD_PARTIALS: tuple[str, ...] = ("part", "partial", "ytdl", "temp", "concat")

# The extensions that mean "this is the finished audio", so a file that is not
# Matroska is only remuxed when it is actually video. An audio download is never
# remuxed: .opus in its Ogg container is what a phone wants.
DOWNLOAD_AUDIO_KEPT: tuple[str, ...] = (
    "opus", "m4a", "m4b", "mp3", "ogg", "oga", "flac", "wav", "aac", "mka",
)

# A multi-fragment download leaves a file per fragment, named by yt-dlp while it
# is in flight; the one that is not one of DOWNLOAD_PARTIALS but is never part of
# a finished episode. Matched on the basename the way find -name would.
_FRAGMENT_PATTERN = "*-Frag[0-9]*"

# A converted subtitle carries its language between the stem and the extension
# ("Episode.en.srt"), so it is not found by the plain "<stem>.<ext>" loop.
_SUBTITLE_SIDEcar_SUFFIXES = (".srt", ".vtt", ".ass")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    """The real runner: the tool on PATH, its output discarded.

    Only the exit status is read, the way the shell version sends the tool's
    stdout and stderr to /dev/null and tests the status.
    """
    return subprocess.run(list(argv), stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)


def download_cleanup_tools(kind: str) -> str:
    """The external tools <kind> of cleanup needs, for the caller's preflight.

    Audio cleanup deletes and renames, which needs nothing installed; video
    cleanup remuxes and attaches, which is mkvtoolnix.
    """
    if kind == "video":
        return "mkvmerge mkvpropedit"
    return ""


def _is_video(extension: str) -> bool:
    """True for a file that is video and is not already Matroska.

    Decided on the extension rather than by probing, because the answer is only
    ever used to decide whether to hand the file to mkvmerge - which will say so
    itself if the extension lied.
    """
    if extension == "mkv":
        return False
    if extension in DOWNLOAD_AUDIO_KEPT:
        return False
    return extension in VIDEO_EXTENSIONS


def _stem_of(path: str) -> str:
    """The stem the shell gets from ``${path%.*}``: what is left of the last dot,
    or the whole path when it has no dot at all - so an extensionless name keeps
    its own name as the stem rather than becoming empty."""
    head, dot, _ = path.rpartition(".")
    return head if dot else path


def _remux_to_matroska(path: str, run: Runner) -> str:
    """Remux one video into Matroska; return where the episode ended up.

    The source is only removed once the new file exists, so an interrupted remux
    costs a repeat and never the episode. Returns the source path unchanged when
    the remux did not happen (the target was already there, or the tool refused),
    and the new ``.mkv`` when it did.
    """
    stem = _stem_of(path)
    target = f"{stem}.mkv"
    if os.path.exists(target):
        return path
    if run(["mkvmerge", "--quiet", "-o", target, "--", path]).returncode != 0:
        # A file mkvmerge will not take is left exactly as it is: it is still a
        # perfectly playable download, and deleting or renaming it would only make
        # the failure harder to find. A partially written target is removed.
        if os.path.isfile(target):
            os.unlink(target)
        return path
    _attach_download_sidecars(target, stem, run)
    os.unlink(path)
    return target


def _attach_download_sidecars(target: str, stem: str, run: Runner) -> None:
    """The description and the metadata json go INTO the Matroska before their
    loose copies are deleted: attached, they travel with the file; left as
    sidecars they are files per episode that no player shows."""
    for sidecar in (f"{stem}.description", f"{stem}.info.json"):
        if not os.path.isfile(sidecar):
            continue
        run(["mkvpropedit", target, "--add-attachment", sidecar])


def _remove_download_sidecars(path: str) -> int:
    """Everything that shares the episode's name and is not the episode.

    Matched on the stem, so the cover art of a folder - which shares no name with
    any episode - is never in the list. Returns how many were removed.
    """
    stem = _stem_of(path)
    removed = 0
    for extension in DOWNLOAD_SIDECARS + DOWNLOAD_PARTIALS:
        sidecar = f"{stem}.{extension}"
        if os.path.isfile(sidecar) and sidecar != path:
            os.unlink(sidecar)
            removed += 1

    # A converted subtitle carries its language between the stem and the
    # extension ("Episode.en.srt"), so it is not found by the loop above. The
    # pattern is matched the way the original's find would: on the stem's
    # basename at the depth the file sits at, regular files only - so a stem
    # with glob characters behaves here as it does in the find, and a link
    # wearing a subtitle's name is left alone the way -type f leaves it.
    directory = os.path.dirname(path) or "."
    stem_base = os.path.basename(stem)
    if os.path.isdir(directory):
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                name = entry.name
                if name == os.path.basename(path):
                    continue
                if any(fnmatch.fnmatch(name, f"{stem_base}.*{suffix}")
                       for suffix in _SUBTITLE_SIDEcar_SUFFIXES):
                    os.unlink(entry.path)
                    removed += 1
    return removed


def clean_downloaded_file(path: str, run: Runner = _run,
                          skip_log: SkipLog | None = None) -> tuple[str, int, bool]:
    """Tidy one finished episode; return (where it ended up, sidecars removed,
    whether the container changed).

    The path it ended up at is not always where it started, because a video that
    was not Matroska becomes one. A file that is already in order is the normal
    case, not a failure; a file that has since been moved or deleted is not an
    error either.
    """
    cleaned_path = path
    sidecars = 0
    remuxed = False
    if not os.path.isfile(path):
        return cleaned_path, sidecars, remuxed

    # FILE.JPG and FILE.jpg are the same file to the phone's filesystem and two
    # different ones here, which is the whole reason this is done before anything
    # else looks for a sidecar by name. lowerCaseEnding renames in place, so the
    # file is now under the lowercased name - unless that name was taken, in which
    # case it refused and the original is still there.
    lower_case_ending(path, skip_log)
    if not os.path.isfile(path):
        path = f"{_stem_of(path)}.{lower_extension_of(path)}"
    if not os.path.isfile(path):
        return cleaned_path, sidecars, remuxed

    cleaned_path = path
    if _is_video(lower_extension_of(path)):
        new_path = _remux_to_matroska(path, run)
        if new_path != path:
            remuxed = True
            path = new_path
            cleaned_path = new_path

    sidecars = _remove_download_sidecars(path)
    return cleaned_path, sidecars, remuxed


def _is_partial(name: str) -> bool:
    """True when a basename is something an interrupted download leaves behind."""
    return any(fnmatch.fnmatch(name, f"*.{extension}")
               for extension in DOWNLOAD_PARTIALS) \
        or fnmatch.fnmatch(name, _FRAGMENT_PATTERN)


def sweep_partial_downloads(directory: str) -> int:
    """The files an interrupted run left behind, wherever they are; how many.

    A ``.part`` or a ``.ytdl`` is never part of a finished download, so unlike a
    ``.jpg`` it cannot be mistaken for something that belongs there - which is why
    this is a sweep of the whole tree rather than a per-episode tidy. A path that
    is not a directory is not an error.
    """
    if not os.path.isdir(directory):
        return 0
    swept = 0
    for parent, _dirs, names in os.walk(directory):
        for name in names:
            full = os.path.join(parent, name)
            # find -type f is false for a symlink however it resolves, so a link
            # to a partial is not swept (its target is not in this tree).
            if not stat.S_ISREG(os.lstat(full).st_mode):
                continue
            if _is_partial(name):
                os.unlink(full)
                swept += 1
    return swept


def prune_empty_folders(directory: str) -> int:
    """The folders a cleanup, or a feed that has gone away, has left empty; how
    many were removed. Depth first, so a folder whose only content was another
    empty folder goes too; the root itself is always kept.
    """
    if not os.path.isdir(directory):
        return 0
    directories = [d for d, _dirs, _files in os.walk(directory) if d != directory]
    # Deepest first: a parent is only empty once its empty children are gone, and
    # find -depth is the original's guarantee that the children go first.
    directories.sort(key=lambda d: d.count(os.sep), reverse=True)
    pruned = 0
    for folder in directories:
        if is_empty_folder(folder):
            os.rmdir(folder)
            pruned += 1
    return pruned