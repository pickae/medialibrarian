"""concat-audio: one audiobook file per input sub-folder, with chapters and a cover.

A sub-folder's tracks are gathered recursively and joined into one file, and the
result gets the two things a book needs: chapter marks, and a cover image. Which
of the four formats a sub-folder holds decides how the join is done, and a folder
holding two of them is skipped rather than mangled - mp3, opus, aac and flac
streams cannot be concatenated with each other.

The chapters come from a cue sheet when the folder holds one audio file and a cue
that describes it, and from the individual files otherwise. The cover comes from
an image in the folder, else a booklet PDF's first page, else the artwork embedded
in the audio.

The concatenation runs several sub-folders at once. It is largely I/O bound - the
input often sits in RAM while the output goes to disk - so overlapping them keeps
whichever drive is the bottleneck busy.
"""

import os
import shutil
import subprocess
import sys

from medialib import commands
from medialib.lib import (
    chapters,
    cleannamescollectively,
    cleannamesindividually,
    clioptions,
    cuechapters,
    enums,
    ffmpegselect,
    imagemagick,
    imagesizes,
    ramscratch,
    runlog,
    safety,
    thumbnails,
    tooldeps,
    workerpool,
)
from medialib.lib.runlog import log
from medialib.lib.versionsort import version_key

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>

    Options
    -------"""

# The spec is DATA, and the page it renders is compared byte for byte against the
# recorded contract under tests/data/cliContract.
OPT_SPEC = """
p |  | opt-in input pretreatment (in-place): renames folders/files in
            <inputDir> before concatenation.
v |  | verbose: show every per-subfolder step. Without it (the default)
            only the '[i/N] Processing ...' progress line is printed per
            subfolder; the individual step messages are hidden.
h |  | print this help page.
"""

USAGE_TAIL = """
    
    Default behavior
    ----------------
    requires per desired output file one subfolder in input
    each subfolder respectively needs to have only mp3, opus, aac or flac audio
    those can be in various recursive folders
    the naming of those files and subfolders should reflect the desired order

    Chapters
    --------
    priority: retrieve chapters from a cue file
      * one cue sheet in the folder (recursively): always used
      * several: prefer the cue next to the audio file sharing its name,
        else fall back to the largest cue sheet
    fallback: concat files and build chapters from individual files

    Thumbnail
    ---------
    embed cover from image file(s) in input folder
    fallback: extract from pdf documents instead
    fallback: extract from audio files themselves

    Dependencies
    ------------
    ffmpeg, imagemagick, wc, mutagen. Optional: mkvtoolnix (chapters and titles
    for MP3/m4b output - without it the concatenation still succeeds, unchaptered),
    pdftoppm (renders a booklet PDF's first page as the cover; without it the
    embedded cover art is used instead), flock (numbers the progress lines)"""

OPT_VARS = "p:pretreat v:verbose"
OPT_COLUMN = 12
OPT_LONG = "p:pretreat v:verbose h:help"

IMAGE_SIZE_LIMIT = 700000
DPI = 300
JPG_QUALITY_LEVEL = 80

# The tier the embedded thumbnail is scaled down to. Above the table's default on
# purpose: this cover is the only picture an audiobook has, and a player showing
# it full-screen on a tablet is the case that decides the size.
THUMBNAIL_RESOLUTION_TIER = "quadHD"

# One job per four CPU threads, matching the other scripts.
JOBS_PER_CORE = 4

# The join, per format: which extension the sources have, which container comes
# out, and how the two are joined. mp3 and opus are already container-framed, so
# their streams are copied verbatim and the total duration comes out right. FLAC
# is different: copying its frames leaves the FIRST file's STREAMINFO in place,
# so the result reports only the first segment's length and players hide every
# chapter past it. Re-encoding recomputes the header, and FLAC being lossless
# that costs CPU rather than quality.
FORMATS = {
    "mp3": ("mp3", "mp3", "demuxer", "copy"),
    "opus": ("opus", "opus", "demuxer", "copy"),
    "flac": ("flac", "flac", "demuxer", "flac"),
    "aac": ("aac", "m4b", "rawRemux", "copy"),
}

# The order the formats are counted in, which decides the label a single-format
# folder is announced with.
FORMAT_ORDER = (("mp3", "MP3"), ("opus", "Opus"), ("aac", "AAC"),
                ("flac", "FLAC"))


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
        tail=USAGE_TAIL,
    )


def _run(argv, **kwargs):
    return subprocess.run(argv, **kwargs)


def select_cue_sheet(cues: list, audio: str) -> str:
    """Which cue sheet a folder's chapters are read from.

    1. exactly one anywhere in the tree: always that one;
    2. several: the one beside the audio file and sharing its stem;
    3. several and none matches: the largest, as the fullest chapter source.
    """
    cues = [cue for cue in cues if cue]
    if len(cues) <= 1:
        return cues[0] if cues else ""

    audio_dir = os.path.dirname(audio)
    audio_stem = os.path.splitext(os.path.basename(audio))[0]
    for cue in cues:
        if (os.path.dirname(cue) == audio_dir
                and os.path.splitext(os.path.basename(cue))[0] == audio_stem):
            return cue

    # -1 is the "nothing seen yet" mark: a cue file can be 0 bytes, so 0 would
    # not tell the two apart.
    best, best_size = "", -1
    for cue in cues:
        try:
            size = os.path.getsize(cue)
        except OSError:
            size = 0
        if size > best_size:
            best, best_size = cue, size
    return best


class Scan:
    """What one sub-folder holds, from ONE directory walk instead of four.

    The case sensitivity differs per extension and is deliberate: the audio
    extensions match exactly, as `-name` did, while a cue matches either case, as
    `-iname` did.
    """

    def __init__(self, root: str) -> None:
        self.counts = {"mp3": 0, "opus": 0, "aac": 0, "flac": 0}
        self.cues: list = []
        self.audio_file = ""
        for parent, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(parent, name)
                for extension in self.counts:
                    if name.endswith("." + extension):
                        self.counts[extension] += 1
                        self.audio_file = path
                if name.lower().endswith(".cue"):
                    self.cues.append(path)

    @property
    def audio_files(self) -> int:
        return sum(self.counts.values())


def _sorted_by_extension(folder: str, extension: str) -> list:
    """The folder's files of one extension, in the order `sort -V` puts them."""
    found = []
    for parent, _dirs, names in os.walk(folder):
        for name in names:
            if name.lower().endswith("." + extension.lower()):
                found.append(os.path.join(parent, name))
    return sorted(found, key=version_key)


