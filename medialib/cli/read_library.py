"""read-library: a library of e-books read aloud into a library of audiobooks.

Every e-book anywhere under <inputDir> is narrated into an audiobook under
<outputDir>, keeping the same name and sub-folder layout. The input tree is never
modified, each book is narrated inside a RAM workspace of its own, and only the
finished audiobook is written to disk - so an interrupted run leaves no half-read
book behind and a re-run picks up exactly where it stopped.

Each book comes out TWICE, in two libraries side by side: a mono Opus to listen
to under <outputDir>/opus, and the engine's own lossless master to keep under
<outputDir>/flac. Each is a complete mirror of the input's sub-folders on its
own, so the listening library can go to a phone and the lossless one to an
archive disk without either dragging the other along.

The reading itself is the narration library; everything here is the library-level
work around it - which books, in what order, how many at a time, where the output
goes, and what the run cost.
"""

import os
import re
import sys
import tempfile
import time

from medialib import commands
from medialib.lib import (
    booklanguage,
    booknarration,
    bookpublishing,
    booktext,
    clioptions,
    enums,
    ffmpegselect,
    formatting,
    imagemagick,
    ramscratch,
    runlog,
    safety,
    statusline,
    tooldeps,
    workerpool,
)
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>

    inputDir      scanned RECURSIVELY for e-books.
    outputDir     where the audiobooks are written: one folder per format, and
                  inside each the same name and sub-folder as the book -
                  <outputDir>/opus/... for the listening copy and
                  <outputDir>/flac/... (or /m4b/...) for the lossless one, two
                  complete libraries beside each other. Created if missing, and
                  refused if it lies inside inputDir.

Options:"""

# The spec is DATA, and the page it renders is compared byte for byte against the
# recorded contract under tests/data/cliContract.
OPT_SPEC = """
h |  | Print this help page.
j | <jobs> | Narrate up to <jobs> books at a time.
                    Default: what the device has room for - free VRAM divided by
                    5 GB on a GPU, and 1 on a CPU, which one narration saturates
                    on its own. At 1, however it was arrived at, the engine's own
                    per-book progress is printed as well.
v | <sample> | Voice sample(s) to clone the narrator's voice from.
                    A FILE is used for every book: any audio or video file, one in
                    the wrong format is transcoded to WAV first, and one longer
                    than a minute is cut down to a slice of speech from its middle.
                    A DIRECTORY holds one sample per language, each named after
                    the language it speaks (deu.wav, german.m4a, de.mp3, plus an
                    optional default.wav): every book is then read by the voice of
                    its own language, because a cloned voice carries the accent of
                    the sample it was cloned from.
                    Default the engine's own voice
b | <kbps> | Bitrate of the Opus listening copy. 0 writes none, leaving
                    only the lossless file.
                    Default 36
o |  | Only the Opus: do not keep the lossless file beside it.
e | <dir> | The ebook2audiobook checkout to drive.
                    Default $narrationHome, else ~/ebook2audiobook
d | <device> | Where the model runs: cpu, cuda, mps, rocm, xpu, jetson.
                    Default cuda when an NVIDIA GPU is present, cpu otherwise
t | <engine> | TTS engine: xtts, bark, vits, fairseq, tacotron, yourtts.
                    Voice cloning (-v) needs one that supports it.
                    Default xtts
