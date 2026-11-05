"""convert-comics: a tree of comic books recompressed into .cbz of AVIF pages.

Every book goes archive -> extracted pages -> AVIF pages -> archive again, and
only that last step touches the disk: the pages in between live in RAM.

A comic that arrived as a PDF is the same book in a different container, so it
joins the pipeline at the same place - its pages are rendered rather than
unzipped, and from the folder of pages onwards nothing knows the difference.
Unlike an archive, though, a PDF does not announce that it is a comic: a
magazine and a manual are also stacks of pages. So every PDF is inspected first
and only the ones shaped like a scan are taken.

A few books at a time are in RAM, not one collection. The run's own process
prepares the next book - unpacks it into RAM, drops its duplicate pages, judges
whether it is starved - while the workers encode the books prepared before it,
and a worker takes one prepared book through convert -> zip and then
frees that book's two RAM folders. One prepared book waits per worker and a
few more are being prepared behind them, so peak RAM is a small multiple of
the number of workers times ONE book however many thousands of archives the
run was handed.

What cannot be decided one book at a time is the collective NAME cleaning, which
strips the affixes sibling books share and so has to see all of them together.
That happens first, on a throwaway tree of markers, before anything is converted
- which is also what makes a re-run able to recognise a finished book instead of
converting it a second time.

The one thing decided per BOOK rather than per page is whether to convert it at
all. A book's pages are judged against medialib/lib/imagebitrate.py's model, and if
more than STARVED_PAGE_PERCENT of them are already starved the book is repackaged
into a .cbz with its own pages untouched: what is left of such a scan is all
there is, and a second generation of loss over most of a book buys nothing. Below
that share every page is converted, starved ones included - which is why the
conversion is asked for with -a. A book whose pages came out in two formats is
not a book, it is a bag of pages, and no reader shows it as one.
"""

import collections
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor

from medialib import commands
from medialib.cli.convert_images import (
    CHROMAS,
    LANE_VARIABLE,
    MAX_SPEED,
    threads_per_conversion,
)
from medialib.lib import (
    adequacy,
    archives,
    av1gpu,
    clioptions,
    comicpdf,
    dynamicqueue,
    encodercalibration,
    enums,
    errorlog,
    gpulane,
    imagebitrate,
    imagemagick,
    ramscratch,
    runlog,
    safety,
    settings,
    toolcapture,
    tooldeps,
    workerpool,
)
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>

Converts every comic book under <inputDir> into a .cbz of AVIF pages in the
mirrored folder under <outputDir>. Books may be .cbr/.cbz/.cb7 archives or PDFs
that hold one large image per page; a PDF that is not shaped like a scanned comic
(a magazine, a text document) is reported and skipped.

Options:"""

# Pages are written 4:4:4, which keeps coloured line art and lettering sharp but
# costs about a quarter more per page at the same quality. The slower speed and
# the lower quality below are what pay for it: on a flat-coloured, inked page,
# 4:4:4 at speed 4 and quality 45 came out the size 4:2:0 at speed 5 and quality
# 50 did.
DEFAULT_CHROMA = "444"
DEFAULT_QUALITY = 45
# Height ceiling for a converted page, in pixels: the long edge of the display
# these are read on. A page is shown fitted to that edge, so pixels past it are
# ones the screen cannot put anywhere - they only cost bytes in every book.
DEFAULT_MAX_RES = 2960
DEFAULT_SPEED_PRESET = 4
DEFAULT_FUZZ = 10

# The spec is DATA, and the page it renders is compared byte for byte against the
# recorded contract under tests/data/cliContract.
OPT_SPEC = """
h |  | Print this help page.
l | <quality> | quality level of the output images, default {quality}
u | <chroma> | page chroma: {chromas}. 444 keeps colour at full resolution,
                    420 at a quarter of it, for smaller books with colour
                    fringes on thin lines and lettering. Default {chroma}.
s | <speed> | av1 speed preset, lower is slower, default {speed}
m | <maxRes> | maximum resolution (height) to keep, default {max_res}
f | <fuzz> | when trimming, how many percent color difference gets still
                    trimmed, default {fuzz}
