"""clean-folder-structure: the names inside a nested folder tree, cleaned.

Every level is considered: the sub-folders directly under the input, their
sub-sub-folders, down to the leaves, and the files sitting at any level. At each
directory the immediate children of one kind - all sub-folders, or all files -
are one group of SIBLINGS, and a group goes through three passes: individual
cleaning with the prefix split off, collective stripping of the text they all
share, and a recovery pass for a prefix that was hidden behind that shared text.

Folders are renamed first, at every level, and files afterwards, because a
directory's name changes when its PARENT is processed - so the tree has to be
stable before anything reads a file's path. Sub-folders left empty are removed
at the very end; the input root itself is always kept.

Simulation mode (-s) runs the whole pipeline against a name-only mirror in a
sandbox, so the input is only ever read, and writes "before.tree"/"after.tree"
into it. Every step here is name-only, which is what makes an empty stand-in per
file a faithful preview rather than an approximation.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

from medialib import commands
from medialib.lib import (
    cleannamescollectively,
    cleannamesindividually,
    clioptions,
    cover,
    mtp,
    numbering,
    plurality,
    prefixes,
    safety,
    tooldeps,
)
from medialib.lib.runlog import log
from medialib.lib.versionsort import version_key

USAGE_HEAD = """Usage:
    {program} [options] <inputDir>
Options:"""

# The spec is DATA, and the page it renders is compared byte for byte against the
# recorded contract under tests/data/cliContract.
OPT_SPEC = """
y |  | Sort files named "YYYYMMDD Filename.ext" into yearly
                    subfolders "YYYY/YYYYMMDD Filename.ext".
d |  | Before cleaning file names, normalise a leading date of the
                    form YYYY<sep>MM<sep>DD<sep> (separators: space . / _ -) at
                    the start of a file name into the compact "YYYYMMDD " form,
                    keeping the rest of the name. Applied across all subfolders.
n |  | Clean folder names as usual, but instead of cleaning file
                    names, just number the files in each folder (01, 02, ...
                    naturally sorted and zero-padded). Only the plurality
                    (most common) filetype in a folder is numbered; other
                    files (e.g. a cover.jpg) are left alone.
f | <file> | Read the name fragments to remove from <file> (one per line,
                    '#' comments and blank lines ignored) instead of from
                    data/fragments.txt beside this script. Without this option that
                    file is used when it is there, and names are cleaned without any
                    fragment removal when it is not.
s |  | Simulate: do not touch the input at all. Mirror its structure
                    into a sandbox, run the full cleaning there, and write
                    "before.tree" and "after.tree" into the input folder so the
                    result can be reviewed and tested risk-free. Combines with the
                    other options (-y, -d, -n) to preview their effect too.
