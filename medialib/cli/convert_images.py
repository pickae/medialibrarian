"""convert-images: a tree of images converted to AVIF - or, with -r, back to
JPEG - into a mirrored output tree.

The interesting half is -c, the crop. What a `-trim` took off, measured as a
percentage of the original edge, is what decides what the trim MEANT: below the
minimum it found no margin worth removing, above the maximum it is cutting into
the picture rather than removing a border, and past the blank threshold there was
nothing on the page at all.

Every conversion is one ImageMagick call whose output is checked and whose stderr
is silenced, so the tools are asked for up front: a missing ImageMagick would
otherwise read as a run in which no image could be converted.
"""

import os
import subprocess
import sys
import time

from medialib import commands
from medialib.lib import (
    clioptions,
    enums,
    imagemagick,
    ramscratch,
    runlog,
    safety,
    tooldeps,
    workerpool,
)
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <inputDir> <outputDir>
Options:"""

OPT_SPEC = """
h |  | Print this help page.
c |  | crop excessive whitespace
r |  | reverse from avif to jpeg
j | <jobs> | Run up to <jobs> encoder processes in parallel.
                    Only needed when RAM is short, otherwise a good guess is made.
q | <quality> | quality level of the output images
s | <speed> | av1 speed preset, lower is slower, default 5
m | <maxRes> | maximum resolution (height) to keep
f | <fuzz> | when trimming, how many percent color difference gets still trimmed
"""

OPT_VARS = "c:crop r:reverse j:jobs q:quality s:speedPreset m:maxRes f:fuzz"
OPT_COLUMN = 20
OPT_LONG = ("h:help c:crop r:reverse j:jobs q:quality s:speed m:max-resolution "
            "f:fuzz")

# One conversion spans about this many threads, so the pool is the cores divided
# by it.
THREADS_PER_CONVERSION = 4

# The width half of every -resize geometry. Only the height is ever capped (-m),
# so the width is given a value no page can reach: "no limit" and "cap the height
# at N" then take the same code path instead of needing two geometries.
UNBOUNDED_EDGE = 10000

# AVIF is written 10-bit even from 8-bit sources: at these quality levels the
# extra headroom costs almost nothing and keeps flat gradients from banding.
AVIF_BIT_DEPTH = 10
# The same perceived quality needs a higher number in JPEG than in AVIF.
JPEG_QUALITY_BONUS = 25

# What a trim took off, in percent of the original edge, and what each threshold
# means about it.
MIN_TRIM_PERCENT = 1
MAX_TRIM_PERCENT = 20
BLANK_TRIM_PERCENT = 99
LIGHTNESS_THRESHOLD = 40

# The stems a disambiguated output has to look out for: two sources in one folder
# sharing a stem would otherwise map to the same converted name.
_SAME_STEM = ("jpg", "JPG", "jpeg", "JPEG", "png", "PNG", "webp", "WEBP",
              "avif", "AVIF")


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
    )


def disambiguated_output(relative: str, new_extension: str, input_dir: str,
                         output_dir: str) -> str:
    """Where this image's conversion goes.

    Normally the mirrored path with the new extension; but when another source in
    the same folder shares the stem - a.jpg and a.png would both become a.avif -
    the source extension is folded into the name so the two cannot clobber one
    another. Deterministic, so each worker can compute it alone, and stable
    across re-runs so the resume check keeps working.
    """
    relative_dir, _, file_name = relative.rpartition("/")
    stem, _, extension = file_name.rpartition(".")
    if not stem:                       # a name with no dot at all
        stem, extension = file_name, ""
    prefix = relative_dir + "/" if relative_dir else ""

    same_stem = 0
    directory = os.path.join(input_dir, relative_dir) if relative_dir \
        else input_dir
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith(stem + "."):
                    continue
                if name.rpartition(".")[2] in _SAME_STEM:
                    same_stem += 1
    except OSError:
        pass

    if same_stem > 1:
        return os.path.join(output_dir,
                            "%s%s-%s.%s" % (prefix, stem, extension,
                                            new_extension))
    return os.path.join(output_dir, "%s%s.%s" % (prefix, stem, new_extension))


class Run:
    """One conversion run: the settled options, and the counters its workers
    share through files because they are separate processes."""

    def __init__(self, input_dir, output_dir, counter_dir, options, total):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.counter_dir = counter_dir
        self.options = options
        self.total = total

    # --- the counters and the one progress line ---------------------------
    def record(self, category: str, label: str, path: str,
               stream=None) -> None:
        """Bump the global counter and this category's, then print one clean
        line. The lock serialises both across the parallel workers - or, without
        flock, takes nothing and the line comes out unnumbered."""
        out = sys.stdout if stream is None else stream
        with open(os.path.join(self.counter_dir, "lock"), "w") as handle, \
                runlog.take_lock(handle):
                current = self._bump("current")
                self._bump(category)
                out.write("%s%s: %s\n" % (
                    runlog.counted_prefix(current, self.total), label, path))

    def _bump(self, name: str) -> int:
        path = os.path.join(self.counter_dir, name)
        try:
            with open(path) as handle:
                value = int(handle.read() or "0")
        except (OSError, ValueError):
            value = 0
        value += 1
        with open(path, "w") as handle:
            handle.write(str(value))
        return value

    def counter(self, name: str) -> int:
        try:
            with open(os.path.join(self.counter_dir, name)) as handle:
                return int(handle.read() or "0")
        except (OSError, ValueError):
            return 0

    # --- the conversions --------------------------------------------------
    def _convert(self, arguments) -> int:
        return subprocess.run(imagemagick.convert_argv(arguments),
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode

    def _identify(self, arguments) -> str:
        proc = subprocess.run(imagemagick.identify_argv(arguments),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", "replace").strip()

    def _avif_arguments(self, source, out):
        return ["-format", "avif", "-depth", str(AVIF_BIT_DEPTH),
                "-quality", str(self.options["quality"]),
                "-define", "heic:speed=%s" % self.options["speedPreset"],
                source, "-resize", self.options["maxResCommand"], out]

    def crop_convert(self, relative: str) -> None:
        """The -c path: trim, then decide from HOW MUCH came off what the trim
        meant."""
        out = disambiguated_output(relative, "avif", self.input_dir,
                                   self.output_dir)
        source = relative
        dimensions = self._identify(["-format", "%w %h", source]).split()
        if len(dimensions) != 2:
            self.record("converted", "CONV", relative)
            self._convert(self._avif_arguments(source, out))
            return
        width, height = int(dimensions[0]), int(dimensions[1])

        trimmed = os.path.join(self.counter_dir, "trimmed_%d.miff" % os.getpid())
        self._convert([source, "-fuzz", self.options["fuzzCommand"], "-trim",
                       "miff:" + trimmed])
        cut = self._identify(["-format", "%w %h", "miff:" + trimmed]).split()
        try:
            if len(cut) != 2:
                raise ValueError
            width_diff = width - int(cut[0])
            height_diff = height - int(cut[1])

            min_w = width * MIN_TRIM_PERCENT // 100
            max_w = width * MAX_TRIM_PERCENT // 100
            blank_w = width * BLANK_TRIM_PERCENT // 100
            min_h = height * MIN_TRIM_PERCENT // 100
            max_h = height * MAX_TRIM_PERCENT // 100
            blank_h = height * BLANK_TRIM_PERCENT // 100

            # Trimmed away past the blank threshold in either direction: there
            # was nothing on the page.
            if width_diff > blank_w or height_diff > blank_h:
                self.record("blank", "SKIP (blank)", relative,
                            stream=sys.stderr)
                return

            in_margin_range = (min_w <= width_diff <= max_w
                               and min_h <= height_diff <= max_h)
            if in_margin_range:
                corner = self._identify_corner(source)
                if corner is not None and corner[1] >= LIGHTNESS_THRESHOLD:
                    # Give the trimmed page a thin margin back, so the content
                    # does not end flush with the page edge.
                    border = min(min_w, min_h)
                    self.record("trimmed", "TRIM", relative)
                    self._convert(
                        ["-format", "avif", "-depth", str(AVIF_BIT_DEPTH),
                         "-quality", str(self.options["quality"]),
                         "-define",
                         "heic:speed=%s" % self.options["speedPreset"],
                         "miff:" + trimmed,
                         "-bordercolor", corner[0], "-border", str(border),
                         "-resize", self.options["maxResCommand"], out])
                    return
            self.record("converted", "CONV", relative)
            self._convert(self._avif_arguments(source, out))
        finally:
            try:
                os.remove(trimmed)
            except OSError:
                pass

    def _identify_corner(self, source: str):
        """The corner pixel's colour and lightness, in one call - what decides
        whether a margin is light enough to be a margin."""
        answer = subprocess.run(
            imagemagick.convert_argv(
                [source, "-crop", "1x1+0+0", "-colorspace", "HSL",
                 "-format", "%[pixel:p{0,0}] %[fx:100*lightness]", "info:"]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if answer.returncode != 0:
            return None
        parts = answer.stdout.decode("utf-8", "replace").strip().split(" ")
        if len(parts) < 2:
            return None
        try:
            # The shell truncates at the dot, so 39.9 is 39 and not 40.
            return parts[0], int(parts[1].split(".")[0])
        except ValueError:
            return None

    def transcode(self, relative: str) -> None:
        out = disambiguated_output(relative, "avif", self.input_dir,
                                   self.output_dir)
        if not os.path.isfile(relative):
            self.record("notFound", "ERROR (not found)", relative,
                        stream=sys.stderr)
            return
        if os.path.isfile(out):
            self.record("alreadyDone", "SKIP (already done)", relative,
                        stream=sys.stderr)
            return
        if self.options["crop"]:
            self.crop_convert(relative)
            return
        self.record("converted", "CONV", relative)
        self._convert(self._avif_arguments(relative, out))

    def reverse_to_jpeg(self, relative: str) -> None:
        out = disambiguated_output(relative, "jpg", self.input_dir,
                                   self.output_dir)
        if os.path.isfile(out):
            return
        quality = self.options["quality"] + JPEG_QUALITY_BONUS
        self.record("converted", "REV", relative)
        geometry = self.options["maxResCommand"]
        if self.options["crop"]:
            self._convert([relative, "-fuzz", self.options["fuzzCommand"],
                           "-trim", "-quality", str(quality),
                           "-resize", geometry, out])
        else:
            self._convert([relative, "-quality", str(quality),
                           "-resize", geometry, out])


# --- the run -------------------------------------------------------------------

def _images_under(directory: str, extensions, lowercase_only=False):
    """Every image below <directory>, as paths relative to it."""
    wanted = set(extensions) if lowercase_only else {
        e.lower() for e in extensions}
    found = []
    for parent, _dirs, names in os.walk(directory):
        for name in names:
            extension = name.rpartition(".")[2]
            hit = extension in wanted if lowercase_only else \
                extension.lower() in wanted
            if hit:
                found.append(os.path.relpath(os.path.join(parent, name),
                                             directory))
    return sorted(found, key=os.fsencode)


def _prune(directory: str) -> None:
    """Empty folders and zero-byte files go, deepest first - the pretreatment
    both trees get before anything is converted."""
    for parent, dirs, files in os.walk(directory, topdown=False):
        for name in files:
            path = os.path.join(parent, name)
            try:
                if os.path.getsize(path) == 0:
                    os.remove(path)
            except OSError:
                pass
        for name in dirs:
            path = os.path.join(parent, name)
            try:
                if not os.listdir(path):
                    os.rmdir(path)
            except OSError:
                pass


def _mirror_folders(input_dir: str, output_dir: str) -> None:
    for parent, _dirs, _files in os.walk(input_dir):
        relative = os.path.relpath(parent, input_dir)
        target = output_dir if relative == "." else os.path.join(output_dir,
                                                                 relative)
        os.makedirs(target, exist_ok=True)


def _in_worker(state, worker_name: str, relative: str) -> None:
    """One item, in a worker PROCESS.

    The worker's interrupt handling is installed here and not in the conversion
    itself, because the serial path runs that same conversion in the RUN's own
    process - where installing the worker's handler would replace the run's, and
    a Ctrl+C would leave with the queue-stopping status instead of the run's own
    and without its closing report.
    """
    safety.trap_worker_abort()
    getattr(state, worker_name)(relative)


def _run_pool(state, work, jobs: int, worker_name: str) -> None:
    if jobs <= 1:
        worker = getattr(state, worker_name)
        for relative in work:
            if safety.abort_requested():
                return
            worker(relative)
        return

    workerpool.run(work, jobs, _in_worker,
                   lambda relative: (state, worker_name, relative))


def main(argv: list, program: str = "convert-images") -> int:
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
    if safety.require_separate_output(input_dir, output_dir):
        return 1

    crop = "c" in result.given
    reverse = "r" in result.given
    options = {
        "crop": crop,
        "quality": int(result.values["quality"] or 60),
        "speedPreset": result.values["speedPreset"] or "5",
        "fuzzCommand": "%s%%" % (result.values["fuzz"] or "10"),
        "maxResCommand": "%dx%s>" % (UNBOUNDED_EDGE,
                                     result.values["maxRes"] or UNBOUNDED_EDGE),
    }
    jobs = int(result.values["jobs"] or runlog.jobs_per_core(THREADS_PER_CONVERSION))

    runlog.settle_flock()
    tools = [imagemagick.CONVERT_SPEC] + (
        [imagemagick.IDENTIFY_SPEC] if crop else [])
    if tooldeps.require_tools(
            program, tools,
            skip_preflight=bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))):
        return 1
    # fdupes is the one tool whose absence changes WHAT happens rather than
    # whether it happens. A parent may have settled it already; inherit that
    # instead of warning a second time.
    if "HAVE_FDUDES" not in os.environ:
        if tooldeps.tool_present("fdupes"):
            os.environ["HAVE_FDUDES"] = "1"
        else:
            os.environ["HAVE_FDUDES"] = ""
            log("WARNING: fdupes not found (apt install fdupes) - the input "
                "will not be de-duplicated,")
            log("         so identical files are converted and stored as "
                "separate copies. The")
            log("         conversion itself is unaffected.")
    runlog.warn_uncounted_progress()

    # Nothing to convert? Say so before the input is de-duplicated, pruned and
    # lower-cased, so a refused run leaves it exactly as it was.
    if reverse:
        wanted = "AVIF images (.avif) to convert back to JPEG"
        extensions = ["avif"]
    else:
        wanted = "images (%s)" % enums.extension_list(
            list(enums.IMAGE_EXTENSIONS))
        extensions = list(enums.IMAGE_EXTENSIONS)
    if not _images_under(input_dir, extensions):
        return safety.fail_no_relevant_input(input_dir, wanted)

    counter_dir = ""
    started = None
    state = None
    own_safety_log = not (os.environ.get("SAFETY_LOG")
                          and os.path.isfile(os.environ["SAFETY_LOG"]))
    try:
        ramscratch.init_ram_base()
        counter_dir, status = ramscratch.ram_scratch_dir("counters")
        if status != 0 or not counter_dir:
            sys.stderr.write("\nError: no scratch directory could be made for "
                             "this run.\nNothing was changed.\n")
            return 1
        ramscratch.add_exit_cleanup([counter_dir])
        if own_safety_log:
            safety.init_safety_log(os.path.join(counter_dir, "safetySkips.log"))
        safety.init_abort_flag(os.path.join(counter_dir, "abortRequested"))
        safety.trap_run_abort()

        def footer():
            # A run stopped before the conversion began has only the safety
            # recap to give, and gives that rather than a page of zeroes.
            if started is not None and state is not None:
                _print_footer(state, started, result.values, own_safety_log)
            elif own_safety_log:
                safety.report_safety_skips()

        safety.set_run_footer(footer)

        if os.environ.get("HAVE_FDUDES"):
            subprocess.run(["fdupes", "-rdN", input_dir],
                           stdout=subprocess.DEVNULL)
        os.makedirs(output_dir, exist_ok=True)
        _prune(input_dir)
        _prune(output_dir)
        safety.lower_case_extensions(os.path.realpath(input_dir))
        _mirror_folders(input_dir, output_dir)

        os.chdir(input_dir)
        # Lower-cased above, so the search is case-sensitive from here on - the
        # way the shell's second find drops its -iname.
        work = _images_under(".", extensions, lowercase_only=True)
        total = len(work)
        # Belt and braces: the probe refused an empty input, but the preparation
        # between the two could have taken the last image away.
        if total == 0:
            return safety.fail_no_relevant_input(input_dir, wanted)

        for name in ("current", "converted", "trimmed", "blank", "alreadyDone",
                     "notFound"):
            with open(os.path.join(counter_dir, name), "w") as handle:
                handle.write("0")

        state = Run(os.path.realpath(input_dir), os.path.realpath(output_dir),
                    counter_dir, options, total)
        started = time.time()
        _run_pool(state, work, jobs,
                  "reverse_to_jpeg" if reverse else "transcode")
        safety.exit_if_aborted()
        safety.print_run_footer()
        return 0
    finally:
        ramscratch.run_exit_cleanup()


def _print_footer(state, started, values, own_safety_log) -> None:
    runtime = time.time() - started
    total = state.total
    print("")
    print("Converted %d images in %.2f seconds" % (total, runtime))
    if total > 0:
        print("%.3f seconds per image" % (runtime / total))

    for label, name in (("input not found", "notFound"),
                        ("already done", "alreadyDone"),
                        ("blank", "blank"),
                        ("converted", "converted"),
                        ("trimmed", "trimmed")):
        count = state.counter(name)
        if count >= 1 and total > 0:
            print("%s: %d (%.1f%%)" % (label, count, 100.0 * count / total))

    # The same numbers again, machine-readable, for a caller that runs this
    # script once per unit of work and throws each child's output away. Under a
    # lock, because several of those children can finish at once and each one's
    # record has to survive whole.
    stats_file = os.environ.get("imageStatsFile", "")
    if stats_file:
        with open(stats_file + ".lock", "w") as handle, \
                runlog.take_lock(handle), \
                open(stats_file, "a") as out:
            out.write("%d %d %d %d %d %d\n" % (
                total, state.counter("converted"),
                state.counter("trimmed"), state.counter("blank"),
                state.counter("alreadyDone"),
                state.counter("notFound")))

    if own_safety_log:
        safety.report_safety_skips()


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name))


if __name__ == "__main__":
    sys.exit(cli())