""".format(quality=DEFAULT_QUALITY, chromas=" or ".join(CHROMAS),
           chroma=DEFAULT_CHROMA, speed=DEFAULT_SPEED_PRESET,
           max_res=DEFAULT_MAX_RES, fuzz=DEFAULT_FUZZ)

# Checked here rather than left to convert-images, where a bad value would be
# refused once per book, deep into the run.
OPT_CHECKS = """
s | int:0:{top} | speed preset
u | enum:{chromas} | chroma
""".format(chromas="\\|".join(CHROMAS), top=MAX_SPEED)

OPT_VARS = "l:quality u:chroma s:speedPreset m:maxRes f:fuzz"
OPT_COLUMN = 20
OPT_LONG = "h:help l:quality u:chroma s:speed m:max-resolution f:fuzz"

# The smallest an AVIF page can plausibly be, in bytes. A page that encodes to
# less than this is an empty page - nothing on it worth a page in the book - and
# it is dropped and counted as such. Not a failure: the encode did its job.
MIN_PAGE_SIZE = 10 * 1024

# How much of a book has to be starved before the book is left as it is, in
# percent of the pages that could be measured. Above four fifths the book IS a
# starved scan - the handful of pages still holding bytes are the covers and the
# text pages, not evidence that the rest can be improved - and converting it
# would spend a generation of loss on nearly every page to save bytes off a file
# that is already small. Below it the odd thin page is converted with the rest,
# because a book whose pages are half AVIF and half JPEG is worse than either.
STARVED_PAGE_PERCENT = 80

# Two files under one page name are one page only below this RMSE, as a share
# of full scale. A scan's JPEG export against its own PSD measured 0.011, two
# neighbouring pages of the same book 0.43.
SAME_PICTURE_RMSE = 0.03

# Pages converting at once inside one book, when there are books enough to keep
# the host busy; fewer books than workers each get a wider share. What one page
# costs in threads is convert-images' figure, which moves with -s.
IMAGES_PER_BOOK = 4
# Deliberate oversubscription: a book worker is not converting the whole time -
# it zips at the end, and the encoder winds down over a book's last pages - so
# sizing the pool to the bare thread count would leave the CPU idling through
# those stretches.
OVERSUBSCRIBE = 2

# Under the lane a book worker only sorts its pages - a header read, the trim's
# box and, for a page it cut, the crop the card is given - and the lane encodes
# them. That is what feeds the card, so it is run wider than the encoding it
# replaced: this many books at once, this many pages of each.
LANE_BOOKS = 4
LANE_PAGES_PER_BOOK = 8

# What av1gpu is asked for in place of -s and -l. convert-images' calibration
# was measured on photographs at 4:2:0, and on comic pages at 4:4:4 it is off:
# its quality 47 for -l 45 scored 65 in SSIMULACRA2 where the CPU's page scored
# 73, so the card's pages would be visibly worse than the CPU's in the same
# book. Measured 2026-10-31 on an RTX 5090 (av1gpu 84394ca) against ImageMagick
# 7.1.2 / libaom at -s 4 4:4:4, over 24 1280x1280 crops of four comics - a
# digital colour comic and three album scans. The preset is turbo whatever -s
# says: at equal score its files were 6 % larger than fast's, at twice the
# speed (129-148 MP/s against 69); faster bought nothing at the same 6 %, and
# turbo2 and turbo3 cost 19 % and 26 % on line art. Only -s 4 was measured.
COMIC_CALIBRATION = encodercalibration.Calibration(
    presets={4: "turbo"},
    qualities={4: [(35, 46.6), (45, 56.1), (55, 66.9)]})

# Prepared books waiting for a worker, per worker. Each one is a whole book
# unpacked in RAM, so the buffer is kept to what a freed worker needs to start
# again at once.
BOOKS_BUFFERED_PER_WORKER = 1

# Threads one book's preparation is spread over: the unpacking of a .cbz and
# the header probe of every page. The preparation runs in the run's own process,
# one book at a time, and the workers can only start a book once it is
# prepared, so a serial unzip - 3.6 s of one core for a 360 MB book, against
# 0.3 s on eight - left the workers waiting for it on every book that encodes
# faster than it unpacks.
PREPARE_THREADS = 8

# Books prepared at once. A .cbr is unpacked by unrar on one core - 1.5 s for
# a 100-page album - and the next books' unpacking and probing overlap it.
PREPARE_BOOKS = 3

# How far the input reader runs ahead of the unpacking, in bytes of archives
# read into RAM and not yet unpacked. The input is often a mechanical drive, so
# one thread reads every archive whole, back to back, in the order they will be
# prepared: two books read at once would make the heads seek between them, and
# a read that waited for the unpacking would leave the drive idle meanwhile.
READ_AHEAD_BYTES = 8 * 1024 ** 3

# The record separator between a book and the output path it is destined for: a
# comic's name may contain anything else.
UNIT = "\x1f"


def spec(program: str) -> clioptions.Spec:
    return clioptions.with_verbosity(clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        checks=OPT_CHECKS,
        column=OPT_COLUMN,
    ))


def page_slots(threads: int, speed: int) -> int:
    """How many pages the whole run converts at once, across every book."""
    return int(threads * OVERSUBSCRIBE / threads_per_conversion("avif", speed))


def book_workers(threads: int, speed: int = DEFAULT_SPEED_PRESET) -> int:
    """How many books convert at once.

    Books, not pages, is what scales with the host, because that is the axis that
    costs RAM: one more book worker is one more book resident, while a fifth page
    inside an already-unpacked book would cost none. Two is the floor - with a
    single worker there is no second book to convert through the first one's
    zipping.
    """
    return max(2, page_slots(threads, speed) // IMAGES_PER_BOOK)


def pages_per_book(threads: int, speed: int, books: int) -> int:
    """How many pages one book converts at once.

    IMAGES_PER_BOOK while the pool is full. A run with fewer books than workers
    leaves slots no book would fill, so the books that are running share them:
    one book alone on a 32-thread host would otherwise hold half of it.
    """
    running = max(1, min(books, book_workers(threads, speed)))
    return max(IMAGES_PER_BOOK, page_slots(threads, speed) // running)


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


def containers_held(root: str) -> set:
    """The archive types (``zip``, ``rar``, ``7z``) this input's comic archives
    really are - which extractors it needs. A library of .cbz should not have to
    have unrar and 7-Zip installed, and one whose .cbz is really a RAR does."""
    return {comic_container(path)
            for path in _files_matching(root, _COMIC_CONTAINERS)} - {""}


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
            runlog.say("%s%s\n" % (
                runlog.counted_prefix(current, self.total), line))
            sys.stderr.flush()

    def note(self, line: str, failed: bool = False) -> None:
        """One line about a book that has already been counted. A book announces
        itself when it STARTS, before anything is known about how it ends, so the
        few that end badly still have something to say afterwards - and saying it
        must not make the run look one book further along than it is. A book
        that FAILED is said under -q too: the footer counts it but never names
        it."""
        with self._lock() as lock, runlog.take_lock(lock):
            if failed:
                sys.stderr.write("    %s\n" % line)
            else:
                runlog.say("    %s\n" % line)
            sys.stderr.flush()

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
    name_by_content(in_path, skips)


def name_by_content(in_path: str, skips: safety.SkipLog) -> None:
    """Every comic archive renamed to the extension of what it really is: a
    zip called .cbr becomes .cbz, a RAR called .cbz becomes .cbr. Its listing
    and unpacking go by what it is either way; the rename is so the library
    stops saying otherwise. A name already taken is left alone and recorded."""
    for path in _files_matching(in_path, _COMIC_CONTAINERS, exact_case=True):
        real = archives.archive_type_by_content(path)
        stem, _, ext = path.rpartition(".")
        if not real or _COMIC_CONTAINERS[ext] == real:
            continue
        renamed = stem + "." + _COMIC_EXTENSION_OF[real]
        if safety.safe_rename(path, renamed, skips):
            log('  renamed "%s" to "%s": it is a %s archive'
                % (os.path.basename(path), os.path.basename(renamed), real))


COMIC_INPUT_EXTENSIONS = tuple(enums.COMIC_EXTENSIONS) + tuple(
    enums.COMIC_PDF_EXTENSIONS)

# Which archive each comic container really is, for the member check below.
_COMIC_CONTAINERS = {"cbz": "zip", "cbr": "rar", "cb7": "7z"}
_COMIC_EXTENSION_OF = {kind: ext for ext, kind in _COMIC_CONTAINERS.items()}


def comic_container(path: str) -> str:
    """The archive a comic container is - by its first bytes, and by its
    extension only when those match no archive - or ``""`` for one that is not
    a comic container at all (a PDF)."""
    ext = path.rsplit(".", 1)[-1].lower()
    if ext not in _COMIC_CONTAINERS:
        return ""
    return archives.archive_type_by_content(path) or _COMIC_CONTAINERS[ext]


def extract(archive: str, destination: str, max_res: int, source: str = ""):
    """One book's pages into <destination>, whatever container they arrived in.

    Every extractor's output is kept rather than shown, stdout included: each
    is chatty in its own way, and several books unpack at once so the lines
    would arrive interleaved and unattributed. What matters is whether pages
    appeared, which is decided by looking at the folder afterwards - so an
    extractor that fails lands the book in the "no pages" case, exactly where a
    corrupt archive lands, and the unpacker's run is handed back for the caller
    to replay - there, or beside the pages it did get out. None when no
    unpacker tool ran - a .cbz the threads unpacked whole included.
    """
    lowered = archive.lower()
    unpacked = None

    # A comic container is a zip, a rar or a 7z under another name, and is held
    # to the same rule: a book asking to write outside its own folder is read
    # before an unpacker starts, and not unpacked at all. Which of the three it
    # is comes from its contents, not its name. A refused book gets no folder,
    # which is what stops it here rather than at an empty conversion.
    container = comic_container(archive)
    if container:
        refused = archives.unsafe_members(archive, container)
        if refused:
            # Left alone to keep the library safe, which -q still says.
            runlog.error('not unpacking "%s": %s (%s)'
                         % (os.path.basename(archive), refused[0][1],
                            refused[0][0]), files=[source or archive])
            return None
    os.makedirs(destination, exist_ok=True)

    if container == "rar":
        unpacked = toolcapture.run(["unrar", "e", "--", archive, destination],
                                   report=False)
    elif container == "zip":
        if not _unzip_in_threads(archive, destination):
            unpacked = toolcapture.run(
                ["unzip", "-o", "-d", destination, "--", archive],
                report=False)
    elif container == "7z":
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
        unpacked = toolcapture.run(
            [seven, "x", "-y", "-o" + destination, "--", archive],
            report=False)
    elif lowered.endswith(".pdf"):
        comicpdf.render_comic_pdf_pages(archive, destination, str(max_res))

    # The links an archive was allowed to carry, gone before the flattening
    # walks the tree: a page that is a link is somebody else's file.
    archives.prune_irregular(destination)
    _flatten(destination)
    if os.path.isdir(destination):
        # One line per book rather than per page: a scanner's working files
        # left beside every export would otherwise be a line for every page.
        dropped = drop_alternate_formats(destination)
        if dropped:
            log('  "%s": dropped %d page(s), the same picture(s) also there in '
                "another format: %s" % (os.path.basename(archive),
                                        len(dropped), ", ".join(dropped)))
    return unpacked


def _unzip_in_threads(archive: str, destination: str) -> bool:
    """A .cbz unpacked across PREPARE_THREADS, or False with <destination>
    left empty for unzip to do it instead.

    Anything out of the ordinary goes to unzip - a damaged or encrypted
    member, a format zipfile does not read - because unzip's status and words
    are what the run reports a damaged book with. A link member is left out,
    as the unzip path prunes it afterwards: zipfile would write it as a file
    holding the link's target, and that file would pass for a page.
    """
    try:
        with zipfile.ZipFile(archive) as book:
            members = [member for member in book.infolist()
                       if not member.is_dir()
                       and not stat.S_ISLNK(member.external_attr >> 16)]
        if not members:
            return False

        def unpack(share: list) -> None:
            with zipfile.ZipFile(archive) as book:
                for member in share:
                    book.extract(member, destination)

        threads = min(PREPARE_THREADS, len(members))
        with ThreadPoolExecutor(threads) as pool:
            for _ in pool.map(unpack, [members[n::threads]
                                       for n in range(threads)]):
                pass
        return True
    except Exception:  # noqa: BLE001 - any failure is unzip's to report
        shutil.rmtree(destination, ignore_errors=True)
        os.makedirs(destination, exist_ok=True)
        return False


def _flatten(destination: str) -> None:
    """The archive's nested layout flattened, then everything that is not a page
    pruned. Two same-named pages from different sub-folders would clobber each
    other, so a colliding file is given a " (N)" suffix instead.

    The resource forks a Mac leaves in an archive - ``__MACOSX/`` and ``._``
    files, which carry a page's name and none of its picture - go first and
    without a word: they are not pages, and no encoder can read one."""
    _drop_apple_double(destination)
    for parent, _dirs, names in os.walk(destination):
        if os.path.abspath(parent) == os.path.abspath(destination):
            continue
        for name in names:
            source = os.path.join(parent, name)
            target = os.path.join(destination, name)
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


def _drop_apple_double(destination: str) -> None:
    for parent, dirs, names in os.walk(destination):
        for name in [name for name in dirs if name == "__MACOSX"]:
            shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
            dirs.remove(name)
        for name in names:
            if name.startswith("._"):
                try:
                    os.remove(os.path.join(parent, name))
                except OSError:
                    pass


def same_picture(first: str, second: str) -> bool:
    """Whether two page files hold the same picture, as far as can be shown.

    The same size in pixels, and a root-mean-square difference under
    SAME_PICTURE_RMSE of full scale. Anything that cannot be measured is a
    difference, so a page is only ever dropped on evidence.
    """
    sizes = set()
    for path in (first, second):
        done = subprocess.run(
            imagemagick.identify_argv(["-format", "%w %h", path + "[0]"]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if done.returncode != 0 or not done.stdout.strip():
            return False
        sizes.add(done.stdout.strip())
    if len(sizes) != 1:
        return False
    # compare answers 0 for identical, 1 for different and 2 for failed, and
    # prints the metric on stderr as "absolute (normalised)".
    done = subprocess.run(
        imagemagick.compare_argv(["-metric", "RMSE", first + "[0]",
                                  second + "[0]", "null:"]),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if done.returncode not in (0, 1):
        return False
    text = done.stderr.decode("utf-8", "replace")
    try:
        normalised = float(text[text.index("(") + 1:text.index(")")])
    except ValueError:
        return False
    return normalised < SAME_PICTURE_RMSE


def drop_alternate_formats(book_dir: str) -> list[str]:
    """The second copy dropped of every page the book holds in two formats, and
    the names dropped returned.

    A scanner's working file left beside its export - 015.psd next to 015.jpg -
    is the same page twice, and a reader opening the archive shows the one it can
    read. Converted, both become pages and the book repeats itself. Only a
    shared NAME makes two files candidates, and only same_picture() makes them
    duplicates: two different pictures under one name are both kept. The copy
    kept is the one in the format most of the book's pages are in, because that
    is the book's own; a tie goes to the earlier format in enums.IMAGE_EXTENSIONS.
    """
    pages = [entry.name for entry in os.scandir(book_dir) if entry.is_file()]
    by_stem: dict[str, list[str]] = {}
    tally: dict[str, int] = {}
    for name in pages:
        stem, extension = os.path.splitext(name)
        by_stem.setdefault(stem.casefold(), []).append(name)
        tally[extension] = tally.get(extension, 0) + 1
    order = ["." + extension for extension in enums.IMAGE_EXTENSIONS]

    def rank(name: str) -> tuple:
        extension = os.path.splitext(name)[1]
        return (-tally[extension],
                order.index(extension) if extension in order else len(order))

    dropped = []
    for names in by_stem.values():
        if len(names) < 2:
            continue
        kept, *others = sorted(names, key=rank)
        for name in others:
            path = os.path.join(book_dir, name)
            if not same_picture(os.path.join(book_dir, kept), path):
                continue
            try:
                os.remove(path)
            except OSError:
                continue
            dropped.append(name)
    return sorted(dropped)


def starved_pages(book_dir: str) -> tuple[int, int]:
    """``(starved, measured)`` over the pages of one unpacked book.

    One ``identify -ping`` per page - the header and nothing else - so this is a
    read of a few hundred bytes per page against a book that is already in RAM,
    PREPARE_THREADS of them at once.
    Pages nothing could measure are left out of BOTH counts rather than counted
    as sound: the share is what decides the book's fate, and an unreadable page
    is evidence about neither side of it.
    """
    starved = measured = 0
    pages = _files_matching(book_dir, enums.IMAGE_EXTENSIONS)
    with ThreadPoolExecutor(PREPARE_THREADS) as pool:
        verdicts = list(pool.map(imagebitrate.image_adequacy, pages))
    for verdict in verdicts:
        if verdict == adequacy.UNKNOWN:
            continue
        measured += 1
        if adequacy.is_starved(verdict):
            starved += 1
    return starved, measured


def book_is_starved(starved: int, measured: int) -> bool:
    """Whether that count puts the book past :data:`STARVED_PAGE_PERCENT`.

    A book none of whose pages could be measured is NOT starved: the share is
    undefined, and the answer that converts it is the one that matches what every
    other unmeasurable thing in this repo gets.
    """
    return measured > 0 and starved * 100 > STARVED_PAGE_PERCENT * measured


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
        counters.bump("existing")
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
        done = toolcapture.run(["zip", "-0", "-q", "-X", "-D", staged] + pages,
                               cwd=book_dir,
                               warning="could not pack %s" % book_name)
        if not done.ok:
            return False

        # Which archive this came from, in the zip's own comment. The name is
        # decided collectively, so re-running over a folder that has since grown
        # can compute a different name for a book already converted, and matching
        # on names would then write a second copy. A comment lives in the central
        # directory: it survives a rename, costs about 30 bytes, and is not an
        # entry, so no reader shows it as a page.
        toolcapture.watched(["zip", "-z", "-q", staged],
                            input=(source_archive + "\n").encode(),
                            stdout=subprocess.DEVNULL,
                            skipped="the archive it came from could not be "
                                    "written into %s" % os.path.basename(cbz))

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
    chroma: str
    speed_preset: str
    pages_per_book: int
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
    # The GPU lane, None when the pages are all encoded on the CPU - and the
    # one setting with a default, since it is made after the rest. It lives in
    # the run's process only: its threads cannot go to a worker.
    lane: "gpulane.Lane | None" = None
    # Where each archive is read into RAM before it is unpacked; "" reads it
    # where it is.
    read_path: str = ""

    def __init__(self, **settings) -> None:
        self.__dict__.update(settings)

    def __getstate__(self) -> dict:
        state = dict(self.__dict__)
        state["lane"] = None
        return state

    def prepare_book(self, record: str, staged: str = ""):
        """The comic this is about, named for every error recorded meanwhile -
        a page of it that fails in RAM, or in the convert-images it starts."""
        with self._about(record):
            return self._prepare_book(record, staged)

    def _about(self, record: str):
        partial_path = record.partition(UNIT)[0]
        return errorlog.working_on(os.path.join(self.in_path, partial_path),
                                   item=partial_path)

    def _prepare_book(self, record: str, staged: str = ""):
        """One book's preparation, in the RUN's process: the unpacking into RAM
        and the starved verdict, while the workers encode the books prepared
        before it. <staged> is the archive's copy the reader made in RAM,
        unpacked in place of the input and removed once it has been. ``(item, size)`` for the queue, or None for a
        book that ends here, holding no page. A book already on disk never
        gets this far: split_packaged keeps it out of the queue.

        Exactly one COUNTED line per book, reported at its START rather than when
        it finishes: with several books converting at once and a big one taking
        minutes, a run that only spoke on completion left the screen silent while
        it was busiest.
        """
        partial_path = record.partition(UNIT)[0]
        book_temp = os.path.join(self.temp_path, partial_path)

        # The whole path as the user gave it, not just the file name: a collection
        # has several "01.cbz" in different series folders, and with parallel
        # workers interleaving their lines it has to say which is being worked on.
        # And the path it is going to, which the collective naming decided.
        self.counters.progress("Converting: %s\n    into: %s"
                               % (os.path.join(self.in_path, partial_path),
                                  os.path.join(self.out_path,
                                               record.partition(UNIT)[2])))

        # The pages unpacked into RAM, and the encodes made from them, name the
        # comic they came from in any error about one of them.
        source = os.path.join(self.in_path, partial_path)
        errorlog.note_origin(book_temp, source)
        errorlog.note_origin(os.path.join(self.avif_path, partial_path), source)
        staged = staged or source
        try:
            unpacked = extract(staged, book_temp, self.max_res, source=source)
        finally:
            if staged != source:
                shutil.rmtree(os.path.dirname(staged), ignore_errors=True)

        if not os.path.isdir(book_temp):
            if unpacked is not None and not unpacked.ok:
                toolcapture.report_failure(
                    unpacked, warning="could not unpack %s"
                    % os.path.basename(partial_path))
            self.counters.note("Skip (no pages): %s"
                               % os.path.basename(partial_path), failed=True)
            return None
        if unpacked is not None and not unpacked.ok:
            # Pages came out, but the unpacker did not finish cleanly. Status 1
            # is unrar's, unzip's and 7-Zip's own word for "done, with
            # warnings"; anything else gave up partway, and the book goes on
            # with the pages before the damage - an error, not a footnote.
            unpacked.label = os.path.basename(partial_path)
            if unpacked.returncode == 1:
                toolcapture.report_warnings(unpacked)
            else:
                toolcapture.report_failure(unpacked)
        self.counters.bump("pagesFound")

        # Judged before a single page is encoded, because the answer decides
        # whether any of them are.
        starved, measured = starved_pages(book_temp)
        if book_is_starved(starved, measured):
            # Last in the buffer: repackaging is a zip, not an encode.
            return (record, starved, measured), 0
        # The unpacked bytes stand in for how long the encode takes: the queue
        # only needs the ORDER right, and a book's bytes follow its pixels.
        size = sum(entry.stat().st_size for entry in os.scandir(book_temp)
                   if entry.is_file())
        return (record, starved, measured), size

    def finish_book(self, item: tuple):
        """The comic this is about, named for every error recorded meanwhile -
        a page of it that fails in RAM, or in the convert-images it starts."""
        with self._about(item[0]):
            return self._finish_book(item)

    def _finish_book(self, item: tuple) -> None:
        """One prepared book, in a worker: convert, zip, free.

        Under the lane the worker only sorts the pages: each one's encode,
        on the card or the CPU, is listed in the book's lane folder, marked
        ready, and the lane calls :meth:`package_pages` once every page has
        been encoded. The unpacked pages stay until then, since the encoders
        read them where they are."""
        record, starved, measured, lane_folder = item
        partial_path, _, out_rel = record.partition(UNIT)
        book_temp = os.path.join(self.temp_path, partial_path)
        book_avif = os.path.join(self.avif_path, partial_path)
        file_name = os.path.basename(partial_path)

        if book_is_starved(starved, measured):
            self._repackage_book(book_temp, out_rel, partial_path, file_name,
                                 starved, measured)
            shutil.rmtree(book_temp, ignore_errors=True)
            return

        os.makedirs(os.path.dirname(book_avif) or self.avif_path, exist_ok=True)
        self._convert_pages(book_temp, book_avif, file_name, lane_folder)
        if lane_folder:
            open(os.path.join(lane_folder, gpulane.READY), "w").close()
            return
        self.package_pages(item)

    def package_pages(self, item: tuple):
        """The comic this is about, named for every error recorded meanwhile -
        a page of it that fails in RAM, or in the convert-images it starts."""
        with self._about(item[0]):
            return self._package_pages(item)

    def _package_pages(self, item: tuple) -> None:
        """A converted book: drop its empty pages, say which are missing,
        zip, free."""
        record = item[0]
        partial_path, _, out_rel = record.partition(UNIT)
        book_temp = os.path.join(self.temp_path, partial_path)
        book_avif = os.path.join(self.avif_path, partial_path)
        file_name = os.path.basename(partial_path)
        shutil.rmtree(book_temp, ignore_errors=True)
        # A book the lane gives back after a Ctrl+C may be missing pages the
        # card never got to; it is not packaged as if it were whole.
        if safety.abort_requested():
            shutil.rmtree(book_avif, ignore_errors=True)
            return

        # The empty pages, counted as they are dropped: this is the only place
        # the run learns of them, because the converter counted such a page as
        # converted - it wrote a file and exited 0 - and only its size
        # afterwards says it holds nothing.
        empty = 0
        for parent, _dirs, names in os.walk(book_avif):
            for name in names:
                if not name.endswith(".avif"):
                    continue
                path = os.path.join(parent, name)
                try:
                    if os.path.getsize(path) < MIN_PAGE_SIZE:
                        os.remove(path)
                        empty += 1
                except OSError:
                    pass
        if empty:
            self.counters.bump("pagesEmpty", empty)

        # The pages the lane could not encode at all - the card left them to
        # the CPU, and the CPU wrote nothing - are missing from the book: an
        # error, each one's encode already replayed and kept on its own.
        missing = (self.lane.missing.pop(item[3], 0)
                   if self.lane is not None and len(item) > 3 and item[3]
                   else 0)
        if missing:
            self.counters.bump("pagesMissing", missing)
            runlog.error("%d page(s) of %s could not be encoded and are "
                         "missing from it" % (missing, file_name),
                         kind="output-check",
                         files=[os.path.join(self.in_path, partial_path)])

        if not _files_matching(book_avif, ("avif",)):
            self.counters.note("Skip (nothing converted): %s" % file_name,
                               failed=True)
            shutil.rmtree(book_avif, ignore_errors=True)
            return

        if package_book(book_avif, self.out_path, out_rel, partial_path,
                        self.avif_path, self.counters):
            self.counters.bump("packaged")

        # The pages are inside the archive on disk now: this book's RAM back
        # before the next one is unpacked into it.
        shutil.rmtree(book_avif, ignore_errors=True)

    def _repackage_book(self, book_temp: str, out_rel: str, partial_path: str,
                        file_name: str, starved: int, measured: int) -> None:
        """A book past the starved share, into a .cbz with its OWN pages.

        The same archive every other book gets - its pages under their own
        names, stored rather than deflated, stamped with the archive it came from - so a library converted
        by this command is one shelf of .cbz whether or not a given book was
        worth re-encoding. What differs is only that the pages inside it are the
        ones that came out of the original.
        """
        self.counters.note(
            "Repackaged unconverted (%d of %d page(s) already starved): %s"
            % (starved, measured, file_name))
        if package_book(book_temp, self.out_path, out_rel, partial_path,
                        self.temp_path, self.counters):
            self.counters.bump("packaged")
            self.counters.bump("repackaged")

    def _convert_pages(self, book_temp: str, book_avif: str,
                       file_name: str, lane_folder: str = "") -> None:
        """This book's pages, converted by convert-images as its own process -
        with its output kept rather than shown, because a few dozen books' worth
        of its progress would bury this run's own. Replayed when the conversion
        actually fails. With a <lane_folder>, the pages the GPU can take are
        only made ready there."""
        # Named here rather than inherited: this runs in a worker, which a
        # forkserver started with the environment of its own start - before
        # the run set the stats file, when PDFs were vetted first.
        environment = dict(os.environ, imageStatsFile=self.stats_file)
        if lane_folder:
            environment[LANE_VARIABLE] = lane_folder
        argv, env = commands.command_line(
                "convert-images",
                # -a: every page or none. That command's own default leaves a
                # starved image alone, which is the right answer for a folder of
                # loose pictures and the wrong one inside a book - the decision
                # was already taken above, for the WHOLE book, and a book that
                # got this far is one being converted.
                ["-c", "-a", "-u", self.chroma, "-j", self.pages_per_book,
                 "-m", self.max_res, "-l", self.quality, "-s", self.speed_preset,
                 "-f", self.fuzz, book_temp, book_avif],
                script_dir=self.script_dir, env=environment)
        toolcapture.run(argv, file_name, tool="convert-images", env=env,
                        warning="conversion failed for %s" % file_name)

    def vet_pdf(self, relative: str) -> None:
        """Whether one PDF is a comic, with the numbers the verdict was reached
        on - because "this PDF is not a comic" is a claim the user has to be able
        to check: 2 full-page images in 80 pages is a magazine, 23 in 24 is a
        comic with a credits page."""
        comic, pages, good, dpi = comicpdf.comic_pdf_verdict(
            os.path.join(self.in_path, relative), str(self.max_res))
        with open(os.path.join(self.counters.dir, "pdfList.lock"), "w") as lock, \
                runlog.take_lock(lock):
            share = "%d of %d page(s) hold one full-page image" % (good, pages)
            if comic:
                with open(self.input_list, "a") as handle:
                    handle.write(relative + "\0")
                runlog.say("Comic: %s (%s, rendering at %d dpi)\n"
                           % (relative, share, dpi))
            elif pages <= 0:
                # No page count at all - a damaged file, or the poppler tools
                # missing, which was said above - so there is no share to give.
                sys.stderr.write("Not a comic, skipped: %s (its pages could not "
                                 "be read)\n" % relative)
            else:
                runlog.say("Not a comic, skipped: %s (%s)\n"
                           % (relative, share))
            sys.stderr.flush()
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
            with errorlog.work_item(item):
                getattr(state, method)(item)
        return

    workerpool.run(items, jobs, _in_worker, lambda item: (state, method, item))


class _Reader:
    """The input's one reader: every archive of the run read whole into
    <read_path>, in the order given, back to back, up to READ_AHEAD_BYTES
    ahead of what :meth:`release` has given back. :meth:`take` hands over
    one archive's copy - ``(path, size)`` - once it is read; the input itself
    and 0 when there was nowhere to read it to, the copy failed, or the run is
    stopping, which leaves the unpacker to read it and say what is wrong."""

    def __init__(self, sources: list, read_path: str) -> None:
        self.read_path = read_path
        self.room = threading.Condition()
        self.held = 0
        self.stopping = False
        self.copies: list[Future] = [Future() for _ in sources]
        self.thread = threading.Thread(target=self._read, args=(sources,),
                                       name="input-reader", daemon=True)
        self.thread.start()

    def take(self, index: int) -> tuple:
        return self.copies[index].result()

    def release(self, size: int) -> None:
        with self.room:
            self.held -= size
            self.room.notify_all()

    def stop(self) -> None:
        with self.room:
            self.stopping = True
            self.room.notify_all()
        self.thread.join()

    def _read(self, sources: list) -> None:
        for index, source in enumerate(sources):
            with self.room:
                # One archive is always let through, however large: the
                # budget only keeps the reader from running further ahead.
                while (self.held > 0 and self.held >= READ_AHEAD_BYTES
                       and not self.stopping
                       and not safety.abort_requested()):
                    self.room.wait(0.5)
                halted = self.stopping or safety.abort_requested()
            copy = (source, 0) if halted or not self.read_path \
                else self._copy(source)
            with self.room:
                self.held += copy[1]
            self.copies[index].set_result(copy)

    def _copy(self, source: str) -> tuple:
        folder = tempfile.mkdtemp(dir=self.read_path)
        staged = os.path.join(folder, os.path.basename(source))
        errorlog.note_origin(folder, source)
        try:
            shutil.copyfile(source, staged)
            return staged, os.path.getsize(staged)
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            return source, 0


def _prepared_books(state: Run, book_list: list):
    """The queue's producer: every book that still needs a worker, prepared,
    in the order they were found.

    With a lane, a book to be converted gets a lane folder, and the next book
    is unpacked as soon as either of the lane's sides is short of work and
    there is RAM to hold it. Which side asked does not pick the book: nearly
    every page goes to the card, and the CPU's encoders take the card's pages
    when they have none of their own.

    PREPARE_BOOKS are prepared at once and handed over in the order they were
    found, so the next ones go on unpacking while the queue has not yet asked
    for them. Only the oldest is waited for when the lane has no room - the
    others start once it has. Their archives are read by the one reader, which
    goes on reading while they unpack."""
    ordered = collections.deque(enumerate(book_list))
    preparing: collections.deque = collections.deque()
    reader = _Reader([os.path.join(state.in_path, record.partition(UNIT)[0])
                      for record in book_list], state.read_path)

    def prepare(index: int, record: str):
        staged, size = reader.take(index)
        try:
            if safety.abort_requested():
                return None
            return state.prepare_book(record, staged)
        finally:
            reader.release(size)

    pool = ThreadPoolExecutor(PREPARE_BOOKS, thread_name_prefix="prepare")
    try:
        while ordered or preparing:
            while ordered and len(preparing) < PREPARE_BOOKS:
                if state.lane is not None:
                    if not preparing:
                        state.lane.wait_for_room()
                    elif not state.lane.has_room():
                        break
                if safety.abort_requested():
                    return
                preparing.append(pool.submit(prepare, *ordered.popleft()))
            prepared = preparing.popleft().result()
            if prepared is None or safety.abort_requested():
                continue
            (record, starved, measured), size = prepared
            folder = ""
            if state.lane is not None and not book_is_starved(starved,
                                                              measured):
                folder = state.lane.folder()
                state.lane.expect(folder, (record, starved, measured, folder))
            yield (record, starved, measured, folder), size
    finally:
        reader.stop()
        pool.shutdown(wait=True, cancel_futures=True)


def _megapixels_per_second(pixels: int, busy: float) -> str:
    """", 85.2 MP/s": a side's pixels over the time it was working, or
    nothing for a side that was never timed."""
    return ", %.1f MP/s" % (pixels / 1e6 / busy) if busy > 0 else ""


def _page_stats(stats_file: str) -> tuple:
    """The per-book page counts, summed by column.

    One line per book, written by the converter, because its own report went into
    a per-book log this script threw away. An empty file sums to zeroes rather
    than to nothing, so a run in which every book was already on disk still lands
    on numbers.
    """
    totals = [0] * 8
    books = 0
    try:
        with open(stats_file) as handle:
            for line in handle:
                if not line.strip():
                    continue
                books += 1
                for index, field in enumerate(line.split()[:8]):
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
    runlog.say("    %-26s %d/%d (%s%%)\n"
               % (label + ":", count, total, "%.1f" % (100.0 * count / total)))


def footer(state: Run) -> None:
    """The closing report, named above every point the run can be cut short - so
    a Ctrl+C still reports the books and pages it got through."""
    counters = state.counters
    # Every figure below is produced by the conversion phase, so a run stopped
    # before that phase began has only the safety recap to give, and says so by
    # printing nothing rather than a page of zeroes about work that never began.
    if state.phase_start is not None:
        # A book an earlier run already packaged counts as packaged - it is
        # what lets a resumed run of a finished library refuse nothing - but it
        # took no time here, so it is kept out of the books this run converted
        # and out of the time per book.
        existing = counters.read("existing")
        packaged = counters.read("packaged") - existing
        end = state.phase_end if state.phase_end is not None else time.time()
        runtime = end - state.phase_start
        runlog.say("\n")
        runlog.say("Converted and packaged %d of %d book(s) in %.2f seconds\n"
                   % (packaged, state.total, runtime))
        if packaged > 0:
            runlog.say("%.2f seconds per book\n" % (runtime / packaged))
        repackaged = counters.read("repackaged")
        if repackaged > 0:
            runlog.say("%d of those were repackaged with their own pages: more "
                       "than %d%% of each was\n"
                       % (repackaged, STARVED_PAGE_PERCENT))
            runlog.say("already starved, so re-encoding could only have cost "
                       "them.\n")
        lane = state.lane
        if lane is not None and lane.calls:
            share = (" (%.0f%% of the run)" % (100.0 * lane.busy / runtime)
                     if runtime > 0 else "")
            runlog.say("GPU: %d page(s), %.0f MP in %d call(s), busy %.1f "
                       "seconds%s%s\n" % (lane.pages, lane.pixels / 1e6,
                                           lane.calls, lane.busy, share,
                                           _megapixels_per_second(
                                               lane.pixels, lane.busy)))
        if lane is not None and lane.cpu_pages:
            runlog.say("CPU: %d page(s), %.0f MP, %d of them taken from the "
                       "GPU's queue, busy %.1f seconds%s\n"
                       % (lane.cpu_pages, lane.cpu_pixels / 1e6,
                          lane.stolen + lane.fallbacks, lane.cpu_busy,
                          _megapixels_per_second(lane.cpu_pixels,
                                                 lane.cpu_busy)))
        # Last, so the "of those" above cannot be read as being about these.
        if existing > 0:
            runlog.say("%d already packaged by an earlier run, skipped\n"
                       % existing)

        # A book is a coarse unit to judge a run by - collections hold 20-page
        # floppies and 300-page omnibuses in the same folder - so seconds per PAGE
        # is the number comparable between runs.
        totals, books = _page_stats(state.stats_file)
        page_total = totals[0]
        if page_total > 0:
            runlog.say("\n")
            runlog.say("%d page(s) in %d converted book(s), %.3f seconds per "
                       "page\n" % (page_total, books, runtime / page_total))
            # The six below partition the page total - the converter puts every
            # page in exactly one of them - so what is printed adds up to 100%.
            # (The seventh column, starved, is never set here: -a converts
            # every page of a book that is converted at all.)
            _print_page_stat("converted", totals[1], page_total)
            _print_page_stat("trimmed", totals[2], page_total)
            _print_page_stat("blank, dropped", totals[3], page_total)
            _print_page_stat("already done, skipped", totals[4], page_total)
            _print_page_stat("not found, failed", totals[5], page_total)
            _print_page_stat("failed, missing", totals[7], page_total)
            # Not categories of their own but subsets of the first two, hence
            # "of those": a page counted as converted when it was sorted onto
            # the GPU lane, which then wrote it empty - or could not write it.
            _print_page_stat("of those empty, dropped",
                             counters.read("pagesEmpty"), page_total)
            _print_page_stat("of those failed, missing",
                             counters.read("pagesMissing"), page_total)

        if os.path.isdir(state.out_path):
            for parent, _dirs, _names in os.walk(state.out_path, topdown=False):
                if parent != state.out_path:
                    try:
                        os.rmdir(parent)
                    except OSError:
                        pass
            runlog.say("\n")
            written = len(_files_matching(state.out_path, ("cbz",)))
            # With books from an earlier run among them, the count is what the
            # folder holds now rather than what this run wrote.
            if existing > 0:
                runlog.say("%s now holds %d cbz file(s)\n"
                           % (state.out_path, written))
            else:
                runlog.say("Wrote %d cbz file(s) to %s\n"
                           % (written, state.out_path))

    # The recap goes to stderr and everything above it to stdout, so the two are
    # only in the order they were written if this one is flushed first - a log
    # capturing both would otherwise show the recap ahead of the report.
    sys.stderr.flush()
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
        # what is there: "Nighthawk.cbz" and "Nighthawk.pdf" in one folder are
        # two books that both want to be called "Nighthawk.cbz". Overwriting
        # would drop one of them; giving the second a " (2)" marker would be
        # worse - the collective rules would then see two siblings sharing
        # "Nighthawk" and strip it from both, so a pair of books would come out
        # as "Nighthawk.cbz" and "(2).cbz".
        marker = os.path.join(marker_dir,
                              os.path.splitext(marker_name)[0] + ".cbz")
        with open(marker, "a", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write(archive_rel + "\0")

    # Quiet unless it fails: what it says is about the tree of markers, which
    # the user never sees - each book's own line says what it is named.
    argv, env = commands.command_line("clean-folder-structure", [name_root],
                                      script_dir=state.script_dir)
    toolcapture.run(argv, "the books' names", tool="clean-folder-structure",
                    env=env, warning="the books' names could not be cleaned")

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


def is_packaged(state: Run, record: str) -> bool:
    """Whether a named book is already in the output, by PROVENANCE first:
    every .cbz records the archive it was made from, which survives the
    collective naming being set-dependent. Then by name, for a .cbz written
    before provenance was recorded: no comment to go by, but a matching name
    is still a finished book."""
    partial_path, _, out_rel = record.partition(UNIT)
    return (partial_path in state.converted
            or os.path.exists(os.path.join(state.out_path, out_rel)))


def split_packaged(state: Run,
                   book_list: list) -> tuple[list[str], list[str]]:
    """The named books as ``(pending, packaged)``, settled in memory before the
    queue is fed - so the queue, its [n/total] count, the GPU lane and the
    pages-per-book split are all sized by the books that still need work, and
    a resumed run of a large library does not walk the finished ones through
    the workers one by one."""
    pending: list[str] = []
    packaged: list[str] = []
    for record in book_list:
        (packaged if is_packaged(state, record) else pending).append(record)
    return pending, packaged


def main(argv: list, program: str = "convert-comics",
         script_dir: str = "") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
        clioptions.settle_verbosity(result)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        clioptions.refuse_usage(declaration,
                                error.message)
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
        # Both RAM work trees and the counter dir go back to the tmpfs however
        # this run ends.
        ramscratch.run_exit_cleanup()


def _run(result, declaration, program: str, script_dir: str,
         counter_dir: str) -> int:
    if clioptions.args_out_of_range(len(result.positionals), 2, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1
    in_path = result.positionals[0].rstrip("/")
    out_path = result.positionals[1].rstrip("/")

    quality = result.values["quality"] or DEFAULT_QUALITY
    chroma = result.values["chroma"] or DEFAULT_CHROMA
    speed_preset = result.values["speedPreset"] or DEFAULT_SPEED_PRESET
    speed = int(speed_preset)
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
    held = containers_held(in_path)
    if "rar" in held:
        tools.append("unrar")
    if "7z" in held:
        tools.append("7z|7zz|7za")
    if tooldeps.require_tools(program, tools):
        return 1

    have_fdupes = _settle_fdupes()
    runlog.warn_uncounted_progress()

    # Both work directories live in the RAM base, so no intermediate file ever
    # hits the disk: temp_path holds each book's extracted pages, avif_path
    # receives its AVIF pages and is where the zipping happens.
    temp_path, temp_status = ramscratch.ram_scratch_dir("convertComics")
    avif_path, avif_status = ramscratch.ram_scratch_dir("convertComics.avif")
    read_path, read_status = ramscratch.ram_scratch_dir("convertComicsRead")
    if temp_status != 0 or avif_status != 0 or not temp_path or not avif_path:
        sys.stderr.write("\nError: no scratch directory could be made for this "
                         "run.\nNothing was changed.\n")
        return 1
    ramscratch.add_exit_cleanup([temp_path, avif_path])
    if read_status == 0 and read_path:
        ramscratch.add_exit_cleanup([read_path])
    else:
        read_path = ""

    safety.init_safety_log(os.path.join(counter_dir, "safetySkips.log"))
    skips = safety.RunSkipLog()
    safety.init_abort_flag(os.path.join(counter_dir, "abortRequested"))
    safety.trap_run_abort()

    counters = Counters(counter_dir)
    state = Run(
        in_path=in_path, out_path=out_path, script_dir=script_dir,
        temp_path=temp_path, avif_path=avif_path, counters=counters,
        quality=quality, chroma=chroma, speed_preset=speed_preset,
        pages_per_book=IMAGES_PER_BOOK, max_res=max_res, fuzz=fuzz,
        converted=set(), input_list=os.path.join(counter_dir, "inputs"),
        stats_file=os.path.join(counter_dir, "pageStats"),
        phase_start=None, phase_end=None, total=0, lane=None,
        read_path=read_path,
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
        toolcapture.watched(["fdupes", "-rdN", in_path],
                            skipped="duplicates were not removed",
                            stdout=subprocess.DEVNULL)
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
        runlog.say("PDFs\n====\n\n")
        # Asked once and said out loud: with the poppler tools absent every PDF
        # reads as "no full-page images" and is skipped as not a comic, which is
        # indistinguishable from a correct verdict on a folder of magazines.
        missing = _missing_pdf_tools()
        if missing:
            runlog.say("WARNING: %s not found - PDFs cannot be inspected and "
                       "are all skipped.\n\n" % missing, sys.stderr)
        for name in ("pdf.comic", "pdf.other"):
            with open(os.path.join(counter_dir, name), "w") as handle:
                handle.write("0")
        # At the page-conversion width rather than the book width: reading a
        # PDF's page and image tables is a short burst of parsing, not a book's
        # worth of work, and none of it is held in RAM afterwards.
        _run_pool(state, "vet_pdf", pdfs,
                  max(1, int(runlog.cpu_count()
                             / threads_per_conversion("avif", speed))))
        safety.exit_if_aborted()
        runlog.say("\n")
        # A single PDF's verdict is its own line above; the tally only adds
        # something once there are several.
        if len(pdfs) > 1:
            comic_count = counters.read("pdf.comic")
            runlog.say("%d of %d PDF(s) taken as comic book(s)\n\n"
                       % (comic_count, len(pdfs)))

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

    runlog.say("Naming\n======\n\n")

    # What is already converted, read out of the archives themselves rather than
    # guessed from their names. Only .cbz written by a version that records it are
    # listed; older ones fall back to the name check in prepare_book.
    if os.path.isdir(out_path):
        for existing in _files_matching(out_path, ("cbz",)):
            done = subprocess.run(["unzip", "-z", "-qq", "--", existing],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
            first = done.stdout.decode("utf-8", "surrogateescape").split("\n")
            if first and first[0].strip():
                state.converted.add(first[0].strip())

    named = _naming_pass(state, books_found)
    book_list, packaged = split_packaged(state, named)
    cleaner = sum(1 for record in named
                  if os.path.splitext(record.partition(UNIT)[0])[0]
                  != record.partition(UNIT)[2][:-len(".cbz")])
    runlog.say("%d book(s) named, %d of them more cleanly than their "
               "archive\n" % (len(named), cleaner))

    runlog.say("\nConverting\n==========\n\n")
    # The footer reports against every book found; the progress count only
    # against the ones this run works on.
    state.total = len(book_list) + len(packaged)
    counters.total = len(book_list)
    for name in ("current", "pagesFound", "pagesEmpty", "pagesMissing"):
        with open(os.path.join(counter_dir, name), "w") as handle:
            handle.write("0")
    for name in ("packaged", "existing"):
        with open(os.path.join(counter_dir, name), "w") as handle:
            handle.write(str(len(packaged)))
    for record in packaged:
        runlog.say("Skip (exists): %s\n"
                   % os.path.basename(record.partition(UNIT)[2]))
    if packaged and book_list:
        runlog.say("\n")
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

    state.pages_per_book = pages_per_book(runlog.cpu_count(), speed,
                                          len(book_list))
    # The card, when this host has one: owned by this process for the whole
    # run, and fed the pages of every book, since a call per book would pay
    # av1gpu's start-up per book. The book workers then only sort the pages,
    # and the lane encodes them - the card's and the CPU's at once, across
    # books. -l and -s are the CPU's; what av1gpu has to be asked for to match
    # them on comic pages was measured (COMIC_CALIBRATION).
    # Nothing left to convert starts no card: av1gpu's start-up would be all
    # a resumed run of a finished library spent.
    encoder = av1gpu.binary(log) if book_list else None
    if encoder:
        lane_root, lane_status = ramscratch.ram_scratch_dir("convertComics.gpu")
        if lane_status == 0 and lane_root:
            ramscratch.add_exit_cleanup([lane_root])
            preset, gpu_quality = COMIC_CALIBRATION.gpu_settings(
                speed, int(quality))
            state.lane = gpulane.Lane(
                encoder, lane_root, gpu_quality, preset, chroma,
                state.package_pages, page_slots(runlog.cpu_count(), speed),
                log)
    workers = book_workers(runlog.cpu_count(), speed)
    if state.lane is not None:
        workers, state.pages_per_book = LANE_BOOKS, LANE_PAGES_PER_BOOK
    # One prepared book waiting per worker: enough that a freed worker starts
    # its next book at once, and no more, because every book waiting is a book
    # held in RAM.
    # The first worker starts on the first book prepared, not once the buffer
    # is full: on a cold drive four books took 15 s to read and unpack, and the
    # card waited for all of them.
    try:
        dynamicqueue.run(_prepared_books(state, book_list), workers, _in_worker,
                         lambda item: (state, "finish_book", item),
                         buffer_factor=BOOKS_BUFFERED_PER_WORKER, log=log,
                         start_after=1)
    finally:
        if state.lane is not None:
            state.lane.close()
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
            "or came out empty)")

    safety.print_run_footer()
    return workerpool.exit_status()


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
    settings.load()
    errorlog.start_run(argv)
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