def concat_via_demuxer(folder: str, source_extension: str, output_file: str,
                       audio_codec: str, ffmpeg: str, run=_run) -> list:
    """ffmpeg's concat demuxer, for streams that already carry a container.

    Returns the ordered list of concat entries, so the chapter builder can reuse
    the exact order the join used.
    """
    files = _sorted_by_extension(folder, source_extension)
    if not files:
        return []
    concat_list = ["file '%s'" % path for path in files]

    codec = ["-codec", "copy"] if audio_codec == "copy" \
        else ["-c:a", audio_codec]
    # The list reaches ffmpeg through a pipe rather than a temp file, which is
    # what the shell's process substitution does.
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(write_fd, "w") as handle:
            handle.write("\n".join(concat_list) + "\n")
        run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
             "-safe", "0", "-f", "concat", "-i", "/dev/fd/%d" % read_fd]
            + codec + [output_file], pass_fds=(read_fd,))
    finally:
        os.close(read_fd)
    return concat_list


def concat_via_raw_remux(folder: str, base_name: str, source_extension: str,
                         output_file: str, ram_dir: str, ffmpeg: str,
                         run=_run) -> list:
    """Raw bytes joined and re-wrapped, for ADTS aac - which has no container to
    demux. The sources are joined in RAM and removed once they are in the m4b."""
    files = _sorted_by_extension(folder, source_extension)
    if not files:
        return []

    temporary = os.path.join(ram_dir, "%s.%s" % (os.path.basename(base_name),
                                                 source_extension))
    with open(temporary, "wb") as target:
        for path in files:
            with open(path, "rb") as source:
                shutil.copyfileobj(source, target)
    run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-i", temporary, "-acodec", "copy", "-bsf:a", "aac_adtstoasc",
         output_file])
    os.remove(temporary)
    for path in files:
        try:
            os.remove(path)
        except OSError:
            pass
    return ["file '%s'" % path for path in files]


