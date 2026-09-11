"""content-census-bi: the census reports turned into a queryable DuckDB
database of hypercubes, and one .html page to explore them in.

Both halves are written every time, which is the design rather than an
afterthought: a cube nobody can look at answers no question.

Everything about WHAT a cube is made of - which columns are measures and which
are axes, how a resolution becomes a tier, why a bitrate is averaged by duration
- lives in medialib/lib/cubes.py. This module decides which reports are real,
where the database goes, and in what order the statements are handed over.
"""

import os
import shutil
import subprocess
import sys
import tempfile

from medialib import commands
from medialib.lib import censusviewer, clioptions, cubes, ramscratch, safety, tooldeps
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <reportPath>...
Arguments:
    <reportPath>    A census report (audio*.csv, video*.csv, images*.csv,
                    books*.csv, comics*.csv, or the .tsv of each), or a folder to
                    look for them in, recursively. Several may be given,
                    including several libraries' worth of the same type: they are
                    loaded into one cube per type and stay separable along its
                    \"library\" axis.
Options:"""

OPT_SPEC = """
    (may be given before the reports or after them)
o | <file> | Write the database to <file> instead of to
                    contentCensusBI.duckdb beside the first report. The .html page
                    is always written beside it, under the same name.
e | <dir> | Also export each cube as <type>Cube.csv into <dir>.
q | <sql> | Run one query against the finished database and print it.
                    The tables are named above; try
                      -q \"SELECT * FROM videoCube WHERE depth = 1 AND resolution != 'ALL'\"
s |  | Print each cube's grand total when it is built.
h |  | Print this help page.
"""

OPT_VARS = "o:dbPath e:exportDir q:query s:showTotals"
OPT_COLUMN = 20
OPT_LONG = "o:database e:export-dir q:query s:show-totals h:help"

# The report name prefixes this command looks for, which are the census's own
# content types and not a second list of them.
REPORT_GLOBS = cubes.TYPES


class Refusal(Exception):
    """A refusal that ends the run: the text is what the user sees."""

    def __init__(self, text: str, status: int = 1):
        super().__init__(text)
        self.text = text
        self.status = status


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
    )


def _looks_like_a_report(name: str) -> bool:
    lowered = name.lower()
    return (lowered.endswith((".csv", ".tsv"))
            and lowered.startswith(REPORT_GLOBS))


def collect_paths(arguments):
    """Every candidate report, and whether each was NAMED or FOUND.

    A path given on the command line is a CLAIM that it is a report and is
    refused by name when it is not; a path found by looking inside a folder is a
    GUESS, and a file that does not fit is passed over with a line saying so - a
    folder holding a census and somebody's spreadsheet is not an error.
    """
    found = []
    for argument in arguments:
        if os.path.isdir(argument):
            directory = os.path.realpath(argument)
            inside = []
            for parent, _dirs, names in os.walk(directory):
                for name in names:
                    if _looks_like_a_report(name):
                        inside.append(os.path.join(parent, name))
            for path in sorted(inside, key=os.fsencode):
                found.append((path, False))
        elif os.path.isfile(argument):
            found.append((argument, True))
        else:
            raise Refusal('\nError: "%s" is neither a file nor a folder.\n'
                          "Nothing was changed.\n" % argument)
    if not found:
        raise Refusal(
            "\nError: none of the given paths holds a census report.\n"
            "Expected %s (or .tsv),\nas written by content-census. Nothing was "
            "changed.\n" % _expected_names())
    return found


def _expected_names():
    """The report names a refusal offers - "audio*.csv, video*.csv, ... or
    comics*.csv" - built from the census's own types so a type added there is
    named here too."""
    names = ["%s*.csv" % content for content in REPORT_GLOBS]
    return "%s or %s" % (", ".join(names[:-1]), names[-1])


def _spoken_types():
    """The content types the way a sentence reads them - "audio, video, images,
    books or comics"."""
    names = list(REPORT_GLOBS)
    return "%s or %s" % (", ".join(names[:-1]), names[-1])


def _first_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as handle:
            return handle.readline().rstrip("\n").rstrip("\r")
    except OSError:
        return ""


