"""find-covers: a cover looked for online for every audio file in a tree that
carries none, written into a COPY of the file in a mirrored output tree - the
input is only read.

The search is the one concat-audio and convert-audio end their cover ladders
with: the catalogues of the provider table, then, under -A, a language model
that can search the web, with a vision model judging what it found. A file that
is the only audio in its folder is looked for as a book, by its folder's name;
any other as a track, by its own.

A file is left alone when it carries a cover of its own, and - unless -i - when
its folder holds an image named as a cover (folder.jpg, cover.png, front.webp,
...) or one sharing its own name, which the converters would embed instead.
Either is overlooked when it is smaller than -m megapixels: the file is then
looked for, and what is found embedded only when it has more pixels.

Each cover found is embedded in a copy of the file at its own relative path
under <outputDir>, so what was found can be looked at before anything is moved
back. A format that cannot carry a cover is not looked for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    outputDir     where a copy of each file a cover was found for is written,
                  at the same relative path, with the cover embedded. Created
                  if missing, and refused if it lies inside inputDir.
Options:"""

CREDITS_LINE = "look online for the covers audio files lack"

OPT_SPEC = """
h |  | Print this help page.
i |  | Also look for a file whose folder holds a cover image
                    (folder.jpg, cover.png, an image sharing its name, ...),
                    rather than taking that image as its cover.
m | <megapixels> | A cover smaller than this - embedded, or the image
                    beside the file - is looked to be replaced, and only by
                    one with more pixels. A fraction is allowed; 0 keeps
                    every cover. Default {minimum}
n |  | Only list the files that would be looked for; nothing is
                    searched or written.
"""

OPT_CHECKS = """
m | nonNegNum | megapixel threshold
"""

OPT_VARS = "i:ignoreFolderCovers m:minMegapixels n:dryRun"
OPT_COLUMN = 20
OPT_LONG = "h:help i:ignore-folder-covers m:min-megapixels n:dry-run"

# A cover with fewer megapixels than this is looked to be replaced: 500x500
# and up is kept.
DEFAULT_MIN_MEGAPIXELS = 0.25

# What the found image is recompressed to before it is embedded: the figures
# convert-audio embeds a track's cover with.
COVER_THRESHOLD = 500000
COVER_QUALITY = 75

# The words an image's name holds when it is a folder's cover - the converters'
# own cover-name words, the back cover and the inlay left out.
COVER_WORDS = ("cover", "folder", "front", "albumart")

_AUDIO_SUFFIXES = frozenset(
    "." + shell_lower(e)
    for e in (*enums.AUDIO_EXTENSIONS, *enums.LOSSLESS_AUDIO_EXTENSIONS))
_IMAGE_SUFFIXES = frozenset(
    "." + shell_lower(e)
    for e in (*enums.IMAGE_EXTENSIONS, *enums.COVER_IMAGE_EXTENSIONS))

# The prefix of the copy a cover is embedded in before it takes the output
# name, so an interrupted run never leaves a half-written file under it.
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


def folder_cover(source: str) -> str:
    """The image in ``source``'s own folder that is its cover, or "": one
    named with a cover word, or one sharing the file's name."""
    folder = os.path.dirname(source) or "."
    stem = shell_lower(os.path.splitext(os.path.basename(source))[0])
    try:
        names = sorted(os.listdir(folder), key=version_key)
    except OSError:
        return ""
    for name in names:
        if _suffix(name) not in _IMAGE_SUFFIXES:
            continue
        own = shell_lower(os.path.splitext(name)[0])
        if own == stem or any(word in own for word in COVER_WORDS):
            return os.path.join(folder, name)
    return ""


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
    unembedded: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    no_larger: list[str] = field(default_factory=list)
    cannot_carry: int = 0
    own_cover: int = 0
    folder_cover: int = 0
    done_before: int = 0