h |  | Print this help page.
"""

OPT_VARS = ("y:sortIntoYears d:fixDates n:numberFiles s:simulate "
            "f:fragmentsOverride")
OPT_COLUMN = 20
OPT_LONG = ("y:sort-into-years d:fix-dates n:number-files f:fragments s:simulate "
            "h:help")

# A file is sorted into a year folder by the first four digits of an eight-digit
# date at the start of its name, followed by a space and at least one character.
YEAR_PREFIX = r"^([0-9]{4})[0-9]{4} "

# The date shape -d normalises. Loose but effective: the year starts 1 or 2, the
# month 0 or 1, the day 0-3, and one space, dot, slash, underscore or dash
# separates each part. A name already in "YYYYMMDD ..." form has no separator
# after the year, so it does not match and repeated runs are idempotent.
DATE_PREFIX = r"^([12][0-9]{3})[ ./_-]([01][0-9])[ ./_-]([0-3][0-9])[ ./_-](.+)$"


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
    )


def _extension_of(base: str, mode: str) -> tuple[str, str]:
    """(name, extension) for one sibling, by the rule the shell splits on.

    Folders never have one, and a dotfile's leading dot begins a NAME rather than
    an extension.
    """
    if mode == "files" and "." in base and not base.startswith("."):
        stem, _, extension = base.rpartition(".")
        return stem, extension
    return base, ""


def siblings(directory: str, mode: str) -> list[str]:
    """The immediate children of one kind, in the order `sort -V` puts them.

    The PATHS are sorted rather than the names, because that is what `find`
    prints and hands to `sort`.
    """
    want_dir = mode == "folders"
    found = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) == want_dir:
                    found.append(entry.path)
    except OSError:
        return []
    return sorted(found, key=version_key)


def rename_siblings(directory: str, mode: str, fragments_file: str,
                    skips: safety.SkipLog) -> int:
    """One pass over one directory's siblings; how many really were renamed."""
    if not os.path.isdir(directory):
        return 0
    items = siblings(directory, mode)
    if not items:
        return 0

    # First pass: individual cleaning, splitting off the prefix and (for files
    # that have one) the extension.
    extensions, item_prefixes, cores = [], [], []
    for full in items:
        name, extension = _extension_of(os.path.basename(full), mode)
        prefix, core = cleannamesindividually.clean_names_individually(
            name, fragments_file)
        extensions.append(extension)
        item_prefixes.append(prefix)
        cores.append(core)

    clean_names = list(cores)

    # Which siblings take part in the collective pass: the plurality filetype in
    # files mode, all of them in folders mode. A lone odd file - one cover.jpg
    # among many .mp3s - must neither join the group nor, by not sharing their
    # affix, stop the group's common text from being stripped.
    group = plurality.plurality_group_indices(mode, extensions)

    if len(group) > 1:
        group_cores = [cores[i] for i in group]
        # The ORIGINAL basename lengths, extension included: the files-mode
        # truncation guard on the suffix is measured against them.
        group_lengths = [len(os.path.basename(items[i])) for i in group]
        cleaned = cleannamescollectively.clean_names_collectively(
            group_cores, mode, group_lengths)
        for position, index in enumerate(group):
            clean_names[index] = cleaned[position]

    # Third pass: a prefix can have been hidden behind the common leading text
    # the collective pass has just removed, so the individual cleaner runs again
    # on the group's cleaned names to recover it.
    if group and not item_prefixes[group[0]]:
        for index in group:
            # Only an item that came out of the first pass WITHOUT a prefix can
            # have one hidden. One that already split a prefix off has nothing to
            # recover - its core no longer holds it - and re-running would
            # overwrite the real prefix with an empty one. That is how a sparsely
            # numbered group ("02 Title" and "04 Title" among unnumbered
            # siblings) lost exactly the numbers that told its members apart.
            if item_prefixes[index]:
                continue
            prefix, core = cleannamesindividually.clean_names_individually(
                clean_names[index], fragments_file)
            item_prefixes[index] = prefix
            clean_names[index] = core

    # A prefix that is ALSO still the leading token of the core says nothing
    # twice, so the outer copy goes.
    item_prefixes = prefixes.wipe_doubled_prefixes(item_prefixes, clean_names,
                                                   group)
    # Then the group's numeric prefixes are re-padded to one width, before the
    # final names are assembled.
    item_prefixes, clean_names = prefixes.normalize_prefix_padding(
        item_prefixes, clean_names, group)
    # And a prefix the whole group shares distinguishes nothing.
    item_prefixes = prefixes.wipe_uniform_prefixes(item_prefixes, clean_names,
                                                   group)

    return _apply(directory, item_prefixes, clean_names, extensions, items,
                  skips)


def _apply(directory: str, item_prefixes, clean_names, extensions, items,
           skips: safety.SkipLog) -> int:
    """The last step, kept apart so the passes above read as the sequence of
    decisions they are and this reads as the one place that touches disk."""
    changes = 0
    for index, name in enumerate(clean_names):
        prefix = item_prefixes[index]
        extension = extensions[index]

        # "01 chapter 01" becomes "01", not "01 01".
        if prefix == name:
            name = ""

        # The prefix only when there is one, so no name gains a leading space;
        # and when the core is empty - a folder that is nothing but a numeric or
        # date prefix, a "YYYY" year folder - the prefix alone, so the name is
        # stable across runs rather than growing a trailing space.
        if prefix:
            name = "%s %s" % (prefix, name) if name else prefix
        # Never rename to an empty name: it would collide with the parent.
        if not name:
            continue

        path = os.path.join(directory, "%s.%s" % (name, extension)
                            if extension else name)
        path = path.replace("//", "/")

        # Only a real rename counts; a no-op and a refused clobber are both
        # decided inside safe_rename, which records the second.
        if safety.safe_rename(items[index], path, skips):
            changes += 1
    return changes


