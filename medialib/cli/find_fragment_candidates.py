"""find-fragment-candidates: the recurring word-ish leftovers in a tree of
names, so they can be reviewed and added to data/fragments.txt.

Input is either a folder - a tree of it is generated with the `tree` command and
written into it as "<folder>.tree" - or an existing tree file, parsed directly.
From every name everything that is obviously NOT a fragment is stripped: the
tree drawing syntax, a trailing extension, punctuation, pure numbers, lone
characters and common words. Quality and source tags (1080p, x264, webrip) are
kept ON PURPOSE - those are exactly the leftovers worth surfacing.

A token is counted once per NAME, so the tally is a prevalence (how many names
carry it) rather than a raw occurrence count.
"""

import locale
import os
import re
import subprocess
import sys
import time

from medialib import commands
from medialib.lib import clioptions, safety, tooldeps

USAGE_HEAD = """Usage:
    {program} [options] <folderOrTreeFile>
Argument:
    <folderOrTreeFile>  Either a folder (a tree of it is generated and parsed)
                        or an existing tree file (parsed directly).
Options:"""

OPT_SPEC = """
m | <n> | Only report candidates whose prevalence is at least <n>
                    (how many distinct names carry them). Default: 2, so purely
                    one-off title words are hidden and only recurring fragments
                    remain. Pass "-m 1" to list every candidate.
o | <file> | Write the report to <file> instead of the default location.
h |  | Print this help page.
"""

# A prevalence of zero would report every candidate and one below it nothing at
# all, so neither is a threshold anybody means to ask for.
OPT_CHECKS = """
m | posInt | prevalence threshold
"""

OPT_VARS = "m:minCount o:outOverride"
OPT_COLUMN = 20
OPT_LONG = "m:min-prevalence o:output h:help"

# The tree drawing syntax: any number of 4-column indentation cells, then the
# branch connector. Matching the exact cell structure means a stray "-- " inside
# a real name is never mistaken for a connector, and a line without one - the
# root, or a blank - contributes nothing.
_TREE_LINE = re.compile(r"^(?:\|   |    |│   )*(?:\|-- |`-- |├── |└── )")

_EXTENSION = re.compile(r"\.[a-z][a-z0-9]{0,3}$")
_DIGITS = re.compile(r"^[0-9]+$")


def _libc_alnum():
    """``iswalnum`` from the C library the run's awk is classifying with.

    awk splits names on ``[^[:alnum:]]``, and what that class HOLDS is the
    host's locale data, not a property of the language: glibc counts the
    combining marks of several scripts as alphabetic and counts a superscript
    two as not a digit, and no Python predicate draws the same line -
    ``str.isalnum`` disagrees on 1,003 codepoints below U+3000 alone.

    So the same question is asked of the same library, the way the version sort
    follows the ``sort`` that is installed: the answer belongs to the host. A host
    without a usable libc falls back to the closest Python rule.
    """
    try:
        import ctypes
        # Both the character classes and the collation below are the host's, so
        # the whole locale is taken from the environment the way awk and sort
        # take it.
        locale.setlocale(locale.LC_ALL, "")
        libc = ctypes.CDLL(None, use_errno=False)
        libc.iswalnum.argtypes = [ctypes.c_wchar]
        libc.iswalnum.restype = ctypes.c_int
        probe = libc.iswalnum
        # It has to agree about the plain cases before it is trusted with the
        # hard ones.
        if all(bool(probe(c)) for c in "aZ9é") and not any(
                bool(probe(c)) for c in " .-"):
            return probe
    except Exception:                             # pragma: no cover - no libc
        pass
    import unicodedata

    def fallback(character):
        return (character.isalpha() or character.isdecimal()
                or unicodedata.category(character) == "Nl")
    return fallback


_ALNUM = _libc_alnum()
_ALNUM_CACHE: dict[str, bool] = {}


def _is_alnum(character: str) -> bool:
    cached = _ALNUM_CACHE.get(character)
    if cached is None:
        cached = bool(_ALNUM(character))
        _ALNUM_CACHE[character] = cached
    return cached


def _tokens(line: str):
    """The words ``gsub(/[^[:alnum:]]+/, " ")`` then ``split`` leaves."""
    token = []
    for character in line:
        if _is_alnum(character):
            token.append(character)
        elif token:
            yield "".join(token)
            token = []
    if token:
        yield "".join(token)

# Common English words (plus a few ubiquitous file-type words) that are
# obviously not fragments to clean. Quality/source tags (1080p, x264, web,
# bluray, ...) are deliberately NOT listed: those ARE the kind of leftover
# fragment worth surfacing.
STOP_WORDS = frozenset([
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "by",
    "for", "with", "from", "into", "over", "under", "is", "are", "was",
    "were", "be", "been", "being", "am", "as", "it", "its", "this",
    "that", "these", "those", "he", "she", "they", "them", "his", "her",
    "their", "you", "your", "we", "our", "us", "i", "me", "my", "mine",
    "not", "no", "nor", "so", "if", "then", "than", "too", "very", "can",
    "will", "just", "but", "also", "s", "t", "d", "ll", "re", "ve", "m",
    "o", "mp3", "mp4", "mkv", "avi", "flac", "wav", "m4a", "mov", "webm",
    "opus", "jpg", "jpeg", "png", "gif", "txt", "pdf", "epub", "cbz",
    "cbr", "cb7", "avif", "webp", "srt", "ass", "vtt",
])


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        checks=OPT_CHECKS,
        column=OPT_COLUMN,
    )


