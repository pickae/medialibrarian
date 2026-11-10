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
import sys
import tempfile
import time

from medialib import commands
from medialib.lib import (
    clioptions,
    console,
    durationcheck,
    enums,
    errorlog,
    ffmpegselect,
    ramscratch,
    runlog,
    safety,
    settings,
    toolcapture,
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
really run on it, otherwise the CPU), and so is how wide it runs, so there is
nothing to configure.

Options:"""

FORMATS = ("txt", "srt", "vtt", "tsv")

# The formats whose output is CUED, and which a batched run has to be asked to
# cut into subtitle-shaped lines rather than one block per speech run.
CUED_FORMATS = ("srt", "vtt")

OPT_SPEC = """
h |  | Print this help page.
f | <format> | Transcript format: txt, srt, vtt or tsv.
                    Default txt
j | <jobs> | Run up to <jobs> transcriptions in parallel.
                    Default: what the card can hold, or 2 on the CPU
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
    return clioptions.with_verbosity(clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        checks=OPT_CHECKS,
        column=OPT_COLUMN,
        credits=CREDITS,
        tail="\n",
    ))


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

    Ties keep the order the walk found them in, which is what a stable sort
    over the size does.
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
                 counter_dir, total):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.fmt = fmt
        self.model = model
        self.ram_root = ram_root
        # The run's console: the lock every worker prints under, and the
        # count of tracks done.
        self.console = console.Console(counter_dir)
        self.failed_file = os.path.join(counter_dir, "failed")
        self.total = total
        # Nought slots is the unbatched path. Only the cued formats want the
        # line flags: a transcript that is read has no cues to spoil.
        slots = model.get("batchSlots", 0)
        self.batch_args = (["--batched", "True", "--batch_size", str(slots)]
                           if slots else [])
        if slots and fmt in CUED_FORMATS:
            self.batch_args += [
                "--word_timestamps", "True",
                "--max_line_width", str(whisper.WHISPER_LINE_WIDTH),
                "--max_line_count", str(whisper.WHISPER_LINE_COUNT)]

    def report_progress(self, label: str, failed: bool = False) -> None:
        """Increment the shared counter and print one clean progress line.

        The counter is a FILE under a lock because the workers are separate
        processes; without flock it is not taken at all and the line comes out
        unnumbered. Under -q only a <failed> line is printed, but every job
        still counts.
        """
        self.console.counted(label, self.total, always=failed)

    def record_failure(self) -> None:
        """Mark the run as having a failed transcription, in a file because the
        workers are separate processes."""
        open(self.failed_file, "w").close()

    def any_failed(self) -> bool:
        return os.path.exists(self.failed_file)

    def transcribe_one(self, relative: str) -> None:
        """One queued job: this input's speech into its mirrored transcript."""
        source = os.path.join(self.input_dir, relative)
        out = os.path.join(self.output_dir,
                           os.path.splitext(relative)[0] + "." + self.fmt)

        # Resume: a finished, non-empty transcript is kept where it is, so a
        # re-run - or one restarted after a Ctrl+C - does not pay for it again.
        # It is tested before anything else, since lifting a film's audio out
        # costs minutes the transcript already made unnecessary.
        if os.path.exists(out) and os.path.getsize(out) > 0:
            self.report_progress("already present: " + relative)
            return

        # whisper names its output after the input file, so every run gets a
        # directory of its own: the extracted track and the transcript both live
        # there, and it goes when the job does.
        work = tempfile.mkdtemp(prefix="whisper.", dir=self.ram_root)
        try:
            if _is_video(relative):
                for_whisper = os.path.join(work, "track.wav")
                extracted = toolcapture.run(
                    ["ffmpeg", "-nostdin", "-y", "-hide_banner",
                     *toolcapture.ffmpeg_loglevel(), "-nostats",
                     "-i", source, "-map", "0:a:0", "-ac", "1",
                     "-ar", "16000", "-c:a", "pcm_s16le", for_whisper],
                    relative,
                    warning="no first audio track to transcribe: " + relative)
                if not extracted.ok:
                    self.report_progress("skipped (no audio): " + relative)
                    return
                # A short extraction transcribes cleanly and stops early, and a
                # transcript that simply ends is the hardest kind of wrong to
                # notice - there is no gap in it to see.
                if not durationcheck.verify(relative, source, for_whisper):
                    self.report_progress(
                        "skipped (audio extracted short): " + relative,
                        failed=True)
                    return
            else:
                for_whisper = source

            log("Transcribing: " + relative)
            name = os.path.basename(for_whisper)
            whisper_out = os.path.join(
                work, os.path.splitext(name)[0] + "." + self.fmt)
            done = toolcapture.run(
                [*whisper.WHISPER_COMMAND, for_whisper,
                 "--output_dir", work,
                 "--model", self.model["modelMulti"],
                 "--output_format", self.fmt,
                 "--device", self.model["device"],
                 "--compute_type", self.model["computeType"],
                 "--threads", str(self.model["threads"]),
                 "--vad_filter", "True", *self.batch_args],
                relative, tool="whisper",
                # The status alone is not the evidence: whisper-ctranslate2
                # exits 0 after a CUDA out-of-memory error, having written
                # nothing.
                verify=lambda: ("" if os.path.isfile(whisper_out)
                                else "wrote no transcript"))
            if done.ok:
                os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
                shutil.move(whisper_out, out)
                self.report_progress("transcribed: " + relative)
            else:
                self.record_failure()
                self.report_progress("failed: " + relative, failed=True)
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
    state.console.activate()
    state.transcribe_one(relative)


