"""find-covers: a cover looked for online for every audio file in a tree that
carries none, written into a mirrored output tree - the input is only read.

The search is the one concat-audio and convert-audio end their cover ladders
with: the catalogues of the provider table, then, under -A, a language model
that can search the web, with a vision model judging what it found.

A folder of numbered tracks, the way a CD is ripped, is an album: it is looked
for once, by its folder's name and its tracks' tags, and what is found is
written as one folder.jpg (or .png) for the whole folder. It is left alone when
it holds a cover-named image already - what its tracks carry does not count.
Under -t its tracks are looked for one by one, like any other file.

Any other file that is the only audio in its folder is looked for as a book,
by its folder's name; any other as a track, by its own. Such a file is left
alone when it carries a cover of its own, and - unless -i - when its folder
holds an image named as a cover (folder.jpg, cover.png, front.webp, ...) or
one sharing its own name, which the converters would embed instead; also when
it is shorter than -l minutes or smaller than -z megabytes. What is found is
embedded in a copy of the file, or under -s written beside it as an image
named after it.

A cover smaller than -m megapixels is overlooked: the file or album is then
looked for. What is found is never written when it is smaller than -m
megapixels itself - neither to replace a small cover nor where there is none.

Everything is written at its own relative path under <outputDir>, so what was
found can be looked at before anything is moved back. A format that cannot
carry a cover is not looked for to embed one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from medialib import commands
from medialib.lib import (
    clioptions,
    enums,
    imagecodecs,
    imagemagick,
    imagesizes,
    modelmeter,
    mutagentags,
    onlinecover,
    ramscratch,
    runlog,
    safety,
    settings,
    thumbnails,
    toolcapture,
    tooldeps,
)
from medialib.lib.enums import lower_extension_of, shell_lower
from medialib.lib.runlog import log
from medialib.lib.versionsort import version_key

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>

    inputDir      the tree whose audio files are looked at; only read.
    outputDir     where what is found is written, at the same relative path:
                  a copy of the file with the cover embedded, or the cover as
                  an image - folder.jpg for an album, one named after the
                  file under -s. Created if missing, and refused if it lies
                  inside inputDir.
Options:"""

CREDITS_LINE = "look online for the covers audio files lack"

OPT_SPEC = """
h |  | Print this help page.
i |  | Also look for a file or album whose folder holds a cover
                    image (folder.jpg, cover.png, an image sharing its name,
                    ...), rather than taking that image as its cover.
l | <minutes> | Leave alone a file shorter than this; an album is not
                    held to it, its tracks are under -t. A fraction is
                    allowed. Default 0, every length.
m | <megapixels> | A cover smaller than this - embedded, or the image
                    beside the file - is looked to be replaced, and one
                    found smaller than this is not written. A fraction is
                    allowed; 0 keeps and writes any cover. Default {minimum}
n |  | Only list the files and albums that would be looked for;
                    nothing is searched or written.
s |  | Write the cover found for a file that is no album track
                    beside it, as an image named after it (track.jpg),
                    rather than embedding it in a copy.
t |  | Look for an album's tracks one by one, like any other file,
                    rather than for the album once as its folder.jpg.
z | <megabytes> | Leave alone a file smaller than this; an album is not
                    held to it, its tracks are under -t. A fraction is
                    allowed. Default 0, every size.
"""

OPT_CHECKS = """
l | nonNegNum | minimum length
m | nonNegNum | megapixel threshold
z | nonNegNum | minimum size
"""

OPT_VARS = ("i:ignoreFolderCovers l:minMinutes m:minMegapixels n:dryRun "
            "s:sidecar t:perTrack z:minMegabytes")
OPT_COLUMN = 20
OPT_LONG = ("h:help i:ignore-folder-covers l:min-length m:min-megapixels "
            "n:dry-run s:sidecar t:per-track z:min-size")

# A cover with fewer megapixels than this is looked to be replaced, and one
# found with fewer is not written: 500x500 and up is kept.
DEFAULT_MIN_MEGAPIXELS = 0.25