def extract_names(tree_file: str) -> list:
    """The bare name of every entry in the tree - the indentation cells and the
    connector stripped, and only from the lines that had a connector."""
    names = []
    with open(tree_file, encoding="utf-8", errors="surrogateescape") as handle:
        for line in handle:
            line = line.rstrip("\n").rstrip("\r")
            match = _TREE_LINE.match(line)
            if match:
                names.append(line[match.end():])
    return names


def tally(names, minimum: int) -> list:
    """The candidates and how many NAMES carry each, at or above the floor.

    Lower-cased, a trailing extension dropped, every separator run a break, then
    the pure numbers, lone characters and common words dropped. A token is
    counted at most once per name, which is what makes the number a prevalence.
    """
    counts: dict[str, int] = {}
    for name in names:
        line = name.lower()
        line = _EXTENSION.sub("", line)
        seen = set()
        for token in _tokens(line):
            if token in seen:
                continue
            if _DIGITS.match(token):
                continue
            if len(token) < 2:
                continue
            if token in STOP_WORDS:
                continue
            seen.add(token)
            counts[token] = counts.get(token, 0) + 1
    return [(count, token) for token, count in counts.items()
            if count >= minimum]


def _collation_key(token: str):
    """The candidate as ``sort`` orders it on THIS host.

    ``sort -k2,2`` compares with the locale's collation, so the order a report
    comes out in is a property of the host and not of the code: under C.UTF-8
    it is byte order, and under en_US.UTF-8 - a perfectly ordinary interactive
    shell - it is dictionary order, which puts "überproper" somewhere else
    entirely. The suite pins C.UTF-8 and would never have shown it.

    So the same C library is asked here as is asked what a letter is: what is
    INSTALLED decides.
    """
    try:
        return locale.strxfrm(token)
    except (ValueError, MemoryError):             # pragma: no cover - odd input
        return token


def _sorted_rows(rows):
    """Descending prevalence, ties broken by the candidate - what
    ``sort -t $'\\t' -k1,1nr -k2,2`` prints."""
    return sorted(rows, key=lambda row: (-row[0], _collation_key(row[1])))


def resolve_paths(argument: str, program: str = "") -> tuple[str, str]:
    """The tree file to parse and the report to write, as ``(tree, report)``.

    A folder is turned into a tree first - and that branch, and only that
    branch, has an external dependency. A plain file is taken as an
    already-made tree, and its report lands right beside it.
    """
    if os.path.isdir(argument):
        # Routed through the shared preflight so the refusal reads like every
        # other missing-tool refusal in the repo.
        if tooldeps.require_tools(
                "%s on a folder (a tree file needs no tool)" % program,
                ["tree"],
                skip_preflight=bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))):
            raise SystemExit(1)
        # Fragments are found in the NAMES of the entries, so an input holding
        # no entry has nothing to look at. Refused before the .tree snapshot is
        # written into the folder, so nothing is left behind.
        if safety.is_empty_folder(argument):
            raise SystemExit(safety.fail_no_relevant_input(
                argument,
                "files or sub-folders whose names could be examined"))
        directory = argument.rstrip("/") or "/"
        base = os.path.basename(directory)
        tree_file = os.path.join(directory, base + ".tree")
        # A stable, deterministic invocation: ascii glyphs, hidden entries, no
        # colour, no summary, rooted at the folder's own basename.
        with open(tree_file, "w") as handle:
            subprocess.run(["tree", "-a", "-n", "--charset", "ascii",
                            "--noreport", base],
                           cwd=os.path.dirname(directory) or ".",
                           stdout=handle, check=False)
        return tree_file, os.path.join(directory, "fragmentCandidates.txt")

    if os.path.isfile(argument):
        tree_dir = os.path.realpath(os.path.dirname(argument) or ".")
        tree_base = os.path.basename(argument)
        stem = tree_base.rsplit(".", 1)[0] if "." in tree_base else tree_base
        return argument, os.path.join(tree_dir,
                                      stem + ".fragmentCandidates.txt")

    sys.stderr.write('Error: "%s" is neither a folder nor a readable file.\n'
                     % argument)
    raise SystemExit(1)


def write_report(report: str, tree_file: str, rows, minimum: int) -> int:
    """The report, and how many candidates it names."""
    with open(report, "w") as handle:
        handle.write("# Fragment candidates from: %s\n" % tree_file)
        handle.write("# Generated: %s\n"
                     % time.strftime("%Y-%m-%d %H:%M:%S"))
        handle.write("# Minimum prevalence reported: %s\n" % minimum)
        handle.write("# Columns: <prevalence><TAB><candidate>\n")
        handle.write("# Review these and copy the genuine fragments into "
                     "data/fragments.txt.\n")
        for count, token in _sorted_rows(rows):
            handle.write("%d\t%s\n" % (count, token))
    return len(rows)


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name))


def main(argv: list, program: str = "find-fragment-candidates") -> int:
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

    minimum = int(result.values["minCount"] or 2)
    try:
        tree_file, report = resolve_paths(result.positionals[0], program)
    except SystemExit as exit_request:
        # Both raises carry a status, and both are a shell exit code.
        return int(exit_request.code or 0)

    if result.values["outOverride"]:
        report = result.values["outOverride"]

    reported = write_report(report, tree_file,
                            tally(extract_names(tree_file), minimum), minimum)
    print('Wrote %d candidate(s) to "%s".' % (reported, report))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
