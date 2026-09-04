"""ingest-books: a folder of e-books ingested into a clean, uniform library.

Every ingestible book under the input becomes one output file under the output,
keeping the name and the sub-folder layout:

* PDFs are stripped of their (usually oversized) images and copied across - they
  never enter the epub pipeline, because a PDF is not something to re-flow;
* mobi/chm/azw3/lit/txt are converted to epub;
* epubs (and the just-converted ones) are unpacked, cleaned - embedded fonts
  dropped, images downscaled, junk images removed - repacked, and re-converted
  once more for consistent readability.

With -t the whole thing is bypassed: every recognised input becomes a raw .txt,
a PDF read by poppler and everything else by ebook-convert.

The input tree is never modified. All intermediate work happens in RAM and only
the finished book reaches the disk, so a book's workspace can be dropped the
moment it is emitted and only a handful are ever resident.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile

from medialib import commands
from medialib.lib import (
    booktext,
    clioptions,
    enums,
    imagemagick,
    imagesizes,
    ramscratch,
    runlog,
    safety,
    tooldeps,
    workerpool,
)

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>
Options:"""

OPT_SPEC = """
h |  | Print this help page.
c |  | Run clean-folder-structure on the output folder when done.
t |  | Text mode: convert every recognized input to a raw .txt file,
          mirroring the input sub-folder layout, instead of the epub pipeline.
z |  | Text mode only: also create a zpaq archive of the resulting folder
          (output folder, or input folder when no conversion was done).
"""

OPT_VARS = "c:runCleanFolderStructure t:textMode z:zpaqArchive"
OPT_COLUMN = 10
OPT_LONG = "h:help c:clean-structure t:text z:zpaq"

# Images larger than this are downscaled to at most the tier's geometry; smaller
# ones are left alone. An illustration inside an epub is read on a reader, not on
# a monitor, which is what the table's default tier is for.
FILE_SIZE_LIMIT = 500000
IMAGE_QUALITY = 75

# Case-insensitive name substrings that mark a throwaway image (teasers, "back
# matter" adverts) to delete during cleaning.
JUNK_IMAGE_SUBSTRINGS = ("teaser", "backadd")

FONT_EXTENSIONS = ("ttf", "otf")


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
    )


def ext_in_list(extension: str, candidates) -> bool:
    """``extInList``: is this extension one of those?"""
    return extension in list(candidates)


def _grant_tree_access(path: str) -> None:
    """``chmod -R u+rwX``: the owner gets read and write on everything under
    ``path``, and execute on the directories and on whatever already carried an
    execute bit.

    Each directory is granted BEFORE it is listed, the way chmod's own descent
    does it - a directory missing its write or execute bit cannot have its
    entries reached until it has them. Symlinks are passed over rather than
    followed, which is chmod's own rule, and an entry that refuses is passed
    over too: the shell's chmod had its errors sent to /dev/null.
    """
    def grant(target: str) -> None:
        try:
            info = os.stat(target)
        except OSError:
            return
        mode = stat.S_IMODE(info.st_mode)
        wanted = mode | stat.S_IRUSR | stat.S_IWUSR
        # +X, not +x: execute only where some execute bit is already there, so
        # a plain file does not come out of this an executable one.
        any_execute = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        if stat.S_ISDIR(info.st_mode) or mode & any_execute:
            wanted |= stat.S_IXUSR
        if wanted != mode:
            try:
                os.chmod(target, wanted)
            except OSError:
                pass

    def descend(directory: str) -> None:
        grant(directory)
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                descend(entry.path)
            else:
                grant(entry.path)

    if os.path.isdir(path):
        descend(path)
    else:
        grant(path)


def safe_rmrf(*paths) -> None:
    """Remove a tree even when an epub extracted read-only content: a directory
    needs write+execute before its own entries can go."""
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        _grant_tree_access(path)
        shutil.rmtree(path, ignore_errors=True)


def _is_image(name: str) -> bool:
    return enums.lower_extension_of(name) in [
        e.lower() for e in enums.BOOK_IMAGE_EXTENSIONS]