# What the found image is recompressed to before it is embedded: the figures
# convert-audio embeds a track's cover with.
COVER_THRESHOLD = 500000
COVER_QUALITY = 75

# The words an image's name holds when it is a folder's cover - the converters'
# own cover-name words, the back cover and the inlay left out.
COVER_WORDS = ("cover", "folder", "front", "albumart")

# The name an album's cover is written under, the one media servers and the
# converters read first.
ALBUM_COVER_STEM = "folder"

# The fewest tracks a folder holds to be an album.
ALBUM_MIN_TRACKS = 2

# The track number a ripped CD's track name opens with: "01 Title",
# "1-03 Title", "CD2-04 Title", "Disc 1 - 05", "03. Title", "Track 07", "07".
# Four digits are a year, not a track: "2019-05-01 Episode" is no track.
_TRACK_NAME = re.compile(
    r"^\s*(?:(?:cd|dis[ck])\s*\d{1,2}\s*[-_. ]\s*)?(?:track\s*)?"
    r"\d{1,3}(?:\s*[-_.]\s*\d{1,3})?(?=[\s._\-)\]]|$)", re.IGNORECASE)

# The image formats a cover written beside the files keeps; any other is
# turned into a JPEG first.
_KEPT_IMAGE_SUFFIXES = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png"}

_AUDIO_SUFFIXES = frozenset(
    "." + shell_lower(e)
    for e in (*enums.AUDIO_EXTENSIONS, *enums.LOSSLESS_AUDIO_EXTENSIONS))
_IMAGE_SUFFIXES = frozenset(
    "." + shell_lower(e)
    for e in (*enums.IMAGE_EXTENSIONS, *enums.COVER_IMAGE_EXTENSIONS))

# The prefix of a file written before it takes the output name, so an
# interrupted run never leaves a half-written file under it.
PARTIAL_PREFIX = ".find-covers."


def spec(program: str) -> clioptions.Spec:
    return clioptions.with_shared_options(clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC.format(minimum=DEFAULT_MIN_MEGAPIXELS),
        long=OPT_LONG,
        vars=OPT_VARS,
        checks=OPT_CHECKS,
        column=OPT_COLUMN,
        credits=CREDITS_LINE,
    ))


# --- what is looked for ---------------------------------------------------------

def _suffix(name: str) -> str:
    return "." + lower_extension_of(name)


def _stem(name: str) -> str:
    return os.path.splitext(os.path.basename(name))[0]


def audio_files(root: str) -> list[str]:
    """Every audio file under ``root``, relative to it, version-sorted folder
    by folder. Hidden entries and links are left out."""
    found: list[str] = []
    for parent, folders, names in os.walk(root):
        folders[:] = sorted((f for f in folders if not f.startswith(".")
                             and not os.path.islink(os.path.join(parent, f))),
                            key=version_key)
        for name in sorted(names, key=version_key):
            path = os.path.join(parent, name)
            if (name.startswith(".") or os.path.islink(path)
                    or _suffix(name) not in _AUDIO_SUFFIXES):
                continue
            found.append(os.path.relpath(path, root))
    return found


def by_folder(relatives: list[str]) -> dict[str, list[str]]:
    """``relatives`` grouped by the folder they lie in ("" for the top), in
    the order they come."""
    groups: dict[str, list[str]] = {}
    for relative in relatives:
        groups.setdefault(os.path.dirname(relative), []).append(relative)
    return groups


def _images(folder: str) -> list[str]:
    """The image files' names in ``folder``, version-sorted; [] when it
    cannot be read."""
    try:
        names = sorted(os.listdir(folder), key=version_key)
    except OSError:
        return []
    return [name for name in names if _suffix(name) in _IMAGE_SUFFIXES]


def _cover_named(stem: str) -> bool:
    own = shell_lower(stem)
    return any(word in own for word in COVER_WORDS)


def folder_cover(source: str) -> str:
    """The image in ``source``'s own folder that is its cover, or "": one
    named with a cover word, or one sharing the file's name."""
    folder = os.path.dirname(source) or "."
    stem = shell_lower(_stem(source))
    for name in _images(folder):
        own = _stem(name)
        if shell_lower(own) == stem or _cover_named(own):
            return os.path.join(folder, name)
    return ""


