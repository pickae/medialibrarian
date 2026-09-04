"""The census itself: what content-census does once its command line is read.

Two things here are not visible from the code and decide its shape. One WORKER
per path given, because that is one per disk, and the books alone go to a pool
as wide as the cores - the book row is the one whose cost is the cores rather
than the seek. And every worker is a separate PROCESS, so nothing it learns can
come back in a variable: rows, skips and counts go to files, which the parent
reads back in the order the paths were given.
"""

import multiprocessing
import os
import shutil
import signal
import sys

from medialib import commands
from medialib.lib import (
    census,
    contentcensus,
    enums,
    ffmpegselect,
    imagemagick,
    ramscratch,
    runlog,
    safety,
    tooldeps,
    workerpool,
)
from medialib.lib.runlog import log

CENSUS_TYPES = ("audio", "video", "books", "comics")


class Refusal(Exception):
    """A refusal that ends the run: the text is already what the user sees."""

    def __init__(self, text: str, status: int = 1):
        super().__init__(text)
        self.text = text
        self.status = status


def _capitalise(text: str) -> str:
    """bash's ``${var^}``: the first character upper-cased, the rest untouched."""
    return text[:1].upper() + text[1:]


def _absolute(path: str) -> str:
    """The path as ``cd -- "$path" && pwd`` prints it: physical, no symlinks."""
    return os.path.realpath(path)


# --- the libraries -------------------------------------------------------------

def resolve_input_paths(arguments):
    """Every path given, resolved and checked before anything is scanned - so a
    typo in the third of five folders is found now rather than after the first
    two have been read.

    Returns (paths, report_names). The same folder named twice is one library:
    censusing it twice would write the same report twice and say nothing new.
    """
    in_paths, report_names = [], []
    for argument in arguments:
        if not os.path.isdir(argument):
            raise Refusal('\nError: "%s" is not a folder.\n'
                          "Nothing was changed.\n" % argument)
        absolute = _absolute(argument)
        if absolute in in_paths:
            log('Ignoring "%s": that folder is already being censused'
                % argument)
            continue

        name = os.path.basename(absolute)
        if name in ("/", ""):
            name = "root"
        report_name = _capitalise(name)
        # Two libraries named the same would be given the same report names, and
        # the reports are the only thing carrying a library's identity into the
        # cube. Refused up front, naming both, because the two ways it could go
        # wrong are both silent: an overwrite under -o, or a merge into one
        # library in the backend afterwards.
        if report_name in report_names:
            first = in_paths[report_names.index(report_name)]
            raise Refusal(
                '\nError: two of the folders given are both named "%s":\n'
                "  %s\n  %s\n"
                "Their reports would be named the same, and a library is known "
                "by its\nreport name from here on. Rename one, or census them "
                "in separate runs\ninto separate folders. Nothing was "
                "changed.\n" % (name, first, absolute))
        in_paths.append(absolute)
        report_names.append(report_name)

    if not in_paths:
        raise Refusal("\nError: no folder to census.\nNothing was changed.\n")
    return in_paths, report_names


def _folders_at_depth(root: str, depth: int):
    """``find "$root" -mindepth N -maxdepth N -type d -print0 | sort -z``."""
    level = [root]
    for _ in range(depth):
        below: list[str] = []
        for parent in level:
            try:
                with os.scandir(parent) as entries:
                    below.extend(entry.path for entry in entries
                                 if entry.is_dir(follow_symlinks=False))
            except OSError:
                continue
        level = below
    return sorted(level, key=os.fsencode)