def concat_format(folder: str, base_name: str, fmt: str, ram_dir: str,
                  ffmpeg: str, run=_run) -> list:
    """One sub-folder joined, by whichever strategy its format calls for."""
    source_extension, output_extension, strategy, audio_codec = FORMATS[fmt]
    output_file = "%s.%s" % (base_name, output_extension)
    if strategy == "demuxer":
        return concat_via_demuxer(folder, source_extension, output_file,
                                  audio_codec, ffmpeg, run)
    return concat_via_raw_remux(folder, base_name, source_extension,
                                output_file, ram_dir, ffmpeg, run)


def name_output_files(input_dir: str, output_dir: str) -> tuple:
    """The input sub-folders and the output path each one becomes."""
    input_paths = sorted(
        (entry.path for entry in os.scandir(input_dir) if entry.is_dir()),
        key=version_key)
    if not input_paths:
        return [], []

    prefixes, names = [], []
    for path in input_paths:
        name = os.path.basename(path)
        # Audio-specific: space out chapter words so "Track01" becomes
        # "Track 01" (and "Piste01" likewise). This is text to EXTEND rather
        # than a fragment to remove, and it is specific to audio, so it lives
        # here rather than in the shared cleaner. It makes the common
        # "Track"/"Piste" prefix easy to crop collectively in the next pass; a
        # doubled space is collapsed by the trimming inside the cleaner.
        name = name.replace("Track", "Track ").replace("Piste", "Piste ")
        prefix, cleaned = cleannamesindividually.clean_names_individually(name)
        prefixes.append(prefix)
        names.append(cleaned)

    # The collective pass only means anything with more than one folder.
    if len(input_paths) > 1:
        clean_names = cleannamescollectively.clean_names_collectively(names)
    else:
        clean_names = list(names)

    # A prefix that was blocked by common text in front of it can be split off
    # now that the text is gone.
    if not prefixes[0]:
        prefixes, cleaner_names = [], []
        for name in clean_names:
            prefix, cleaned = cleannamesindividually.clean_names_individually(
                name)
            prefixes.append(prefix)
            cleaner_names.append(cleaned)
    else:
        cleaner_names = list(clean_names)

    output_paths = []
    for index, name in enumerate(cleaner_names):
        prefix = prefixes[index]
        # A dot in a folder name would not make an acceptable file name.
        name = name.replace(".", "")
        path = os.path.join(output_dir,
                            "%s %s" % (prefix, name) if prefix else name)
        output_paths.append(path.replace("//", "/"))

    return input_paths, output_paths


def pretreat_input(input_dir: str, ffmpeg: str, skips: safety.SkipLog,
                   run=_run) -> None:
    """Only the known problems in names and paths, with as little interference
    as possible."""
    # Folders breadth-first, so a tree needing several levels cleaned is cleaned
    # from the top down and each level still carries the path it was found at.
    depths: dict[int, list[str]] = {}
    for parent, dirs, _names in os.walk(input_dir):
        for name in dirs:
            path = os.path.join(parent, name)
            depths.setdefault(
                path.count(os.sep) - input_dir.rstrip("/").count(os.sep), [])
    for depth in sorted(depths):
        for folder in _directories_at_depth(input_dir, depth):
            safety.safe_rename(folder, safety.clean_input_path(folder), skips)

    for path in _files_matching(input_dir,
                                ("opus", "mp3", "m4a", "m4b", "aac")):
        safety.safe_rename(path, safety.clean_input_path(path), skips)

    safety.lower_case_extensions(input_dir, skips)

    for path in _files_matching(input_dir, ("m4b",)):
        safety.safe_rename(path, os.path.splitext(path)[0] + ".m4a", skips)

    for path in _files_matching(input_dir, ("m4a",)):
        output = os.path.splitext(path)[0] + ".aac"
        if not os.path.isfile(output):
            run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", path, "-acodec", "copy", output])

    for path in _files_matching(input_dir, ("jpeg",)):
        safety.safe_rename(path, os.path.splitext(path)[0] + ".jpg", skips)


def _directories_at_depth(root: str, depth: int) -> list:
    found = []
    root_depth = root.rstrip("/").count(os.sep)
    for parent, dirs, _names in os.walk(root):
        for name in dirs:
            path = os.path.join(parent, name)
            if path.count(os.sep) - root_depth == depth:
                found.append(path)
    return found