def album_cover(folder: str) -> str:
    """The image in ``folder`` that is its album's cover - one named with a
    cover word - or ""."""
    for name in _images(folder):
        if _cover_named(_stem(name)):
            return os.path.join(folder, name)
    return ""


def image_named(folder: str, stem: str) -> str:
    """An image in ``folder`` called ``stem`` with any image extension, or
    ""."""
    wanted = shell_lower(stem)
    for name in _images(folder):
        if shell_lower(_stem(name)) == wanted:
            return os.path.join(folder, name)
    return ""


def is_track_name(name: str) -> bool:
    """Whether ``name`` opens with a track number, the way a ripped CD's
    tracks are named."""
    return bool(_TRACK_NAME.match(_stem(name)))


def is_album(folder: str, names: list[str],
             probe: Callable[[str], dict] | None = None) -> bool:
    """Whether the audio files ``names`` in ``folder`` are an album's tracks:
    at least two, most of them named with a track number - or, where the
    names do not say, the first and the last tagged with a track number and
    the same album."""
    if len(names) < ALBUM_MIN_TRACKS:
        return False
    numbered = sum(1 for name in names if is_track_name(name))
    if 2 * numbered > len(names):
        return True
    reader = probe or onlinecover.probe_tags
    ordered = sorted(names, key=version_key)
    first, last = (reader(os.path.join(folder, os.path.basename(name)))
                   for name in (ordered[0], ordered[-1]))
    album = shell_lower(first.get("album", "").strip())
    return (bool(album)
            and album == shell_lower(last.get("album", "").strip())
            and bool(first.get("track", "").strip())
            and bool(last.get("track", "").strip()))