def resolve_libraries(in_paths, report_names, depth):
    """Which folders are the libraries, what each is reported as, and which path
    given it was found under - the three parallel lists the run walks.

    The report name is the given folder's name followed by each step down to it,
    every one capitalised, so the whole reads as one camel-case word after the
    type ("videoFilmsMarvel.csv").
    """
    lib_paths, lib_names, lib_roots = [], [], []
    seen_paths = set()
    for root_index, root in enumerate(in_paths):
        root_name = report_names[root_index]
        if depth == 0:
            lib_paths.append(root)
            lib_names.append(root_name)
            lib_roots.append(root_index)
            seen_paths.add(root)
            continue

        found = 0
        for candidate in _folders_at_depth(root, depth):
            # Nested paths given on one command line can reach the same folder
            # twice. One folder is one library, exactly as the same path named
            # twice is.
            if candidate in seen_paths:
                log('Ignoring "%s": that folder is already being censused'
                    % candidate)
                continue
            seen_paths.add(candidate)
            relative = candidate[len(root):].lstrip("/")
            name = root_name + "".join(
                _capitalise(segment) for segment in relative.split("/"))
            lib_paths.append(candidate)
            lib_names.append(name)
            lib_roots.append(root_index)
            found += 1
        if found == 0:
            log('Nothing %d level(s) below "%s" - no library there, nothing '
                "censused from it" % (depth, root))

    if not lib_paths:
        raise Refusal(
            "\nError: -d %d was asked for, and none of the %d folder(s) given "
            "holds a\nfolder that many levels down, so there is no library to "
            "census.\nNothing was changed.\n" % (depth, len(in_paths)))

    # The same refusal one level further in: two libraries found under different
    # shelves can still work out to one report name ("Films" holding "ExtraX"
    # against "FilmsExtra" holding "X"), and a collision is just as silent here.
    seen_names = {}
    for index, name in enumerate(lib_names):
        if name in seen_names:
            raise Refusal(
                '\nError: two of the libraries found would both be reported as '
                '"%s":\n  %s\n  %s\n'
                "Their reports would be named the same, and a library is known "
                "by its\nreport name from here on. Rename one, or census them "
                "in separate runs\ninto separate folders. Nothing was "
                "changed.\n"
                % (name, lib_paths[seen_names[name]], lib_paths[index]))
        seen_names[name] = index
    return lib_paths, lib_names, lib_roots


# --- where the reports go ------------------------------------------------------

def resolve_out_dirs(lib_paths, out_dir):
    """One report folder per library: the shared -o one, or each library's own.

    Returns (out_dirs, created_root) - the topmost level -o had to create, which
    the run gives back if it writes no report into it.
    """
    if not out_dir:
        for path in lib_paths:
            if not os.access(path, os.W_OK):
                raise Refusal(
                    '\nCannot write the reports into "%s": no write '
                    "permission.\nGive a writable folder with -o instead. "
                    "Nothing was changed.\n" % path)
        return list(lib_paths), ""

    created_root = ""
    if not os.path.exists(out_dir):
        # The topmost level that is missing, remembered before it is made, so the
        # run can take back exactly what it created and nothing that was there.
        probe = out_dir
        while not os.path.isdir(probe):
            created_root = probe
            parent = os.path.dirname(probe) or "."
            if parent == probe:
                break
            probe = parent
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError:
            raise Refusal(
                '\nError: the report folder "%s" is not there and could not be '
                "made.\nGive a path whose parent exists and is writable. "
                "Nothing was changed.\n" % out_dir) from None
        log('Created the report folder "%s"' % out_dir)

    if not os.path.isdir(out_dir):
        raise Refusal('\nError: the report folder "%s" is not a folder.\n'
                      "Nothing was changed.\n" % out_dir)
    if not os.access(out_dir, os.W_OK):
        raise Refusal('\nCannot write the reports into "%s": no write '
                      "permission.\nGive a writable folder with -o instead. "
                      "Nothing was changed.\n" % out_dir)
    resolved = _absolute(out_dir)
    return [resolved] * len(lib_paths), created_root


# --- the file lists ------------------------------------------------------------