def classify(found):
    """The reports that survive the check, per type, and what was passed over.

    A report is what its first line says it is: the header has to be exactly the
    one content-census writes for that type, which is asked of the column
    definitions themselves so the two cannot disagree.
    """
    by_type = {content: [] for content in cubes.TYPES}
    skipped = []
    for path, explicit in found:
        content = cubes.report_type(path)
        if not content:
            reason = ("its name does not start with %s"
                      % _spoken_types())
            if explicit:
                raise Refusal('\nError: "%s" is not a census report: %s.\n'
                              "Nothing was changed.\n" % (path, reason))
            skipped.append("%s: %s" % (path, reason))
            continue

        separator = "\t" if path.lower().endswith(".tsv") else ","
        if not cubes.header_matches(content, _first_line(path), separator):
            reason = ("its first line is not the header content-census "
                      "writes for a %s report" % content)
            if explicit:
                raise Refusal(
                    '\nError: "%s" cannot be read: %s.\n'
                    "Re-run content-census over that library to get a current "
                    "report.\nNothing was changed.\n" % (path, reason))
            skipped.append("%s: %s" % (path, reason))
            continue

        # Absolute, because the path goes into a SQL statement that DuckDB
        # resolves against its own working directory and not against this one's.
        by_type[content].append(
            os.path.join(os.path.realpath(os.path.dirname(path) or "."),
                         os.path.basename(path)))

    if not any(by_type.values()):
        text = ("\nError: none of the %d file(s) found is a census report.\n"
                % len(found))
        for entry in skipped:
            text += "  %s\n" % entry
        raise Refusal(text + "Nothing was changed.\n")
    return by_type, skipped


def separators(by_type):
    """One separator per type, refused when a type's reports mix the two.

    One reader call per type reads all of that type's reports at once and a
    single read_csv has one delimiter, so a type whose reports are not all the
    same format is refused rather than half-read - before anything is written,
    or "nothing was changed" would not be true.
    """
    settled = {}
    for content, files in by_type.items():
        if not files:
            continue
        kinds = {"\t" if path.lower().endswith(".tsv") else ","
                 for path in files}
        if len(kinds) > 1:
            raise Refusal(
                "\nError: the %s reports are not all in the same format - some "
                "are .csv\nand some are .tsv, and one table cannot be read from "
                "both at once.\nBuild them in two runs, or re-census with one "
                "format. Nothing was changed.\n" % content)
        settled[content] = kinds.pop()
    return settled


def resolve_db_path(db_path: str, first_report: str) -> str:
    if not db_path:
        db_path = os.path.join(os.path.dirname(first_report),
                               "contentCensusBI.duckdb")
    directory = os.path.dirname(db_path) or "."
    if not os.path.isdir(directory):
        raise Refusal('\nError: "%s" is not a folder, so the database cannot be '
                      "written there.\nNothing was changed.\n" % directory)
    if not os.access(directory, os.W_OK):
        raise Refusal('\nCannot write the database into "%s": no write '
                      "permission.\nGive a writable path with -o instead. "
                      "Nothing was changed.\n" % directory)
    return os.path.join(os.path.realpath(directory),
                        os.path.basename(db_path))


def resolve_export_dir(export_dir: str) -> str:
    if not export_dir:
        return ""
    if not os.path.isdir(export_dir):
        raise Refusal('\nError: the export folder "%s" is not a folder.\n'
                      "Nothing was changed.\n" % export_dir)
    if not os.access(export_dir, os.W_OK):
        raise Refusal('\nCannot export into "%s": no write permission.\n'
                      "Nothing was changed.\n" % export_dir)
    return os.path.realpath(export_dir)


# --- the build -----------------------------------------------------------------

def build_sql(by_type, settled, export_dir, scratch):
    """The whole build as one script, and which types it builds.

    Written to one file and handed to DuckDB in one go rather than one statement
    per call: the build is then a single process's worth of work, and the file is
    left behind on failure so the statement that broke can be read.
    """
    statements = []
    built = []
    for content in cubes.TYPES:
        files = by_type.get(content) or []
        if not files:
            continue
        log("%s: %d report(s)" % (content, len(files)))
        statements.append(cubes.load_sql(content, files, settled[content]))
        statements.append("")
        statements.append(cubes.fact_sql(content))
        statements.append("")
        statements.append(cubes.cube_sql(content))
        if export_dir:
            statements.append(cubes.export_sql(content, export_dir))
        # The viewer's copy of the facts, in the same pass so the database is
        # opened once. It lands in the scratch: it is an ingredient of the page,
        # not an output of this run.
        statements.append(censusviewer.viewer_grain_export_sql(
            content, os.path.join(scratch, content + ".viewer.csv")))
        built.append(content)
    return "\n".join(part for part in statements if part is not None), built