def _files_matching(root: str, extensions) -> list:
    """Every file under <root> whose extension is one of these, either case."""
    wanted = tuple("." + extension.lower() for extension in extensions)
    found = []
    for parent, _dirs, names in os.walk(root):
        for name in names:
            if name.lower().endswith(wanted):
                found.append(os.path.join(parent, name))
    return found


class Run:
    """One run's settings, and the per-sub-folder work that reads them."""

    # Declared, not defaulted: the settings dict supplies every one, so a name
    # it does not carry is still an AttributeError at the read.
    verbose: bool
    ram_dir: str
    script_dir: str
    ffmpeg: str
    have_mkvtoolnix: bool
    # A geometry, not None: the tier is a row of the table by construction.
    thumbnail_resolution: str
    progress_file: str
    total: int

    def __init__(self, **settings) -> None:
        self.__dict__.update(settings)

    def report_progress(self, name: str) -> None:
        """The one status line kept per folder, atomic and ordered across the
        parallel workers - so it is printed directly rather than through log(),
        which quiet mode silences."""
        with open(self.progress_file + ".lock", "w") as lock, \
                runlog.take_lock(lock):
            try:
                with open(self.progress_file) as handle:
                    current = int(handle.read().strip() or 0) + 1
            except (OSError, ValueError):
                current = 1
            with open(self.progress_file, "w") as handle:
                handle.write("%d\n" % current)
            sys.stderr.write('==> %sProcessing "%s"\n' % (
                runlog.counted_prefix(current, self.total), name))

    def process_subfolder(self, input_path: str, output_path: str) -> None:
        chapter_file = output_path.rstrip("/") + ".ch"
        self.report_progress(os.path.basename(output_path.rstrip("/")))

        # Quiet mode (the default) silences the per-step lines from here on.
        # Each sub-folder is its own worker, so this reaches only that worker
        # and the helpers it calls - never the run's own top-level lines. The
        # progress line above is printed directly, so it stays visible.
        step = log if self.verbose else (lambda _message: None)

        scan = Scan(input_path)

        # Joined only when the folder holds EXACTLY ONE format: mp3, opus, aac
        # and flac streams can never be joined with each other.
        present = [(fmt, label) for fmt, label in FORMAT_ORDER
                   if scan.counts[fmt] > 0]
        concat_list = []
        joined = False
        if len(present) == 1:
            fmt, label = present[0]
            step("    Concatenating %d %s file(s)" % (scan.counts[fmt], label))
            concat_list = concat_format(input_path, output_path, fmt,
                                        self.ram_dir, self.ffmpeg)
            joined = True
        elif scan.audio_files > 0:
            # A graceful skip, logged rather than silent: the other sub-folders
            # keep processing.
            step("    Skipping: mixed audio formats (mp3:%d opus:%d aac:%d "
                 "flac:%d) cannot be concatenated"
                 % (scan.counts["mp3"], scan.counts["opus"],
                    scan.counts["aac"], scan.counts["flac"]))

        if not joined:
            return

        # Chapters, one of two ways. A cue sheet wins when there is one and the
        # music is not yet split - which means a CD1/CD2 input needs one folder
        # per disc.
        chapter_lines = []
        if scan.cues and scan.audio_files == 1:
            cue = select_cue_sheet(scan.cues, scan.audio_file)
            step("    Reading chapters from cue sheet")
            chapter_lines = cuechapters.chapters_from_cue(cue)
        elif scan.audio_files >= 2:
            step("    Building chapters from individual files")
            chapter_lines = chapters.chapters_from_files(concat_list)

        if chapter_lines:
            step("    Embedding chapters")
            chapters.embed_chapters(chapter_file, chapter_lines, self.ram_dir,
                                    self.script_dir, self.have_mkvtoolnix)

        step("    Embedding thumbnail")
        thumbnails.embed_thumbnail(input_path, output_path, IMAGE_SIZE_LIMIT,
                                   JPG_QUALITY_LEVEL, self.thumbnail_resolution,
                                   DPI, self.have_mkvtoolnix, self.ram_dir,
                                   self.script_dir)


