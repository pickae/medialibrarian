"""transcribe-audio: an input tree of audio and video, transcribed by whisper
into a mirrored tree of transcripts.

A video is transcribed from its FIRST audio track only - lifted out to the 16 kHz
mono wav whisper resamples to anyway - so whisper never sees a second track or
the video stream. An audio file is handed over as it is.

The queue is ordered LARGEST FILE FIRST, because byte size is a cheap probe-free
proxy for how much speech there is: the long transcriptions start at the front
instead of one huge file landing last and pinning a worker to the end.

Several inputs share one whisper process, which loads the model once for all
of them; a film goes alone - see :func:`_groups`.
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
    ffmpegselect,
    ramscratch,
    runlog,
    safety,
    settings,
    toolcapture,
    tooldeps,
    transcripttags,
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

With -l an Opus, FLAC or MP4 audio file also gets a copy in <outputDir> with its
transcript embedded: as timed lyrics in Opus and FLAC, as a subtitle track in
.m4a and .m4b. The input is never touched.

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
l |  | Also write a copy of each Opus, FLAC, .m4a and .m4b input into the
                    output, with its transcript embedded as lyrics.
j | <jobs> | Run up to <jobs> transcriptions in parallel.
                    Default: what the card can hold, or one per
                    4 CPU threads on the CPU
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

OPT_VARS = "f:fmt j:whisperJobs l:lyrics"
OPT_COLUMN = 20
OPT_LONG = "h:help f:format j:jobs l:lyrics"


def spec(program: str) -> clioptions.Spec:
    return clioptions.with_shared_options(clioptions.Spec(
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


def _takes_lyrics(name: str) -> bool:
    """Whether an input is audio in a container a transcript can be embedded
    in; any other is only transcribed."""
    return (not _is_video(name)
            and name.lower().endswith((".opus", ".flac", ".m4a", ".m4b")))


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
                 counter_dir, total, lyrics=False):
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
        self.lyrics = lyrics
        # Nought slots is the unbatched path. Only the cued formats want the
        # line flags: a transcript that is read has no cues to spoil.
        slots = model.get("batchSlots", 0)
        self.batch_args = (["--batched", "True", "--batch_size", str(slots)]
                           if slots else [])
        # Lyrics are timed, so the cues are wanted whatever the format is.
        if slots and (fmt in CUED_FORMATS or lyrics):
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

    def prepared(self, relatives):
        """Each input made ready for whisper, as ``((relative, for_whisper,
        work), size)`` for :func:`medialib.lib.workerpool.map`.

        This runs on the queue's own thread, beside the workers: lifting a
        film's audio out is minutes of ffmpeg in which a worker would leave
        the GPU idle, so it is done for the next input while whisper works on
        the last. The size is the input's, the queue's order largest first.
        """
        for relative in relatives:
            if safety.abort_requested():
                return
            ready = self._prepare(relative)
            if ready is not None:
                try:
                    size = os.path.getsize(
                        os.path.join(self.input_dir, relative))
                except OSError:
                    size = 0
                yield ready, size

    def _prepare(self, relative: str):
        """``(relative, for_whisper, work)``, or None when there is nothing
        for whisper to do: already transcribed, or no usable audio."""
        source = os.path.join(self.input_dir, relative)
        out = self._transcript(relative)

        # Resume: a finished, non-empty transcript is kept where it is, so a
        # re-run - or one restarted after a Ctrl+C - does not pay for it again.
        # It is tested before anything else, since lifting a film's audio out
        # costs minutes the transcript already made unnecessary.
        if os.path.exists(out) and os.path.getsize(out) > 0 and not (
                self.lyrics and _takes_lyrics(relative)
                and not os.path.isfile(self._audio_copy(relative))):
            self.report_progress("already present: " + relative)
            return None

        # Every input gets a directory of its own: the extracted track lives
        # there, and it goes when the job does. The extract is named after
        # that directory, because one whisper call writes the transcripts of
        # all its inputs into one folder, by input name, and two extracts
        # called the same would overwrite each other's.
        work = tempfile.mkdtemp(prefix="whisper.", dir=self.ram_root)
        if not _is_video(relative):
            return relative, source, work
        for_whisper = os.path.join(work, os.path.basename(work) + ".track.wav")
        extracted = toolcapture.run(
            ["ffmpeg", "-nostdin", "-y", "-hide_banner",
             *toolcapture.ffmpeg_loglevel(), "-nostats",
             "-i", source, "-map", "0:a:0", "-ac", "1",
             "-ar", "16000", "-c:a", "pcm_s16le", for_whisper],
            relative,
            warning="no first audio track to transcribe: " + relative)
        if not extracted.ok:
            shutil.rmtree(work, ignore_errors=True)
            self.report_progress("skipped (no audio): " + relative)
            return None
        # A short extraction transcribes cleanly and stops early, and a
        # transcript that simply ends is the hardest kind of wrong to notice -
        # there is no gap in it to see.
        if not durationcheck.verify(relative, source, for_whisper):
            shutil.rmtree(work, ignore_errors=True)
            self.report_progress("skipped (audio extracted short): "
                                 + relative, failed=True)
            return None
        return relative, for_whisper, work

    def _transcript(self, relative: str) -> str:
        return os.path.join(self.output_dir,
                            os.path.splitext(relative)[0] + "." + self.fmt)

    def _audio_copy(self, relative: str) -> str:
        return os.path.join(self.output_dir, relative)

    def _whisper(self, inputs: list, output_dir: str) -> list:
        """The whisper command for ``inputs``, transcribed into
        ``output_dir`` - one process, so one load of the model for them all."""
        return [*whisper.WHISPER_COMMAND, *inputs,
                "--output_dir", output_dir,
                "--model", self.model["modelMulti"],
                # "all" when lyrics need a timed transcript the format is not.
                "--output_format",
                "all" if self.lyrics and self.fmt not in CUED_FORMATS
                else self.fmt,
                "--device", self.model["device"],
                "--compute_type", self.model["computeType"],
                "--threads", str(self.model["threads"]),
                "--vad_filter", "True", *self.batch_args]

    def _whisper_out(self, for_whisper: str, output_dir: str) -> str:
        """Where whisper writes the transcript of ``for_whisper``: named after
        the input, original extension dropped."""
        name = os.path.splitext(os.path.basename(for_whisper))[0]
        return os.path.join(output_dir, name + "." + self.fmt)

    def _embed(self, relative: str, whisper_out: str) -> None:
        """A copy of the input in the output carrying its transcript as
        lyrics. The copy appears whole, or not at all."""
        timed = (whisper_out if self.fmt in CUED_FORMATS
                 else os.path.splitext(whisper_out)[0] + ".srt")
        target = self._audio_copy(relative)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        staged = target + ".part" + os.path.splitext(target)[1]
        scratch = tempfile.mkdtemp(prefix="lyrics.", dir=self.ram_root)
        try:
            shutil.copyfile(os.path.join(self.input_dir, relative), staged)
            if transcripttags.embed_transcript(staged, timed, scratch) == 0:
                os.replace(staged, target)
                return
            runlog.warn("lyrics not embedded: " + relative)
            self.record_failure()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            if os.path.exists(staged):
                os.remove(staged)

    def _keep(self, relative: str, whisper_out: str) -> None:
        out = self._transcript(relative)
        if self.lyrics and _takes_lyrics(relative):
            self._embed(relative, whisper_out)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        # Moved in under another name and renamed in place, so the transcript
        # appears whole: a chapter run reading the folder as it fills never
        # sees half of one.
        shutil.move(whisper_out, out + ".part")
        os.replace(out + ".part", out)
        self.report_progress("transcribed: " + relative)

    def _say_language(self, relative: str, detection) -> None:
        """The line the detection leaves behind, under -v: the language and
        the probability whisper's own output says it transcribed in. A
        detection that was not read says nothing, rather than a guess."""
        if detection is None:
            return
        language, probability = detection
        runlog.detail("%s detected (probability %.3f): %s"
                      % (language, probability, relative))

    @staticmethod
    def _detections(done) -> list:
        """The detections of one whisper call, in input order: whisper prints
        one of its own lines per input it transcribed, and the call's kept
        output is where they are."""
        return whisper.detected_languages(done.output.lines())

    def transcribe_one(self, ready: tuple) -> None:
        """One prepared input through whisper into its mirrored transcript;
        its work directory goes when it is done."""
        relative, for_whisper, work = ready
        try:
            log("Transcribing: " + relative)
            whisper_out = self._whisper_out(for_whisper, work)
            done = toolcapture.run(
                self._whisper([for_whisper], work),
                relative, tool="whisper",
                # The status alone is not the evidence: whisper-ctranslate2
                # exits 0 after a CUDA out-of-memory error, having written
                # nothing.
                verify=lambda: ("" if os.path.isfile(whisper_out)
                                else "wrote no transcript"))
            if done.ok:
                detections = self._detections(done)
                self._say_language(relative, detections[0]
                                   if detections else None)
                self._keep(relative, whisper_out)
            else:
                self.record_failure()
                self.report_progress("failed: " + relative, failed=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def transcribe_group(self, group: tuple) -> None:
        """Several prepared inputs through ONE whisper process, so the model
        is loaded into memory once for them all rather than once a file.

        ``group`` is ``(label, members)`` - see :func:`_groups`. A group of
        one is :meth:`transcribe_one`. whisper-ctranslate2 transcribes its
        inputs one after the other and writes each transcript as that input
        is done; an input it cannot transcribe - a CUDA out-of-memory error
        among them - it reports and passes over, and it still exits 0. So the
        evidence is again each input's own transcript: every one that is
        there is kept, and every one that is not is tried once more in a call
        of its own, which is the call today's failure is reported from. What
        a failed group costs is that retry, never the inputs it did finish.
        """
        label, members = group
        if len(members) == 1:
            self.transcribe_one(members[0])
            return
        retry = []
        output_dir = tempfile.mkdtemp(prefix="whisper.group.",
                                      dir=self.ram_root)
        try:
            for relative, _for_whisper, _work in members:
                log("Transcribing: " + relative)
            outs = [self._whisper_out(for_whisper, output_dir)
                    for _relative, for_whisper, _work in members]
            # Not reported here: a file this call missed is tried again on
            # its own, and only that call's failure is the file's.
            done = toolcapture.run(
                self._whisper([m[1] for m in members], output_dir),
                label, tool="whisper", report=False,
                verify=lambda: ("" if all(os.path.isfile(o) for o in outs)
                                else "wrote no transcript for some files"))
            # One detection per member whisper transcribed, in member order:
            # a member it passed over printed no line, so the lines line up
            # with the members that have a transcript. (The one case they do
            # not - a member whisper transcribed but whose transcript write
            # failed - only shifts the lines of the members after it, under
            # -v, in a run that is already reporting that member.)
            languages = self._detections(done)
            index = 0
            for member, whisper_out in zip(members, outs, strict=True):
                if os.path.isfile(whisper_out):
                    self._say_language(
                        member[0], languages[index]
                        if index < len(languages) else None)
                    index += 1
                    self._keep(member[0], whisper_out)
                    shutil.rmtree(member[2], ignore_errors=True)
                else:
                    retry.append(member)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)
        for member in retry:
            if safety.abort_requested():
                # Stopped: not a failure, and not worth a second start.
                _discard(member)
                continue
            log("Retrying alone: " + member[0])
            self.transcribe_one(member)


def _discard(ready: tuple) -> None:
    """An input made ready but never transcribed: its extract let go of."""
    shutil.rmtree(ready[2], ignore_errors=True)


def _discard_group(group: tuple) -> None:
    for member in group[1]:
        _discard(member)


def group_size(files: int, jobs: int) -> int:
    """How many inputs one whisper process is given: the files spread evenly
    over the workers, so a second run is not left idle while the first holds
    them all, and never more than :data:`whisper.WHISPER_GROUP_FILES`."""
    jobs = max(int(jobs), 1)
    even = -(-files // jobs)
    return max(1, min(even, whisper.WHISPER_GROUP_FILES))


def _groups(prepared, size: int):
    """The items of ``prepared`` - ``(ready, bytes)`` pairs - gathered into
    groups of up to ``size`` for one whisper process each, as
    ``((label, members), bytes)``.

    A group is handed on as soon as it is full, its sources add up to
    :data:`whisper.WHISPER_GROUP_BYTES`, or the next input would be written
    under a name already in it (whisper names a transcript after its input,
    and every input of a call writes into one folder). A source that large -
    a film - therefore goes alone and at once: its own transcription dwarfs a
    reload of the model, and holding it back for the next extraction would
    only leave the GPU idle.
    """
    members: list = []
    names: set = set()
    total = 0

    def handed():
        label = members[0][0]
        if len(members) > 1:
            label += " (+%d more)" % (len(members) - 1)
        return (label, tuple(members)), total

    for ready, size_bytes in prepared:
        name = os.path.splitext(os.path.basename(ready[1]))[0].casefold()
        if members and name in names:
            yield handed()
            members, names, total = [], set(), 0
        members.append(ready)
        names.add(name)
        total += size_bytes
        if len(members) >= size or total >= whisper.WHISPER_GROUP_BYTES:
            yield handed()
            members, names, total = [], set(), 0
    if members:
        yield handed()


def _mirror_folders(input_dir: str, output_dir: str) -> None:
    """The input's sub-folder structure, mirrored into the output, so the two
    trees line up folder for folder."""
    for parent, _dirs, _files in os.walk(input_dir):
        relative = os.path.relpath(parent, input_dir)
        target = output_dir if relative == "." else os.path.join(output_dir,
                                                                 relative)
        os.makedirs(target, exist_ok=True)


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
    lyrics = bool(result.values["lyrics"])
    # Nought stands for "not overridden": the settlement that fills it in
    # cannot run until the scratch its probe writes into exists.
    jobs = int(result.values["whisperJobs"] or 0)

    runlog.settle_flock()
    try:
        return _transcribe(program, input_dir, output_dir, fmt, jobs, lyrics)
    finally:
        # The ffmpeg pin can make the run directory before anything below
        # does, and every refusal after it must hand it back.
        ramscratch.run_exit_cleanup()


def _transcribe(program: str, input_dir: str, output_dir: str, fmt: str,
                jobs: int, lyrics: bool = False) -> int:
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
                len(tracks), lyrics)
    # The pool is never wider than the work.
    runlog.say("Transcribing %d file(s) on %d worker(s)...\n"
               % (len(tracks), min(int(jobs), len(tracks))))
    # Each worker spans all CPU threads or the GPU, which is why the width is
    # whisper's and not the core count. The workers start on the first input
    # made ready; the rest are made ready while they work. The inputs go to
    # whisper in groups, one process - one load of the model - a group.
    groups = _groups(state.prepared(tracks), group_size(len(tracks),
                                                        int(jobs)))
    workerpool.map(groups, int(jobs), state.transcribe_group, prepared=True,
                   look_ahead=1, discard=_discard_group, start_after=1)
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
    runlog.start_run(argv)
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name))


if __name__ == "__main__":
    sys.exit(cli())
