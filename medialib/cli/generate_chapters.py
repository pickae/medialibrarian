"""generate-chapters: an input tree of spoken-word audio and video, given
chapter files - start times and titles - worked out by a language model from a
transcript of what is said.

Two phases, run one after the other over the whole tree. The first is
transcribe-audio's, run as that command for every input that has no transcript
yet: the transcripts land in the output folder beside the chapter files, so a
rerun pays for neither again. A transcript some other recogniser wrote is used
as it is, from the output folder or from beside the input. The second asks the
model the settings name (medialib/lib/chapterllm.py) for the chapters, in the
two passes medialib/lib/aichapters.py describes.

Nothing in the input is touched. A chapter file is the OGM list mkvmerge,
ffmpeg's ingest and convert-audio all read; convert-audio's -t writes the same
chapters straight into what it encodes.
"""

import os
import sys

from medialib import commands
from medialib.lib import (
    aichapters,
    chapterllm,
    clioptions,
    enums,
    ramscratch,
    runlog,
    safety,
    settings,
)

CREDITS = "chapter spoken-word audio with a language model"

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>

Each input gets a chapter file and a transcript in <outputDir>, in the same
sub-folder structure:
    <inputDir>/a/show.mp3    ->   <outputDir>/a/show.chapters.txt
                                  <outputDir>/a/show.srt
A transcript already in <outputDir>, or beside the input, as .srt, .vtt, .tsv,
.json or .lrc, is used instead of transcribing again.

The language model is set with the chapterLlm settings: any server speaking the
OpenAI chat protocol (local or hosted) or Claude.

Options:"""

OPT_SPEC = """
h |  | Print this help page.
f |  | Make the chapter files again, over the ones already there.
                    Default false
m | <minutes> | The shortest chapter.
                    Default {minimum}
l | <minutes> | The length of chapter the model is steered towards.
                    Default {typical}
""".format(minimum=aichapters.DEFAULT_MIN_MINUTES,
           typical=aichapters.DEFAULT_TYPICAL_MINUTES)

OPT_CHECKS = """
m | posInt | shortest chapter
l | posInt | chapter length
"""

OPT_VARS = "f:force m:minMinutes l:typicalMinutes"
OPT_COLUMN = 20
OPT_LONG = "h:help f:force m:min-length l:length"

# What a chapter file is called beside the transcript.
CHAPTER_SUFFIX = ".chapters.txt"


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
    min_minutes = int(result.values["minMinutes"] or aichapters.DEFAULT_MIN_MINUTES)
    typical = int(result.values["typicalMinutes"] or aichapters.DEFAULT_TYPICAL_MINUTES)

    try:
        return _run(program, script_dir or commands.script_dir(), input_dir,
                    output_dir, force, min_minutes, typical)
    finally:
        ramscratch.run_exit_cleanup()


def _run(program: str, script_dir: str, input_dir: str, output_dir: str,
         force: bool, min_minutes: int, typical: int) -> int:
    llm = chapterllm.require(program)
    if llm is None:
        return 1

    inputs = _inputs(input_dir)
    if not inputs:
        return safety.fail_no_relevant_input(
            input_dir, "audio files (%s) or video files (%s)"
            % (enums.extension_list(list(enums.AUDIO_EXTENSIONS)
                                    + list(enums.LOSSLESS_AUDIO_EXTENSIONS)),
               enums.extension_list(list(enums.VIDEO_EXTENSIONS))))
    os.makedirs(output_dir, exist_ok=True)
    safety.init_abort_flag()
    safety.trap_run_abort()
    ramscratch.init_ram_base()

    def chapter_file(relative: str) -> str:
        return os.path.join(output_dir, os.path.splitext(relative)[0] + CHAPTER_SUFFIX)

    def transcript(relative: str) -> str:
        stem = os.path.splitext(relative)[0]
        return (aichapters.find_transcript(os.path.join(output_dir, stem))
                or aichapters.find_transcript(os.path.join(input_dir, stem)))

    todo = [relative for relative in inputs
            if force or not os.path.isfile(chapter_file(relative))
            or os.path.getsize(chapter_file(relative)) == 0]
    for relative in inputs:
        if relative not in todo:
            runlog.say("already present: %s\n" % relative)
    untranscribed = [(relative, os.path.join(input_dir, relative))
                     for relative in todo if not transcript(relative)]

    failed = 0
    if untranscribed:
        runlog.say("Transcribing %d file(s)...\n" % len(untranscribed))
        stage, status = ramscratch.ram_scratch_dir("generateChapters.stage")
        if status != 0 or not stage:
            sys.stderr.write("\nError: no scratch directory could be made for this "
                             "run.\nNothing was changed.\n")
            return 1
        ramscratch.add_exit_cleanup([stage])
        if aichapters.transcribe(untranscribed, stage, output_dir, script_dir):
            failed = 1
        safety.exit_if_aborted()

    runlog.say("Chaptering %d file(s) with %s...\n" % (len(todo), llm.describe()))
    made = 0
    for number, relative in enumerate(todo, 1):
        safety.exit_if_aborted()
        source = transcript(relative)
        label = "[%d/%d] %s" % (number, len(todo), relative)
        if not source:
            runlog.error("%s: no transcript to chapter from" % label)
            failed = 1
            continue
        cues = aichapters.read_transcript(source)
        runlog.say("%s: %d transcript line(s)\n" % (label, len(cues)))
        marks = aichapters.generate(cues, llm, min_minutes=min_minutes,
                                    typical_minutes=typical, log=runlog.log)
        if not marks:
            runlog.error("%s: no chapters (too short, or the model gave no usable "
                         "outline)" % label)
            failed = 1
            continue
        target = chapter_file(relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("\n".join(aichapters.chapter_rows(marks)) + "\n")
        for start, title in marks:
            runlog.log("    %s  %s" % (aichapters.time_label(start), title))
        made += 1
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