@dataclass
class Run:
    input_dir: str
    output_dir: str
    ram_dir: str
    model_meter: str
    resolution: str
    # A cover with fewer pixels than this is looked to be replaced; 0 never.
    min_pixels: int = 0
    tally: Tally = field(default_factory=Tally)
    # The files looked for to replace a small cover, with that cover's size.
    replacing: dict[str, tuple[int, int]] = field(default_factory=dict)

    def copy_of(self, relative: str) -> str:
        """Where the copy of one input file is written."""
        return os.path.join(self.output_dir, relative)

    def too_small(self, size: tuple[int, int]) -> bool:
        """Whether a cover of ``size`` is looked to be replaced. One whose size
        could not be read is kept, since nothing is known to be gained."""
        return 0 < _pixels(size) < self.min_pixels

    def wanted(self, relative: str, ignore_folder_covers: bool) -> bool:
        """Whether ``relative`` is looked for, counting why not where not."""
        source = os.path.join(self.input_dir, relative)
        if not relative.lower().endswith(mutagentags.COVER_SUFFIXES):
            self.tally.cannot_carry += 1
            runlog.detail("    %s: a %s cannot carry a cover"
                          % (relative, _suffix(relative)))
            return False
        if os.path.exists(self.copy_of(relative)):
            self.tally.done_before += 1
            runlog.detail("    %s: in the output already" % relative)
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

    def label(self, relative: str) -> str:
        """``relative`` as a listing shows it: with the size of the small
        cover it is looked for to replace, where it is."""
        small = self.replacing.get(relative)
        return ("%s (its cover is %s)" % (relative, _shown(small)) if small
                else relative)

    def find(self, relative: str) -> None:
        """One file looked for, and its copy written where a cover was found."""
        source = os.path.join(self.input_dir, relative)
        scratch, status = ramscratch.ram_scratch_dir("findCovers")
        if status != 0 or not scratch:
            runlog.error("no scratch directory for %s" % relative)
            self.tally.missed.append(relative)
            return
        try:
            self._find_in(relative, source, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _find_in(self, relative: str, source: str, scratch: str) -> None:
        seconds = onlinecover.media_seconds(source)
        stereo = onlinecover.media_channels(source) >= 2
        if is_book(source):
            image = thumbnails.online_book_cover(
                os.path.dirname(source), seconds, stereo,
                os.path.join(scratch, "book"), self.model_meter)
        else:
            image = onlinecover.track_image(source, seconds, scratch,
                                            stereo=stereo,
                                            meter_file=self.model_meter)
        if not image:
            self.tally.missed.append(relative)
            return
        found = os.path.join(scratch, "found.jpg")
        if image.endswith(".jpg"):
            os.replace(image, found)
        else:
            toolcapture.run(imagemagick.convert_argv(
                ["-quiet", imagecodecs.coder_prefixed(image), found]),
                warning="the cover found was not converted to a JPEG")
            if not os.path.isfile(found):
                self.tally.missed.append(relative)
                return

        small = self.replacing.get(relative)
        if small:
            size = image_size(found)
            if _pixels(size) <= _pixels(small):
                runlog.detail("    %s: the cover found, %s, is no larger than "
                              "its own %s" % (relative, _shown(size),
                                              _shown(small)))
                self.tally.no_larger.append(relative)
                return

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
            self.tally.unembedded.append(relative)
            return
        # The source's own modification time: the copy is the source with a
        # cover, and the embed is what bumped it.
        shutil.copystat(source, partial)
        os.replace(partial, copy)
        log("    Embedded in %s" % os.path.relpath(copy, self.output_dir))
        self.tally.embedded.append(relative)


def report(run: Run, looked_for: int) -> None:
    tally = run.tally
    spans, models = modelmeter.gathered(run.model_meter)
    if models.requests > 1:
        log("Cover model over the run: "
            + modelmeter.rate_line("all", spans.seconds, models))
    if tally.missed:
        log("No cover found for:")
        for relative in tally.missed:
            log("  " + relative)
    if tally.no_larger:
        log("Nothing larger than the small cover it has found for:")
        for relative in tally.no_larger:
            log("  " + run.label(relative))
    if tally.unembedded:
        log("A cover found but not embedded for:")
        for relative in tally.unembedded:
            log("  " + relative)
    skipped = []
    if tally.own_cover:
        skipped.append("%d carry their own" % tally.own_cover)
    if tally.folder_cover:
        skipped.append("%d have a cover image beside them" % tally.folder_cover)
    if tally.done_before:
        skipped.append("%d are in the output already" % tally.done_before)
    if tally.cannot_carry:
        skipped.append("%d cannot carry a cover" % tally.cannot_carry)
    no_larger = (", %d no larger" % len(tally.no_larger)
                 if tally.no_larger else "")
    runlog.result("Looked for %d file(s): %d embedded, %d not embedded, "
                  "%d not found%s%s."
                  % (looked_for, len(tally.embedded), len(tally.unembedded),
                     len(tally.missed), no_larger,
                     ("; skipped: " + ", ".join(skipped)) if skipped else ""))
    if tally.embedded:
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
        run = Run(input_dir, output_dir, ram_dir,
                  os.path.join(ram_dir, "coverModel.meter"),
                  imagesizes.geometry(imagesizes.DEFAULT_TIER) or "",
                  min_pixels=round(megapixels * 1_000_000))
        return _run(run, relatives, "i" in result.given, "n" in result.given)
    finally:
        ramscratch.run_exit_cleanup()


def _run(run: Run, relatives: list[str], ignore_folder_covers: bool,
         dry_run: bool) -> int:
    log('Checking %d audio file(s) in "%s" for a cover'
        % (len(relatives), run.input_dir))
    wanted = [r for r in relatives if run.wanted(r, ignore_folder_covers)]
    if dry_run:
        for relative in wanted:
            runlog.result(run.label(relative))
        runlog.result("%d file(s) would be looked for." % len(wanted))
        return 0
    for index, relative in enumerate(wanted, 1):
        safety.exit_if_aborted()
        log("[%d/%d] %s" % (index, len(wanted), run.label(relative)))
        run.find(relative)
    report(run, len(wanted))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