def collect_files(lib_paths, extensions):
    """Every file this census can read, all libraries at once.

    Collected up front rather than streamed, for the two things that need the
    whole list before the first file is probed: the "[n/total]" progress lines,
    and the tool preflight, which asks only for the tools the types actually
    present in THESE trees need.

    One flat list with the per-library ranges beside it, sorted the way
    ``find | sort -z`` sorts, so the run is deterministic and the reports diff
    against the last one.
    """
    wanted = {extension.lower() for extension in extensions}
    files, totals, starts = [], [], []
    for path in lib_paths:
        starts.append(len(files))
        found = []
        for parent, _dirs, names in os.walk(path):
            for name in names:
                candidate = os.path.join(parent, name)
                if not os.path.isfile(candidate):
                    continue
                extension = os.path.splitext(name)[1].lstrip(".").lower()
                if extension in wanted:
                    found.append(candidate)
        found.sort(key=os.fsencode)
        files.extend(found)
        totals.append(len(found))
        if not found:
            log('Nothing this census can read under "%s" - no report for it'
                % path)
    return files, totals, starts


# --- the tools THIS tree needs -------------------------------------------------
# Asked for per content type actually present - unrar only when the input holds a
# .cbr: a library of audiobooks should not have to have mediainfo, poppler and
# Calibre installed to be censused. The split between "refuse without it" and
# "say it is missing and go on" is what its absence COSTS - a whole report, or
# one column.

def seen_extensions(files):
    """The extensions the trees hold, in the order the file list meets them."""
    seen = []
    for path in files:
        extension = enums.lower_extension_of(path)
        if extension and extension not in seen:
            seen.append(extension)
    return seen


def needed_tools(seen, run_bi):
    """The tools whose absence would cost a whole content type, each named once -
    a tool two types both need is not reported as missing twice."""
    def any_seen(*extensions):
        return any(extension in seen for extension in extensions)

    tools = []

    def add(*specs):
        for spec in specs:
            if spec not in tools:
                tools.append(spec)

    if any_seen(*enums.AUDIO_EXTENSIONS):
        add("ffprobe")
    if any_seen(*os.environ.get(
            "censusVideoExtensions",
            contentcensus.census_video_extensions()).split()):
        add("ffprobe")
    if any_seen("cbz"):
        add("unzip", imagemagick.IDENTIFY_SPEC)
    if any_seen("cbr"):
        add("unrar", imagemagick.IDENTIFY_SPEC)
    if any_seen("cb7"):
        add("7z|7zz|7za", imagemagick.IDENTIFY_SPEC)
    # duckdb is asked for HERE and not when the cubes are built, which is the
    # whole point of a preflight: -b's work happens after the census, so a
    # machine without DuckDB would otherwise walk an entire library and only then
    # discover it cannot do the half it was asked for.
    if run_bi:
        add("duckdb")
    return tools


def _books_needing_calibre():
    """The book formats Calibre is asked for: everything that is neither already
    text nor a PDF, because a PDF is read by pdftotext instead - so its word
    count survives a machine with no Calibre on it."""
    comic_pdf = os.environ.get(
        "comicPdfExtensions", " ".join(enums.COMIC_PDF_EXTENSIONS)).split()
    return [extension for extension in enums.BOOK_INPUT_EXTENSIONS
            if extension != "txt" and extension not in comic_pdf]