def _duckdb(arguments, **kwargs):
    return subprocess.run(["duckdb"] + list(arguments), **kwargs)


def run_build(sql_file: str, build_path: str, db_path: str = "") -> None:
    """The whole script through DuckDB, into the database being BUILT.

    ``db_path`` is where that database will end up, and is only used to name the
    SQL kept behind on failure: a build that failed leaves nothing at the
    destination, so the statements have to be readable from beside it.
    """
    db_path = db_path or build_path
    with open(sql_file) as handle:
        done = _duckdb(["-bail", build_path], stdin=handle,
                       stdout=subprocess.DEVNULL)
    if done.returncode == 0:
        return
    sys.stderr.write("\nDuckDB refused the build. The statements it was given "
                     "are in:\n  %s\n" % sql_file)
    # The scratch is in RAM and about to go, so the one thing worth keeping -
    # the SQL that failed - is copied out next to the database first.
    try:
        shutil.copyfile(sql_file, db_path + ".failed.sql")
        sys.stderr.write("  (copied to %s)\n" % (db_path + ".failed.sql"))
    except OSError:
        pass
    raise Refusal("Nothing but that file was changed; the previous database, "
                  "if there was one, is still there.\n")


# --- the replacement -----------------------------------------------------------
# A cube is rebuilt from nothing every time, and "from nothing" must not mean
# "after the last good one was deleted": the build happens beside the
# destination and only a finished database is moved onto it.

def build_target(db_path: str) -> str:
    """Where the new database is assembled: inside a directory of its own, in
    the folder the database itself goes to.

    Beside the destination rather than in the RAM scratch, because the commit is
    an ``os.replace`` and that only works within one filesystem. A directory
    rather than a name, because the name has to be free for DuckDB to create and
    a directory is the only thing this can claim without a race.
    """
    try:
        holder = tempfile.mkdtemp(prefix=".building.",
                                  dir=os.path.dirname(db_path) or ".")
    except OSError as error:
        raise Refusal('\nCannot build the database beside "%s": %s\n'
                      "Nothing was changed.\n"
                      % (db_path, error)) from error
    return os.path.join(holder, os.path.basename(db_path))


def commit_build(build_path: str, db_path: str) -> None:
    """The finished database onto the destination, and its write-ahead log with
    it.

    ``os.replace`` is the whole point: the destination is either the database it
    was or the one just built, never a half-written one and never nothing. The
    destination's old WAL belongs to the database that has just been replaced,
    so it goes in the same breath - left behind, it would be read as this one's.
    """
    try:
        os.replace(build_path, db_path)
        if os.path.exists(build_path + ".wal"):
            os.replace(build_path + ".wal", db_path + ".wal")
            return
    except OSError as error:
        raise Refusal('\nThe database was built but could not be moved onto '
                      '"%s": %s\n'
                      "That file is untouched.\n" % (db_path, error)) from error
    try:
        os.remove(db_path + ".wal")
    except OSError:
        pass


def discard_build(build_path: str) -> None:
    """The scaffolding of a build that is over, however it ended. Nothing here
    can touch the destination: everything it removes is inside the directory
    made for this run."""
    if not build_path:
        return
    shutil.rmtree(os.path.dirname(build_path), ignore_errors=True)