def image_size(path: str) -> tuple[int, int]:
    """An image file's (width, height), or (0, 0) when it cannot be read."""
    try:
        done = subprocess.run(
            imagemagick.identify_argv(["-ping", "-format", "%w %h\\n",
                                       imagecodecs.coder_prefixed(path)]),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    except OSError:
        return 0, 0
    if done.returncode != 0:
        return 0, 0
    return onlinecover.largest_size(done.stdout.decode("utf-8", "replace"))


def _pixels(size: tuple[int, int]) -> int:
    return size[0] * size[1]


def _shown(size: tuple[int, int]) -> str:
    return "%dx%d" % size


def _figure(value: float) -> str:
    return "%g" % value


def is_book(source: str) -> bool:
    """Whether ``source`` is the only audio file in its folder: a joined book,
    looked for by its folder's name the way concat-audio looks for one."""
    folder = os.path.dirname(source) or "."
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return sum(1 for n in names if _suffix(n) in _AUDIO_SUFFIXES
               and os.path.isfile(os.path.join(folder, n))) == 1


# --- the run --------------------------------------------------------------------

@dataclass
class Tally:
    embedded: list[str] = field(default_factory=list)
    beside: list[str] = field(default_factory=list)
    unwritten: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    too_small: list[str] = field(default_factory=list)
    cannot_carry: int = 0
    own_cover: int = 0
    folder_cover: int = 0
    album_cover: int = 0
    below_minimum: int = 0
    done_before: int = 0


@dataclass
class Run:
    input_dir: str
    output_dir: str
    ram_dir: str
    model_meter: str
    resolution: str
    # A cover with fewer pixels than this is looked to be replaced, and one
    # found with fewer is not written; 0 never.
    min_pixels: int = 0
    # Whether a file that is no album track has its cover written beside it
    # rather than embedded.
    sidecar: bool = False
    # Whether an album's tracks are looked for one by one.
    per_track: bool = False
    # A file that is no album track, shorter or smaller than this, is left
    # alone; 0 holds none to it.
    min_seconds: float = 0.0
    min_bytes: int = 0
    tally: Tally = field(default_factory=Tally)
    # The files and albums looked for to replace a small cover, with that
    # cover's size.
    replacing: dict[str, tuple[int, int]] = field(default_factory=dict)
    # The albums looked for, by their folder, with their tracks.
    albums: dict[str, list[str]] = field(default_factory=dict)

    def copy_of(self, relative: str) -> str:
        """Where the copy of one input file or folder is written."""
        return os.path.join(self.output_dir, relative)

    def too_small(self, size: tuple[int, int]) -> bool:
        """Whether a cover of ``size`` is looked to be replaced. One whose size
        could not be read is kept, since nothing is known to be gained."""
        return 0 < _pixels(size) < self.min_pixels

    def plan(self, relatives: list[str],
             ignore_folder_covers: bool) -> list[str]:
        """What is looked for, in order: an album by its folder, any other
        file by itself - counting why not where not."""
        looked: list[str] = []
        for folder, files in by_folder(relatives).items():
            if not self.per_track and is_album(
                    os.path.join(self.input_dir, folder), files):
                if self.wanted_album(folder, files, ignore_folder_covers):
                    looked.append(folder)
                continue
            looked.extend(relative for relative in files
                          if self.wanted(relative, ignore_folder_covers))
        return looked

    def wanted_album(self, folder: str, tracks: list[str],
                     ignore_folder_covers: bool) -> bool:
        """Whether the album in ``folder`` is looked for: whether it lacks a
        cover image - what its tracks carry is not asked."""
        shown = self.label_of_album(folder, len(tracks))
        if image_named(self.copy_of(folder), ALBUM_COVER_STEM):
            self.tally.done_before += 1
            runlog.detail("    %s: in the output already" % shown)
            return False
        if not ignore_folder_covers:
            beside = album_cover(os.path.join(self.input_dir, folder))
            if beside:
                size = image_size(beside) if self.min_pixels else (0, 0)
                if not self.too_small(size):
                    self.tally.album_cover += 1
                    runlog.detail("    %s: has the cover image %s"
                                  % (shown, os.path.basename(beside)))
                    return False
                self.replacing[folder] = size
                runlog.detail("    %s: the cover image %s is only %s"
                              % (shown, os.path.basename(beside),
                                 _shown(size)))
        self.albums[folder] = tracks
        return True

    def below_minimum(self, source: str) -> str:
        """Why ``source`` is too short or too small to be looked for, or "".
        A length that cannot be read holds it to nothing."""
        if self.min_bytes:
            try:
                size = os.path.getsize(source)
            except OSError:
                size = self.min_bytes
            if size < self.min_bytes:
                return "smaller than %s MB" % _figure(self.min_bytes / 1e6)
        if self.min_seconds:
            seconds = onlinecover.media_seconds(source)
            if 0 < seconds < self.min_seconds:
                return ("shorter than %s minute(s)"
                        % _figure(self.min_seconds / 60))
        return ""

    def wanted(self, relative: str, ignore_folder_covers: bool) -> bool:
        """Whether ``relative`` is looked for, counting why not where not."""
        source = os.path.join(self.input_dir, relative)
        if self.sidecar:
            if image_named(self.copy_of(os.path.dirname(relative)),
                           _stem(relative)):
                self.tally.done_before += 1
                runlog.detail("    %s: in the output already" % relative)
                return False
        else:
            if not relative.lower().endswith(mutagentags.COVER_SUFFIXES):
                self.tally.cannot_carry += 1
                runlog.detail("    %s: a %s cannot carry a cover"
                              % (relative, _suffix(relative)))
                return False
            if os.path.exists(self.copy_of(relative)):
                self.tally.done_before += 1
                runlog.detail("    %s: in the output already" % relative)
                return False
        why = self.below_minimum(source)
        if why:
            self.tally.below_minimum += 1
            runlog.detail("    %s: %s" % (relative, why))
            return False
        small = (0, 0)
        if not ignore_folder_covers:
            beside = folder_cover(source)
            if beside:
                size = image_size(beside) if self.min_pixels else (0, 0)
                if not self.too_small(size):
                    self.tally.folder_cover += 1
                    runlog.detail("    %s: has the cover image %s beside it"
                                  % (relative, os.path.basename(beside)))
                    return False
                small = size
                runlog.detail("    %s: the cover image %s beside it is only %s"
                              % (relative, os.path.basename(beside),
                                 _shown(size)))
        if onlinecover.has_embedded_cover(source):
            size = (onlinecover.embedded_cover_size(source)
                    if self.min_pixels else (0, 0))
            if not self.too_small(size):
                self.tally.own_cover += 1
                runlog.detail("    %s: carries a cover of its own" % relative)
                return False
            runlog.detail("    %s: the cover it carries is only %s"
                          % (relative, _shown(size)))
            small = max(small, size, key=_pixels)
        if _pixels(small):
            self.replacing[relative] = small
        return True

    @staticmethod
    def label_of_album(folder: str, tracks: int) -> str:
        return "%s (album of %d tracks)" % (folder or ".", tracks)

    def label(self, relative: str) -> str:
        """``relative`` as a listing shows it: an album as its folder and
        track count, and with the size of the small cover it is looked for
        to replace, where it is."""
        if relative in self.albums:
            shown = self.label_of_album(relative, len(self.albums[relative]))
        else:
            shown = relative
        small = self.replacing.get(relative)
        return ("%s (its cover is %s)" % (shown, _shown(small)) if small
                else shown)

    def find(self, relative: str) -> None:
        """One file or album looked for, and what was found written."""
        source = os.path.normpath(os.path.join(self.input_dir, relative))
        scratch, status = ramscratch.ram_scratch_dir("findCovers")
        if status != 0 or not scratch:
            runlog.error("no scratch directory for %s" % self.label(relative))
            self.tally.missed.append(relative)
            return
        try:
            self._find_in(relative, source, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _search(self, relative: str, source: str, scratch: str) -> str:
        """The image found for one file or album, or ""."""
        tracks = self.albums.get(relative)
        if tracks is not None:
            paths = [os.path.join(self.input_dir, t) for t in tracks]
            seconds = sum(onlinecover.media_seconds(p) for p in paths)
            stereo = onlinecover.media_channels(paths[0]) >= 2
            return thumbnails.online_book_cover(
                source, seconds, stereo, os.path.join(scratch, "album"),
                self.model_meter, root=self.input_dir)
        seconds = onlinecover.media_seconds(source)
        stereo = onlinecover.media_channels(source) >= 2
        if is_book(source):
            return thumbnails.online_book_cover(
                os.path.dirname(source), seconds, stereo,
                os.path.join(scratch, "book"), self.model_meter,
                root=self.input_dir)
        return onlinecover.track_image(source, seconds, scratch,
                                       stereo=stereo,
                                       meter_file=self.model_meter,
                                       root=self.input_dir)

    def _find_in(self, relative: str, source: str, scratch: str) -> None:
        album = relative in self.albums
        image = self._search(relative, source, scratch)
        if not image:
            self.tally.missed.append(relative)
            return
        found = settled_image(image, scratch, keep_png=album or self.sidecar)
        if not found:
            self.tally.missed.append(relative)
            return

        why = self.unworthy(relative, found)
        if why:
            runlog.detail("    %s: the cover found, %s"
                          % (self.label(relative), why))
            self.tally.too_small.append(relative)
            return

        if album:
            self._write_beside(relative, found, relative, ALBUM_COVER_STEM)
        elif self.sidecar:
            self._write_beside(relative, found, os.path.dirname(relative),
                               _stem(relative))
        else:
            self._embed(relative, source, found, scratch)

    def unworthy(self, relative: str, found: str) -> str:
        """Why the image ``found`` for ``relative`` is not written, or "": it
        is under the minimum, or no larger than the small cover it would
        replace. One whose size cannot be read is written only where there
        is no cover to lose."""
        small = self.replacing.get(relative)
        if not self.min_pixels and not small:
            return ""
        size = image_size(found)
        if self.too_small(size):
            return "%s, is under %s megapixels" % (
                _shown(size), _figure(self.min_pixels / 1e6))
        if small and _pixels(size) <= _pixels(small):
            return "%s, is no larger than its own %s" % (_shown(size),
                                                         _shown(small))
        return ""

    def _write_beside(self, relative: str, found: str, folder: str,
                      stem: str) -> None:
        """``found`` written as ``stem`` in the output's ``folder``."""
        target_dir = self.copy_of(folder)
        name = stem + _suffix(found)
        target = os.path.join(target_dir, name)
        partial = os.path.join(target_dir, PARTIAL_PREFIX + name)
        try:
            os.makedirs(target_dir, exist_ok=True)
            safety.remove_at_exit(partial)
            shutil.copyfile(found, partial)
            os.replace(partial, target)
        except OSError as error:
            runlog.warn("%s: the cover found could not be written: %s"
                        % (self.label(relative), error))
            self.tally.unwritten.append(relative)
            return
        log("    Wrote %s" % os.path.relpath(target, self.output_dir))
        self.tally.beside.append(relative)

    def _embed(self, relative: str, source: str, found: str,
               scratch: str) -> None:
        copy = self.copy_of(relative)
        os.makedirs(os.path.dirname(copy), exist_ok=True)
        partial = os.path.join(os.path.dirname(copy),
                               PARTIAL_PREFIX + os.path.basename(copy))
        safety.remove_at_exit(partial)
        shutil.copy2(source, partial)
        cover = os.path.join(scratch, "cover.jpg")
        if thumbnails.embed_cover_file(
                relative, partial, found, cover, COVER_THRESHOLD,
                COVER_QUALITY, self.resolution,
                origin="an online search") != 0:
            os.remove(partial)
            runlog.warn("%s: the cover found could not be embedded" % relative)
            self.tally.unwritten.append(relative)
            return
        # The source's own modification time: the copy is the source with a
        # cover, and the embed is what bumped it.
        shutil.copystat(source, partial)
        os.replace(partial, copy)
        log("    Embedded in %s" % os.path.relpath(copy, self.output_dir))
        self.tally.embedded.append(relative)


def settled_image(image: str, scratch: str, keep_png: bool) -> str:
    """``image`` moved to ``scratch`` as found.jpg - or, with ``keep_png``,
    found.png for a PNG - turned into a JPEG first where it is neither; ""
    when that fails."""
    kept = _KEPT_IMAGE_SUFFIXES.get(_suffix(image))
    if kept and (kept == ".jpg" or keep_png):
        found = os.path.join(scratch, "found" + kept)
        os.replace(image, found)
        return found
    found = os.path.join(scratch, "found.jpg")
    toolcapture.run(imagemagick.convert_argv(
        ["-quiet", imagecodecs.coder_prefixed(image), found]),
        warning="the cover found was not converted to a JPEG")
    return found if os.path.isfile(found) else ""


def report(run: Run, looked_for: list[str]) -> None:
    tally = run.tally
    spans, models = modelmeter.gathered(run.model_meter)
    if models.requests > 1:
        log("Cover model over the run: "
            + modelmeter.rate_line("all", spans.seconds, models))
    if tally.missed:
        log("No cover found for:")
        for relative in tally.missed:
            log("  " + run.label(relative))
    if tally.too_small:
        log("Only a cover under %s megapixels found for:"
            % _figure(run.min_pixels / 1e6))
        for relative in tally.too_small:
            log("  " + run.label(relative))
    if tally.unwritten:
        log("A cover found but not written for:")
        for relative in tally.unwritten:
            log("  " + run.label(relative))
    skipped = []
    if tally.album_cover:
        skipped.append("%d album(s) have a cover image" % tally.album_cover)
    if tally.own_cover:
        skipped.append("%d carry their own" % tally.own_cover)
    if tally.folder_cover:
        skipped.append("%d have a cover image beside them" % tally.folder_cover)
    if tally.below_minimum:
        skipped.append("%d are under the minimum length or size"
                       % tally.below_minimum)
    if tally.done_before:
        skipped.append("%d are in the output already" % tally.done_before)
    if tally.cannot_carry:
        skipped.append("%d cannot carry a cover" % tally.cannot_carry)
    albums = sum(1 for relative in looked_for if relative in run.albums)
    what = "%d file(s)" % (len(looked_for) - albums)
    if albums:
        what = "%d album(s) and %s" % (albums, what)
    written = ["%d embedded" % len(tally.embedded)]
    if tally.beside or run.albums or run.sidecar:
        written.append("%d written as an image" % len(tally.beside))
    written.append("%d not written" % len(tally.unwritten))
    written.append("%d not found" % len(tally.missed))
    if tally.too_small:
        written.append("%d too small" % len(tally.too_small))
    runlog.result("Looked for %s: %s%s."
                  % (what, ", ".join(written),
                     ("; skipped: " + ", ".join(skipped)) if skipped else ""))
    if tally.embedded or tally.beside:
        runlog.result('Written to "%s"' % run.output_dir)


def cli(argv: list | None = None) -> int:
    settings.load()
    runlog.start_run(argv)
    if tooldeps.require_declared_packages(
            commands.program_name(__spec__.name)):
        return 1
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name))


