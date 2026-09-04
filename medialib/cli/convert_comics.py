"""convert-comics: a tree of comic books recompressed into .cbz of AVIF pages.

Every book goes archive -> extracted pages -> AVIF pages -> archive again, and
only that last step touches the disk: the pages in between live in RAM.

A comic that arrived as a PDF is the same book in a different container, so it
joins the pipeline at the same place - its pages are rendered rather than
unzipped, and from the folder of pages onwards nothing knows the difference.
Unlike an archive, though, a PDF does not announce that it is a comic: a
magazine and a manual are also stacks of pages. So every PDF is inspected first
and only the ones shaped like a scan are taken.

ONE BOOK at a time is in RAM, not one collection: each book is taken through
extract -> convert -> number -> zip by a single worker which then frees that
book's two RAM folders, so peak RAM is the number of workers times ONE book
however many thousands of archives the run was handed.

What cannot be decided one book at a time is the collective NAME cleaning, which
strips the affixes sibling books share and so has to see all of them together.
That happens first, on a throwaway tree of markers, before anything is converted
- which is also what makes a re-run able to recognise a finished book instead of
converting it a second time.
"""

import os
import shutil
import subprocess
import sys
import time
from typing import Any

from medialib import commands
from medialib.lib import (
    clioptions,
    comicpdf,
    enums,
    imagemagick,
    numbering,
    ramscratch,
    runlog,
    safety,
    tooldeps,
    workerpool,
)
from medialib.lib.runlog import log
from medialib.lib.versionsort import version_key

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>

Converts every comic book under <inputDir> into a .cbz of AVIF pages in the
mirrored folder under <outputDir>. Books may be .cbr/.cbz/.cb7 archives or PDFs
that hold one large image per page; a PDF that is not shaped like a scanned comic
(a magazine, a text document) is reported and skipped.