def report_counts(db_path: str, built) -> None:
    for content in built:
        query = ("SELECT (SELECT COUNT(*) FROM %sFiles) || '|' || "
                 "(SELECT COUNT(*) FROM %sCube);" % (content, content))
        proc = _duckdb(["-noheader", "-list", db_path, "-c", query],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        counts = (proc.stdout.decode("utf-8", "replace").strip()
                  if proc.returncode == 0 else "?|?") or "?|?"
        files, _, buckets = counts.partition("|")
        log("%s: %s file(s) rolled up into %s bucket(s) in %sCube"
            % (content, files, buckets, content))


def write_page(db_path: str, scratch: str, built) -> str:
    """The page, beside the database and named after it, so the two travel
    together and neither has to be found from the other."""
    html_path = db_path[:-len(".duckdb")] + ".html" if db_path.endswith(
        ".duckdb") else db_path + ".html"
    pairs = []
    for content in built:
        grain = os.path.join(scratch, content + ".viewer.csv")
        if os.path.exists(grain) and os.path.getsize(grain) > 0:
            pairs.append("%s:%s" % (content, grain))
    if not pairs:
        return ""

    name = os.path.basename(html_path[:-len(".html")])
    part = html_path + ".part"
    try:
        with open(part, "w") as handle:
            handle.write(censusviewer.viewer_html(
                "Content census - " + name, pairs))
        shutil.move(part, html_path)
    except (OSError, UnicodeError, ValueError) as error:
        # The database is the run's real output and it is already written; a
        # page that could not be assembled is worth a warning, not a failed run.
        # Its own words with it: a read-only folder and a full disk are two
        # different things to do about it. Only the expected failures are caught,
        # so a programming error here still reaches the tests as one.
        try:
            os.remove(part)
        except OSError:
            pass
        log("WARNING: the database was written but the viewer page could not "
            "be: %s: %s" % (type(error).__name__, error))
        return ""
    size = _human_size(html_path)
    log('Wrote "%s" (%s, %s)' % (html_path, size, " ".join(built)))
    return html_path


def _human_size(path: str) -> str:
    """What ``du -h`` prints for this file: powers of 1024, one decimal below
    ten, and no space before the unit."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "0"
    value = float(size)
    for unit in ("", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            if unit == "":
                return "%d" % int(value)
            if value < 10:
                return "%.1f%s" % (value, unit)
            return "%d%s" % (int(value + 0.5), unit)
        value /= 1024
    return "%d" % size


def main(argv: list, program: str = "content-census-bi") -> int:
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

    if clioptions.args_out_of_range(len(result.positionals), 1, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    if tooldeps.require_tools(
            program, ["duckdb"],
            skip_preflight=bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))):
        return 1

    scratch = ""
    build_path = ""
    try:
        found = collect_paths(result.positionals)
        by_type, skipped = classify(found)
        settled = separators(by_type)
        first = next(path for content in cubes.TYPES
                     for path in by_type.get(content) or [])
        db_path = resolve_db_path(result.values["dbPath"], first)
        export_dir = resolve_export_dir(result.values["exportDir"])

        # Rebuilt from nothing on every run, and said out loud. A cube is derived
        # data with no history in it: keeping the old tables would only leave the
        # cubes of a type that is no longer among the reports sitting beside the
        # ones that are, with nothing to tell them apart afterwards.
        #
        # "From nothing" is about the CONTENT, not the file: the new one is built
        # beside the old and moved onto it, so a build that fails leaves the last
        # good database where it was.
        if os.path.exists(db_path):
            log("Replacing the existing " + os.path.basename(db_path))

        ramscratch.init_ram_base(os.environ.get("censusRamBase", ""))
        scratch, status = ramscratch.ram_scratch_dir("contentCensusBI")
        if status != 0 or not scratch:
            raise Refusal("\nError: no scratch directory could be made for "
                          "this run.\nNothing was changed.\n")
        ramscratch.add_exit_cleanup([scratch])
        safety.init_abort_flag()
        safety.trap_run_abort()

        sql, built = build_sql(by_type, settled, export_dir, scratch)
        sql_file = os.path.join(scratch, "build.sql")
        with open(sql_file, "w") as handle:
            handle.write(sql)

        log("Building %s from %d content type(s)"
            % (os.path.basename(db_path), len(built)))
        build_path = build_target(db_path)
        run_build(sql_file, build_path, db_path)

        report_counts(build_path, built)

        if result.values["showTotals"]:
            for content in built:
                sys.stderr.write("\n%s, all of it:\n" % content)
                _duckdb(["-box", build_path, "-c", cubes.totals_sql(content)])

        # Every statement ran and every count came back, so this is the run's
        # result and takes the destination.
        commit_build(build_path, db_path)

        if export_dir:
            log('Exported %sinto "%s"'
                % ("".join("%sCube.csv " % content for content in built),
                   export_dir))

        html_path = write_page(db_path, scratch, built)

        sys.stderr.write("\n")
        log('Wrote "%s".' % db_path)
        log('Query it with: duckdb "%s"' % db_path)
        if html_path and os.path.isfile(html_path):
            log('Or open "%s" in a browser to pivot it.'
                % os.path.basename(html_path))

        if skipped:
            log("%d file(s) in those folders were not census reports and were "
                "passed over:" % len(skipped))
            for entry in skipped:
                log("  " + entry)

        if result.values["query"]:
            sys.stderr.write("\n")
            _duckdb(["-box", db_path, "-c", result.values["query"]])
        return 0
    except Refusal as refusal:
        sys.stderr.write(refusal.text)
        return refusal.status
    finally:
        discard_build(build_path)
        ramscratch.run_exit_cleanup()


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name))


if __name__ == "__main__":
    sys.exit(cli())
