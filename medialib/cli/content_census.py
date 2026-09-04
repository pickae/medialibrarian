"""content-census: what a media library HOLDS, as four reports.

The spec below is data, rendered and parsed by medialib.lib.clioptions, so the
help page and every refusal come out of the one renderer every command shares.
What reaches a terminal is pinned byte for byte by the recorded contract under
tests/data/cliContract rather than described here.
"""

import sys

from medialib import commands
from medialib.cli import census_run
from medialib.lib import clioptions

# The name is the caller's to supply: a command started by another one prints the
# name that command was told, not its own.
USAGE_HEAD = """Usage:
    {program} [options] <inputPath>...
Argument:
    <inputPath>     The folder to census, recursively. The reports are written
                    into it, named after it. Several may be given: each is
                    censused as its own library, which is what lets
                    content-census-bi tell them apart afterwards - and each is
                    read by a worker of its own, since several paths means
                    several disks.
Options:"""

OPT_SPEC = """
d | <levels> | Take the libraries <levels> level(s) BELOW each <inputPath>
                    instead of the paths themselves: -d 1 makes every subfolder
                    of a given path a library of its own, -d 2 every grandchild,
                    and so on. Files sitting beside those folders, or above them
                    next to the path given, belong to no library and are not
                    censused. Each such library gets its own reports, named after
                    the way down to it (-d 1 over /media/Films writes
                    videoFilmsMarvel.csv for its Marvel subfolder), so the cube's
                    library axis gets a level to drill down into. The libraries
                    under one given path are censused one after the other - they
                    share that path's disk. Default 0: the paths given are the
                    libraries.
o | <dir> | Write the reports into <dir> instead of into the library, for
                    censusing a read-only or a shared library. With several
                    libraries this is what puts all their reports in one place.
                    Made if it is not there yet, so a run can be pointed at a
                    fresh folder without creating it first.
b |  | Also build the DuckDB hypercubes from the reports this run
                    writes, by handing them to content-census-bi. Only a
                    shorthand: that command can always be run on its own over
                    reports that already exist, which is the thing to do when the
                    census itself does not need repeating.
t |  | Write tab-separated .tsv files instead of comma-separated
                    .csv ones.
h |  | Print this help page.
"""

# -d is a number of levels, so anything else is a typo rather than a depth of
# zero: "-d one" silently censusing the paths themselves would look like the flag
# simply doing nothing.
OPT_CHECKS = """
d | nonNegInt | depth in levels
"""

OPT_VARS = "d:depth o:outDir b:runBI"
OPT_COLUMN = 20
OPT_LONG = "d:depth o:output-dir b:build-cubes t:tsv h:help"


def spec(program: str) -> clioptions.Spec:
    """The script's declaration, with the program name the shell would print."""
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        checks=OPT_CHECKS,
        column=OPT_COLUMN,
    )


def cli(argv: list[str] | None = None) -> int:
    """The module's own entry point, and the console script's."""
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


def main(argv: list[str], program: str = "content-census",
         script_dir: str = "") -> int:
    """The command line, then the census. Returns the process status.

    -h prints the page and exits 0; an unusable command line prints the refusal
    and exits 1, which is what the CLI contract fixtures pin. An interrupted run
    exits 130, the repo's interrupt convention.
    """
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        sys.stderr.write(clioptions.usage_error_text(declaration, error.message))
        return 1

    if clioptions.args_out_of_range(len(result.positionals), 1, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    # -t is a flag the script acts on itself rather than a value, so it is read
    # from what the parse recorded as given.
    tab = "t" in result.given
    script_dir = script_dir or commands.script_dir()
    try:
        return census_run.run(
            arguments=result.positionals,
            depth=int(result.values["depth"] or 0),
            out_dir=result.values["outDir"],
            run_bi=bool(result.values["runBI"]),
            separator="\t" if tab else ",",
            extension="tsv" if tab else "csv",
            script_dir=script_dir,
            program=program,
        )
    except census_run.Refusal as refusal:
        sys.stderr.write(refusal.text)
        return refusal.status


if __name__ == "__main__":
    sys.exit(cli())