def resize_image(path: str, geometry: str,
                 run=subprocess.run) -> None:
    """Downscale one image in place, but only when it is big enough to be worth
    it. The name and extension are kept so references inside the epub stay
    valid - for an image-wrapper SVG that rasterises the payload, which is
    exactly the shrink wanted without breaking the link."""
    stem, _, extension = path.rpartition(".")
    temporary = "%s.temp.%s" % (stem or path, extension)
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size >= FILE_SIZE_LIMIT:
        done = run(
            imagemagick.convert_argv(
                [path, "-quality", str(IMAGE_QUALITY),
                 "-resize", geometry + ">", temporary]),
            stderr=subprocess.DEVNULL)
        if getattr(done, "returncode", 1) != 0:
            return
    else:
        shutil.copyfile(path, temporary)
    os.remove(path)
    shutil.move(temporary, path)


def clean_book_folder(directory: str, geometry: str = "",
                      run=subprocess.run) -> None:
    """Tidy an unpacked epub in place: drop the embedded fonts (the reader falls
    back to a sane default), delete the junk images by name substring, and
    downscale what is left."""
    for parent, _dirs, names in os.walk(directory):
        for name in names:
            if enums.lower_extension_of(name) in FONT_EXTENSIONS:
                try:
                    os.remove(os.path.join(parent, name))
                except OSError:
                    pass

    for parent, _dirs, names in os.walk(directory):
        for name in names:
            if not _is_image(name):
                continue
            lowered = name.lower()
            if any(word in lowered for word in JUNK_IMAGE_SUBSTRINGS):
                try:
                    os.remove(os.path.join(parent, name))
                except OSError:
                    pass

    if not geometry:
        return
    for parent, _dirs, names in os.walk(directory):
        for name in sorted(names, key=os.fsencode):
            if _is_image(name):
                resize_image(os.path.join(parent, name), geometry, run=run)