Options:"""

# The spec is DATA, and the page it renders is compared byte for byte against the
# recorded contract under tests/data/cliContract.
OPT_SPEC = """
h |  | Print this help page.
q | <quality> | quality level of the output images
s | <speed> | av1 speed preset, lower is slower, default 5
m | <maxRes> | maximum resolution (height) to keep
f | <fuzz> | when trimming, how many percent color difference gets still trimmed
"""

OPT_VARS = "q:quality s:speedPreset m:maxRes f:fuzz"
OPT_COLUMN = 20
OPT_LONG = "h:help q:quality s:speed m:max-resolution f:fuzz"

DEFAULT_QUALITY = 50
# Height ceiling for a converted page, in pixels: the long edge of the display
# these are read on. A page is shown fitted to that edge, so pixels past it are
# ones the screen cannot put anywhere - they only cost bytes in every book.
DEFAULT_MAX_RES = 2960
DEFAULT_SPEED_PRESET = 5
DEFAULT_FUZZ = 10

# The smallest an AVIF page can plausibly be, in bytes. A conversion that
# produced less than this produced no picture - a truncated or empty encode - and
# the page is dropped rather than shipped inside the finished book.
MIN_PAGE_SIZE = 10 * 1024

# ImageMagick spends about this many threads on one page whatever we do; four
# pages convert at once inside a book, fixed.
THREADS_PER_CONVERSION = 4
IMAGES_PER_BOOK = 4
# Deliberate oversubscription: a book worker is not converting the whole time -
# it unpacks at the start and zips at the end, and those stretches are I/O rather
# than arithmetic - so sizing the pool to the bare thread count would leave the
# CPU idling through them.
OVERSUBSCRIBE = 2

# The record separator between a book and the output path it is destined for: a
# comic's name may contain anything else.
UNIT = "\x1f"


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
    )


def book_workers(threads: int) -> int:
    """How many books convert at once.

    Books, not pages, is what scales with the host, because that is the axis that
    costs RAM: one more book worker is one more book resident, while a fifth page
    inside an already-unpacked book would cost none. Two is the floor - with a
    single worker there is no second book to convert through the first one's
    unpacking and zipping, which is half the point.
    """
    workers = threads * OVERSUBSCRIBE // (THREADS_PER_CONVERSION * IMAGES_PER_BOOK)
    return max(2, workers)


def _files_matching(root: str, extensions, exact_case: bool = False) -> list:
    """Every file under <root> with one of these extensions."""
    wanted = tuple("." + extension for extension in extensions) if exact_case \
        else tuple("." + extension.lower() for extension in extensions)
    found = []
    for parent, _dirs, names in os.walk(root):
        for name in names:
            candidate = name if exact_case else name.lower()
            if candidate.endswith(wanted):
                found.append(os.path.join(parent, name))
    return found


def input_holds_extension(root: str, extension: str) -> bool:
    """Whether this input holds the format that needs a particular extractor - a
    library of .cbz should not have to have unrar and 7-Zip installed."""
    return bool(_files_matching(root, (extension,)))


class Counters:
    """The shared counters and the one progress line, which the workers keep as
    they go - so the closing report is already correct for a part-run."""

    def __init__(self, counter_dir: str, total: int = 0) -> None:
        self.dir = counter_dir
        self.total = total

    def _lock(self, name: str = "lock"):
        return open(os.path.join(self.dir, name), "w")

    def read(self, name: str) -> int:
        """A counter the run has not created yet reads as 0: the footer is named
        above every point the run can be cut short, so it can be asked for
        figures from before the phase that produces them."""
        try:
            with open(os.path.join(self.dir, name)) as handle:
                return int(handle.read() or 0)
        except (OSError, ValueError):
            return 0

    def progress(self, line: str) -> None:
        with self._lock() as lock, runlog.take_lock(lock):
            current = self.read("current") + 1
            with open(os.path.join(self.dir, "current"), "w") as handle:
                handle.write(str(current))
            sys.stdout.write("%s%s\n" % (
                runlog.counted_prefix(current, self.total), line))
            sys.stdout.flush()

    def note(self, line: str) -> None:
        """One line about a book that has already been counted. A book announces
        itself when it STARTS, before anything is known about how it ends, so the
        few that end badly still have something to say afterwards - and saying it
        must not make the run look one book further along than it is."""
        with self._lock() as lock, runlog.take_lock(lock):
            sys.stdout.write("    %s\n" % line)
            sys.stdout.flush()

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock(name + ".lock") as lock, runlog.take_lock(lock):
            current = self.read(name) + amount
            with open(os.path.join(self.dir, name), "w") as handle:
                handle.write(str(current))


def pretreat_input(in_path: str, skips: safety.SkipLog) -> None:
    """Only the known problems in names and paths."""
    root_depth = in_path.rstrip("/").count(os.sep)
    by_depth: dict[int, list[str]] = {}
    for parent, dirs, _names in os.walk(in_path):
        for name in dirs:
            path = os.path.join(parent, name)
            by_depth.setdefault(path.count(os.sep) - root_depth, []).append(path)
    # Breadth first, so a tree needing several levels cleaned is cleaned from the
    # top down and each level still carries the path it was found at.
    for depth in sorted(by_depth):
        for folder in by_depth[depth]:
            safety.safe_rename(folder, safety.clean_input_path(folder), skips)

    for path in _files_matching(in_path, COMIC_INPUT_EXTENSIONS):
        safety.safe_rename(path, safety.clean_input_path(path), skips)

    safety.lower_case_extensions(in_path, skips)


COMIC_INPUT_EXTENSIONS = tuple(enums.COMIC_EXTENSIONS) + tuple(
    enums.COMIC_PDF_EXTENSIONS)


def extract(archive: str, destination: str, max_res: int) -> None:
    """One book's pages into <destination>, whatever container they arrived in.

    Every extractor is silenced, stdout included: each is chatty in its own way,
    several books unpack at once so the lines arrive interleaved and unattributed,
    and nothing downstream reads them. What matters is whether pages appeared,
    which is decided by looking at the folder afterwards - so an extractor that
    fails silently lands the book in the "no pages" case, exactly where a corrupt
    archive lands.
    """
    os.makedirs(destination, exist_ok=True)
    quiet: dict[str, Any] = {"stdout": subprocess.DEVNULL,
                             "stderr": subprocess.DEVNULL}
    lowered = archive.lower()

    if lowered.endswith(".cbr"):
        subprocess.run(["unrar", "e", "--", archive, destination], **quiet)
    elif lowered.endswith(".cbz"):
        subprocess.run(["unzip", "-o", "-d", destination, "--", archive],
                       **quiet)
    elif lowered.endswith(".cb7"):
        # 7-Zip's binary has three names in the wild - 7z from p7zip, 7zz in the
        # official build, 7za where only the reduced package is installed. None
        # present leaves the last name to fail on its own, which lands the book in
        # the "no pages" case rather than anywhere worse.
        seven = "7za"
        for candidate in ("7z", "7zz", "7za"):
            if shutil.which(candidate):
                seven = candidate
                break
        # x, not e: the archive's own folders are kept and flattened below,
        # together with the name collisions that flattening can produce.
        subprocess.run([seven, "x", "-y", "-o" + destination, "--", archive],
                       **quiet)
    elif lowered.endswith(".pdf"):
        comicpdf.render_comic_pdf_pages(archive, destination, str(max_res))

    _flatten(destination)


def _flatten(destination: str) -> None:
    """The archive's nested layout flattened, then everything that is not a page
    pruned. Two same-named pages from different sub-folders would clobber each
    other, so a colliding file is given a " (N)" suffix instead."""
    for parent, _dirs, names in os.walk(destination):
        if os.path.abspath(parent) == os.path.abspath(destination):
            continue
        for name in names:
            source = os.path.join(parent, name)
            target = os.path.join(destination, name)
            if source == target:
                continue
            if os.path.exists(target):
                target = safety.unique_suffix_path(target)
            try:
                shutil.move(source, target)
            except (OSError, shutil.Error):
                pass

    safety.lower_case_extensions(destination)

    wanted = tuple("." + extension for extension in enums.IMAGE_EXTENSIONS)
    for parent, _dirs, names in os.walk(destination):
        for name in names:
            if not name.endswith(wanted):
                try:
                    os.remove(os.path.join(parent, name))
                except OSError:
                    pass
    # No minimum depth on purpose: this is our own RAM work folder, not a path the
    # user gave us, and an archive that held no page should leave nothing behind.
    for parent, _dirs, _names in os.walk(destination, topdown=False):
        try:
            os.rmdir(parent)
        except OSError:
            pass


def package_book(book_dir: str, out_path: str, out_rel: str,
                 source_archive: str, stage_root: str,
                 counters: Counters) -> bool:
    """One finished book folder of AVIF pages into a single .cbz.

    The folder itself does NOT appear in the output: it becomes the archive in its
    own parent folder, so the output tree has the same depth as the input tree of
    archives. Stored rather than deflated - AVIF is already compressed, so
    re-compressing buys nothing and costs CPU.
    """
    book_name = os.path.basename(out_rel)[:-len(".cbz")] \
        if out_rel.endswith(".cbz") else os.path.basename(out_rel)
    cbz = os.path.join(out_path, out_rel)

    # Resume: a finished archive from a PREVIOUS run is left alone rather than
    # rewritten. This is the name the book will ALWAYS be given, worked out before
    # any conversion started, which is what makes the check meaningful.
    if os.path.exists(cbz):
        counters.note("Skip (exists): %s.cbz" % book_name)
        return True

    try:
        pages = sorted("./" + name for name in os.listdir(book_dir)
                       if os.path.isfile(os.path.join(book_dir, name)))
    except OSError:
        return False
    if not pages:
        return False

    # Built in RAM and only the finished file moved to disk, so an interrupted run
    # never leaves a truncated .cbz the resume check would take for a whole book.
    import tempfile
    stage = tempfile.mkdtemp(prefix=".pack.", dir=stage_root)
    staged = os.path.join(stage, book_name + ".cbz")
    try:
        done = subprocess.run(["zip", "-0", "-q", "-X", "-D", staged] + pages,
                              cwd=book_dir)
        if done.returncode != 0:
            return False

        # Which archive this came from, in the zip's own comment. The name is
        # decided collectively, so re-running over a folder that has since grown
        # can compute a different name for a book already converted, and matching
        # on names would then write a second copy. A comment lives in the central
        # directory: it survives a rename, costs about 30 bytes, and is not an
        # entry, so no reader shows it as a page.
        subprocess.run(["zip", "-z", "-q", staged],
                       input=(source_archive + "\n").encode(),
                       stdout=subprocess.DEVNULL)

        destination = os.path.dirname(cbz) or out_path
        os.makedirs(destination, exist_ok=True)
        shutil.move(staged, cbz)
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)


class Run:
    """One run's settings, and the per-book work that reads them."""

    # Declared, not defaulted: the settings dict supplies every one, so a name
    # it does not carry is still an AttributeError at the read.
    in_path: str
    out_path: str
    script_dir: str
    temp_path: str
    avif_path: str
    counters: "Counters"
    quality: str
    speed_preset: str
    max_res: int
    fuzz: str
    # The books already packaged, so a resumed run skips them.
    converted: set[str]
    input_list: str
    stats_file: str
    total: int
    # The phase clock, unset until the phase it times has begun.
    phase_start: float | None
    phase_end: float | None

    def __init__(self, **settings) -> None:
        self.__dict__.update(settings)

    def process_book(self, record: str) -> None:
        """One book, end to end: unpack into RAM, convert, number, zip, free.

        Exactly one COUNTED line per book, reported at its START rather than when
        it finishes: with several books converting at once and a big one taking
        minutes, a run that only spoke on completion left the screen silent while
        it was busiest.
        """
        partial_path, _, out_rel = record.partition(UNIT)
        rel = partial_path
        book_temp = os.path.join(self.temp_path, rel)
        book_avif = os.path.join(self.avif_path, rel)

        # Resume on PROVENANCE rather than on names: every .cbz records the
        # archive it was made from, which is what survives the collective naming
        # being set-dependent.
        if partial_path in self.converted:
            self.counters.progress("Skip (exists): %s"
                                   % os.path.basename(out_rel))
            self.counters.bump("packaged")
            return
        # And the same check by name, for archives converted before provenance was
        # recorded: no comment to go by, but a matching name is still a finished
        # book.
        if os.path.exists(os.path.join(self.out_path, out_rel)):
            self.counters.progress("Skip (exists): %s"
                                   % os.path.basename(out_rel))
            self.counters.bump("packaged")
            return

        # The whole path as the user gave it, not just the file name: a collection
        # has several "01.cbz" in different series folders, and with parallel
        # workers interleaving their lines it has to say which is being worked on.
        self.counters.progress("Converting: %s"
                               % os.path.join(self.in_path, partial_path))

        extract(os.path.join(self.in_path, partial_path), book_temp,
                self.max_res)

        file_name = os.path.basename(partial_path)
        if not os.path.isdir(book_temp):
            self.counters.note("Skip (no pages): %s" % file_name)
            return
        self.counters.bump("pagesFound")

        os.makedirs(os.path.dirname(book_avif) or self.avif_path, exist_ok=True)
        self._convert_pages(book_temp, book_avif, file_name)
        shutil.rmtree(book_temp, ignore_errors=True)

        # The pages that came out broken, counted as they are dropped: this is the
        # only place the run learns of them, because the converter counted such a
        # page as converted - it wrote a file and exited 0 - and only its size
        # afterwards says otherwise.
        broken = 0
        for parent, _dirs, names in os.walk(book_avif):
            for name in names:
                if not name.endswith(".avif"):
                    continue
                path = os.path.join(parent, name)
                try:
                    if os.path.getsize(path) < MIN_PAGE_SIZE:
                        os.remove(path)
                        broken += 1
                except OSError:
                    pass
        if broken:
            self.counters.bump("pagesBroken", broken)

        if not _files_matching(book_avif, ("avif",)):
            self.counters.note("Skip (nothing converted): %s" % file_name)
            shutil.rmtree(book_avif, ignore_errors=True)
            return

        # Numbering is per folder by nature, so it needs nothing from the other
        # books and belongs here, while the collective NAME cleaning had to wait
        # for all of them. Before packaging, so the pages are numbered inside the
        # archive.
        numbering.number_files_in_folder(
            book_avif,
            sorted((entry.path for entry in os.scandir(book_avif)
                    if entry.is_file()), key=version_key))

        if package_book(book_avif, self.out_path, out_rel, partial_path,
                        self.avif_path, self.counters):
            self.counters.bump("packaged")

        # The pages are inside the archive on disk now: this book's RAM back
        # before the next one is unpacked into it.
        shutil.rmtree(book_avif, ignore_errors=True)

    def _convert_pages(self, book_temp: str, book_avif: str,
                       file_name: str) -> None:
        """This book's pages, converted by convert-images as its own process -
        with its output captured, because a few dozen books' worth of its progress
        would bury this run's own. Shown when the conversion actually fails."""
        log_path = os.path.join(self.counters.dir,
                                "convert.%d.log" % os.getpid())
        with open(log_path, "w") as handle:
            done = commands.run_command(
                "convert-images",
                ["-c", "-j", IMAGES_PER_BOOK, "-m", self.max_res,
                 "-q", self.quality, "-s", self.speed_preset,
                 "-f", self.fuzz, book_temp, book_avif],
                script_dir=self.script_dir,
                stdout=handle, stderr=subprocess.STDOUT)
        if done.returncode != 0:
            sys.stderr.write("WARNING: conversion failed for %s\n" % file_name)
            try:
                with open(log_path) as handle:
                    sys.stderr.write(handle.read())
            except OSError:
                pass
        try:
            os.remove(log_path)
        except OSError:
            pass

    def vet_pdf(self, relative: str) -> None:
        """Whether one PDF is a comic, with the numbers the verdict was reached
        on - because "this PDF is not a comic" is a claim the user has to be able
        to check: 2 full-page images in 80 pages is a magazine, 23 in 24 is a
        comic with a credits page."""
        comic, pages, good, dpi = comicpdf.comic_pdf_verdict(
            os.path.join(self.in_path, relative), str(self.max_res))
        with open(os.path.join(self.counters.dir, "pdfList.lock"), "w") as lock, \
                runlog.take_lock(lock):
            if comic:
                with open(self.input_list, "a") as handle:
                    handle.write(relative + "\0")
                sys.stdout.write(
                    "Comic: %s (%d of %d page(s) hold one full-page image, "
                    "rendering at %d dpi)\n" % (relative, good, pages, dpi))
            else:
                sys.stdout.write(
                    "Not a comic, skipped: %s (%d of %d page(s) hold one "
                    "full-page image)\n" % (relative, good, pages))
            sys.stdout.flush()
        self.counters.bump("pdf.comic" if comic else "pdf.other")


