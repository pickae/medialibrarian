"""transcribe-audio: an input tree of audio and video, transcribed by whisper
into a mirrored tree of transcripts.

A video is transcribed from its FIRST audio track only - lifted out to the 16 kHz
mono wav whisper resamples to anyway - so whisper never sees a second track or
the video stream. An audio file is handed over as it is.

The queue is ordered LARGEST FILE FIRST, because byte size is a cheap probe-free
proxy for how much speech there is: the long transcriptions start at the front
instead of one huge file landing last and pinning a worker to the end.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

from medialib import commands
from medialib.lib import (
    clioptions,
    enums,
    ffmpegselect,
    ramscratch,
    runlog,
    safety,
    tooldeps,
    whisper,
    workerpool,
)
from medialib.lib.formatting import fmt_hms
from medialib.lib.runlog import log

CREDITS = "transcribe an input folder of audio and video with whisper"

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>

Audio files are transcribed as they are. Video files are transcribed from their
FIRST audio track only - the video stream and any further audio tracks are ignored.
Each input gets one transcript in <outputDir>, in the same sub-folder structure:
    <inputDir>/a/track.mp3    ->   <outputDir>/a/track.txt
    <inputDir>/b/movie.mkv    ->   <outputDir>/b/movie.txt

The whisper model is picked for this host automatically (the GPU when whisper can
really run on it, otherwise the CPU), so there is nothing to configure.

Options:"""

FORMATS = ("txt", "srt", "vtt", "tsv")

OPT_SPEC = """
h |  | Print this help page.
f | <format> | Transcript format: txt, srt, vtt or tsv.
                    Default txt
j | <jobs> | Run up to <jobs> transcriptions in parallel.
                    Default 2
"""

# The format has to be one whisper-ctranslate2 knows, or it would only surface as
# a per-file failure deep into the run; the parallelism feeds the pool, where a
# bad number would only show up on the first worker. Both are settled here.
# The choices are joined with an ESCAPED pipe, because the spec is a wire format
# whose fields are separated by a plain one: `\|` is how a field says "a literal
# pipe of my own", and cliSpecField unescapes it. Written with a bare pipe, the
# kind ends at the first choice and every other format is refused.
OPT_CHECKS = """
f | enum:%s | transcript format
j | posInt | job count
""" % "\\|".join(FORMATS)

OPT_VARS = "f:fmt j:whisperJobs"
OPT_COLUMN = 20
OPT_LONG = "h:help f:format j:jobs"


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        checks=OPT_CHECKS,
        column=OPT_COLUMN,
        credits=CREDITS,
        tail="\n",
    )


def _is_video(name: str) -> bool:
    """The one case where a source's speech is not the file itself: a video
    container, whose first audio track has to be lifted out first."""
    return enums.lower_extension_of(name) in [
        enums.shell_lower(e) for e in enums.VIDEO_EXTENSIONS]


def _input_extensions():
    return (list(enums.AUDIO_EXTENSIONS) + list(enums.LOSSLESS_AUDIO_EXTENSIONS)
            + list(enums.VIDEO_EXTENSIONS))


def _tracks(input_dir: str):
    """Every input, largest first, as paths relative to the input folder.

    Ties keep the order the walk found them in, which is what a stable sort over
    the size does - the shell's ``sort -k1,1nr`` says nothing about them either.
    """
    wanted = {e.lower() for e in _input_extensions()}
    found = []
    for parent, _dirs, names in os.walk(input_dir):
        for name in names:
            full = os.path.join(parent, name)
            if not os.path.isfile(full):
                continue
            if os.path.splitext(name)[1].lstrip(".").lower() not in wanted:
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            found.append((size, os.path.relpath(full, input_dir)))
    found.sort(key=lambda item: -item[0])
    return [relative for _size, relative in found]