l | <language> | Language of the books, ISO 639-3 (eng, deu, ita, ...). Given,
                    it is used for every book and nothing is detected. Omitted,
                    each book's own language is established from its metadata,
                    or from its text when the metadata says nothing.
                    Default: detected per book"""

OPT_VARS = ("j:jobs v:voiceSample b:opusBitrate e:narrationCheckout "
            "d:narrationDeviceArg t:narrationEngine l:languageArg")

# -j left out is the device's own answer, which is why only a value somebody
# typed is checked. -b takes 0 as "write no Opus at all", so zero belongs in its
# range - what 0 must not combine with is -o, and that pairing is settled where
# both are known.
OPT_CHECKS = """
j | posInt | job count
b | nonNegInt | bitrate in kbps
"""

OPT_COLUMN = 20
OPT_LONG = ("h:help j:jobs v:voice b:opus-bitrate o:opus-only e:checkout d:device "
            "t:engine l:language")

# The listening copy and the archival one. 36 kbps of mono Opus is transparent
# for one synthetic voice; 0 writes no Opus at all.
DEFAULT_OPUS_BITRATE = 36

# How often the per-book percentages are worked out afresh. They come from
# reading the tail of each book's engine log, far too much work to repeat every
# two seconds for a number that moves by a percent every few minutes - so the
# answer is cached for a minute and the row redraws the cached one in between.
BOOK_PROGRESS_INTERVAL = 60


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        checks=OPT_CHECKS,
        column=OPT_COLUMN,
    )


def output_path_for(out_path: str, relative: str, extension: str) -> str:
    """Where the audiobook for one input book belongs: under a folder of its own
    FORMAT, and inside that the same name and sub-folder as the input book.

    One library of e-books therefore comes out as two complete libraries side by
    side, each a whole tree that can be synced or handed to a media server on its
    own - the listening library to a phone, the lossless one to the archive disk,
    and neither dragging the other along.

    The folder is the extension itself, so it never claims anything the files in
    it are not: a book whose lossless copy could only be the engine's own m4b
    lands in <out>/m4b/ rather than among the FLACs.

    Deterministic, so the resume check and the emit derive it independently
    without agreeing on anything but the input path.
    """
    stem = os.path.splitext(os.path.basename(relative))[0]
    relative_dir = os.path.dirname(relative)
    parts = [out_path, extension]
    if relative_dir and relative_dir != ".":
        parts.append(relative_dir)
    return os.path.join(*parts, "%s.%s" % (stem, extension))


def book_already_read(out_path: str, relative: str, resume_extensions: list,
                      run_marker: str) -> bool:
    """Whether an EARLIER run finished this book, which is what makes a re-run
    resume instead of reading everything again.

    Asked of the file published LAST, so a book interrupted between its two
    output files reads as unfinished and is done again rather than remembered as
    one whose lossless copy is missing forever.

    An output newer than the run-start marker belongs to THIS run - a same-stem
    sibling emitted moments ago - so it must not be skipped here; the emit keeps
    both instead.
    """
    try:
        marker_at = os.stat(run_marker).st_mtime
    except OSError:
        marker_at = 0.0
    for extension in resume_extensions:
        destination = output_path_for(out_path, relative, extension)
        try:
            if os.stat(destination).st_mtime <= marker_at:
                return True
        except OSError:
            continue
    return False


def emit_output(source: str, target: str) -> bool:
    """Publish a finished audiobook, never clobbering: a collision keeps BOTH via
    a " (N)" suffix.

    Two steps on purpose. The scratch is a tmpfs and the output is a disk, so the
    "move" out of RAM is really a COPY - and a copy straight onto the final name
    would leave a growing, incomplete file sitting exactly where the resume check
    looks, so an interrupted run would mark that book done forever. The bytes go
    to a hidden staging name in the destination folder first and are only then
    renamed, and a rename within one filesystem is atomic.

    The staging name is dotted and carries no audio extension, so nothing that
    scans the output can mistake it for an audiobook if the run is killed between
    the two steps.
    """
    if not os.path.exists(source):
        return False
    destination_dir = os.path.dirname(target)
    try:
        os.makedirs(destination_dir, exist_ok=True)
        handle, staged = tempfile.mkstemp(prefix=".readLibrary.",
                                          dir=destination_dir)
        os.close(handle)
    except OSError:
        return False

    try:
        os.replace(source, staged)
    except OSError:
        try:
            import shutil
            shutil.move(source, staged)
        except (OSError, Exception):
            _remove(staged)
            return False

    # Several books are published at once, so another worker can take the same
    # name between choosing it and renaming - the rename must never overwrite, and
    # the next suffix is tried instead. Bounded, so a destination that refuses
    # every rename fails the book rather than spinning forever.
    for _attempt in range(100):
        candidate = target
        if os.path.exists(candidate):
            candidate = safety.unique_suffix_path(candidate)
        try:
            os.link(staged, candidate)
            os.unlink(staged)
            return True
        except OSError:
            if not os.path.exists(staged):
                return True
    _remove(staged)
    return False


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class Counters:
    """What the console says while a run is going on.

    A book takes hours and several are read at once, so the console has three
    jobs: say when a book STARTS, say when it FINISHES, and in between show that
    the run is still moving. The first two are ordinary lines that scroll; the
    third is the one row pinned under them.

    Both scrolling lines carry the SAME number for the same book - "[3/12]
    Reading ..." and, hours later, "[3/12] Done ..." - so a start can be paired
    with its finish in a console where four books' lines interleave. That number
    is the book's own, claimed when it starts, not a position in a queue that
    finishes out of order.
    """

    def __init__(self, counter_dir: str, total: int, render=None) -> None:
        self.dir = counter_dir
        self.total = total
        self.render = render

    def _lock_path(self) -> str:
        return os.path.join(self.dir, "lock")

    def read(self, name: str) -> int:
        try:
            with open(os.path.join(self.dir, name)) as handle:
                return int(handle.read() or 0)
        except (OSError, ValueError):
            return 0

    def write(self, name: str, value) -> None:
        with open(os.path.join(self.dir, name), "w") as handle:
            handle.write(str(value))

    def claim_book_number(self) -> int:
        """The next book's number, claimed under the console lock so two books
        that start at the same moment cannot take the same one."""
        with open(self._lock_path(), "w") as lock, runlog.take_lock(lock):
            number = self.read("started") + 1
            self.write("started", number)
            return number

    def book_line(self, number: int, line: str) -> None:
        """One scrolling line about a book, printed without disturbing the status
        row: the row is erased before the line goes out and re-pinned under it
        afterwards, under the lock the row's own refresher takes before it
        draws."""
        with open(self._lock_path(), "w") as lock, runlog.take_lock(lock):
            statusline.clear_status()
            sys.stdout.write("%s%s\n" % (
                runlog.counted_prefix(number, self.total), line))
            sys.stdout.flush()
            if self.render is not None:
                statusline.repin_status(self.render)

    def note(self, line: str) -> None:
        """What a book says between its start and its finish. Indented, so it
        reads as subordinate to the numbered lines rather than as another book."""
        with open(self._lock_path(), "w") as lock, runlog.take_lock(lock):
            statusline.clear_status()
            sys.stdout.write("        %s\n" % line)
            sys.stdout.flush()
            if self.render is not None:
                statusline.repin_status(self.render)

    def report_progress(self, category: str, number: int, line: str) -> None:
        """Record one FINISHED book, then print its closing line.

        The counter counts BOOKS FINISHED against a denominator of books found,
        so this is called exactly ONCE per book on every path through it - read,
        skipped or failed - and always as that book's last line.
        """
        with open(self._lock_path(), "w") as lock, runlog.take_lock(lock):
            self.write("current", self.read("current") + 1)
            self.write(category, self.read(category) + 1)
        self.book_line(number, line)

    def mark_book_active(self, number: int, what: str) -> None:
        """One file per book being read, named after that book's number and
        holding what the status row should say about it: the path of the engine's
        log while it is being narrated, or "=<word>" for the short phases
        afterwards, where there is no percentage to read."""
        try:
            os.makedirs(os.path.join(self.dir, "active"), exist_ok=True)
            with open(os.path.join(self.dir, "active", str(number)),
                      "w") as handle:
                handle.write(what)
        except OSError:
            pass

    def mark_book_done(self, number: int) -> None:
        _remove(os.path.join(self.dir, "active", str(number)))

    def tally_duration(self, add: str) -> None:
        """Accumulate the audiobook-seconds actually produced, serialised across
        the parallel readers. Tallied as books finish rather than re-probed at the
        end, so a resumed run reports the speed-up of the work IT did."""
        if not add:
            return
        duration_file = os.path.join(self.dir, "duration")
        with open(duration_file + ".lock", "w") as lock, runlog.take_lock(lock):
            try:
                with open(duration_file) as handle:
                    current = float(handle.read() or 0)
            except (OSError, ValueError):
                current = 0.0
            try:
                current += float(add)
            except ValueError:
                pass
            with open(duration_file, "w") as handle:
                handle.write("%.3f\n" % current)


class Run:
    """One run's settings, and the per-book work that reads them."""

    # Declared, not defaulted: the settings dict supplies every one, so a name
    # it does not carry is still an AttributeError at the read.
    in_path: str
    out_path: str
    temp_path: str
    script_dir: str
    counters: "Counters"
    total: int
    opus_bitrate: int
    keep_lossless: bool
    opus_jobs: int
    resume_extensions: list[str]
    run_marker: str
    narration_language: str
    narration_engine: str
    # The run's clocks, the end unset until the run has one.
    run_start: float
    run_start_epoch: int
    run_end: float | None

    def __init__(self, **settings) -> None:
        self.__dict__.update(settings)

    # --- the live status row --------------------------------------------------

    def active_book_progress(self) -> str:
        """The "[3] 41%  [4] 12%" part: one entry per book in flight, cheapest
        first - the cache is only rebuilt once a minute, and the rebuild is what
        reads the logs."""
        cache = os.path.join(self.counters.dir, "activeProgress")
        try:
            if time.time() - os.stat(cache).st_mtime < BOOK_PROGRESS_INTERVAL:
                with open(cache) as handle:
                    return handle.read()
        except OSError:
            pass

        text = ""
        active = os.path.join(self.counters.dir, "active")
        try:
            numbers = sorted(os.listdir(active), key=lambda name: int(name)
                             if name.isdigit() else 0)
        except OSError:
            numbers = []
        for number in numbers:
            try:
                with open(os.path.join(active, number)) as handle:
                    what = handle.read()
            except OSError:
                continue
            if what.startswith("="):
                percent = what[1:]
            else:
                percent = booknarration.narration_progress(what) or ""
                percent = ("%s%%" % percent) if percent else "..."
            text += "  [%s] %s" % (number, percent)

        # Written through a temporary and renamed, so a reader never catches the
        # cache half written - and two workers rebuilding it at once cannot
        # interleave.
        staged = "%s.%d" % (cache, os.getpid())
        try:
            with open(staged, "w") as handle:
                handle.write(text)
            os.replace(staged, cache)
        except OSError:
            _remove(staged)
        return text

    def reading_status_text(self) -> str:
        """ONE line, pinned at the bottom while the per-book lines scroll past
        above it. It answers the two questions a run this slow raises: how the RUN
        is doing, and how the books being read RIGHT NOW are doing.

        Deliberately no ETA: the books left are of unknown length, so extrapolating
        from a position in the queue would be inventing a number.
        """
        current = self.counters.read("current")
        duration_file = os.path.join(self.counters.dir, "duration")
        with open(duration_file + ".lock", "w") as lock, runlog.take_lock(lock):
            try:
                with open(duration_file) as handle:
                    duration = float(handle.read() or 0)
            except (OSError, ValueError):
                duration = 0.0

        elapsed = max(0, int(time.time()) - self.run_start_epoch)
        speed = formatting.fmt_ratio(
            "%.6f" % (duration / elapsed) if elapsed > 0 else "0")

        # Without flock the counter can lose increments, so the row says how much
        # work there is without claiming a position in it - the same distinction
        # the counted prefix draws for the per-book lines.
        position = ("%d/%d books" % (current, self.total)
                    if runlog.have_flock() else
                    "%d books" % self.total)
        # The books being read come LAST: the row is truncated to the terminal's
        # width from the right, so what a narrow console loses is the fourth
        # book's percentage rather than the run's own figures.
        return "  reading %s: elapsed %s  narrated %s  %sx realtime%s" % (
            position, formatting.fmt_clock(elapsed),
            formatting.fmt_clock("%.3f" % duration), speed,
            self.active_book_progress())

    # --- how long a book will take to read ------------------------------------

    def book_queue_weight(self, relative: str) -> tuple:
        """How many WORDS the book holds, which is what the queue is ordered by.

        A TTS engine speaks at a near-constant rate, so a word count is very
        nearly a minute count, and nothing else about a book predicts its reading
        time half as well. Byte size is that prediction only for the formats
        whose bytes ARE the text: an illustrated epub is mostly JPEG and a
        scanned PDF is entirely pictures, any of which outranks a 200k-word
        novel that zips down to 3 MB, putting the longest book in the library at
        the BACK of the queue.

        Zero is the answer for a book that could not be measured, which sends it
        to the back - right twice over: a book Calibre cannot turn into text is
        usually one the engine cannot read either, and a PDF that yields no text
        is a scan, which would narrate to an hour of silence.
        """
        # An already-read book is a resume skip - it costs milliseconds, and it is
        # not worth a conversion to place it. Zero puts every one of them behind
        # the books that actually have to be read, which is also where they
        # belong: the first book of a run may be read alone to populate the
        # checkout, and a skip in that slot would install nothing.
        if book_already_read(self.out_path, relative, self.resume_extensions,
                             self.run_marker):
            return 0, relative

        words = ""
        try:
            handle, text = tempfile.mkstemp(prefix="weigh.", suffix=".txt",
                                            dir=self.temp_path)
            os.close(handle)
        except OSError:
            return 0, relative
        try:
            if booktext.book_to_text_fast(
                    os.path.join(self.in_path, relative), text) == 0:
                words = booktext.book_text_counts(text)[0]
        finally:
            _remove(text)
        # The count is left EMPTY rather than 0 for a file that could not be read,
        # and is not reached at all when the conversion itself failed, so both
        # arrive here as "not a number" and mean the same thing: unplaced.
        return (int(words) if re.fullmatch(r"[0-9]+", words or "") else 0,
                relative)

    # --- one book, all the way into the library -------------------------------

    def read_book(self, relative: str) -> None:
        """Narrated, encoded, and published under the output in its format folder
        and mirrored sub-folder. Runs entirely inside a private RAM workspace that
        is removed on the way out, whatever happened.

        A book that cannot be read is reported and the run carries on: a library
        holds the odd DRM-locked, empty or malformed file, and one of them must
        not end a run that has hours of good books left in it.
        """
        import shutil

        source = os.path.join(self.in_path, relative)
        base = os.path.basename(relative)
        number = self.counters.claim_book_number()

        if book_already_read(self.out_path, relative, self.resume_extensions,
                             self.run_marker):
            self.counters.report_progress(
                "skipped", number, "Skip (already read): %s" % base)
            return

        # What language to read it in. Established per book unless -l settled it
        # for the run, and done HERE rather than in the scan so it costs nothing
        # for a book that was skipped above. A language the engine cannot speak is
        # dropped rather than passed on: the engine would refuse the book outright
        # over it, and reading it in the default voice is the lesser wrong.
        language = self.narration_language or ""
        if not language:
            language = booklanguage.book_language(source)
            if language and not booknarration.narration_supports_language(
                    language):
                self.counters.note(
                    "%s cannot speak %s - reading in its default: %s"
                    % (self.narration_engine, language, base))
                language = ""

        voice = booknarration.voice_sample_for(language)

        try:
            work = tempfile.mkdtemp(prefix="book.", dir=self.temp_path)
        except OSError:
            self.counters.report_progress(
                "failed", number, "FAILED (no scratch): %s" % base)
            return

        # Everything below happens before ANYTHING is published, and the
        # publishing is ordered so that the file the resume check looks at is
        # written last. A book is therefore either fully in the library or not in
        # it at all: a run interrupted between the two files leaves a book to read
        # again, never one remembered as done while half of it is missing.
        narration_log = os.path.join(work, "narration.log")
        try:
            self.counters.book_line(
                number, "Reading (%s%s): %s"
                % (language or "default", ", cloned" if voice else "", base))
            self.counters.mark_book_active(number, narration_log)

            produced = booknarration.narrate_book(source,
                                                  os.path.join(work, "audiobook"),
                                                  voice, language, narration_log)
            if not produced:
                self.counters.report_progress("failed", number,
                                              "FAILED: %s" % base)
                return

            narrated_seconds = booknarration._media_duration(produced)
            lossless = ""
            if self.keep_lossless:
                # The lossless file, if the engine left a master behind: finished
                # the way the audiobook itself was, given the chapter marks and
                # the cover art the engine only wrote into the m4b. A conversion
                # that left none keeps the m4b itself, which is still a generation
                # better than the Opus made from it.
                self.counters.mark_book_active(number, "=mastering")
                master = booknarration.narration_lossless_master(narration_log,
                                                                 produced)
                if master:
                    lossless = bookpublishing.audiobook_lossless(
                        master, produced, work, self.script_dir) or ""
                if not lossless:
                    lossless = produced

            opus = ""
            if self.opus_bitrate != 0:
                self.counters.mark_book_active(number, "=encoding")
                opus = bookpublishing.audiobook_to_opus(
                    produced, work, self.opus_bitrate, self.opus_jobs,
                    os.path.join(work, "opus.log"), self.script_dir) or ""

            self.counters.mark_book_active(number, "=writing")
            self._publish(number, base, relative, lossless, opus,
                          narrated_seconds)
        finally:
            self.counters.mark_book_done(number)
            # The engine keeps its own working copy of every book it converts -
            # the whole audiobook as FLAC, plus one file per chapter and one per
            # sentence, and the cloned voice sample beside them - and only clears
            # it out by age. Over a library that is several times the size of
            # what this run produces.
            booknarration.narration_drop_session(narration_log)
            shutil.rmtree(work, ignore_errors=True)

    def _publish(self, number: int, base: str, relative: str, lossless: str,
                 opus: str, narrated_seconds: str) -> None:
        """The two output files, the lossless one first so the file the resume
        check reads is written last."""
        if self.opus_bitrate != 0 and not opus:
            self.counters.report_progress(
                "failed", number,
                "FAILED (read, but could not be encoded to Opus): %s" % base)
            return

        written = ""
        if lossless:
            extension = lossless.rsplit(".", 1)[-1]
            if not emit_output(lossless,
                               output_path_for(self.out_path, relative,
                                               extension)):
                self.counters.report_progress(
                    "failed", number,
                    "FAILED (could not write the output): %s" % base)
                return
            written = extension

        if opus:
            if not emit_output(opus, output_path_for(self.out_path, relative,
                                                     "opus")):
                self.counters.report_progress(
                    "failed", number,
                    "FAILED (could not write the output): %s" % base)
                return
            written = "%s + opus" % written if written else "opus"

        self.counters.tally_duration(narrated_seconds)
        self.counters.report_progress("narrated", number,
                                      "Done (%s): %s" % (written, base))


