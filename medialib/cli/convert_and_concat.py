"""convert-and-concat: a tree of audiobooks transcoded and joined, in four
phases, with the bulk of the work in RAM.

    1  clean the input's names   (-i, clean-folder-structure)
    2  unpack the archives standing in for folders
    3  transcode to opus         (convert-audio)
    4  concatenate               (concat-audio)

The intermediate opus tree lives entirely in RAM and only the finished book
reaches the disk. The three phases that are other scripts run as CHILD
processes: they inherit the run directory through the environment, and a phase
that exits non-zero ends this run rather than being skipped.

A BOOK is a folder whose audio is all one type, or an archive standing in for
one. That is asked of the input before the run spends itself on it: mp3 and flac
in one folder can never be joined into a single file, so transcoding such a tree
would be work with nothing to show for it.
"""

import os
import shutil
import sys

from medialib import commands
from medialib.lib import (
    archives,
    clioptions,
    enums,
    ffmpegselect,
    imagemagick,
    ramscratch,
    runlog,
    safety,
    tooldeps,
)
from medialib.lib.runlog import log

# The spec is DATA, and the page it renders is compared byte for byte against the
# recorded contract under tests/data/cliContract.
CREDITS = "convert-and-concat 0.1"

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>

    inputDir      the folder whose subfolders are ingested and concatenated.
    outputDir     where the finished books are written. Created if missing, and
                  refused if it lies inside inputDir. A trailing slash on either
                  is tolerated.

    One output file is made per subfolder of inputDir - or per subfolder of each
    of those, with -s. An ARCHIVE lying where such a folder would (.zip, .rar,
    .7z, .tar and the compressed tars) counts as one: it is unpacked into RAM
    under its own name minus the extension, and ingested from there like a folder
    of that name. An archive lying beside a FOLDER of the same name is left alone,
    on the assumption it has already been unpacked there.

    The intermediate transcoded \"opus\" tree is kept entirely in RAM
    (/dev/shm), so only the final output folder is written to disk.

    OPTIONS
    ======="""

OPT_SPEC = """
m |  | mono output, lower default bitrate.
s |  | iterate through different input subfolders independently
c |  | only concat
b |  | specify bitrate
i |  | clean input folders
"""

OPT_VARS = "m:mono s:subFolders c:onlyConcat b:bitrate i:inputClean"
OPT_FLAGS = "arg:b"
OPT_COLUMN = 12
OPT_LONG = "m:mono s:sub-folders c:only-concat b:bitrate i:clean-input"


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        flags=OPT_FLAGS,
        column=OPT_COLUMN,
        credits=CREDITS,
    )


def progress(*message) -> None:
    """The wrapper's own progress line.

    Named for what it is rather than `log`: the phases keep their own verbosity
    and their own log(), and this is the layer above them.
    """
    import time
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"),
                                    " ".join(str(part) for part in message)))


def _audio_names():
    return {e.lower() for e in enums.AUDIO_EXTENSIONS}


def _ingestible_names():
    return ({e.lower() for e in enums.AUDIO_EXTENSIONS}
            | {e.lower() for e in enums.VIDEO_EXTENSIONS})


def holds_one_audio_type(directory: str) -> bool:
    """True when <directory> holds audio, recursively, and every audio file in
    it is ONE type - which is what can be joined into a single file."""
    wanted = _audio_names()
    kinds = set()
    for _parent, _dirs, names in os.walk(directory):
        for name in names:
            extension = enums.lower_extension_of(name)
            if extension in wanted:
                kinds.add(extension)
    return len(kinds) == 1


def holds_ingestible_audio(directory: str, depth=None) -> bool:
    """True when <directory> holds a file the transcoding phase would ingest -
    audio, or video to take the audio from.

    Asked in two shapes: over a whole tree, to decide whether it is worth
    handing to the transcoder at all, and one level deep, which is what makes a
    folder ONE output file.
    """
    wanted = _ingestible_names()
    for _parent, dirs, names in os.walk(directory):
        for name in names:
            if enums.lower_extension_of(name) in wanted:
                return True
        if depth == 1:
            del dirs[:]
    return False


def holds_concat_unit(directory: str) -> bool:
    """True when <directory> has at least one immediate subfolder with audio
    anywhere under it - exactly what the concatenator turns into an output file,
    and what it REFUSES a folder for when it finds none.

    Asked before handing a folder over, because that refusal is an exit: right
    for a script called on its own, fatal for a wrapper that walks a tree. A
    folder with no book in it - loose covers, notes - is one folder to pass over,
    not a reason to end a run that has other folders to finish.
    """
    try:
        with os.scandir(directory) as entries:
            children = [entry.path for entry in entries
                        if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return False
    return any(holds_ingestible_audio(child) for child in children)


def input_holds_a_book(in_path: str, sub_folders: bool) -> bool:
    """True when the input holds a book at this run's book depth: the input's
    immediate subfolder, or its sub-subfolder with -s."""
    depth = 2 if sub_folders else 1
    for candidate in _at_depth(in_path, depth, want_dirs=True):
        if holds_one_audio_type(candidate):
            return True
    archive_names = {e.lower() for e in enums.ARCHIVE_EXTENSIONS}
    for candidate in _at_depth(in_path, depth, want_dirs=False):
        if enums.lower_extension_of(candidate) in archive_names:
            return True
    return False


def _at_depth(root: str, depth: int, want_dirs: bool):
    """What `find -mindepth N -maxdepth N` lists, sorted the way `sort -z` does."""
    level = [root]
    for _ in range(depth - 1):
        below = []
        for parent in level:
            below.extend(_children(parent, want_dirs=True))
        level = below
    found = []
    for parent in level:
        found.extend(_children(parent, want_dirs=want_dirs))
    return sorted(found, key=os.fsencode)


def _children(directory: str, want_dirs: bool):
    try:
        with os.scandir(directory) as entries:
            return [entry.path for entry in entries
                    if entry.is_dir(follow_symlinks=False) == want_dirs]
    except OSError:
        return []


class Staging:
    """Phase 2's tree: every archive standing in for a folder, unpacked into one
    of its own name."""

    def __init__(self, path: str):
        self.path = path
        self.unpacked = 0

    def unpack_in(self, parent: str, relative_dir: str = "") -> None:
        """Unpack every archive lying directly in <parent>."""
        # Per parent folder, because the collisions it resolves are between
        # siblings.
        claimed_by: dict[str, str] = {}
        for path in sorted(_children(parent, want_dirs=False), key=os.fsencode):
            if not archives.is_archive_file(path):
                continue
            base = archives.archive_base_name(path)
            name = os.path.basename(path)
            # A folder of the same name beside it wins.
            if archives.archive_shadowed_by_folder(path):
                progress('  "%s" is already a folder here, leaving its archive '
                         "alone" % base)
                continue
            # And so does the first of two archives that would claim one name (a
            # .zip and a .rar of the same book), which would otherwise unpack
            # into a single folder and mix their tracks. Sorted order, so which
            # one that is does not depend on how the file system lists them.
            if base in claimed_by:
                progress('  skipping "%s": "%s" already stands in for "%s"'
                         % (name, claimed_by[base], base))
                continue
            dest = os.path.join(self.path, relative_dir, base) if relative_dir \
                else os.path.join(self.path, base)
            if archives.extract_archive_as_folder(path, dest) != 0:
                progress("  could not unpack, skipping: " + name)
                continue
            claimed_by[base] = name
            self.unpacked += 1
            progress("  unpacked: %s -> %s" % (name, base))
            # Said here rather than left to look like an encoder that skipped
            # it: an archive of covers or notes unpacks into a folder with no
            # audio under it at all, which is not what one output file is made
            # of - exactly as it would not be had it arrived unpacked.
            if not holds_ingestible_audio(dest):
                progress('  ... but "%s" holds no audio, so nothing is '
                         "concatenated from it" % base)


def _trap_record_only() -> None:
    import signal

    def handler(_number, _frame):
        safety.request_abort()

    for number in safety.interrupt_signals():
        try:
            signal.signal(number, handler)
        except (OSError, ValueError):
            pass


def _stop_if_interrupted() -> None:
    """The boundary between phases: if the run was interrupted while the last
    one ran, stop here - after that phase has finished tidying up."""
    if not safety.abort_requested():
        return
    sys.stderr.write("\nInterrupted - stopping.\n")
    safety.print_run_footer()
    raise SystemExit(safety.INTERRUPTED_EXIT_STATUS)


def _run_phase(command: str, argv, script_dir: str = "") -> None:
    """One phase that is another command, as a CHILD.

    A phase that refuses ends this run rather than being skipped, which is what
    the shell's `set -e` did when these were sourced - so a non-zero status is
    raised here rather than returned.
    """
    if commands.run_command(command, argv,
                            script_dir=script_dir).returncode != 0:
        # A phase shares this run's abort flag, so an interrupt stops it too and
        # it ends non-zero because of that. That is this run being stopped, not
        # the phase refusing the work.
        _stop_if_interrupted()
        raise SystemExit(1)


def _copy_cue_files(source: str, destination: str) -> None:
    """The cue sheets, into the transcoded tree beside the audio they describe.

    Only the .cue files and the directories on the way to them, which is what
    `rsync -rm --include='*/' --include='*.cue' --exclude='*'` copies.
    """
    for parent, _dirs, names in os.walk(source):
        for name in names:
            if enums.lower_extension_of(name) != "cue":
                continue
            relative = os.path.relpath(os.path.join(parent, name), source)
            target = os.path.join(destination, relative)
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            shutil.copyfile(os.path.join(parent, name), target)


def main(argv: list, program: str = "convert-and-concat",
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

    mono = "m" in result.given
    sub_folders = "s" in result.given
    only_concat = "c" in result.given
    input_clean = "i" in result.given
    bitrate = result.values["bitrate"]
    script_dir = script_dir or commands.script_dir()

    in_path = result.positionals[0].rstrip("/")
    out_path = result.positionals[1].rstrip("/")
    if not os.path.isdir(in_path):
        sys.stdout.write(clioptions.missing_dir_text(declaration, in_path))
        return 1

    # Absolute, because the concatenating phase runs from INSIDE the tree it
    # reads and takes its work list from a walk of it: a relative path handed on
    # from there would be read against the new working directory. Resolved after
    # the existence check, so that message still quotes the path as typed.
    in_path = os.path.abspath(in_path)
    out_path = os.path.abspath(out_path)

    # The concatenating phase writes the same kind of file the transcoding one
    # reads, so an output inside the input would hand the next run its own books.
    if safety.require_separate_output(in_path, out_path):
        return 1

    if not input_holds_a_book(in_path, sub_folders):
        where = ("a subfolder of a subfolder of the input" if sub_folders
                 else "a subfolder of the input")
        sys.stderr.write("\n")
        sys.stderr.write('Nothing to concatenate: no %s in "%s" holds a '
                         "book.\n" % (where, in_path))
        sys.stderr.write("A book is a folder whose audio files are all one type "
                         "(all .mp3, all .opus, ...),\nor an archive standing "
                         "in for one; a folder mixing two types can never be\n"
                         "joined into a single file, so converting it would be "
                         "work with nothing to show.\n")
        sys.stderr.write("Nothing was changed.\n")
        return 1

    ramscratch.init_ram_base()
    temp_path = in_path
    stage = None
    try:
        if not only_concat:
            temp_path, status = ramscratch.ram_scratch_dir("convertAndConcat")
            if status != 0 or not temp_path:
                sys.stderr.write("\nError: no scratch directory could be made "
                                 "for this run.\nNothing was changed.\n")
                return 1
            ramscratch.add_exit_cleanup([temp_path])
        safety.init_abort_flag()
        # RECORDS the interrupt; the phase that is running finishes and is
        # stopped at the next boundary. bash defers a trap until the foreground
        # command returns, so a sourced - or spawned - phase always got to
        # finish, print its own closing report and hand its scratch back. A
        # handler that left immediately would strand the child mid-write and
        # leak its scratch, which is exactly what the interrupt contract asserts
        # against.
        _trap_record_only()
        runlog.settle_flock()

        archive_depth = 2 if sub_folders else 1
        archive_types, archive_tools = _archives_present(in_path, archive_depth)

        _summarise(in_path, out_path, temp_path, only_concat, mono, bitrate,
                   sub_folders, archive_types)
        if sub_folders:
            _warn_top_level_archives(in_path)

        # The external tools of BOTH called scripts, asked for here once: they
        # start in the transcoding and concatenating phases, and the transcode is
        # a full pass over the input, so a tool only the concatenator needs would
        # otherwise be reported after the long part of the run.
        chain = ["ffmpeg", "ffprobe", imagemagick.CONVERT_SPEC]
        if not only_concat:
            chain += ["rsync"]
        chain += archive_tools
        skip = bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))
        ffmpegselect.select_ffmpeg()
        ffmpegselect.report_ffmpeg_selection()
        if tooldeps.require_tools(program, chain,
                                  skip_preflight=skip):
            return 1
        runlog.warn_uncounted_progress()
        _settle_mkvtoolnix()
        if tooldeps.require_python_module(
                "mutagen", program,
                "writes the chapter marks and the cover art into the "
                "concatenated book", skip_preflight=skip):
            return 1

        # PHASE 1
        if not input_clean:
            progress("PHASE 1/4: skipped")
        else:
            progress("PHASE 1/4: cleaning input folder")
            _run_phase("clean-folder-structure", [in_path], script_dir)
        _stop_if_interrupted()

        # PHASE 2
        stage = _unpack_phase(in_path, archive_types, archive_depth)
        _stop_if_interrupted()

        # PHASE 3
        _transcode_phase(in_path, temp_path, stage, only_concat, mono, bitrate,
                         script_dir)
        _stop_if_interrupted()

        # PHASE 4
        _concat_phase(in_path, out_path, temp_path, stage, only_concat,
                      sub_folders, script_dir)

        if not only_concat or stage:
            progress("Cleaning up RAM temp folder")
            ramscratch.run_exit_cleanup()
        progress("Done. Output in: " + out_path)
        return 0
    finally:
        ramscratch.run_exit_cleanup()


def _settle_mkvtoolnix() -> None:
    """Settled once here, for BOTH phases, and exported: the two then inherit it
    instead of settling - and warning - it themselves, so the fact is said once
    per run. This wording is the UNION, because this is the one run that reaches
    both halves; the per-script wordings are for their standalone runs.

    A warning rather than a refusal: the transcoding half uses mkvtoolnix only to
    pull cover art out of a Matroska source, and the concatenating half only for
    the MP3/m4b chapter-and-title detour.
    """
    if "HAVE_MKVTOOLNIX" in os.environ:
        return
    if all(tooldeps.tool_present(tool)
           for tool in ("mkvmerge", "mkvpropedit", "mkvextract")):
        os.environ["HAVE_MKVTOOLNIX"] = "1"
        return
    os.environ["HAVE_MKVTOOLNIX"] = ""
    log("WARNING: mkvtoolnix not found (apt install mkvtoolnix) - in the "
        "transcoding phase, cover art")
    log("         embedded in Matroska sources will not be extracted (sidecar "
        "images still work); in the")
    log("         concatenating phase, chapters and titles cannot be embedded "
        "in MP3 and m4b output (Opus")
    log("         and FLAC go through mutagen) and cover art embedded in opus "
        "sources is left unextracted.")
    log("         The encoding and concatenation themselves are unaffected.")


def _archives_present(in_path: str, depth: int):
    """Which archive types lie at the book depth, asked once and reused: it
    decides both which extractors the preflight demands and whether phase 2 has
    anything to do. Only the types actually present are asked for - a folder of
    zips should not have to have unrar and 7-Zip installed to be read."""
    types, tools, seen = [], [], set()
    at_depth = {enums.lower_extension_of(path)
                for path in _at_depth(in_path, depth, want_dirs=False)}
    for extension in enums.ARCHIVE_EXTENSIONS:
        if extension.lower() not in at_depth:
            continue
        types.append(extension)
        for tool in archives.archive_tool_specs(extension).split():
            if tool not in seen:
                seen.add(tool)
                tools.append(tool)
    return types, tools


def _summarise(in_path, out_path, temp_path, only_concat, mono, bitrate,
               sub_folders, archive_types) -> None:
    progress("Input:   " + in_path)
    progress("Output:  " + out_path)
    progress("Temp:    " + temp_path)
    progress("Mode: %s | channels: %s | bitrate: %s | per-subfolder: %s"
             % ("only-concat" if only_concat else "transcode+concat",
                "mono" if mono else "stereo", bitrate or "default",
                "yes" if sub_folders else "no"))
    progress("Archives standing in for a folder: %s"
             % (" ".join(archive_types) if archive_types else "none"))


def _warn_top_level_archives(in_path: str) -> None:
    """With -s the archives that count lie one level further in, so a folder full
    of archives handed to -s would otherwise silently do nothing."""
    for path in _at_depth(in_path, 1, want_dirs=False):
        if archives.is_archive_file(path):
            progress("Ignoring the archives lying directly in the input: with "
                     "-s an archive stands in for a subfolder of a subfolder")
            return


def _unpack_phase(in_path: str, archive_types, depth: int):
    if not archive_types:
        progress("PHASE 2/4: no archives to unpack")
        return None
    progress("PHASE 2/4: unpacking archives (%s)" % " ".join(archive_types))
    path, status = ramscratch.ram_scratch_dir("convertAndConcat.archives")
    if status != 0 or not path:
        progress("PHASE 2/4: no scratch for the archives, unpacking nothing")
        return None
    ramscratch.add_exit_cleanup([path])
    progress("Unpacked to: " + path)
    stage = Staging(path)
    if depth == 1:
        stage.unpack_in(in_path)
    else:
        # -s: the archives that count lie beside the books INSIDE each
        # subfolder, so each is walked on its own and keeps its name in the
        # staging tree - that name is what this run's per-subfolder output is
        # named for.
        for group in _at_depth(in_path, 1, want_dirs=True):
            stage.unpack_in(group, os.path.basename(group))
    progress("PHASE 2/4: unpacked %d archive(s)" % stage.unpacked)
    if stage.unpacked == 0:
        # Every archive was shadowed, doubled or broken: hand the empty staging
        # tree back now, so the phases below carry on as they would without it.
        ramscratch.release_exit_cleanup([stage.path])
        return None
    return stage


def _transcode_phase(in_path, temp_path, stage, only_concat, mono, bitrate,
                     script_dir) -> None:
    if only_concat:
        progress("PHASE 3/4: skipped")
        return
    options = ["-m", "-c"] if mono else ["-c"]
    if bitrate:
        options += ["-b", bitrate]

    # One opus tree, fed from up to two input trees: the input as given, and the
    # unpacked archives. Each is handed over only when it holds something to
    # transcode, because the transcoder refuses an input with no audio in it at
    # all - and a phase that refuses ends this whole run rather than skipping one
    # tree. Both being empty is that same refusal, made here.
    inputs = []
    if holds_ingestible_audio(in_path):
        inputs.append(in_path)
    if stage and holds_ingestible_audio(stage.path):
        inputs.append(stage.path)
    if not inputs:
        raise SystemExit(safety.fail_no_relevant_input(
            in_path,
            "audio files (%s) or video files to take the audio from (%s), in "
            "its subfolders or in an archive standing in for one"
            % (enums.extension_list(list(enums.AUDIO_EXTENSIONS)),
               enums.extension_list(list(enums.VIDEO_EXTENSIONS)))))

    for source in inputs:
        progress("PHASE 3/4: transcoding to opus: " + source)
        _run_phase("convert-audio", options + [source, temp_path],
                   script_dir)
    progress("PHASE 3/4: transcoding complete")

    # The cue sheets, so the concatenation can use them. A book that arrived as
    # an archive brought its own along inside it, so that one is in the staging
    # tree rather than in the input.
    progress("Copying cue files for concatenation")
    _copy_cue_files(in_path, temp_path)
    if stage:
        _copy_cue_files(stage.path, temp_path)


def _concat_phase(in_path, out_path, temp_path, stage, only_concat,
                  sub_folders, script_dir) -> None:
    os.chdir(temp_path)
    # -p, because the given output can name a path several levels into an empty
    # target disk.
    os.makedirs(out_path, exist_ok=True)
    progress("PHASE 4/4: concatenating audio")

    # The trees the finished books are read from. One in the normal case:
    # whatever came out of an archive was transcoded into the opus tree
    # alongside the folders from disk. Two under -c, which builds no opus tree -
    # there the input is read as it lies.
    roots = [temp_path]
    if only_concat and stage:
        roots.append(stage.path)

    if sub_folders:
        # Collected first, so progress can be reported as "current/total".
        sub_dirs = []
        for root in roots:
            for sub_dir in _at_depth(root, 1, want_dirs=True):
                if holds_concat_unit(sub_dir):
                    sub_dirs.append(sub_dir)
                else:
                    progress('Passing over "%s": it holds no folder of audio'
                             % os.path.basename(sub_dir))
        if not sub_dirs:
            raise SystemExit(safety.fail_no_relevant_input(
                in_path,
                "subfolders whose own subfolders each hold the audio files "
                "(%s) of one output file, as folders or as archives standing "
                "in for them"
                % enums.extension_list(list(enums.AUDIO_EXTENSIONS))))
        progress("Found %d subfolder(s) to concatenate" % len(sub_dirs))
        for index, sub_dir in enumerate(sub_dirs, start=1):
            name = os.path.basename(sub_dir)
            # Sanitised, so the folder's name matches the files placed inside
            # it; the same path is handed over, so the created folder and the
            # output target match. The input keeps its real, raw name.
            safe = safety.clean_input_path(name)
            progress("Concatenating subfolder %d/%d: %s -> %s"
                     % (index, len(sub_dirs), name, safe))
            target = os.path.join(out_path, safe)
            os.makedirs(target, exist_ok=True)
            _run_phase("concat-audio", [sub_dir, target], script_dir)
            _stop_if_interrupted()
        return

    unit_roots = [root for root in roots if holds_concat_unit(root)]
    if not unit_roots:
        raise SystemExit(safety.fail_no_relevant_input(
            in_path,
            "subfolders each holding the audio files (%s) of one output file, "
            "as folders or as archives standing in for them"
            % enums.extension_list(list(enums.AUDIO_EXTENSIONS))))
    for root in unit_roots:
        progress("Concatenating single output folder: " + root)
        _run_phase("concat-audio", [root, out_path], script_dir)
        _stop_if_interrupted()


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