def main(argv: list, program: str = "find-covers",
         script_dir: str = "") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
        clioptions.settle_verbosity(result)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        clioptions.refuse_usage(declaration, error.message)
        return 1

    if clioptions.args_out_of_range(len(result.positionals), 2, 2):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    input_arg, output_arg = result.positionals
    if not os.path.isdir(input_arg):
        clioptions.refuse_missing_dir(declaration, input_arg)
        return 1
    input_dir = os.path.realpath(input_arg)
    output_dir = os.path.realpath(output_arg)
    if safety.require_separate_output(input_dir, output_dir):
        return 1
    if not onlinecover.enabled():
        runlog.error("the coverSearch setting is off, so there is nothing to "
                     "look with. Nothing was changed.", kind="usage")
        return 1
    if tooldeps.require_tools(program,
                              ("ffprobe", imagemagick.CONVERT_SPEC)):
        return 1

    relatives = audio_files(input_dir)
    if not relatives:
        return safety.fail_no_relevant_input(
            input_arg, "audio files (%s)"
            % enums.extension_list(list(enums.AUDIO_EXTENSIONS)))

    ramscratch.init_ram_base()
    try:
        ram_dir, status = ramscratch.ram_scratch_dir("findCovers")
        if status != 0 or not ram_dir:
            sys.stderr.write("\nError: no scratch directory could be made for "
                             "this run.\nNothing was changed.\n")
            return 1
        ramscratch.add_exit_cleanup([ram_dir])
        safety.init_abort_flag(os.path.join(ram_dir, "abortRequested"))
        safety.trap_run_abort()
        megapixels = float(result.values["minMegapixels"]
                           or DEFAULT_MIN_MEGAPIXELS)
        minutes = float(result.values["minMinutes"] or 0)
        megabytes = float(result.values["minMegabytes"] or 0)
        run = Run(input_dir, output_dir, ram_dir,
                  os.path.join(ram_dir, "coverModel.meter"),
                  imagesizes.geometry(imagesizes.DEFAULT_TIER) or "",
                  min_pixels=round(megapixels * 1_000_000),
                  sidecar="s" in result.given,
                  per_track="t" in result.given,
                  min_seconds=minutes * 60,
                  min_bytes=round(megabytes * 1_000_000))
        return _run(run, relatives, "i" in result.given, "n" in result.given)
    finally:
        ramscratch.run_exit_cleanup()


def _run(run: Run, relatives: list[str], ignore_folder_covers: bool,
         dry_run: bool) -> int:
    log('Checking %d audio file(s) in "%s" for a cover'
        % (len(relatives), run.input_dir))
    wanted = run.plan(relatives, ignore_folder_covers)
    if dry_run:
        for relative in wanted:
            runlog.result(run.label(relative))
        runlog.result("%d file(s) or album(s) would be looked for."
                      % len(wanted))
        return 0
    for index, relative in enumerate(wanted, 1):
        safety.exit_if_aborted()
        log("[%d/%d] %s" % (index, len(wanted), run.label(relative)))
        run.find(relative)
    report(run, wanted)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
