"""generate-chapters: an input tree of spoken-word audio and video, given
chapter files - start times and titles - worked out by a language model from a
transcript of what is said.

Two phases, run one after the other over the whole tree. The first is
transcribe-audio's, run as that command for every input that has no transcript
yet: the transcripts land in the output folder beside the chapter files, so a
rerun pays for neither again. A transcript some other recogniser wrote is used
as it is, from the output folder or from beside the input. The second asks the
model the settings name (medialib/lib/chapterllm.py) for the chapters, in the
two passes medialib/lib/aichapters.py describes. With a hosted model the two
overlap: each file is chaptered as soon as its transcript is there.

Nothing in the input is touched unless -w asks for the chapters in it as well:
then each input takes its chapter file in place, without being re-encoded, or
beside it when its format has no place for chapters. A
chapter file is the OGM list mkvmerge, ffmpeg's ingest and convert-audio all
read; convert-audio's -C writes the same chapters straight into what it encodes.
"""

import os
import shutil
import sys

from medialib import commands
from medialib.lib import (
    aichapters,
    chapterllm,
    chapters,
    clioptions,
    enums,
    ramscratch,
    runlog,
    safety,
    settings,
    tooldeps,
)

CREDITS = "chapter spoken-word audio with a language model"

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>

Each input gets a chapter file and a transcript in <outputDir>, in the same
sub-folder structure:
    <inputDir>/a/show.mp3    ->   <outputDir>/a/show.chapters.txt
                                  <outputDir>/a/show.srt
A transcript already in <outputDir>, or beside the input, as .srt, .vtt, .tsv,
.json or .lrc, is used instead of transcribing again. With -w each input
takes its chapters in as well, without being re-encoded.

The language model is set with the chapterLlm settings: any server speaking the
OpenAI chat protocol (local or hosted) or Claude.

Options:"""

OPT_SPEC = """
h |  | Print this help page.
f |  | Make the chapter files again, over the ones already there.
m | <minutes> | The shortest chapter, in minutes; a fraction is allowed,
                    0.5 being thirty seconds.
                    Default {minimum}
l | <minutes> | The length of chapter the model is steered towards.
                    Default {typical}
w |  | Write the chapters into the input files too, replacing the
                    chapters they hold and keeping everything else; the
                    audio is not re-encoded. A format with no place for
                    chapters (raw AAC, WAV) gets the chapter file beside it
                    instead. Chapter files already made are written in
                    without asking the model again.
P | <text> | More instructions for the model, added to every request,
                    e.g. "a chapter per speaker, titled with their name and
                    topic". The model then reasons at medium effort, and Claude
                    may search the web.