def _in_worker(state: Run, method: str, item: str) -> None:
    """One item, in a worker PROCESS. The worker's interrupt handling is
    installed here rather than in the work, because at width 1 that same work
    runs in the RUN's own process - where the worker's handler would replace the
    run's."""
    safety.trap_worker_abort()
    getattr(state, method)(item)


def _run_pool(state: Run, method: str, items: list, jobs: int) -> None:
    if jobs <= 1:
        for item in items:
            if safety.abort_requested():
                return
            getattr(state, method)(item)
        return

    workerpool.run(items, jobs, _in_worker, lambda item: (state, method, item))


def _page_stats(stats_file: str) -> tuple:
    """The per-book page counts, summed by column.

    One line per book, written by the converter, because its own report went into
    a per-book log this script threw away. An empty file sums to zeroes rather
    than to nothing, so a run in which every book was already on disk still lands
    on numbers.
    """
    totals = [0] * 6
    books = 0
    try:
        with open(stats_file) as handle:
            for line in handle:
                if not line.strip():
                    continue
                books += 1
                for index, field in enumerate(line.split()[:6]):
                    try:
                        totals[index] += int(field)
                    except ValueError:
                        pass
    except OSError:
        pass
    return totals, books


def _print_page_stat(label: str, count: int, total: int) -> None:
    """One line per category that actually occurred: an ordinary run of comics
    would otherwise carry three lines of zeroes."""
    if count <= 0:
        return
    print("    %-26s %d/%d (%s%%)"
          % (label + ":", count, total, "%.1f" % (100.0 * count / total)))