def emit_output(source: str, destination: str) -> None:
    """Move a finished book to the output, never clobbering: a collision keeps
    BOTH via a " (N)" suffix. A missing source is a silent no-op - a failed
    conversion simply produces no output for that book.

    Several books are emitted in parallel, so another worker may take the same
    name between choosing it and moving; the move is retried with the next free
    suffix until it actually lands.
    """
    if not os.path.exists(source):
        return
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    while True:
        candidate = destination
        if os.path.exists(candidate):
            candidate = safety.unique_suffix_path(candidate)
        try:
            # Claim the name atomically: two workers emitting at once must not
            # both find it free, which is what `mv -n` buys the shell. The
            # workspace is in RAM and the library on disk, so this is a copy
            # either way - a rename across filesystems is one too.
            handle = os.open(candidate,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue                      # another worker took it; next suffix
        except OSError:
            return
        os.close(handle)
        shutil.copyfile(source, candidate)
        os.remove(source)
        return


class Run:
    """One ingest: the settled world its workers inherit, and the counters they
    speak back through because each is a separate process."""

    def __init__(self, in_path, out_path, counter_dir, temp_path, options,
                 total, run_marker):
        self.in_path = in_path
        self.out_path = out_path
        self.counter_dir = counter_dir
        self.temp_path = temp_path
        self.options = options
        self.total = total
        self.run_marker = run_marker

    def progress(self, line: str) -> None:
        """Bump the shared counter and print one clean line.

        The counter counts BOOKS FINISHED against the number of input books, so
        this is called exactly ONCE per book - on every path through, including
        the ones that skip or fail, and always as that book's last line. A step
        inside one book's pipeline reports through note() instead, which prints
        without counting: calling this twice for one book is what produced
        "[2/1]".
        """
        with open(os.path.join(self.counter_dir, "lock"), "w") as handle, \
                runlog.take_lock(handle):
            path = os.path.join(self.counter_dir, "current")
            try:
                with open(path) as counter:
                    current = int(counter.read() or "0") + 1
            except (OSError, ValueError):
                current = 1
            with open(path, "w") as counter:
                counter.write(str(current))
            sys.stdout.write("%s%s\n" % (
                runlog.counted_prefix(current, self.total), line))

    def note(self, line: str) -> None:
        """An UNCOUNTED line about a step inside one book's pipeline, indented
        so it reads as subordinate. Shares progress()'s lock so the two never
        interleave."""
        with open(os.path.join(self.counter_dir, "lock"), "w") as handle, \
                runlog.take_lock(handle):
            sys.stdout.write("        %s\n" % line)

    def _output_extension(self, extension: str) -> str:
        if self.options["textMode"]:
            return "txt"
        return "pdf" if extension == "pdf" else "epub"

    def process_book(self, relative: str) -> None:
        """One input book, all the way to a single output file under the output,
        mirroring its sub-folder. Runs inside a private RAM workspace that is
        removed on the way out."""
        source = os.path.join(self.in_path, relative)
        base = os.path.basename(relative)
        stem = base.rsplit(".", 1)[0] if "." in base else base
        extension = enums.lower_extension_of(base)
        relative_dir = os.path.dirname(relative)
        dest_dir = os.path.join(self.out_path, relative_dir) if relative_dir \
            else self.out_path

        # Skip entirely when the finished output already exists FROM A PREVIOUS
        # RUN. An output newer than the run-start marker belongs to THIS run - a
        # same-stem sibling emitted moments ago - and that collision is
        # emit_output's to settle, which keeps both.
        target = os.path.join(dest_dir,
                              "%s.%s" % (stem, self._output_extension(extension)))
        if os.path.exists(target) and not _newer_than(target, self.run_marker):
            self.progress("Skip (exists): " + base)
            return

        work = tempfile.mkdtemp(prefix="book.", dir=self.temp_path)
        try:
            if self.options["textMode"]:
                self.note("Text: " + base)
                out = os.path.join(work, "out.txt")
                if booktext.book_to_text(source, out) == 0:
                    emit_output(out, os.path.join(dest_dir, stem + ".txt"))
                    self.progress("Done (txt): " + base)
                else:
                    self.progress("FAILED (txt): " + base)
                return

            # A PDF is not something to re-flow: strip its images to shrink it
            # and copy straight across, skipping unpack/clean/repack.
            if extension == "pdf":
                self.note("PDF: " + base)
                out = os.path.join(work, "out.pdf")
                done = subprocess.run(
                    ["gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                     "-dPDFSETTINGS=/default", "-dNOPAUSE", "-dQUIET",
                     "-dBATCH", "-dFILTERIMAGE", "-dCompressFonts=true",
                     "-r25", "-sOutputFile=" + out, source],
                    stderr=subprocess.DEVNULL)
                if done.returncode == 0:
                    emit_output(out, os.path.join(dest_dir, stem + ".pdf"))
                    self.progress("Done (pdf): " + base)
                else:
                    self.progress("FAILED (pdf): " + base)
                return

            # Everything else is normalised to an epub first.
            epub = os.path.join(work, "book.epub")
            if extension == "epub":
                shutil.copyfile(source, epub)
            elif ext_in_list(extension, enums.BOOK_CONVERT_EXTENSIONS):
                self.note("Converting: " + base)
                done = subprocess.run(
                    ["ebook-convert", source, epub, "--no-default-epub-cover"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if done.returncode != 0:
                    self.progress("FAILED (convert): " + base)
                    return
            else:
                # Unreachable while the scan only picks up the input list, but
                # still counted so the tally stays exact.
                self.progress("Skip (unsupported): " + base)
                return

            unpacked = os.path.join(work, "unpacked")
            os.makedirs(unpacked, exist_ok=True)
            subprocess.run(["unzip", "-q", "-o", "-d", unpacked, "--", epub],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            clean_book_folder(unpacked, self.options["imageResolution"])

            repacked = os.path.join(work, "repacked.epub")
            subprocess.run(["zip", "-q", "-r", "-X", repacked, "."],
                           cwd=unpacked, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

            # Re-converted once more for consistent readability; the repacked
            # epub is the fallback when that heavier step is unavailable.
            final = os.path.join(work, "final.epub")
            done = subprocess.run(
                ["ebook-convert", repacked, final, "--no-default-epub-cover"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if done.returncode != 0 or not os.path.exists(final):
                shutil.copyfile(repacked, final)

            emit_output(final, os.path.join(dest_dir, stem + ".epub"))
            self.progress("Done: " + base)
        finally:
            safe_rmrf(work)


def _newer_than(path: str, marker: str) -> bool:
    """``[[ a -nt b ]]``: is a's modification time later than b's?"""
    try:
        return os.path.getmtime(path) > os.path.getmtime(marker)
    except OSError:
        return False


def _in_worker(state, relative: str) -> None:
    """One book, in a worker PROCESS - where the worker's interrupt handler
    belongs, and not in process_book, which the serial path runs inline."""
    safety.trap_worker_abort()
    state.process_book(relative)


# --- the run -------------------------------------------------------------------

def _books_under(in_path: str):
    """Every ingestible book under the input, LARGEST FILE FIRST.

    Byte size is a cheap probe-free proxy for how long a book takes through its
    single-threaded pipeline, so the big ones start at the front instead of one
    huge book landing last and pinning a core while everything else has drained.
    """
    wanted = {e.lower() for e in enums.BOOK_INPUT_EXTENSIONS}
    found = []
    for parent, _dirs, names in os.walk(in_path):
        for name in names:
            if enums.lower_extension_of(name) not in wanted:
                continue
            full = os.path.join(parent, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            found.append((size, os.path.relpath(full, in_path)))
    found.sort(key=lambda item: -item[0])
    return [relative for _size, relative in found]


def _run_pool(state, work, jobs: int) -> None:
    if jobs <= 1:
        for relative in work:
            if safety.abort_requested():
                return
            state.process_book(relative)
        return

    workerpool.run(work, jobs, _in_worker, lambda relative: (state, relative))


def _word_count(path: str) -> int:
    """``wc -w``: runs of non-whitespace."""
    try:
        with open(path, "rb") as handle:
            return len(handle.read().split())
    except OSError:
        return 0


def _text_mode_report(state, books, out_path, temp_path, all_txt,
                      in_path) -> str:
    """Word-count every measured .txt, write the summary the run is named after,
    and say what the whole corpus came to."""
    if all_txt:
        measured = [os.path.join(in_path, relative) for relative in books]
    else:
        measured = []
        for parent, _dirs, names in os.walk(out_path):
            for name in names:
                if enums.lower_extension_of(name) == "txt":
                    measured.append(os.path.join(parent, name))
        measured.sort(key=os.fsencode)

    report = os.path.join(out_path, os.path.basename(out_path) + ".txt")
    rows, total_words, counted = [], 0, 0
    for path in measured:
        if path == report:
            continue
        words = _word_count(path)
        rows.append((words, path))
        total_words += words
        counted += 1
    rows.sort(key=lambda row: -row[0])
    with open(report, "w") as handle:
        for words, path in rows:
            handle.write("%d\t%s\n" % (words, path))

    print("Word-count summary written to: %s" % report)
    print("Total words across %d file(s): %d" % (counted, total_words))
    return report


def main(argv: list, program: str = "ingest-books",
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

    in_path = result.positionals[0].rstrip("/")
    out_path = result.positionals[1].rstrip("/")
    text_mode = "t" in result.given
    zpaq_archive = "z" in result.given
    clean_structure = "c" in result.given
    script_dir = script_dir or commands.script_dir()

    temp_path = counter_dir = ""
    try:
        ramscratch.init_ram_base()
        temp_path, status = ramscratch.ram_scratch_dir("ingestBooks")
        counter_dir, counter_status = ramscratch.ram_scratch_dir(
            "ingestBooks.counter")
        if status != 0 or counter_status != 0 or not temp_path \
                or not counter_dir:
            sys.stderr.write("\nError: no scratch directory could be made for "
                             "this run.\nNothing was changed.\n")
            return 1
        ramscratch.add_exit_cleanup([temp_path, counter_dir])

        # An emitted epub or pdf is itself an ingestible book, so an output
        # folder inside the input would be ingested again by the next run.
        if safety.require_separate_output(in_path, out_path):
            return 1

        # Built per mode rather than one fixed list: the two modes share almost
        # nothing, and one call names everything a bare machine is missing at
        # once. pdftotext is deliberately absent - book_to_text falls back to
        # ebook-convert without it, so it buys speed and never capability.
        tools = ["ebook-convert"]
        if not text_mode:
            tools += ["gs", "unzip", "zip", imagemagick.CONVERT_SPEC]
        if zpaq_archive:
            tools += ["zpaq"]
        runlog.settle_flock()
        if tooldeps.require_tools(
                program, tools,
                skip_preflight=bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))):
            return 1
        runlog.warn_uncounted_progress()

        safety.init_safety_log(os.path.join(counter_dir, "safetySkips.log"))
        safety.init_abort_flag(os.path.join(counter_dir, "abortRequested"))
        safety.trap_run_abort()
        safety.set_run_footer(safety.report_safety_skips)

        # Stamped at run start, so the "skip when the output exists" guard fires
        # only for outputs left by a PREVIOUS run.
        run_marker = os.path.join(counter_dir, "runStart")
        open(run_marker, "w").close()

        books = _books_under(in_path)
        total = len(books)
        with open(os.path.join(counter_dir, "current"), "w") as handle:
            handle.write("0")
        if total == 0:
            return safety.fail_no_relevant_input(
                in_path, "e-books (%s)" % enums.extension_list(
                    list(enums.BOOK_INPUT_EXTENSIONS)))

        os.makedirs(out_path, exist_ok=True)
        options = {
            "textMode": text_mode,
            "imageResolution": imagesizes.geometry() or "",
        }
        state = Run(os.path.abspath(in_path), os.path.abspath(out_path),
                    counter_dir, temp_path, options, total, run_marker)
        jobs = runlog.cpu_count()

        if text_mode:
            print("Ingesting %d book(s) in text mode" % total)
            print("=====================================")
            print("")
            # All inputs already plain .txt? Then nothing is copied or
            # converted: the originals stay untouched and are measured in place.
            all_txt = all(enums.lower_extension_of(relative) == "txt"
                          for relative in books)
            if all_txt:
                print("All inputs are already .txt - skipping copy and "
                      "conversion")
                print("")
                archive_root = in_path
            else:
                _run_pool(state, books, jobs)
                safety.exit_if_aborted()
                archive_root = out_path
            _text_mode_report(state, books, out_path, temp_path, all_txt,
                              in_path)
            if zpaq_archive:
                _archive(out_path, archive_root)
        else:
            print("Ingesting %d book(s)" % total)
            print("========================")
            print("")
            _run_pool(state, books, jobs)
            safety.exit_if_aborted()

        if clean_structure:
            print("")
            print("Cleaning output folder structure")
            commands.run_command("clean-folder-structure", [out_path],
                                 script_dir=script_dir)

        safety.print_run_footer()
        return 0
    finally:
        ramscratch.run_exit_cleanup()


def _archive(out_path: str, archive_root: str) -> None:
    """The optional zpaq archive of the resulting corpus: the output folder when
    a conversion happened, or the untouched input when all inputs were already
    text. Named after the output folder and written into it."""
    archive = os.path.join(out_path, os.path.basename(out_path) + ".zpaq")
    print("")
    print('Creating zpaq archive of "%s" -> %s' % (archive_root, archive))
    done = subprocess.run(["zpaq", "a", archive, archive_root, "-m5"])
    if done.returncode == 0:
        print("Archive written: %s" % archive)
    else:
        print('WARNING: zpaq archiving failed for "%s"' % archive_root)


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