def rename_siblings_until_stable(directory: str, mode: str,
                                 fragments_file: str,
                                 skips: safety.SkipLog) -> None:
    """Clean one directory's siblings until a whole pass changes nothing.

    Stabilising each directory on its own, at this granularity, is what keeps a
    name that only settles after two passes from being left half-cleaned.
    """
    count = len(siblings(directory, mode))
    # Silent when there is nothing of this kind here, so no directory without
    # work is ever announced.
    if not count:
        return

    log('  "%s": cleaning %d %s' % (os.path.basename(directory), count, mode))

    # Only the iterations that CHANGED something are counted: an already stable
    # directory reports 0, one change followed by a clean pass reports 1.
    iterations = 0
    while True:
        changed = rename_siblings(directory, mode, fragments_file, skips)
        if not changed:
            break
        iterations += 1

    if iterations >= 2:
        log("    Stabilised after %d iteration(s)" % iterations)


def sort_files_into_year_folders(root: str, skips: safety.SkipLog) -> None:
    """Every "YYYYMMDD Name.ext" file into a "YYYY" folder beside it (-y)."""
    log("Sorting date-named files into yearly subfolders")

    # The list is taken up front. Moving files into folders created during a live
    # traversal would let the walk descend into them and process the same file
    # twice.
    files = _files_below(root)

    moved = 0
    for path in files:
        base = os.path.basename(path)
        match = re.match(YEAR_PREFIX, base)
        if not match:
            continue
        year = match.group(1)
        parent = os.path.dirname(path)
        # Already in the right place, which is what makes a repeated run a no-op.
        if os.path.basename(parent) == year:
            continue

        target_dir = os.path.join(parent, year)
        os.makedirs(target_dir, exist_ok=True)
        if safety.safe_rename(path, os.path.join(target_dir, base), skips):
            moved += 1

    log("  Moved %d file(s) into yearly subfolders" % moved)


def fix_date_prefixes(root: str, skips: safety.SkipLog) -> None:
    """A leading "YYYY<sep>MM<sep>DD<sep>" compacted to "YYYYMMDD " (-d).

    Recursive in one pass, and kept out of both the per-sibling cleaner and the
    -n numbering path, either of which would overwrite the prefix.
    """
    log("Normalising date prefixes in file names (YYYYMMDD)")

    files = _files_below(root)

    fixed = 0
    for path in files:
        base = os.path.basename(path)
        match = re.match(DATE_PREFIX, base)
        if not match:
            continue
        new_base = "%s%s%s %s" % match.groups()
        if new_base == base:
            continue
        target = os.path.join(os.path.dirname(path), new_base)
        if safety.safe_rename(path, target, skips):
            fixed += 1

    log("  Fixed %d date prefix(es)" % fixed)


def _files_below(root: str) -> list[str]:
    """Every file under <root>, at any depth: what `find -type f` lists."""
    found = []
    for parent, _dirs, names in os.walk(root):
        for name in names:
            found.append(os.path.join(parent, name))
    return found


def _directories(root: str, deepest_first: bool) -> list[str]:
    """<root> and every directory below it, in `find`'s order or `-depth`'s."""
    found = []
    for parent, _dirs, _names in os.walk(root, topdown=not deepest_first):
        found.append(parent)
    return found


def remove_empty_subfolders(root: str) -> None:
    """The sub-folders that are now empty, deepest first; the root is kept.

    Deleting an entry bumps the containing directory's date, so each removal
    puts its parent's back - a folder that merely lost an empty child should not
    show a fresh date.
    """
    log("Removing empty sub-folders")

    # Gathered up front, deepest-first, so a folder that only becomes empty once
    # its own empty children are gone is still removed in this single pass.
    directories = [d for d in _directories(root, deepest_first=True)
                   if d != root]

    removed = 0
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        try:
            with os.scandir(directory) as entries:
                if any(True for _ in entries):
                    continue
        except OSError:
            continue
        parent = os.path.dirname(directory)
        try:
            parent_at = os.stat(parent).st_mtime
        except OSError:
            parent_at = None
        try:
            os.rmdir(directory)
        except OSError:
            continue
        removed += 1
        if parent_at is not None:
            try:
                os.utime(parent, (parent_at, parent_at))
            except OSError:
                pass

    log("  Removed %d empty sub-folder(s)" % removed)