def footer(state: Run) -> None:
    """The closing report, named above every point the run can be cut short - so
    a Ctrl+C still reports the books and pages it got through."""
    counters = state.counters
    # Every figure below is produced by the conversion phase, so a run stopped
    # before that phase began has only the safety recap to give, and says so by
    # printing nothing rather than a page of zeroes about work that never began.
    if state.phase_start is not None:
        packaged = counters.read("packaged")
        end = state.phase_end if state.phase_end is not None else time.time()
        runtime = end - state.phase_start
        print("")
        print("Converted and packaged %d of %d book(s) in %.2f seconds"
              % (packaged, state.total, runtime))
        if packaged > 0:
            print("%.2f seconds per book" % (runtime / packaged))

        # A book is a coarse unit to judge a run by - collections hold 20-page
        # floppies and 300-page omnibuses in the same folder - so seconds per PAGE
        # is the number comparable between runs.
        totals, books = _page_stats(state.stats_file)
        page_total = totals[0]
        if page_total > 0:
            print("")
            print("%d page(s) in %d converted book(s), %.3f seconds per page"
                  % (page_total, books, runtime / page_total))
            # The five below partition the page total - the converter puts every
            # page in exactly one of them - so what is printed adds up to 100%.
            _print_page_stat("converted", totals[1], page_total)
            _print_page_stat("trimmed", totals[2], page_total)
            _print_page_stat("blank, dropped", totals[3], page_total)
            _print_page_stat("already done, skipped", totals[4], page_total)
            _print_page_stat("not found, failed", totals[5], page_total)
            # Not a sixth category but a subset of the first two, hence "of
            # those": a page that encoded to something too small to be a picture
            # was counted as converted by the encoder, which wrote a file and
            # exited 0 and had no way to know better.
            _print_page_stat("of those broken, dropped",
                             counters.read("pagesBroken"), page_total)

        if os.path.isdir(state.out_path):
            for parent, _dirs, _names in os.walk(state.out_path, topdown=False):
                if parent != state.out_path:
                    try:
                        os.rmdir(parent)
                    except OSError:
                        pass
            print("")
            print("Wrote %d cbz file(s) to %s"
                  % (len(_files_matching(state.out_path, ("cbz",))),
                     state.out_path))

    # The recap goes to stderr and everything above it to stdout, so the two are
    # only in the order they were written if this one is flushed first - a log
    # capturing both would otherwise show the recap ahead of the report.
    sys.stdout.flush()
    safety.report_safety_skips()