def _run_queue(state, tracks, jobs: int) -> None:
    """The queue, at ``jobs`` workers - each spanning all CPU threads or the
    GPU, which is why the width is whisper's and not the core count."""
    if jobs <= 1:
        for relative in tracks:
            if safety.abort_requested():
                return
            with errorlog.work_item(relative):
                state.transcribe_one(relative)
        return

    workerpool.run(tracks, jobs, _in_worker, lambda relative: (state, relative))


def main(argv: list, program: str = "transcribe-audio") -> int:
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

    if clioptions.args_out_of_range(len(result.positionals), 2, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    input_dir, output_dir = result.positionals[0], result.positionals[1]
    if not os.path.isdir(input_dir):
        clioptions.refuse_missing_dir(declaration, input_dir)
        return 1

    # Both paths absolute here, once: the run reads the input tree relative to
    # itself, and a relative output path would otherwise resolve against it.
    # $PWD rather than realpath, because the output folder need not exist yet.
    if not input_dir.startswith("/"):
        input_dir = os.path.join(os.getcwd(), input_dir)
    if not output_dir.startswith("/"):
        output_dir = os.path.join(os.getcwd(), output_dir)

    fmt = result.values["fmt"] or "txt"
    # Nought stands for "not overridden": the settlement that fills it in
    # cannot run until the scratch its probe writes into exists.
    jobs = int(result.values["whisperJobs"] or 0)

    runlog.settle_flock()
    try:
        return _transcribe(program, input_dir, output_dir, fmt, jobs)
    finally:
        # The ffmpeg pin can make the run directory before anything below
        # does, and every refusal after it must hand it back.
        ramscratch.run_exit_cleanup()


def _transcribe(program: str, input_dir: str, output_dir: str, fmt: str,
                jobs: int) -> int:
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
    ramscratch.init_ram_base()
    ram_root, status = ramscratch.ram_scratch_dir("transcribeAudio")
    if status != 0 or not ram_root:
        sys.stderr.write("\nError: no scratch directory could be made for "
                         "this run.\nNothing was changed.\n")
        return 1
    ramscratch.add_exit_cleanup([ram_root])

    cores = str(runlog.cpu_count())
    model = whisper.init_whisper_model(cores, ram_root, log)
    jobs = jobs or model["jobs"]

    started = time.time()

    def footer():
        total = int(time.time() - started)
        total = total if total > 0 else 0
        runlog.say("\nStats\n=====\n")
        runlog.say("Files:        %d\n" % len(tracks))
        runlog.say("Total time:   %d s (%s)\n" % (total, fmt_hms(total)))
        durationcheck.report()

    durationcheck.init_log()
    safety.set_run_footer(footer)

    _mirror_folders(input_dir, output_dir)

    counter_dir, status = ramscratch.ram_scratch_dir(
        "transcribeAudio.progress")
    if status != 0 or not counter_dir:
        sys.stderr.write("\nError: no scratch file could be made for this "
                         "run.\nNothing was changed.\n")
        return 1
    ramscratch.add_exit_cleanup([counter_dir])

    state = Run(input_dir, output_dir, fmt, model, ram_root, counter_dir,
                len(tracks))
    # The pool is never wider than the work.
    runlog.say("Transcribing %d file(s) on %d worker(s)...\n"
               % (len(tracks), min(int(jobs), len(tracks))))
    _run_queue(state, tracks, jobs)
    safety.exit_if_aborted()
    runlog.say("Done.\n")
    safety.print_run_footer()
    # A file that came out with no transcript, or with a short one, means
    # the run did not do what it was asked, and a caller chaining commands
    # has to see that.
    return workerpool.exit_status(
        1 if durationcheck.failures() or state.any_failed() else 0)


def cli(argv: list | None = None) -> int:
    settings.load()
    errorlog.start_run(argv)
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name))


if __name__ == "__main__":
    sys.exit(cli())