def warn_optional_tools(seen):
    """The optional tools, announced once here rather than discovered per file.
    Each says which column it costs, so an empty cell in a report is never a
    surprise."""
    def any_seen(extensions):
        return any(extension in seen for extension in extensions)

    video = os.environ.get("censusVideoExtensions",
                           contentcensus.census_video_extensions()).split()
    comic_pdf = os.environ.get(
        "comicPdfExtensions", " ".join(enums.COMIC_PDF_EXTENSIONS)).split()

    if not os.environ.get("CENSUS_HAVE_MEDIAINFO", "") and any_seen(video):
        log("WARNING: mediainfo is not installed, so the video report's "
            "dynamicRange column is")
        log("         read from ffprobe instead: SDR, HLG, HDR10 and Dolby "
            "Vision are still")
        log("         told apart, but an HDR10+ film reads as the HDR10 it "
            "also is.")

    calibre_books = _books_needing_calibre()
    if (not os.environ.get("CENSUS_HAVE_EBOOK_CONVERT", "")
            and any_seen(calibre_books)):
        log("WARNING: ebook-convert is not installed (it ships in Calibre), so "
            "the books")
        log("         report's word and character counts will be empty for %s."
            % enums.extension_list(calibre_books))
        log("         Every book is still listed with its path and size.")

    if not os.environ.get("CENSUS_HAVE_PDFTOTEXT", "") and any_seen(comic_pdf):
        if os.environ.get("CENSUS_HAVE_EBOOK_CONVERT", ""):
            log("WARNING: pdftotext is not installed (it ships in "
                "poppler-utils), so every PDF")
            log("         is read by Calibre instead. That is the same word "
                "count by a much")
            log("         slower route - expect this run to take considerably "
                "longer.")
        else:
            log("WARNING: neither pdftotext (poppler-utils) nor ebook-convert "
                "(Calibre) is")
            log("         installed, so the books report's word and character "
                "counts will be")
            log("         empty for PDFs. Every one is still listed with its "
                "path and size.")

    if not os.environ.get("CENSUS_HAVE_POPPLER", "") and any_seen(comic_pdf):
        log("WARNING: poppler-utils is not installed, so no PDF can be "
            "examined: every one")
        log("         of them is counted as a book (never as a comic) and "
            "without a page count.")


def census_subject(in_paths, run_bi, program):
    """What the tool refusal says cannot run."""
    subject = "%s on %s" % (program,
                            "these folders" if len(in_paths) > 1
                            else "this folder")
    return subject + " with -b" if run_bi else subject


# --- the census ----------------------------------------------------------------
# Ctrl+C stops the walk but still writes the reports for the files already
# probed, rather than throwing away an hour of reading a slow disk. A flag, not a
# kill: the handler cannot interrupt the probe in flight, so it asks the loop to
# stop after the current file, and with several libraries it stops the run after
# the current one.