""".format(minimum=aichapters.DEFAULT_MIN_MINUTES,
           typical=aichapters.DEFAULT_TYPICAL_MINUTES)

OPT_CHECKS = """
m | posNum | shortest chapter
l | posInt | chapter length
"""

OPT_VARS = "f:force m:minMinutes l:typicalMinutes w:embed P:instructions"
OPT_COLUMN = 20
OPT_LONG = "h:help f:force m:min-length l:length w:write-into P:prompt"

# What a chapter file is called beside the transcript.
CHAPTER_SUFFIX = chapters.SIDECAR_SUFFIX


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


def _inputs(input_dir: str) -> list[str]:
    """Every audio and video file under the input, as relative paths, in
    path order."""
    wanted = {e.lower() for e in (list(enums.AUDIO_EXTENSIONS)
                                  + list(enums.LOSSLESS_AUDIO_EXTENSIONS)
                                  + list(enums.VIDEO_EXTENSIONS))}
    found = []
    for parent, _dirs, names in os.walk(input_dir):
        for name in names:
            if os.path.splitext(name)[1].lstrip(".").lower() in wanted \
                    and os.path.isfile(os.path.join(parent, name)):
                found.append(os.path.relpath(os.path.join(parent, name), input_dir))
    return sorted(found)


def main(argv: list, program: str = "generate-chapters",
         script_dir: str = "") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
        clioptions.settle_verbosity(result)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        clioptions.refuse_usage(declaration, error.message)
        return 1

    if clioptions.args_out_of_range(len(result.positionals), 2, 2):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1
    input_dir, output_dir = result.positionals
    if not os.path.isdir(input_dir):
        clioptions.refuse_missing_dir(declaration, input_dir)
        return 1
    input_dir = os.path.abspath(input_dir)
    output_dir = os.path.abspath(output_dir)

    force = "f" in result.given
    min_minutes = float(result.values["minMinutes"] or aichapters.DEFAULT_MIN_MINUTES)
    typical = int(result.values["typicalMinutes"] or aichapters.DEFAULT_TYPICAL_MINUTES)
    instructions = (result.values["instructions"] or "").strip()
    embed = "w" in result.given

    try:
        return _run(program, script_dir or commands.script_dir(), input_dir,
                    output_dir, force, min_minutes, typical, instructions,
                    embed)
    finally:
        ramscratch.run_exit_cleanup()


def _run(program: str, script_dir: str, input_dir: str, output_dir: str,
         force: bool, min_minutes: float, typical: int,
         instructions: str = "", embed: bool = False) -> int:
    inputs = _inputs(input_dir)
    if not inputs:
        return safety.fail_no_relevant_input(
            input_dir, "audio files (%s) or video files (%s)"
            % (enums.extension_list(list(enums.AUDIO_EXTENSIONS)
                                    + list(enums.LOSSLESS_AUDIO_EXTENSIONS)),
               enums.extension_list(list(enums.VIDEO_EXTENSIONS))))
    if embed and any(relative.lower().endswith(chapters.MATROSKA_SUFFIXES)
                     for relative in inputs) \
            and tooldeps.require_tools("writing chapters into Matroska (-w)",
                                       ["mkvpropedit"]):
        return 1

    def chapter_file(relative: str) -> str:
        return os.path.join(output_dir, os.path.splitext(relative)[0] + CHAPTER_SUFFIX)

    def has_chapter_file(relative: str) -> bool:
        return os.path.isfile(chapter_file(relative)) \
            and os.path.getsize(chapter_file(relative)) > 0

    todo = [relative for relative in inputs
            if force or not has_chapter_file(relative)]
    # Only -w over chapter files already made needs no model at all.
    llm = None
    if todo:
        llm = chapterllm.require(program)
        if llm is None:
            return 1
        llm = llm.following(instructions)
    os.makedirs(output_dir, exist_ok=True)
    safety.init_abort_flag()
    safety.trap_run_abort()
    ramscratch.init_ram_base()
    for relative in inputs:
        if relative not in todo:
            runlog.say("already present: %s\n" % relative)
    failed = _chapter(script_dir, input_dir, output_dir, todo, llm,
                      min_minutes, typical, chapter_file) if todo else 0
    if embed:
        failed |= _write_into(input_dir, [relative for relative in inputs
                                          if has_chapter_file(relative)],
                              chapter_file)
    return failed


def _write_into(input_dir: str, relatives: list[str], chapter_file) -> int:
    """Each input's chapter file written into the input itself, in place; an
    input already holding those chapters is left alone. One whose container
    has no place for chapters gets the chapter file beside it instead. 1 when
    any could take neither."""
    failed = 0
    written = 0
    beside = 0
    for relative in relatives:
        safety.exit_if_aborted()
        target = os.path.join(input_dir, relative)
        made = chapter_file(relative)
        sidecar = os.path.splitext(target)[0] + CHAPTER_SUFFIX
        with open(made, encoding="utf-8") as handle:
            text = handle.read()
        rows = [line for line in text.splitlines() if line.strip()]
        if chapters.carries_chapters(target, rows):
            runlog.say("chapters already in: %s\n" % relative)
            continue
        # Not when the output folder is the input folder: the chapter file is
        # then beside every input, whether or not it could hold them.
        if os.path.abspath(sidecar) != os.path.abspath(made) \
                and _same_text(sidecar, text):
            runlog.say("chapters already beside: %s\n" % relative)
            continue
        scratch, _on_disk, status = ramscratch.ram_scratch_dir_for(
            str(os.path.getsize(target)), "generateChapters.write",
            os.path.dirname(target))
        if status != 0 or not scratch:
            runlog.error("%s: no scratch directory to write its chapters in"
                         % relative)
            failed = 1
            continue
        ramscratch.add_exit_cleanup([scratch])
        try:
            status = chapters.write_chapters(target, rows, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if status == 0:
            written += 1
            runlog.say("chapters written into: %s\n" % relative)
        elif status == chapters.CANNOT_HOLD and _put_beside(made, sidecar):
            beside += 1
            runlog.say("chapters written beside: %s (its format has no place "
                       "for them) -> %s\n" % (relative, os.path.basename(sidecar)))
        else:
            runlog.error("%s: the chapters could not be written into it, so it "
                         "is left as it was" % relative)
            failed = 1
    runlog.say("Done: chapters written into %d file(s)%s.\n"
               % (written, ", beside %d" % beside if beside else ""))
    return failed


def _same_text(path: str, text: str) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read() == text
    except OSError:
        return False


def _put_beside(made: str, sidecar: str) -> bool:
    """The chapter file copied beside its input - nothing to do when the
    output folder is the input folder and it is there already."""
    if os.path.abspath(made) == os.path.abspath(sidecar):
        return True
    try:
        shutil.copyfile(made, sidecar)
        return True
    except OSError:
        return False


def _chapter(script_dir: str, input_dir: str, output_dir: str,
             todo: list[str], llm, min_minutes: float,
             typical: int, chapter_file) -> int:
    """The two phases over ``todo``: transcripts first where there are none,
    then a chapter file for each. 1 when any of them failed."""

    def transcript(relative: str) -> str:
        stem = os.path.splitext(relative)[0]
        return (aichapters.find_transcript(os.path.join(output_dir, stem))
                or aichapters.find_transcript(os.path.join(input_dir, stem)))

    # Too short for two chapters of the shortest length: not transcribed only
    # to be turned down, and so not again on every rerun either.
    short = [relative for relative in todo if aichapters.too_short(
        chapters.duration_ms(os.path.join(input_dir, relative)), min_minutes)]
    for relative in short:
        runlog.say("too short for chapters: %s\n" % relative)
    todo = [relative for relative in todo if relative not in short]
    untranscribed = [(relative, os.path.join(input_dir, relative))
                     for relative in todo if not transcript(relative)]

    failed = 0
    transcribe = None
    if untranscribed:
        stage, status = ramscratch.ram_scratch_dir("generateChapters.stage")
        if status != 0 or not stage:
            sys.stderr.write("\nError: no scratch directory could be made for this "
                             "run.\nNothing was changed.\n")
            return 1
        ramscratch.add_exit_cleanup([stage])

        def transcribe() -> None:
            nonlocal failed
            runlog.say("Transcribing %d file(s)...\n" % len(untranscribed))
            if aichapters.transcribe(untranscribed, stage, output_dir, script_dir):
                failed = 1

    numbers = {relative: number for number, relative in enumerate(todo, 1)}

    def chapter(relative: str, source: str) -> bool:
        label = "[%d/%d] %s" % (numbers[relative], len(todo), relative)

        # Side by side, every line says which file it is about.
        def log(line: str) -> None:
            runlog.log(line if llm.local else "%s: %s" % (label, line.strip()))
        if not source:
            runlog.error("%s: no transcript to chapter from" % label)
            return False
        cues = aichapters.read_transcript(source)
        runlog.say("%s: %d transcript line(s)\n" % (label, len(cues)))
        marks = aichapters.generate(cues, llm, min_minutes=min_minutes,
                                    typical_minutes=typical, log=log)
        if not marks:
            runlog.error("%s: no chapters (too short, or the model gave no usable "
                         "outline)" % label)
            return False
        target = chapter_file(relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("\n".join(aichapters.chapter_rows(marks)) + "\n")
        for start, title in marks:
            log("    %s  %s" % (aichapters.time_label(start), title))
        return True

    results = aichapters.as_ready(
        todo, transcript, transcribe, llm, chapter,
        begin=lambda: runlog.say("Chaptering %d file(s) with %s...\n"
                                 % (len(todo), llm.describe())))
    made = sum(results)
    if not all(results):
        failed = 1
    runlog.say("Done: %d chapter file(s) written.\n" % made)
    return failed


def cli(argv: list | None = None) -> int:
    settings.load()
    runlog.start_run(argv)
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, commands.program_name(__spec__.name), commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