def footer(state: Run) -> None:
    """What a run of this shape is judged on: how many books it got through, what
    it cost in wall-clock, and how much faster than real time it read.

    Per book and the speed-up both divide by what was READ, not by what was
    found: a resumed run that skipped 40 of 43 books did the work of three, and
    dividing by 43 would report a machine four times faster than it is.

    Named above every point the run can be cut short, so a Ctrl+C - which on a
    library of audiobooks can easily land hours in - reports the same figures for
    the books that were finished instead of throwing the whole account away.
    """
    counters = state.counters
    narrated = counters.read("narrated")
    try:
        with open(os.path.join(counters.dir, "duration")) as handle:
            audio_seconds = float(handle.read() or 0)
    except (OSError, ValueError):
        audio_seconds = 0.0
    end = state.run_end if state.run_end is not None else time.time()
    total_seconds = end - state.run_start

    print("")
    print("Stats")
    print("=====")
    print("Books found:       %d" % state.total)
    print("Read:              %d" % narrated)
    print("Skipped (done):    %d" % counters.read("skipped"))
    print("Failed:            %d" % counters.read("failed"))
    print("Total time:        %.2f s (%s)"
          % (total_seconds, formatting.fmt_hms("%.2f" % total_seconds)))
    print("Audio produced:    %.0f s (%s)"
          % (audio_seconds, formatting.fmt_hms("%.2f" % audio_seconds)))
    if narrated > 0:
        per_book = total_seconds / narrated
        print("Time per book:     %.2f s (%s)"
              % (per_book, formatting.fmt_hms("%.2f" % per_book)))
    if total_seconds > 0:
        print("Real-time speedup: %sx" % formatting.fmt_ratio(
            "%.6f" % (audio_seconds / total_seconds)))

    sys.stdout.flush()
    safety.report_safety_skips()


