"""cue-to-chapters: a cue sheet written out as the OGM chapter file mkvmerge
consumes, one CHAPTERnn= / CHAPTERnnNAME= pair per chapter.

The conversion itself is `medialib/lib/cuechapters.py`.

The one CLI here that parses no options at all: two paths, both required, so
there is no help page to ask for and the usage is the whole spec.
"""

import sys

from medialib import commands
from medialib.lib import clioptions, cuechapters

USAGE_HEAD = """Usage:
    {program} <input.cue> <output.chapters>"""


def spec(program: str) -> "clioptions.Spec":
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        # The page is printed without the credits line and on stderr, which is
        # where this usage has always gone.
        no_args_with_credits=False,
        no_args_stream="stderr",
    )


def main(argv: list, program: str = "cue-to-chapters",
         script_dir: str = "") -> int:
    declaration = spec(program)

    # No parse loop: this script never had one, so a leading "-h" is a path like
    # any other and the only gate is how many there are. Exactly two, not "at
    # least two": a third path would silently be ignored.
    if clioptions.args_out_of_range(len(argv), 2, 2):
        sys.stderr.write(clioptions.no_args_text(declaration))
        return 1

    cuechapters.write_chapters_from_cue(argv[0], argv[1])
    return 0


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, commands.program_name(__spec__.name),
                commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