class Run:
    """One transcription run: the settled world its workers inherit."""

    def __init__(self, input_dir, output_dir, fmt, model, ram_root,
                 progress_file, total):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.fmt = fmt
        self.model = model
        self.ram_root = ram_root
        self.progress_file = progress_file
        self.total = total

    def report_progress(self, label: str) -> None:
        """Increment the shared counter and print one clean progress line.

        The counter is a FILE under a lock because the workers are separate
        processes; without flock it is not taken at all and the line comes out
        unnumbered, which is what the shell does too.
        """
        with open(self.progress_file + ".lock", "w") as handle, runlog.take_lock(handle):
            try:
                with open(self.progress_file) as counter:
                    current = int(counter.read() or "0") + 1
            except (OSError, ValueError):
                current = 1
            with open(self.progress_file, "w") as counter:
                counter.write("%d\n" % current)
            sys.stdout.write("%s%s\n" % (
                runlog.counted_prefix(current, self.total), label))

    def transcribe_one(self, relative: str) -> None:
        """One queued job: this input's speech into its mirrored transcript."""
        source = os.path.join(self.input_dir, relative)
        out = os.path.join(self.output_dir,
                           os.path.splitext(relative)[0] + "." + self.fmt)

        # whisper names its output after the input file, so every run gets a
        # directory of its own: the extracted track and the transcript both live
        # there, and it goes when the job does.
        work = tempfile.mkdtemp(prefix="whisper.", dir=self.ram_root)
        try:
            if _is_video(relative):
                for_whisper = os.path.join(work, "track.wav")
                extracted = subprocess.run(
                    ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                     "-nostats", "-i", source, "-map", "0:a:0", "-ac", "1",
                     "-ar", "16000", "-c:a", "pcm_s16le", for_whisper],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                if extracted.returncode != 0:
                    log("WARNING: no first audio track to transcribe: "
                        + relative)
                    self.report_progress("skipped (no audio): " + relative)
                    return
            else:
                for_whisper = source

            # Resume: a finished, non-empty transcript is kept where it is, so a
            # re-run - or one restarted after a Ctrl+C - does not pay for it again.
            if os.path.exists(out) and os.path.getsize(out) > 0:
                self.report_progress("already present: " + relative)
                return

            log("Transcribing: " + relative)
            name = os.path.basename(for_whisper)
            whisper_out = os.path.join(
                work, os.path.splitext(name)[0] + "." + self.fmt)
            done = subprocess.run(
                ["pipx", "run", "whisper-ctranslate2", for_whisper,
                 "--output_dir", work,
                 "--model", self.model["modelMulti"],
                 "--output_format", self.fmt,
                 "--device", self.model["device"],
                 "--compute_type", self.model["computeType"],
                 "--threads", str(self.model["threads"]),
                 "--vad_filter", "True"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if done.returncode == 0 and os.path.isfile(whisper_out):
                os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
                shutil.move(whisper_out, out)
                self.report_progress("transcribed: " + relative)
            else:
                log("WARNING: transcription failed: " + relative)
                self.report_progress("failed: " + relative)
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _mirror_folders(input_dir: str, output_dir: str) -> None:
    """The input's sub-folder structure, mirrored into the output, so the two
    trees line up folder for folder."""
    for parent, _dirs, _files in os.walk(input_dir):
        relative = os.path.relpath(parent, input_dir)
        target = output_dir if relative == "." else os.path.join(output_dir,
                                                                 relative)
        os.makedirs(target, exist_ok=True)


def _in_worker(state, relative: str) -> None:
    """One track, in a worker PROCESS. The worker's interrupt handling belongs
    here rather than in the transcription itself: the serial path runs that in
    the RUN's own process, where the worker's handler would replace the run's."""
    safety.trap_worker_abort()
    state.transcribe_one(relative)


def _run_queue(state, tracks, jobs: int) -> None:
    """The queue, at ``jobs`` workers - each spanning all CPU threads or the
    GPU, which is why the width is whisper's and not the core count."""
    if jobs <= 1:
        for relative in tracks:
            if safety.abort_requested():
                return
            state.transcribe_one(relative)
        return

    workerpool.run(tracks, jobs, _in_worker, lambda relative: (state, relative))


def main(argv: list, program: str = "transcribe-audio") -> int:
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

    input_dir, output_dir = result.positionals[0], result.positionals[1]
    if not os.path.isdir(input_dir):
        sys.stdout.write(clioptions.missing_dir_text(declaration, input_dir))
        return 1

    # Both paths absolute here, once: the run reads the input tree relative to
    # itself, and a relative output path would otherwise resolve against it.
    # $PWD rather than realpath, because the output folder need not exist yet.
    if not input_dir.startswith("/"):
        input_dir = os.path.join(os.getcwd(), input_dir)
    if not output_dir.startswith("/"):
        output_dir = os.path.join(os.getcwd(), output_dir)

    fmt = result.values["fmt"] or "txt"
    jobs = int(result.values["whisperJobs"] or whisper.WHISPER_JOBS)

    runlog.settle_flock()
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()
    if tooldeps.require_tools(
            program, ["ffmpeg", "pipx"],
            skip_preflight=bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))):
        return 1
    runlog.warn_uncounted_progress()

    # Nothing to transcribe? Say so and stop before the output folder is built.
    wanted = ("audio files (%s) or video files to take the first audio track "
              "from (%s)"
              % (enums.extension_list(list(enums.AUDIO_EXTENSIONS)
                                      + list(enums.LOSSLESS_AUDIO_EXTENSIONS)),
                 enums.extension_list(list(enums.VIDEO_EXTENSIONS))))
    tracks = _tracks(input_dir)
    if not tracks:
        return safety.fail_no_relevant_input(input_dir, wanted)

    os.makedirs(output_dir, exist_ok=True)

    safety.init_abort_flag()
    safety.trap_run_abort()
    ram_root = ""
    try:
        ramscratch.init_ram_base()
        ram_root, status = ramscratch.ram_scratch_dir("transcribeAudio")
        if status != 0 or not ram_root:
            sys.stderr.write("\nError: no scratch directory could be made for "
                             "this run.\nNothing was changed.\n")
            return 1
        ramscratch.add_exit_cleanup([ram_root])

        cores = str(runlog.cpu_count())
        model = whisper.init_whisper_model(cores, ram_root, log)

        started = time.time()

        def footer():
            total = int(time.time() - started)
            total = total if total > 0 else 0
            sys.stdout.write("\nStats\n=====\n")
            sys.stdout.write("Files:        %d\n" % len(tracks))
            sys.stdout.write("Total time:   %d s (%s)\n"
                             % (total, fmt_hms(total)))

        safety.set_run_footer(footer)

        _mirror_folders(input_dir, output_dir)

        progress_file, status = ramscratch.ram_scratch_file(
            "transcribeAudio.progress")
        if status != 0 or not progress_file:
            sys.stderr.write("\nError: no scratch file could be made for this "
                             "run.\nNothing was changed.\n")
            return 1
        ramscratch.add_exit_cleanup([progress_file,
                                     progress_file + ".lock"])
        with open(progress_file, "w") as handle:
            handle.write("0\n")

        state = Run(input_dir, output_dir, fmt, model, ram_root, progress_file,
                    len(tracks))
        sys.stdout.write("Transcribing %d file(s) on %s worker(s)...\n"
                         % (len(tracks), jobs))
        _run_queue(state, tracks, jobs)
        safety.exit_if_aborted()
        sys.stdout.write("Done.\n")
        safety.print_run_footer()
        return 0
    finally:
        ramscratch.run_exit_cleanup()


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name))


if __name__ == "__main__":
    sys.exit(cli())