def _in_worker(state: Run, input_path: str, output_path: str) -> None:
    """One sub-folder, in a worker PROCESS.

    The worker's interrupt handling is installed here rather than in the work
    itself, because at width 1 that same work runs in the RUN's own process -
    where the worker's handler would replace the run's, and a Ctrl+C would leave
    with the queue-stopping status instead of the run's own and without its
    closing report.
    """
    safety.trap_worker_abort()
    state.process_subfolder(input_path, output_path)


def _run_pool(state: Run, pairs: list, jobs: int) -> None:
    if jobs <= 1:
        for input_path, output_path in pairs:
            if safety.abort_requested():
                return
            state.process_subfolder(input_path, output_path)
        return

    workerpool.run(pairs, jobs, _in_worker,
                   lambda pair: (state, pair[0], pair[1]))


def _has_audio_subfolder(input_dir: str) -> bool:
    """Whether any immediate sub-folder holds audio, at any depth below it.

    This script makes ONE output file per immediate sub-folder, so an input with
    none has nothing for it - even when the folder is full of loose audio files,
    which belong one level deeper. Audio ANYWHERE under a sub-folder counts,
    because the join gathers a sub-folder's tracks recursively: a book split
    across per-disc folders is one output file.
    """
    wanted = tuple("." + extension.lower()
                   for extension in enums.AUDIO_EXTENSIONS)
    for entry in os.scandir(input_dir):
        if not entry.is_dir():
            continue
        for _parent, _dirs, names in os.walk(entry.path):
            if any(name.lower().endswith(wanted) for name in names):
                return True
    return False