def order_books_by_size(scan: list) -> list:
    """The fallback ordering, and the one the whole run uses when nothing can
    measure a book: biggest byte count first, path order breaking the ties - so a
    re-run of an unchanged library queues in the same order twice."""
    return [relative for _size, relative in
            sorted(scan, key=lambda entry: (-entry[0], entry[1]))]


def _in_worker(state: Run, method: str, item) -> None:
    """One book, in a worker PROCESS. The worker's interrupt handling is installed
    here rather than in the work, because at width 1 that same work runs in the
    RUN's own process - where the worker's handler would replace the run's."""
    safety.trap_worker_abort()
    getattr(state, method)(item)


def _run_pool(state: Run, method: str, items: list, jobs: int) -> list:
    """Every item through <method>, up to <jobs> at once. The answers come back
    only from the serial path; the parallel one is for work whose result is the
    files and counters it leaves behind."""
    if jobs <= 1:
        answers: list = []
        for item in items:
            if safety.abort_requested():
                return answers
            answers.append(getattr(state, method)(item))
        return answers

    workerpool.run(items, jobs, _in_worker, lambda item: (state, method, item))
    return []


def _weigh_queue(state: Run, scan: list, jobs: int) -> list:
    """The books, longest first.

    Starting the long books at the front means they finish near the beginning of
    the run instead of one huge book landing last and holding the device on its
    own while everything else has drained.
    """
    weighed = []
    for _size, relative in scan:
        if safety.abort_requested():
            break
        weighed.append(state.book_queue_weight(relative))
    return [relative for _words, relative in
            sorted(weighed, key=lambda entry: (-entry[0], entry[1]))]