class Run:
    """One census: the settled world the workers share, and the files each
    worker writes back into it.

    A class rather than a pile of arguments because a worker is a separate
    process that inherits this whole state at fork and then may only speak back
    through files - the same shape the shell has, where a background subshell
    can read every variable and change none of them.
    """

    def __init__(self, lib_paths, lib_names, lib_roots, lib_out_dirs,
                 files, totals, starts, in_paths, scratch, separator,
                 extension, book_jobs, parallel, total):
        self.lib_paths = lib_paths
        self.lib_names = lib_names
        self.lib_roots = lib_roots
        self.lib_out_dirs = lib_out_dirs
        self.files = files
        self.totals = totals
        self.starts = starts
        self.in_paths = in_paths
        self.scratch = scratch
        self.results = os.path.join(scratch, "results")
        self.counter = os.path.join(scratch, "counter")
        self.console_lock = os.path.join(scratch, "console.lock")
        self.separator = separator
        self.extension = extension
        self.book_jobs = book_jobs
        self.parallel = parallel
        self.total = total
        self.index = 0                      # the serial run's own progress count

    # --- progress ---------------------------------------------------------
    def progress(self, library, label):
        """The one progress line a file gets.

        Under one worker it is exactly the line this script has always printed.
        Under several the counter has to be shared, and the line says which
        library it belongs to - the workers' lines interleave, and
        "[812/40000] 03 - Chapter.opus" on its own would say nothing about which
        of four libraries is 812 files in.
        """
        if not self.parallel:
            self.index += 1
            sys.stderr.write("[%d/%d] %s\n" % (self.index, self.total, label))
            return
        with open(self.console_lock, "r+b") as handle, runlog.take_lock(handle):
            try:
                with open(self.counter) as counter:
                    count = int(counter.read() or "0")
            except (OSError, ValueError):
                count = 0
            count += 1
            with open(self.counter, "w") as counter:
                counter.write(str(count))
            sys.stderr.write("%s%s: %s\n" % (
                runlog.counted_prefix(count, self.total), library, label))

    # --- one book, in a worker of the pool --------------------------------
    def book_one(self, file_index, path, shared_scratch, skipped):
        """One book's row. Runs in its own process - the pool's whole reason for
        being - and so gets a PRIVATE scratch: the row builder writes the
        converted text to a fixed name inside CENSUS_SCRATCH, which two
        concurrent book workers must not share.

        Its row and its skip reason go to per-worker files keyed by the file's
        index in the run's sorted list, which merge_book_results folds back in
        that order: the file list is sorted for a reason, and a books report
        whose rows landed in the order the workers finished would not diff
        against the last run.
        """
        # Dispatched but not yet started, and the run was stopped in the
        # meantime: this file was never read, so it gets neither a row nor a
        # skip.
        if safety.abort_requested():
            return
        try:
            work = os.path.join(shared_scratch, "bookwork.%d" % file_index)
            os.makedirs(work, exist_ok=True)
        except OSError:
            log('  WARNING: skipping "%s": no scratch could be made for its '
                "text" % path)
            _write_bytes(os.path.join(shared_scratch,
                                      "bookskip.%d" % file_index),
                         _skip_entry(path, "no scratch could be made for its "
                                           "text"))
            return

        os.environ["CENSUS_SCRATCH"] = work
        row, reason = contentcensus.census_row("books", path, self.separator)
        if row is not None:
            with open(os.path.join(shared_scratch, "bookrow.%d" % file_index),
                      "w") as handle:
                handle.write(row + "\n")
            # The sanitised flag is raised inside this process, where a variable
            # would die with it, so it is handed back as a marker file the way
            # the row itself is.
            if getattr(row, "sanitised", False):
                open(os.path.join(shared_scratch,
                                  "bookSanitised.%d" % file_index), "w").close()
        else:
            log('  WARNING: skipping "%s": %s' % (path, reason))
            _write_bytes(os.path.join(shared_scratch,
                                      "bookskip.%d" % file_index),
                         _skip_entry(path, reason))
        shutil.rmtree(work, ignore_errors=True)

    def merge_book_results(self, scratch, start, count, rows_path, skipped):
        """Fold the pool's per-worker files back into this library's report and
        skip log, walking the library's file range in order - so the books
        report comes out the way the sorted file list says rather than the order
        the workers finished. Returns whether any row had to be sanitised."""
        sanitised = False
        for file_index in range(start, start + count):
            row_file = os.path.join(scratch, "bookrow.%d" % file_index)
            if os.path.exists(row_file):
                with open(row_file) as handle, open(rows_path, "a") as rows:
                    rows.write(handle.read())
                os.remove(row_file)
            skip_file = os.path.join(scratch, "bookskip.%d" % file_index)
            if os.path.exists(skip_file):
                with open(skip_file, "rb") as handle:
                    _append_bytes(skipped, handle.read())
                os.remove(skip_file)
            marker = os.path.join(scratch, "bookSanitised.%d" % file_index)
            if os.path.exists(marker):
                sanitised = True
                os.remove(marker)
        return sanitised

    # --- one library ------------------------------------------------------
    def library(self, library_index, scratch, reports_path, skipped_path):
        """Probe every file of one library and write its reports.

        Everything but the books is probed here, one after the other; the books
        are handed to a pool at book_jobs width and their rows folded back in
        file order before the reports are assembled. Returns
        (censused, written, sanitised).
        """
        path = self.lib_paths[library_index]
        report_name = self.lib_names[library_index]
        out_dir = self.lib_out_dirs[library_index]
        count = self.totals[library_index]
        start = self.starts[library_index]
        if count == 0:
            return 0, 0, False

        rows = {content: os.path.join(scratch, content)
                for content in CENSUS_TYPES}
        for target in rows.values():
            open(target, "w").close()

        censused = 0
        sanitised = False
        running: list[multiprocessing.Process] = []
        log('Censusing %d file(s) under "%s"' % (count, path))
        for file_index in range(start, start + count):
            if safety.abort_requested():
                log("Interrupted - stopping after the file(s) already read "
                    'under "%s"' % path)
                break
            file_path = self.files[file_index]
            label = file_path[len(path):].lstrip("/")
            self.progress(report_name, label)
            censused += 1

            content, _stats = contentcensus.census_classify(file_path)
            if not content:
                # cannot happen (the walk matched one of the lists), but a row
                # is never written for a file whose type cannot be named
                continue
            if content == "books":
                # The one row that is CPU-bound rather than seek-bound, so the
                # one the cores share. The progress line and the census count
                # were taken above, in this process, on purpose: the counter
                # stays one sequence whether or not the books run in parallel,
                # and the line keeps its place in the library's file order.
                worker = multiprocessing.Process(
                    target=self.book_one,
                    args=(file_index, file_path, scratch, skipped_path))
                worker.start()
                running.append(worker)
                if len(running) >= self.book_jobs:
                    running = workerpool.reap_one(running)
                continue

            row, reason = contentcensus.census_row(content, file_path,
                                                   self.separator)
            if row is not None:
                with open(rows[content], "a") as handle:
                    handle.write(row + "\n")
                if getattr(row, "sanitised", False):
                    sanitised = True
            else:
                log('  WARNING: skipping "%s": %s' % (file_path, reason))
                _append_bytes(skipped_path, _skip_entry(file_path, reason))

        # The books the pool is still holding, then their rows and skips folded
        # back in the library's file order - only then is the books file
        # complete, which is what the reports below read.
        for worker in running:
            worker.join()
        if self.merge_book_results(scratch, start, count, rows["books"],
                                   skipped_path):
            sanitised = True

        # --- this library's reports ---------------------------------------
        # Assembled in the scratch and moved into place at the end of each
        # library, so an interrupted or failed run never leaves a half-written
        # report next to a library. A report is only written for a type that got
        # at least one row: four files of which three hold nothing but a header
        # say less than one file does.
        written = 0
        for content in CENSUS_TYPES:
            with open(rows[content]) as handle:
                row_count = sum(1 for _ in handle)
            if row_count == 0:
                continue
            report = os.path.join(
                out_dir, "%s%s.%s" % (content, report_name, self.extension))
            if os.path.exists(report):
                log("Replacing the existing %s" % os.path.basename(report))
            staged = os.path.join(scratch, content + ".report")
            with open(staged, "w") as out:
                out.write(census.columns(content, self.separator) + "\n")
                with open(rows[content]) as handle:
                    shutil.copyfileobj(handle, out)
            shutil.move(staged, report)
            log('Wrote %d %s row(s) to "%s"' % (row_count, content, report))
            written += 1
            _append_bytes(reports_path, os.fsencode(report) + b"\0")
        return censused, written, sanitised

    # --- one worker's whole job -------------------------------------------
    def root(self, root_index):
        """Every library found under one path given, one after the other,
        because they share its disk.

        Its own CENSUS_SCRATCH, and not only for tidiness: the book conversions
        write their text to one fixed name inside it, so two workers sharing a
        scratch would count each other's books.
        """
        scratch = os.path.join(self.scratch, "worker%d" % root_index)
        os.makedirs(scratch, exist_ok=True)
        os.environ["CENSUS_SCRATCH"] = scratch

        reports_path = os.path.join(self.results, "%d.reports" % root_index)
        skipped_path = os.path.join(self.results, "%d.skipped" % root_index)
        open(reports_path, "wb").close()
        open(skipped_path, "wb").close()

        censused = written = 0
        sanitised = False
        for library_index, root in enumerate(self.lib_roots):
            if root != root_index:
                continue
            if safety.abort_requested():
                break
            one, wrote, dirty = self.library(library_index, scratch,
                                             reports_path, skipped_path)
            censused += one
            written += wrote
            sanitised = sanitised or dirty

        with open(os.path.join(self.results, "%d.counts" % root_index),
                  "w") as handle:
            handle.write("%d %d %d\n" % (censused, written, 1 if sanitised
                                         else 0))