def main(argv: list, program: str = "concat-audio",
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

    pretreat = "p" in result.given
    verbose = "v" in result.given
    input_dir, output_dir = result.positionals[0], result.positionals[1]

    script_dir = script_dir or commands.script_dir()

    if not os.path.isdir(input_dir):
        sys.stdout.write(clioptions.missing_dir_text(declaration, input_dir))
        return 1

    # The two folders, checked before anything runs: the concatenated book is
    # audio in a folder of audio, so written inside the input it would be joined
    # into the next run's output - and -p renames the very tree it would sit in.
    if safety.require_separate_output(input_dir, output_dir):
        return 1

    # Which of this machine's ffmpeg builds does the work, before the preflight
    # below asks whether PATH can reach one.
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()

    # pdftoppm is deliberately not required, though the dependency list names
    # it: a booklet PDF is only the preferred of several cover sources, so the
    # thumbnail step falls through to the embedded artwork with a warning rather
    # than costing the whole run. mkvtoolnix is not required either - the Opus
    # and FLAC outputs of the same run write their chapters with mutagen and
    # never touch it, so a host without it loses only the MP3/m4b chapters and
    # titles, said once at startup rather than refusing a run that can do
    # everything else.
    if tooldeps.require_tools(program, ["ffmpeg", "ffprobe",
                                        imagemagick.CONVERT_SPEC]):
        return 1
    if tooldeps.require_python_module(
            "mutagen", program,
            "writes the chapter marks and the cover art into the concatenated "
            "book"):
        return 1
    runlog.warn_uncounted_progress()

    have_mkvtoolnix = _settle_mkvtoolnix()

    if os.path.isdir(output_dir):
        for parent, _dirs, names in os.walk(output_dir):
            for name in names:
                if name.endswith(".ch"):
                    os.remove(os.path.join(parent, name))
    else:
        os.makedirs(output_dir, exist_ok=True)

    # The bigger intermediary audio and image files go to RAM so they put no
    # write load on the SSD; only the finished output files reach the disk.
    ramscratch.init_ram_base()
    ram_dir, status = ramscratch.ram_scratch_dir("concatAudio")
    if status != 0 or not ram_dir:
        sys.stderr.write("\nError: no scratch directory could be made for this "
                         "run.\nNothing was changed.\n")
        return 1
    ramscratch.add_exit_cleanup([ram_dir])

    try:
        return _run_with_scratch(ram_dir, input_dir, output_dir, pretreat,
                                 verbose, program, script_dir, have_mkvtoolnix)
    finally:
        # The shell's `trap 'runExitCleanup' EXIT`: however this run ends, the
        # scratch goes back to the tmpfs rather than staying there.
        ramscratch.run_exit_cleanup()


def _run_with_scratch(ram_dir: str, input_dir: str, output_dir: str,
                      pretreat: bool, verbose: bool, program: str,
                      script_dir: str, have_mkvtoolnix: bool) -> int:
    safety.init_safety_log(os.path.join(ram_dir, "safetySkips.log"))
    skips = safety.RunSkipLog()
    # Shared with the parallel workers, so one Ctrl+C stops the whole run rather
    # than letting the queue grind through the rest of it.
    safety.init_abort_flag(os.path.join(ram_dir, "abortRequested"))
    safety.trap_run_abort()

    # The closing report: the safety recap, which only pretreatment has anything
    # to put in. Named above the concatenation the run can be cut short in, so a
    # Ctrl+C still says which renames were held back before it stopped.
    safety.set_run_footer(
        lambda: safety.report_safety_skips() if pretreat else None)

    try:
        os.chdir(input_dir)
    except OSError:
        return 1

    # Checked before pretreatment, which would otherwise rename an input the run
    # is about to refuse.
    if not _has_audio_subfolder(input_dir):
        return safety.fail_no_relevant_input(
            input_dir,
            "sub-folders each holding the audio files (%s) of one output file"
            % enums.extension_list(list(enums.AUDIO_EXTENSIONS)))

    if pretreat:
        log('Pretreating input folders in "%s"' % input_dir)
        pretreat_input(input_dir, "ffmpeg", skips)
    else:
        log("Skipping input pretreatment (enable with -p)")

    log("Determining output file names")
    input_paths, output_paths = name_output_files(input_dir, output_dir)

    total = len(input_paths)
    progress_file = os.path.join(ram_dir, "concat.progress")
    with open(progress_file, "w") as handle:
        handle.write("0\n")

    jobs = max(1, runlog.cpu_count() // JOBS_PER_CORE)
    log("Concatenating %d subfolder(s), up to %d at a time" % (total, jobs))

    state = Run(
        verbose=verbose,
        ram_dir=ram_dir,
        script_dir=script_dir,
        ffmpeg="ffmpeg",
        have_mkvtoolnix=have_mkvtoolnix,
        thumbnail_resolution=imagesizes.geometry(THUMBNAIL_RESOLUTION_TIER),
        progress_file=progress_file,
        total=total,
    )

    if total:
        _run_pool(state, list(zip(input_paths, output_paths, strict=True)), jobs)
        safety.exit_if_aborted()

    log("Cleaning up temporary files")
    for path in (progress_file, progress_file + ".lock"):
        try:
            os.remove(path)
        except OSError:
            pass
    for parent, _dirs, names in os.walk(output_dir):
        for name in names:
            if name.endswith(".ls") or name.endswith(".jpg"):
                os.remove(os.path.join(parent, name))
    # Deepest first, so a folder that only becomes empty once its own empty
    # children are gone goes too. The output folder the user named stays, even
    # when nothing landed in it.
    for parent, _dirs, _names in os.walk(output_dir, topdown=False):
        if parent == output_dir:
            continue
        try:
            os.rmdir(parent)
        except OSError:
            pass

    log('Done. Output written to "%s"' % output_dir)
    safety.print_run_footer()

    # This run's scratch back now rather than at exit: a wrapper runs this
    # script once per sub-folder, and waiting would keep every sub-folder's
    # scratch resident at once - which is the tmpfs the run is trying not to
    # fill. Only this run's own directory, never the whole list: the wrapper's
    # is on it too, and that is the tree the NEXT sub-folder is read from.
    ramscratch.release_exit_cleanup([ram_dir])
    return 0


def _settle_mkvtoolnix() -> bool:
    """Settled once and shared with the workers through the environment; a
    wrapper's own settlement, which its run reached first, is inherited rather
    than said again."""
    inherited = os.environ.get("HAVE_MKVTOOLNIX")
    if inherited is not None:
        return bool(inherited)

    present = all(tooldeps.tool_present(tool) for tool in
                  ("mkvmerge", "mkvpropedit", "mkvextract"))
    os.environ["HAVE_MKVTOOLNIX"] = "1" if present else ""
    if not present:
        log("WARNING: mkvtoolnix not found (apt install mkvtoolnix) - chapters "
            "and titles cannot be embedded in")
        log("         MP3 and m4b output (Opus and FLAC are written by mutagen "
            "and do not need it), and cover")
        log("         art embedded in opus sources is left unextracted (sidecar "
            "images still work). The")
        log("         concatenation itself is unaffected.")
    return present


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