def _records(path: str) -> list:
    """The NUL-terminated records of one list file."""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as handle:
            return [record for record in handle.read().split("\0") if record]
    except OSError:
        return []


def _naming_pass(state: Run, archives: list) -> list:
    """The name every book will be packaged under, worked out BEFORE anything is
    converted.

    Two things force this order. The collective rules strip the affixes sibling
    books share, so they have to see every book at once - which converting one
    book at a time gives up. And the name has to be final before a book is
    written, or resume is impossible: a .cbz written under a provisional name and
    renamed afterwards matches nothing on the next run.

    So the naming runs on a tree of MARKERS - one file per archive, named as its
    .cbz would be, in the mirrored folder - and the real clean-folder-structure
    is run over that throwaway tree. Each marker CONTAINS the path of the archive
    it stands for, which is what makes the result usable: renaming reorders the
    tree and a collision resolves to a suffixed name, so no before/after listing
    could be paired up - but a file still knows what it came from.
    """
    name_root = os.path.join(state.counters.dir, "names")
    os.makedirs(name_root, exist_ok=True)
    for archive_rel in archives:
        marker_dir = name_root
        if "/" in archive_rel:
            marker_dir = os.path.join(name_root, os.path.dirname(archive_rel))
        os.makedirs(marker_dir, exist_ok=True)
        marker_name = os.path.basename(archive_rel)
        # One marker per output NAME, and a book APPENDS rather than replacing
        # what is there: "Batman.cbz" and "Batman.pdf" in one folder are two books
        # that both want to be called "Batman.cbz". Overwriting would drop one of
        # them; giving the second a " (2)" marker would be worse - the collective
        # rules would then see two siblings sharing "Batman" and strip it from
        # both, so a pair of books would come out as "Batman.cbz" and "(2).cbz".
        marker = os.path.join(marker_dir,
                              os.path.splitext(marker_name)[0] + ".cbz")
        with open(marker, "a", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write(archive_rel + "\0")

    commands.run_command("clean-folder-structure", [name_root],
                         script_dir=state.script_dir)

    books = []
    for parent, _dirs, names in os.walk(name_root):
        for name in sorted(names):
            marker = os.path.join(parent, name)
            out_rel = os.path.relpath(marker, name_root)
            for copy, source in enumerate(_records(marker)):
                book_out = out_rel
                if copy > 0:
                    # The " (N)" suffix the rest of the repo gives a collision,
                    # applied HERE to the finished name - so it is not something
                    # the name cleaning ever saw and could strip back off as a
                    # shared affix.
                    book_out = "%s (%d).cbz" % (out_rel[:-len(".cbz")], copy + 1)
                books.append(source + UNIT + book_out)
    return books


def main(argv: list, program: str = "convert-comics",
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

    script_dir = script_dir or commands.script_dir()

    # Where the counters and both work trees are created. An explicit
    # comicsRamBase wins over the default tmpfs - a caller that wants the work
    # somewhere else, or a test that needs a private base it can assert on
    # without reading a directory the whole machine shares.
    ramscratch.init_ram_base(os.environ.get("comicsRamBase", ""))
    counter_dir, status = ramscratch.ram_scratch_dir("counters")
    if status != 0 or not counter_dir:
        sys.stderr.write("\nError: no scratch directory could be made for this "
                         "run.\nNothing was changed.\n")
        return 1
    ramscratch.add_exit_cleanup([counter_dir])

    try:
        return _run(result, declaration, program, script_dir, counter_dir)
    finally:
        # The shell's `trap 'runExitCleanup' EXIT`: both RAM work trees and the
        # counter dir go back to the tmpfs however this run ends.
        ramscratch.run_exit_cleanup()


def _run(result, declaration, program: str, script_dir: str,
         counter_dir: str) -> int:
    if clioptions.args_out_of_range(len(result.positionals), 2, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1
    in_path = result.positionals[0].rstrip("/")
    out_path = result.positionals[1].rstrip("/")

    quality = result.values["quality"] or DEFAULT_QUALITY
    speed_preset = result.values["speedPreset"] or DEFAULT_SPEED_PRESET
    max_res = int(result.values["maxRes"] or DEFAULT_MAX_RES)
    fuzz = result.values["fuzz"] or DEFAULT_FUZZ

    # A .cbz written inside the input tree is exactly what this script converts,
    # so the next run would convert it again - and the de-duplication of the input
    # would reach into the finished archives first.
    if safety.require_separate_output(in_path, out_path):
        return 1

    # The tools every run needs, before the input is de-duplicated and its names
    # are pretreated. The extractors join that list only when this input actually
    # holds the format that needs them.
    tools = ["zip", "unzip", imagemagick.CONVERT_SPEC,
             imagemagick.IDENTIFY_SPEC]
    if input_holds_extension(in_path, "cbr"):
        tools.append("unrar")
    if input_holds_extension(in_path, "cb7"):
        tools.append("7z|7zz|7za")
    if tooldeps.require_tools(program, tools):
        return 1

    have_fdupes = _settle_fdupes()
    runlog.warn_uncounted_progress()

    # Both work directories live in the RAM base, so no intermediate file ever
    # hits the disk: temp_path holds each book's extracted pages, avif_path
    # receives its AVIF pages and is where the numbering and zipping happen.
    temp_path, temp_status = ramscratch.ram_scratch_dir("convertComics")
    avif_path, avif_status = ramscratch.ram_scratch_dir("convertComics.avif")
    if temp_status != 0 or avif_status != 0 or not temp_path or not avif_path:
        sys.stderr.write("\nError: no scratch directory could be made for this "
                         "run.\nNothing was changed.\n")
        return 1
    ramscratch.add_exit_cleanup([temp_path, avif_path])

    safety.init_safety_log(os.path.join(counter_dir, "safetySkips.log"))
    skips = safety.RunSkipLog()
    safety.init_abort_flag(os.path.join(counter_dir, "abortRequested"))
    safety.trap_run_abort()

    counters = Counters(counter_dir)
    state = Run(
        in_path=in_path, out_path=out_path, script_dir=script_dir,
        temp_path=temp_path, avif_path=avif_path, counters=counters,
        quality=quality, speed_preset=speed_preset, max_res=max_res, fuzz=fuzz,
        converted=set(), input_list=os.path.join(counter_dir, "inputs"),
        stats_file=os.path.join(counter_dir, "pageStats"),
        phase_start=None, phase_end=None, total=0,
    )
    safety.set_run_footer(lambda: footer(state))

    # Nothing to convert? Said before the input is de-duplicated, before any
    # folder is created and before pretreatment renames anything, so a refused run
    # leaves the input exactly as it was. A PDF counts here on the strength of its
    # extension alone: whether it is really a comic takes reading the file, which
    # happens in the PDF pass below.
    if not _files_matching(in_path, COMIC_INPUT_EXTENSIONS):
        return safety.fail_no_relevant_input(
            in_path, "comic archives (%s) or comic PDFs (%s)"
            % (enums.extension_list(list(enums.COMIC_EXTENSIONS)),
               enums.extension_list(list(enums.COMIC_PDF_EXTENSIONS))))

    if have_fdupes:
        subprocess.run(["fdupes", "-rdN", in_path], stdout=subprocess.DEVNULL)
    # Prune empty folders inside the input, but never the input folder itself.
    for parent, _dirs, _names in os.walk(in_path, topdown=False):
        if parent != in_path:
            try:
                os.rmdir(parent)
            except OSError:
                pass

    try:
        os.chdir(in_path)
    except OSError:
        return 1

    for parent, _dirs, _names in os.walk(in_path):
        os.makedirs(os.path.join(temp_path, os.path.relpath(parent, in_path)),
                    exist_ok=True)

    pretreat_input(in_path, skips)

    # The books this run converts: every archive, plus the PDFs that turn out to
    # be comics. Collected into one list rather than re-found later, because the
    # naming pass has to see exactly the set that will be converted - a magazine
    # left in it would change the affixes the collective rules strip from its
    # siblings.
    archives = sorted(os.path.relpath(path, in_path) for path in
                      _files_matching(in_path, enums.COMIC_EXTENSIONS,
                                      exact_case=True))
    with open(state.input_list, "w", encoding="utf-8",
              errors="surrogateescape") as handle:
        for relative in archives:
            handle.write(relative + "\0")

    pdfs = sorted(os.path.relpath(path, in_path) for path in
                  _files_matching(in_path, enums.COMIC_PDF_EXTENSIONS,
                                  exact_case=True))
    if pdfs:
        print("PDFs")
        print("====")
        print("")
        # Asked once and said out loud: with the poppler tools absent every PDF
        # reads as "no full-page images" and is skipped as not a comic, which is
        # indistinguishable from a correct verdict on a folder of magazines.
        missing = _missing_pdf_tools()
        if missing:
            sys.stderr.write("WARNING: %s not found - PDFs cannot be inspected "
                             "and are all skipped.\n\n" % missing)
        for name in ("pdf.comic", "pdf.other"):
            with open(os.path.join(counter_dir, name), "w") as handle:
                handle.write("0")
        # At the page-conversion width rather than the book width: reading a
        # PDF's page and image tables is a short burst of parsing, not a book's
        # worth of work, and none of it is held in RAM afterwards.
        _run_pool(state, "vet_pdf", pdfs,
                  max(1, runlog.cpu_count() // THREADS_PER_CONVERSION))
        safety.exit_if_aborted()
        print("")
        print("%d of %d PDF(s) taken as comic book(s)"
              % (counters.read("pdf.comic"), len(pdfs)))
        print("")

    books_found = _records(state.input_list)
    # Everything found is a PDF and none of them is a comic. Refused here with
    # what was actually looked at, rather than left to the generic refusal at the
    # end: no page of this input is worth rasterising.
    if not books_found:
        sys.stderr.write('\nNothing to do: none of the %d PDF(s) in "%s" is a '
                         "comic book.\n" % (len(pdfs), in_path))
        sys.stderr.write("A comic PDF holds one large image per page; a magazine "
                         "or a text\n")
        sys.stderr.write("document does not, and rasterising it page by page "
                         "would only produce\n")
        sys.stderr.write("a large, unreadable .cbz. Nothing was converted.\n")
        return 1

    print("Naming")
    print("======")
    print("")

    # What is already converted, read out of the archives themselves rather than
    # guessed from their names. Only .cbz written by a version that records it are
    # listed; older ones fall back to the name check in process_book.
    if os.path.isdir(out_path):
        for existing in _files_matching(out_path, ("cbz",)):
            done = subprocess.run(["unzip", "-z", "-qq", "--", existing],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
            first = done.stdout.decode("utf-8", "surrogateescape").split("\n")
            if first and first[0].strip():
                state.converted.add(first[0].strip())

    book_list = _naming_pass(state, books_found)

    print("")
    print("Converting")
    print("==========")
    print("")
    state.total = len(book_list)
    counters.total = state.total
    for name in ("current", "pagesFound", "packaged", "pagesBroken"):
        with open(os.path.join(counter_dir, name), "w") as handle:
            handle.write("0")
    # Where each book's page counters are collected: the converter appends one
    # line per book here, because its own report goes into a per-book log this
    # script throws away.
    open(state.stats_file, "w").close()
    # The child converter appends one line per book here, so it has to be told
    # where: its own report goes into a per-book log this script throws away, and
    # the closing statistics are summed out of this file.
    os.environ["imageStatsFile"] = state.stats_file
    os.makedirs(out_path, exist_ok=True)
    state.phase_start = time.time()

    _run_pool(state, "process_book", book_list,
              book_workers(runlog.cpu_count()))
    safety.exit_if_aborted()
    state.phase_end = time.time()

    # The same two refusals this script has always made, answered from what the
    # workers saw: the archives were found, but did any of them hold a page, and
    # did any page survive being converted? A run that only found books already on
    # disk counts as packaged and so refuses nothing, which is what a resumed run
    # of a finished library is.
    if counters.read("packaged") == 0:
        if counters.read("pagesFound") == 0:
            return safety.fail_no_relevant_input(
                in_path, "comic books holding pages (%s, or PDF pages that "
                "could be rendered)"
                % enums.extension_list(list(enums.IMAGE_EXTENSIONS)))
        return safety.fail_no_relevant_input(
            in_path, "pages that could be converted to AVIF (all encodes failed "
            "or came out broken)")

    safety.print_run_footer()
    return 0


def _missing_pdf_tools() -> str:
    """The poppler tools that are not installed, space separated."""
    return " ".join(tool for tool in ("pdfinfo", "pdfimages", "pdftoppm")
                    if shutil.which(tool) is None)


def _settle_fdupes() -> bool:
    """fdupes de-duplicates the input before the run, so identical files are
    converted once and stored as hard links. It is the one tool here whose
    absence changes WHAT happens rather than whether it happens - without it the
    duplicates are converted and stored as separate copies, and nothing else is
    lost. Settled once and shared with the per-book children, so the fact is said
    once per run."""
    inherited = os.environ.get("HAVE_FDUDES")
    if inherited is not None:
        return bool(inherited)
    present = tooldeps.tool_present("fdupes")
    os.environ["HAVE_FDUDES"] = "1" if present else ""
    if not present:
        log("WARNING: fdupes not found (apt install fdupes) - the input will not "
            "be de-duplicated,")
        log("         so identical files are converted and stored as separate "
            "copies. The")
        log("         conversion itself is unaffected.")
    return present


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