def _skip_entry(path, reason):
    """One skip as the shell writes it: "<path>: <reason>" and a NUL, so a name
    holding a newline survives the round trip."""
    return os.fsencode("%s: %s" % (path, reason)) + b"\0"


def _write_bytes(path, payload):
    with open(path, "wb") as handle:
        handle.write(payload)


def _append_bytes(path, payload):
    with open(path, "ab") as handle:
        handle.write(payload)


# --- the whole run -------------------------------------------------------------

def _cleanup(scratch, created_out_root):
    """What an exit gives back.

    A report folder this run made and then never wrote a report into is given
    back, deepest level first and only while each is empty, so a run that says
    "nothing was changed" has changed nothing - not even a directory.
    """
    if scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    ramscratch.run_exit_cleanup()
    if not created_out_root:
        return
    for parent, dirs, files in os.walk(created_out_root, topdown=False):
        if not dirs and not files:
            try:
                os.rmdir(parent)
            except OSError:
                pass


def run(arguments, depth, out_dir, run_bi, separator, extension,
        script_dir, program):
    """Everything after the command line. Returns the process status."""
    in_paths, report_names = resolve_input_paths(arguments)
    lib_paths, lib_names, lib_roots = resolve_libraries(in_paths, report_names,
                                                        depth)

    lib_out_dirs, created_out_root = resolve_out_dirs(lib_paths, out_dir)
    scratch = ""
    try:
        os.environ["CENSUS_SEP"] = separator
        contentcensus.census_init()
        runlog.settle_flock()

        files, totals, starts = collect_files(
            lib_paths, os.environ["censusAllExtensions"].split())
        total = len(files)
        if total == 0:
            # It speaks about one folder, which is the only case it can say
            # anything useful about; several empty ones have each been named
            # above.
            readable = enums.extension_list(
                os.environ["censusAllExtensions"].split())
            if len(lib_paths) == 1:
                return safety.fail_no_relevant_input(
                    lib_paths[0],
                    "files this census can read (%s)" % readable)
            raise Refusal(
                "\nNothing to do: none of the %d libraries holds a file this "
                "census\ncan read (%s).\nNothing was changed.\n"
                % (len(lib_paths), readable))

        seen = seen_extensions(files)
        ffmpegselect.select_ffmpeg()
        ffmpegselect.report_ffmpeg_selection()
        tools = needed_tools(seen, run_bi)
        if tools and tooldeps.require_tools(
                census_subject(in_paths, run_bi, program), tools,
                skip_preflight=bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))):
            return 1
        warn_optional_tools(seen)

        ramscratch.init_ram_base(os.environ.get("censusRamBase", ""))
        # The scratch helpers answer the way the shell functions do - what they
        # printed, and the status beside it.
        scratch, status = ramscratch.ram_scratch_dir("contentCensus")
        if status != 0 or not scratch:
            raise Refusal("\nError: no scratch directory could be made for "
                          "this run.\nNothing was changed.\n")
        results = os.path.join(scratch, "results")
        os.makedirs(results, exist_ok=True)

        # The flag is a FILE rather than a variable, because the workers are
        # separate processes and a variable one of them sets is one nobody else
        # ever sees. It lives in the scratch, so it goes when the scratch does.
        safety.init_abort_flag(os.path.join(scratch, "abortRequested"))
        _install_interrupt_handlers()

        # One worker per path given, which is one per disk. A path that turned
        # out to hold no library at all gets no worker.
        worker_roots = [index for index in range(len(in_paths))
                        if index in lib_roots]
        parallel = len(worker_roots) > 1

        # Not named "census": that is the module this file reads its column
        # headers from, and shadowing it here would be a trap for the next edit.
        state = Run(lib_paths, lib_names, lib_roots, lib_out_dirs, files,
                    totals, starts, in_paths, scratch, separator, extension,
                    runlog.cpu_count(), parallel, total)
        if parallel:
            with open(state.counter, "w") as handle:
                handle.write("0")
            open(state.console_lock, "w").close()
            if not runlog.have_flock():
                log("WARNING: flock is not installed (it ships in util-linux), "
                    "so the progress lines")
                log("         of the libraries being read side by side carry "
                    'no "[n of total]"')
                log("         position. The census itself and its closing "
                    "counts are unaffected.")
            log("Censusing %d libraries from the %d folders given, one worker "
                "each" % (len(lib_paths), len(worker_roots)))
            workers = []
            for root_index in worker_roots:
                worker = multiprocessing.Process(target=state.root,
                                                 args=(root_index,))
                worker.start()
                workers.append(worker)
            for worker in workers:
                worker.join()
        else:
            for root_index in worker_roots:
                state.root(root_index)

        return _summarise(state, worker_roots, results, run_bi, script_dir,
                          total)
    finally:
        _cleanup(scratch, created_out_root)