# --- simulation mode ---------------------------------------------------------

def sim_tree_snapshot(root: str) -> str:
    """A stable, deterministic `tree` of <root>, rooted at its own basename.

    No colour, ascii glyphs, hidden files included, no summary line - which is
    what lets two snapshots diff cleanly and lets the tests assert them byte for
    byte.
    """
    done = subprocess.run(
        ["tree", "-a", "-n", "--charset", "ascii", "--noreport",
         os.path.basename(root)],
        cwd=os.path.dirname(root), stdout=subprocess.PIPE)
    return done.stdout.decode("utf-8", "surrogateescape")


def mirror_structure(source: str, destination: str) -> None:
    """<source>'s tree under <destination>, every file re-created empty.

    A before.tree/after.tree left by an earlier simulation is skipped, so
    repeated simulations stay stable.
    """
    os.makedirs(destination, exist_ok=True)
    for parent, dirs, names in os.walk(source):
        relative = os.path.relpath(parent, source)
        for name in dirs:
            os.makedirs(os.path.join(destination, relative, name),
                        exist_ok=True)
        for name in names:
            entry = os.path.normpath(os.path.join(relative, name))
            if entry in ("before.tree", "after.tree"):
                continue
            target = os.path.join(destination, entry)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            open(target, "w").close()


class Simulation:
    """The sandbox mirror, and the two snapshots that are the whole point of -s."""

    def __init__(self, input_path: str) -> None:
        self.original = os.path.abspath(input_path)
        self.sandbox = tempfile.mkdtemp()
        self.mirror = os.path.join(self.sandbox,
                                   os.path.basename(self.original))

    def start(self) -> str:
        log('Simulation mode: mirroring "%s" into a sandbox (original left '
            'untouched)' % self.original)
        mirror_structure(self.original, self.mirror)
        with open(os.path.join(self.sandbox, "before.tree"), "w") as fh:
            fh.write(sim_tree_snapshot(self.mirror))
        return self.mirror

    def finish(self) -> None:
        with open(os.path.join(self.sandbox, "after.tree"), "w") as fh:
            fh.write(sim_tree_snapshot(self.mirror))
        for name in ("before.tree", "after.tree"):
            shutil.copyfile(os.path.join(self.sandbox, name),
                            os.path.join(self.original, name))
        log('Simulation wrote "%s/before.tree" and "%s/after.tree"'
            % (self.original, self.original))

    def clean_up(self) -> None:
        shutil.rmtree(self.sandbox, ignore_errors=True)