def main(argv: list, program: str = "read-library",
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

    keep_lossless = "o" not in result.given
    opus_bitrate = int(result.values["opusBitrate"] or DEFAULT_OPUS_BITRATE)

    # Refused rather than quietly reduced to "read the books and throw them
    # away": hours of narration whose output nobody kept is the one outcome no
    # run wants.
    if opus_bitrate == 0 and not keep_lossless:
        sys.stderr.write("%s\n\nerror: -o with -b 0 would leave nothing to "
                         "write.\n\n%s\n"
                         % (declaration.credits,
                            clioptions.page(declaration)))
        return 1

    # Which extensions a finished book occupies in the output, in the order they
    # are published - and therefore which of them means "this book is done" (the
    # LAST one). With an Opus that is the Opus; without one, the lossless file,
    # which is a .flac when the engine left a master behind and its own .m4b when
    # it did not.
    resume_extensions = ["opus"] if opus_bitrate != 0 else ["flac", "m4b"]

    if clioptions.args_out_of_range(len(result.positionals), 2, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    # Both made ABSOLUTE, whatever was typed, because one step of this run does
    # not happen in the directory it was started in: the narration drives the
    # checkout's app.py from INSIDE that checkout, so a relative path would be
    # looked for under the checkout and every book refused as missing. Done once,
    # where the arguments are read: these two travel into the workers, the resume
    # check and the output tree, and one place forgetting would be the same bug
    # again.
    in_path = os.path.abspath(result.positionals[0].rstrip("/"))
    out_path = os.path.abspath(result.positionals[1].rstrip("/"))

    if not os.path.isdir(in_path):
        sys.stderr.write(clioptions.missing_dir_text(declaration, in_path))
        return 1

    # An audiobook is not an e-book, so this could not read its own output back
    # in - but the guard is asked for anyway, and before anything is created: the
    # output tree mirrors the input's sub-folders, so an output INSIDE the input
    # would grow a copy of the library's own structure inside itself.
    if safety.require_separate_output(in_path, out_path):
        return 1

    script_dir = script_dir or commands.script_dir()

    # The external tools THIS script drives. The narration's own belong to the
    # checkout and are settled by the narration library below.
    tools = ["ffmpeg", "ffprobe"]
    if opus_bitrate != 0:
        tools += ["rsync", imagemagick.CONVERT_SPEC, "python3"]
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()
    if tooldeps.require_tools(program, tools):
        return 1
    if (opus_bitrate != 0 or keep_lossless) and tooldeps.require_python_module(
            "mutagen", program,
            "writes the chapter marks and the cover art into the audiobooks"):
        return 1
    runlog.warn_uncounted_progress()
    _settle_mkvtoolnix()

    narration_engine = result.values["narrationEngine"] or "xtts"
    narration_language = ""
    language_arg = result.values["languageArg"]
    if language_arg:
        # Checked HERE, once, rather than discovered by the engine inside a queue
        # that is hours deep: an engine that cannot speak it refuses every single
        # book, one by one, after the model has loaded.
        narration_language = booklanguage.book_language_code(language_arg)
        if (not narration_language
                or not booknarration.narration_supports_language(
                    narration_language)):
            sys.stderr.write('\nCannot read a library in "%s" with the %s '
                             "engine.\n\n" % (language_arg, narration_engine))
            supported = booknarration.narration_language_list()
            if supported:
                sys.stderr.write("  it speaks (ISO 639-3):  %s\n" % supported)
            else:
                sys.stderr.write("  give a language as an ISO 639-3 code (eng, "
                                 "deu, fra, ita, ...)\n")
            sys.stderr.write("\nNothing was changed.\n")
            return 1

    # Nothing to read? Said before the output folder is created, so a run refused
    # here leaves nothing behind.
    book_what = "e-books (%s)" % enums.extension_list(
        list(enums.NARRATABLE_BOOK_EXTENSIONS))
    scan = _scan_books(in_path)
    if not scan:
        return safety.fail_no_relevant_input(in_path, book_what)

    if not narration_language and not tooldeps.tool_present("ebook-convert"):
        log("WARNING: Calibre's ebook-convert is not installed (apt install "
            "calibre) - a book whose")
        log("         metadata does not state its language cannot have it read "
            "out of its text, and")
        log("         is narrated in the engine's default (English) instead. "
            "Pass -l to set one for")
        log("         the whole run.")

    statusline.init_status_line()

    ramscratch.init_ram_base(os.environ.get("readLibraryRamBase", ""))
    temp_path, temp_status = ramscratch.ram_scratch_dir("readLibrary")
    counter_dir, counter_status = ramscratch.ram_scratch_dir(
        "readLibrary.counters")
    if temp_status != 0 or counter_status != 0 or not temp_path \
            or not counter_dir:
        sys.stderr.write("\nError: no scratch directory could be made for this "
                         "run.\nNothing was changed.\n")
        return 1
    ramscratch.add_exit_cleanup([temp_path, counter_dir])

    try:
        return _read(result, program, script_dir, in_path, out_path, temp_path,
                     counter_dir, scan, book_what, opus_bitrate, keep_lossless,
                     resume_extensions, narration_language, narration_engine)
    finally:
        # The refresher is stopped here as well as after the queue drains: an
        # early exit leaves the run without reaching that line, and a refresher
        # that outlived it would keep redrawing over whatever was printed next.
        statusline.stop_status_monitor()
        ramscratch.run_exit_cleanup()


def _scan_books(in_path: str) -> list:
    """Every readable book anywhere under the input tree, as (size, relpath).

    The size is carried along because it is the fallback ordering; the ordering
    this run actually wants is by word count, which costs a conversion per book
    and is done in the pass after this one.
    """
    wanted = tuple("." + extension.lower()
                   for extension in enums.NARRATABLE_BOOK_EXTENSIONS)
    found = []
    for parent, _dirs, names in os.walk(in_path):
        for name in names:
            if not name.lower().endswith(wanted):
                continue
            path = os.path.join(parent, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            found.append((size, os.path.relpath(path, in_path)))
    return found


def _read(result, program: str, script_dir: str, in_path: str, out_path: str,
          temp_path: str, counter_dir: str, scan: list, book_what: str,
          opus_bitrate: int, keep_lossless: bool, resume_extensions: list,
          narration_language: str, narration_engine: str) -> int:
    run_start = time.time()

    safety.init_safety_log(os.path.join(counter_dir, "safetySkips.log"))
    safety.init_abort_flag(os.path.join(counter_dir, "abortRequested"))
    safety.trap_run_abort()

    # Stamped at run start. The resume guard must only fire for audiobooks left
    # by a PREVIOUS run, not for a same-run sibling whose stem happens to collide
    # (a "story.epub" and a "story.pdf" in one folder both target "story.m4b").
    run_marker = os.path.join(counter_dir, "runStart")
    open(run_marker, "w").close()

    # Settle the checkout, its interpreter and the device once, before any book is
    # read - and before the output folder is created, so a machine without a
    # usable checkout is told so by a run that changed nothing.
    if booknarration.init_book_narration(
            result.values["narrationCheckout"],
            result.values["narrationDeviceArg"]) != 0:
        return 1

    device = os.environ.get("narrationDevice", "")
    if result.values["jobs"]:
        jobs = int(result.values["jobs"])
        jobs_from = "-j"
    else:
        # One book is one loaded model, so what decides this is what the device
        # has room for.
        jobs = int(booknarration.narration_job_budget(device))
        jobs_from = device

    # One book at a time is the only case where the engine's own progress is worth
    # seeing: it belongs to the one book being read, so nothing interleaves and
    # the long silences of a slow model stop looking like a hung run. Past that
    # the same output is several books talking over each other. Decided from the
    # job count the run really ended up with, rather than from the -j that was
    # typed.
    os.environ["narrationVerbose"] = "1" if jobs == 1 else "0"

    # How many cores the Opus encode of a finished book may use. The narration of
    # the NEXT books runs alongside it, so one full set of cores per reader would
    # oversubscribe the machine by exactly <jobs>.
    opus_jobs = max(1, runlog.cpu_count() // max(1, jobs))

    # The voice sample(s), prepared once for the whole run rather than per book:
    # the cut and the transcode are the same work for every book that uses one.
    voice_sample = result.values["voiceSample"]
    voice_map = ""
    if voice_sample:
        if not os.path.exists(voice_sample):
            sys.stderr.write('\nVoice sample "%s" does not exist.\n\n%s\n'
                             % (voice_sample,
                                clioptions.page(spec(program))))
            return 1
        # The log is passed, not defaulted: preparing the samples is where a
        # file that names no language is reported, and the shell says that
        # through the run's own log rather than swallowing it.
        voice_map = booknarration.prepare_voice_samples(voice_sample, temp_path,
                                                        log)
        if not voice_map:
            sys.stderr.write('\nNo usable voice sample was found in "%s".\n'
                             % voice_sample)
            sys.stderr.write("Give an audio or video file with speech in it, or "
                             "a folder of them named\n")
            sys.stderr.write("after the languages they speak (deu.wav, "
                             "german.m4a, de.mp3, default.wav),\n")
            sys.stderr.write("or omit -v to use the engine's own voice. Nothing "
                             "was changed.\n")
            return 1
    os.environ["narrationVoiceMap"] = voice_map or ""

    os.makedirs(out_path, exist_ok=True)

    total = len(scan)
    # Belt and braces: the probe already refused an input with no book in it, but
    # the checkout preparation between the two can have taken a while, and an
    # input that changed underneath the run should be said out loud rather than
    # reported as "0 books".
    if total <= 0:
        return safety.fail_no_relevant_input(in_path, book_what)

    counters = Counters(counter_dir, total)
    state = Run(
        in_path=in_path, out_path=out_path, temp_path=temp_path,
        script_dir=script_dir, counters=counters, total=total,
        opus_bitrate=opus_bitrate, keep_lossless=keep_lossless,
        opus_jobs=opus_jobs, resume_extensions=resume_extensions,
        run_marker=run_marker, narration_language=narration_language,
        narration_engine=narration_engine, run_start=run_start,
        run_start_epoch=int(run_start), run_end=None,
    )
    counters.render = state.reading_status_text

    books = _order_the_queue(state, scan, total)
    if safety.abort_requested():
        safety.exit_if_aborted()

    for counter in ("current", "started", "narrated", "skipped", "failed"):
        counters.write(counter, 0)
    os.makedirs(os.path.join(counter_dir, "active"), exist_ok=True)
    with open(os.path.join(counter_dir, "duration"), "w") as handle:
        handle.write("0\n")

    safety.set_run_footer(lambda: footer(state))

    _announce(state, jobs, jobs_from, device, voice_map, voice_sample)

    # The row is only pinned when it has the console to itself; reading one book
    # at a time prints the engine's own output, which would scroll a row that is
    # rewritten in place into litter.
    if os.environ.get("narrationVerbose") == "0":
        statusline.start_status_monitor(os.path.join(counter_dir, "lock"),
                                        state.reading_status_text)

    # On a checkout whose environment is not populated yet, the FIRST book is read
    # on its own: that book's run is also the checkout's own dependency install,
    # and two of those installing into the same environment at once is how a
    # half-written package tree happens - which would break every book after it.
    first_book = 0
    if (os.environ.get("narrationEnvReady", "1") == "0" and jobs > 1
            and total > 1):
        log("The checkout's environment is not populated yet - reading the first "
            "book on its own,")
        log("so the dependency install it triggers happens once instead of in %d "
            "processes at once." % jobs)
        _run_pool(state, "read_book", books[:1], 1)
        first_book = 1

    _run_pool(state, "read_book", books[first_book:], jobs)
    statusline.stop_status_monitor()
    safety.exit_if_aborted()

    state.run_end = time.time()
    safety.print_run_footer()
    return 0


def _order_the_queue(state: Run, scan: list, total: int) -> list:
    """The queue, longest book first - or by byte size when nothing can measure a
    book.

    Whether the measuring can be spent at all depends on what is installed.
    Calibre reads every narratable format; poppler reads only PDF, and is merely
    the fast way to do it. So the pass needs Calibre, unless the library is
    nothing but PDFs and poppler is there to read them - and with neither present
    there is nothing to measure with.
    """
    needs_calibre = any(not relative.lower().endswith(".pdf")
                        for _size, relative in scan)
    weigh = True
    if not tooldeps.tool_present("ebook-convert"):
        if needs_calibre or not tooldeps.tool_present("pdftotext"):
            weigh = False
            log("WARNING: Calibre's ebook-convert is not installed (apt install "
                "calibre), so how long")
            log("         each book is cannot be measured. The queue is ordered "
                "by file size instead,")
            log("         which reads an illustrated book or a scanned PDF as "
                "though it were long.")
    if weigh and not tooldeps.tool_present("pdftotext"):
        log("NOTE: poppler-utils is not installed (apt install poppler-utils), "
            "so the PDFs are")
        log("      measured with Calibre instead, which is much slower at it. "
            "Only the ordering")
        log("      pass below is affected; nothing about the audiobooks changes.")

    if not weigh:
        return order_books_by_size(scan)

    log("Measuring %d book(s) to read the longest first - this takes a moment."
        % total)
    weigh_start = time.time()
    books = _weigh_queue(state, scan, runlog.cpu_count())
    # A Ctrl+C during a pass this long has to end the run HERE. Left to itself it
    # would fall through to a queue built from however many books were weighed,
    # and start narrating them.
    safety.exit_if_aborted()
    # One weight per book, or the queue is not the library: a short count means a
    # worker died rather than a book being unmeasurable, and the difference
    # between the two lists is books that would never be read at all. The ORDER is
    # worth a fallback; the contents are not.
    if len(books) != total:
        log("WARNING: measured %d of %d books - the measuring pass lost some. The"
            % (len(books), total))
        log("         queue is ordered by file size instead. Every book is still "
            "read.")
        return order_books_by_size(scan)
    log("Measured %d book(s) in %s."
        % (total, formatting.fmt_clock(int(time.time() - weigh_start))))
    return books


def _announce(state: Run, jobs: int, jobs_from: str, device: str,
              voice_map: str, voice_sample: str) -> None:
    line = "Reading %d book(s), %d at a time" % (state.total, jobs)
    if jobs_from == "-j":
        line += " (-j)"
    elif device == "cuda":
        line += " (%s, %s GB of free VRAM each)" % (
            device, os.environ.get("narrationVramPerBookGB", ""))
    else:
        line += " (%s)" % device
    print(line)

    description = "the engine's own"
    if voice_map:
        entries = [entry for entry in voice_map.split("\n") if entry]
        languages = [entry.split("\t")[0] for entry in entries]
        if len(entries) <= 1 and languages == ["-"]:
            description = "cloned from %s" % voice_sample
        else:
            description = "cloned, one per language (%s)" % " ".join(languages)
    print("Voice: %s" % description)

    if state.opus_bitrate == 0:
        print("Output: the lossless file only")
    elif state.keep_lossless:
        print("Output: %d kbps mono Opus, with the lossless file beside it"
              % state.opus_bitrate)
    else:
        print("Output: %d kbps mono Opus" % state.opus_bitrate)
    print("")


def _settle_mkvtoolnix() -> None:
    """Settled once and shared with the encoder children this run spawns, so the
    warning is said once by this run rather than by every book's encoder."""
    if os.environ.get("HAVE_MKVTOOLNIX") is not None:
        return
    present = all(tooldeps.tool_present(tool) for tool in
                  ("mkvmerge", "mkvpropedit", "mkvextract"))
    os.environ["HAVE_MKVTOOLNIX"] = "1" if present else ""
    if not present:
        log("WARNING: mkvtoolnix not found (apt install mkvtoolnix) - cover art "
            "embedded in Matroska sources")
        log("         will not be extracted for the Opus listening copies; "
            "sidecar images and the other cover")
        log("         sources still work. The chapters are written by mutagen "
            "and do not need it - nothing")
        log("         else is lost.")


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