def _install_interrupt_handlers():
    """TERM and HUP alongside INT: closing the terminal window mid-census is the
    same request as Ctrl+C, and it is the one nobody is watching."""
    def handler(_number, _frame):
        safety.request_abort()

    for number in safety.interrupt_signals():
        try:
            signal.signal(number, handler)
        except (OSError, ValueError):      # pragma: no cover - a odd platform
            pass


def _summarise(state, worker_roots, results, run_bi, script_dir, total):
    """What the workers did, read back in the order the paths were given - so
    the summary, the skip list and the reports handed to -b are the same
    whichever worker happened to finish first."""
    interrupted = safety.abort_requested()
    censused = written = 0
    sanitised = False
    reports, skipped = [], []
    for root_index in worker_roots:
        counts = os.path.join(results, "%d.counts" % root_index)
        if os.path.isfile(counts):
            with open(counts) as handle:
                parts = handle.read().split()
            if len(parts) == 3:
                censused += int(parts[0])
                written += int(parts[1])
                sanitised = sanitised or parts[2] != "0"
        reports.extend(_read_entries(
            os.path.join(results, "%d.reports" % root_index)))
        skipped.extend(_read_entries(
            os.path.join(results, "%d.skipped" % root_index)))

    sys.stderr.write("\n")
    if written == 0:
        log("No report was written: none of the %d file(s) found could be "
            "censused." % total)
    elif len(state.lib_paths) > 1:
        log("Censused %d of %d file(s) from %d libraries into %d report(s)."
            % (censused, total, len(state.lib_paths), written))
    else:
        log('Censused %d of %d file(s) into %d report(s) in "%s".'
            % (censused, total, written, state.lib_out_dirs[0]))

    if skipped:
        log("%d file(s) were skipped because they are not what their suffix "
            "says:" % len(skipped))
        for entry in skipped:
            log("  " + entry)

    if sanitised:
        log("Tab-separated output: a tab or line break inside a path was "
            "replaced by a space")
        log("in at least one row (a .tsv has no way to quote one). Use the "
            "default .csv to")
        log("keep such paths exactly as they are.")

    if run_bi:
        _build_cubes(reports, written, interrupted, script_dir)

    return safety.INTERRUPTED_EXIT_STATUS if interrupted else 0


def _build_cubes(reports, written, interrupted, script_dir):
    """The reports this run wrote are handed over BY NAME rather than by the
    folder they are in, so a -o folder that already holds another census's
    reports is not quietly folded into these cubes.

    An interrupted run is the one case where they are not built: the reports are
    valid files, but they hold only the part of the library that was read, and a
    cube says nothing about how complete the thing it was built from is - so a
    total out of it would be wrong in a way nobody could see afterwards.
    """
    if written == 0:
        log("-b: no report was written, so there is nothing to build cubes "
            "from.")
        return
    if interrupted:
        sys.stderr.write("\n")
        log("-b: the run was interrupted, so the cubes were NOT built - the "
            "reports hold")
        log("    only part of the libraries. Build them from a finished census "
            "with:")
        log("    content-census-bi %s" % os.path.dirname(reports[0]))
        return
    sys.stderr.write("\n")
    log("Building the cubes from the %d report(s) just written" % written)
    commands.run_command("content-census-bi", reports, script_dir=script_dir,
                         check=False)


def _read_entries(path):
    """The NUL-delimited entries a worker left behind."""
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
    except OSError:
        return []
    return [os.fsdecode(entry) for entry in payload.split(b"\0") if entry]