def main(argv: list, program: str = "clean-folder-structure",
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

    if clioptions.args_out_of_range(len(result.positionals), 1, 1):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    sort_into_years = "y" in result.given
    fix_dates = "d" in result.given
    number_files = "n" in result.given
    simulate = "s" in result.given
    fragments_override = result.values["fragmentsOverride"]

    script_dir = script_dir or commands.script_dir()

    input_path = result.positionals[0].rstrip("/")

    # A phone folder may be pasted as the "mtp://..." URI a file manager copies,
    # and only the gvfs path it stands for can be stat'ed. Settled before
    # anything looks at the path, so the routing below and the -d check in the
    # run both see that one form.
    if input_path.startswith("mtp://"):
        resolved, refusal = mtp.resolve_mtp_uri(input_path)
        if not resolved:
            sys.stderr.write(refusal + "\n")
            return 1
        input_path = resolved

    # A target on an MTP mount is cleaned in place when the mount can rename,
    # which is the common case and costs nothing: an MTP rename sets an object's
    # name property, so no content moves. A mount that refuses renames is handed
    # to the adb helper, which applies the identical renames with `adb shell mv`
    # - also content-free, at the price of USB debugging. Which of the two is
    # settled by TRYING a rename rather than by assuming.
    #
    # -s never renames on the device, so it skips the probe and stays local. The
    # helper re-invokes this script on a purely local mirror, so there is no
    # recursion back into this path either way.
    if "/gvfs/mtp:host=" in input_path and not simulate:
        if mtp.mtp_mount_can_rename(input_path):
            log("Phone folder on an MTP mount that renames in place - cleaning "
                "it directly")
        else:
            log("Phone folder on an MTP mount that refuses renames - applying "
                "them with adb instead")
            forward = []
            for flag, given in (("-y", sort_into_years), ("-d", fix_dates),
                                ("-n", number_files)):
                if given:
                    forward.append(flag)
            if fragments_override:
                forward += ["-f", fragments_override]
            commands.exec_command("clean-folder-structure-adb",
                                  forward + [input_path],
                                  script_dir=script_dir)

    # Which fragments this run removes, settled once here: the file named with
    # -f, else data/fragments.txt beside this script, else none at all. A -f path
    # that cannot be read STOPS the run, because cleaning a whole tree without
    # the fragments someone asked for would have to be undone by hand.
    fragments_file, ok = cleannamesindividually.fragments_file_for(
        fragments_override)
    if not ok:
        sys.stderr.write('The fragments file "%s" does not exist or is empty.\n'
                         % fragments_override)
        return 1

    if not os.path.isdir(input_path):
        sys.stderr.write("Not a directory: %s\n" % input_path)
        return 1

    # Anything at all inside is work - a file or a sub-folder, at any level - so
    # only a folder with no entry whatsoever has nothing to clean. The folder
    # itself is never removed for being empty, so re-running once it has content
    # just works.
    if safety.is_empty_folder(input_path):
        return safety.fail_no_relevant_input(
            input_path, "files or sub-folders whose names could be cleaned")

    safety.init_safety_log()
    skips = safety.RunSkipLog()
    # The recap of every rename held back to avoid an overwrite. Cleaning a large
    # tree takes a while, and a run stopped part-way has still renamed things and
    # still skipped some - so the recap is exactly as owed then as after a
    # finished run.
    safety.set_run_footer(safety.report_safety_skips)
    safety.trap_run_abort()

    original_input = input_path
    simulation = None
    if simulate:
        # The one external tool this script has, and only in this mode: both
        # snapshots are a `tree` invocation.
        if tooldeps.require_tools("simulation mode (-s)", ["tree"]):
            return 1
        simulation = Simulation(input_path)
        input_path = simulation.start()

    try:
        log('Cleaning folder structure in "%s"'
            % (simulation.original if simulation else original_input))

        # Phase 1: folder names at every level, bottom-up. Deepest first
        # guarantees each directory still carries its original path when it is
        # reached, because a directory's name only changes when its parent is
        # processed. The input path itself is never renamed.
        log("Phase 1/2: Cleaning folder names (all levels)")
        for directory in _directories(input_path, deepest_first=True):
            rename_siblings_until_stable(directory, "folders", fragments_file,
                                         skips)

        # Phase 2: the files. The folder structure is stable now, so the order
        # between levels does not matter.
        if number_files:
            log("Phase 2/2: Numbering files by plurality filetype (all levels)")
            for directory in _directories(input_path, deepest_first=False):
                numbering.number_files_in_folder(directory, siblings(directory, "files"))
        else:
            log("Phase 2/2: Cleaning file names (all levels)")
            # First in this phase, before any per-directory cleaning: it depends
            # on nothing that follows.
            if fix_dates:
                fix_date_prefixes(input_path, skips)
            for directory in _directories(input_path, deepest_first=False):
                rename_siblings_until_stable(directory, "files",
                                             fragments_file, skips)

        # The best folder/front/cover image in each folder, renamed to
        # "folder.<ext>". Independent of -n, -d and -y.
        log("Normalising cover art to folder.<ext> (all levels)")
        for directory in _directories(input_path, deepest_first=False):
            cover.rename_cover_to_folder(directory, skips)

        # After the name cleaning, so the "YYYYMMDD ..." prefixes are already in
        # their final form.
        if sort_into_years:
            sort_files_into_year_folders(input_path, skips)

        # Last, so a folder emptied by moving its files elsewhere is caught too.
        remove_empty_subfolders(input_path)

        safety.print_run_footer()

        if simulation:
            simulation.finish()

        log('Done cleaning folder structure in "%s"'
            % (simulation.original if simulation else original_input))
    finally:
        if simulation:
            simulation.clean_up()
    return 0


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
