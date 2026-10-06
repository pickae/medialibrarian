"""find-gaps: what is missing from the numbered and dated runs in a tree.

Every folder at every depth is looked at on its own, and only for the KIND of
file it mostly holds - video, audio, document and so on
(:data:`enums.FILE_KINDS`): the covers, cue sheets and subtitles beside a run
are not part of it. What that group is numbered by decides what is missing:

* **episodes** - S01E01, s1e14, S02,E13, 1x05: every episode from 1 up to the
  highest one a season has, and every season from 1 up to the highest - unless
  the folder is itself named for its season ("Season 2"), whose parent then
  says what is missing before it;
* **dates** - a leading YYYYMMDD (or YYYY-MM-DD): the stretches with nothing in
  them that are conspicuous for how often the run otherwise comes - a week for a
  daily run, a month for a weekly one, two skipped months for a monthly one. The
  dated files of a folder's year folders (2023/, 2024/) are one run with its own;
* **numbers** - 1, 01, 001, "Chapter 7", "(03)": read the way
  `clean-folder-structure` reads a numbered prefix, padding and all, and
  everything from 1 up to the highest is expected.

Sub-folders form a run of their own the same way: "Season 1", "Season 3", or
"01 ...", "03 ...". A lone "Season 3" is missing Seasons 1 and 2.

The report is a `tree` of the folders that have something missing, each holding
what is missing instead of what is there, and it is written to
`logs/find-gaps/find-gaps-<folder>.tree` - one per folder given. Nothing in the
input is changed.
"""

from __future__ import annotations

import datetime
import os
import re
import statistics
import sys
from dataclasses import dataclass, field

from medialib import commands
from medialib.cli.clean_folder_structure import DATE_PREFIX
from medialib.lib import (
    cleannamesindividually,
    clioptions,
    enums,
    overwrite,
    plurality,
    prefixes,
    safety,
)
from medialib.lib.versionsort import version_key

USAGE_HEAD = """Usage:
    {program} [options] <inputDir>
Options:"""

OPT_SPEC = """
h |  | Print this help page.
"""

OPT_VARS = ""
OPT_COLUMN = 20
OPT_LONG = "h:help"

# The one folder under logs/ this command writes to, and the file it writes
# there per folder given.
LOGS = "find-gaps"
REPORT = os.path.join(LOGS, "find-gaps-%s.tree")

# A season and episode anywhere in a name: "S01E02", "s1e2", "S02,E13",
# "S01 E02", and the episodes after it of a file holding more than one -
# "S01E01E02", "S01E01-E03". Not inside a word: "bs01e02" is not an episode.
_EPISODE = re.compile(
    r"(?<![A-Za-z0-9])[Ss]([0-9]{1,3})[ ._,-]*[Ee]([0-9]{1,4})"
    r"((?:[ ._]*-?[ ._]*[Ee][0-9]{1,4})*)(?![0-9])")
_MORE_EPISODES = re.compile(r"(-?)[ ._]*[Ee]([0-9]{1,4})")
# And the other spelling of the same: "1x05".
_CROSS_EPISODE = re.compile(r"(?<![A-Za-z0-9])([0-9]{1,2})[xX]([0-9]{2,3})(?![0-9])")

# A folder that is one season of a show.
_SEASON_FOLDER = re.compile(
    r"^(?:season|series|staffel|saison|seizoen|temporada|stagione|s)"
    r"[ ._-]*([0-9]{1,3})(?![0-9])", re.IGNORECASE)
# A folder whose name says which season it holds, anywhere in it: "Season 2",
# "Show S02 1080p". Not inside a word: "Jobs 2" names no season.
_NAMES_SEASON = re.compile(
    r"(?<![A-Za-z0-9])(?:season|series|staffel|saison|seizoen|temporada|"
    r"stagione|s)[ ._-]*[0-9]{1,3}(?![0-9])", re.IGNORECASE)

# A leading date, compact or separated. The separated shape is the one
# `clean-folder-structure -d` normalises, so a tree it has not been run over yet
# reads the same.
_COMPACT_DATE = re.compile(r"^([12][0-9]{3})([01][0-9])([0-3][0-9])(?![0-9])")
_SEPARATED_DATE = re.compile(DATE_PREFIX)
_YEAR_FOLDER = re.compile(r"^[12][0-9]{3}$")

# How much of a run has to be there for it to be a run at all: three numbers out
# of 1 to 2021 are years, not the three chapters left of two thousand.
_MIN_COVERAGE = 0.5
# How dense a run has to be between its own ends to be one when it starts far
# from 1 - chapters 51 to 90.
_MIN_DENSITY = 0.75
# And how many dates a run needs before how often it comes can be told.
_MIN_DATES = 4

# (the slowest run a cadence covers, in days between two files, what it is
# called, how many days with nothing in them are conspicuous for it). A run
# slower than the last row is conspicuous at twice its own cadence.
_CADENCES = (
    (2, "daily", 7),
    (10, "weekly", 30),
    (45, "monthly", 75),
)


@dataclass
class Folder:
    """One folder of the report: what is missing in it, and the sub-folders
    that have something missing below them."""

    name: str
    entries: list = field(default_factory=list)
    children: list = field(default_factory=list)
    missing: int = 0
    date_gaps: int = 0

    def has_gaps(self) -> bool:
        return bool(self.entries or self.children)


def spec(program: str) -> clioptions.Spec:
    return clioptions.with_verbosity(clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
    ))


# --- reading the names --------------------------------------------------------

def _listing(directory: str) -> tuple[list[str], list[str]]:
    """(files, folders) directly in ``directory``, version-sorted. Hidden entries
    are left out - they are something a tool left behind - and so are links, a
    folder link being a way out of the tree this run was given."""
    files, folders = [], []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    folders.append(entry.name)
                elif entry.is_file(follow_symlinks=False):
                    files.append(entry.name)
    except OSError:
        pass
    return sorted(files, key=version_key), sorted(folders, key=version_key)


def _run_files(files: list[str]) -> list[str]:
    """The files of the commonest kind: what the folder is a run OF."""
    return [files[i] for i in plurality.plurality_kind_indices(files)]


def episodes_of(name: str) -> list[tuple[int, int]]:
    """Every (season, episode) a name says it holds, in order."""
    match = _EPISODE.search(name)
    if match:
        season, first = int(match.group(1)), int(match.group(2))
        found = [(season, first)]
        previous = first
        for more in _MORE_EPISODES.finditer(match.group(3)):
            number = int(more.group(2))
            # A dash between two episodes is a range: E01-E03 is three.
            if more.group(1) and number > previous:
                found += [(season, n) for n in range(previous + 1, number + 1)]
            else:
                found.append((season, number))
            previous = number
        return found
    match = _CROSS_EPISODE.search(name)
    if match:
        return [(int(match.group(1)), int(match.group(2)))]
    return []


def date_of(name: str) -> datetime.date | None:
    """The date a name leads with, or None - an eight-digit run that is no
    calendar day ("20241341") included."""
    match = _COMPACT_DATE.match(name) or _SEPARATED_DATE.match(name)
    if not match:
        return None
    try:
        return datetime.date(int(match.group(1)), int(match.group(2)),
                             int(match.group(3)))
    except ValueError:
        return None


def numbers_of(names: list[str], mode: str) -> tuple[set[int], int]:
    """The numbers a group of siblings is numbered by, and the widest they are
    written - read by the same split `clean-folder-structure` makes.

    A prefix that is a date is not a number of the run, and a range prefix
    ("01-02") is both of its ends and everything between.
    """
    fragments_file, _ok = cleannamesindividually.fragments_file_for()
    split = prefixes.split_siblings(names, mode, fragments_file,
                                    group=range(len(names)))
    values: set[int] = set()
    width = 0
    for prefix in split.prefixes:
        low, _, high = prefix.partition("-")
        if not low.isascii() or not low.isdigit():
            continue
        if re.match(enums.DATE_PREFIX_PATTERN, low):
            continue
        if high and high.isascii() and high.isdigit():
            first, last = int(low), int(high)
            if first <= last and last - first < 100:
                values.update(range(first, last + 1))
                width = max(width, len(low), len(high))
            continue
        values.add(int(low))
        width = max(width, len(low))
    return values, width


# --- deciding what is missing -------------------------------------------------

def missing_numbers(values: set[int]) -> list[int]:
    """What is missing from a run of numbers, or [] when they are no run.

    Expected is everything from 1 up to the highest, when at least half of that
    is there. A run that starts far from 1 - chapters 51 to 90 - is still one
    when it is dense between its own ends, and everything before it is missing
    too. Two shapes of dense run are not counted from 1, only their inner gaps:
    years (1994 to 1999), and one disc of a disc-and-track numbering (101 to
    112), where nothing numbered 1 to 100 was ever meant to be.
    """
    counted = {value for value in values if value > 0}
    if len(counted) < 2:
        return []
    top = max(counted)
    if len(counted) / top >= _MIN_COVERAGE:
        return [n for n in range(1, top) if n not in counted]
    bottom = min(counted)
    if len(counted) >= 3 and len(counted) / (top - bottom + 1) >= _MIN_DENSITY:
        years = bottom >= 1900 and top <= 2099
        disc = bottom > 100 and bottom % 100 == 1 and top // 100 == bottom // 100
        start = bottom if years or disc else 1
        return [n for n in range(start, top) if n not in counted]
    return []


def _blocks(numbers: list[int]) -> list[tuple[int, int]]:
    """Consecutive numbers as (first, last) blocks."""
    blocks: list[tuple[int, int]] = []
    for number in numbers:
        if blocks and blocks[-1][1] == number - 1:
            blocks[-1] = (blocks[-1][0], number)
        else:
            blocks.append((number, number))
    return blocks


def _number_entries(missing: list[int], width: int, suffix: str = "") -> list[str]:
    return [("%0*d" % (width, first) if first == last
             else "%0*d-%0*d" % (width, first, width, last)) + suffix
            for first, last in _blocks(missing)]


def episode_gaps(names: list[str], folder: str = ""
                 ) -> tuple[list[str], int] | None:
    """What is missing from a folder of episodes: (entries, how many), or None
    when the names are not episodes.

    The seasons before the lowest one there are missing, unless ``folder`` - the
    folder's own name - says which season it is: that folder is one season of a
    show, and its parent reports the others.
    """
    seasons: dict[int, set[int]] = {}
    matched = 0
    for name in names:
        found = episodes_of(name)
        if found:
            matched += 1
        for season, episode in found:
            seasons.setdefault(season, set()).add(episode)
    # One file is a run only on its own: S01E05 alone is missing E01 to E04.
    if not matched or matched < min(2, len(names)) or matched * 2 < len(names):
        return None

    entries: list[str] = []
    count = 0
    season_width = max(2, len(str(max(seasons))))
    episode_width = max(2, *(len(str(max(e))) for e in seasons.values()))
    numbered = sorted(season for season in seasons if season > 0)
    if not numbered:
        whole = []
    else:
        first = numbered[0] if _NAMES_SEASON.search(folder) else 1
        whole = [s for s in range(first, numbered[-1]) if s not in seasons]
    for season in sorted(set(seasons) | set(whole)):
        label = "S%0*d" % (season_width, season)
        if season in whole:
            entries.append("%s (the whole season)" % label)
            count += 1
            continue
        present = seasons[season]
        gaps = [n for n in range(1, max(present)) if n not in present]
        count += len(gaps)
        for first, last in _blocks(gaps):
            entries.append("%sE%0*d" % (label, episode_width, first)
                           if first == last else "%sE%0*d-E%0*d"
                           % (label, episode_width, first, episode_width, last))
    return entries, count


def _cadence(median: float) -> tuple[str, int]:
    """What a run coming every ``median`` days is called, and how many days
    with nothing in them are conspicuous for it."""
    for slowest, label, conspicuous in _CADENCES:
        if median <= slowest:
            return label, conspicuous
    return "every %d days or so" % round(median), int(2 * median)


def date_gaps(dates: list[datetime.date]) -> list[str] | None:
    """The conspicuous stretches with nothing in them, or None when there are
    too few dates to tell how often the run comes."""
    days = sorted(set(dates))
    if len(days) < _MIN_DATES:
        return None
    intervals = [(b - a).days for a, b in zip(days, days[1:], strict=False)]
    label, conspicuous = _cadence(statistics.median(intervals))
    entries = []
    for before, after in zip(days, days[1:], strict=False):
        empty = (after - before).days - 1
        if empty >= conspicuous:
            first = before + datetime.timedelta(days=1)
            last = after - datetime.timedelta(days=1)
            entries.append("%s-%s (nothing for %d days, otherwise %s)"
                           % (first.strftime("%Y%m%d"), last.strftime("%Y%m%d"),
                              empty, label))
    return entries


def _dated(names: list[str]) -> list[datetime.date] | None:
    """The dates of a run whose names mostly lead with one, or None."""
    dates = [date for date in map(date_of, names) if date]
    if len(dates) < 2 or len(dates) * 2 < len(names):
        return None
    return dates


def _file_gaps(files: list[str], year_dates: list[datetime.date],
               node: Folder, in_years: bool) -> None:
    """What is missing from the folder's own run of files, into ``node``.

    ``in_years`` is a year folder whose dates its parent has already counted,
    as one run with the other years'.
    """
    run = _run_files(files) if len(files) >= 2 else files
    episodes = episode_gaps(run, os.path.basename(node.name)) if run else None
    if episodes:
        node.entries += episodes[0]
        node.missing += episodes[1]
        return
    if episodes is not None:
        return
    dates = _dated(run) if len(run) >= 2 else None
    if dates is not None and in_years:
        return
    if dates is not None or year_dates:
        gaps = date_gaps((dates or []) + year_dates) or []
        node.entries += gaps
        node.date_gaps += len(gaps)
        return
    if len(run) >= 2:
        values, width = numbers_of(run, "files")
        missing = missing_numbers(values)
        node.entries += _number_entries(missing, width)
        node.missing += len(missing)


def _folder_gaps(folders: list[str], node: Folder) -> None:
    """What is missing from the run the sub-folders form, into ``node``. A
    single season folder is a run, one that is missing the seasons before it."""
    seasons = [_SEASON_FOLDER.match(name) for name in folders]
    matched = [match for match in seasons if match]
    if matched and len(matched) * 2 >= len(folders):
        present = {int(match.group(1)) for match in matched}
        missing = [n for n in range(1, max(present)) if n not in present]
        # Named the way the folders there are: "Season 1" is followed by a
        # missing "Season 2", "S01" by "S02".
        sample = matched[0]
        width = len(sample.group(1))
        for number in missing:
            node.entries.append("%s%0*d (folder)" % (
                sample.string[:sample.start(1)], width, number))
        node.missing += len(missing)
        return
    if len(folders) < 2:
        return
    values, width = numbers_of(folders, "folders")
    missing = missing_numbers(values)
    node.entries += _number_entries(missing, width, " (folder)")
    node.missing += len(missing)


def _year_dates(directory: str, folders: list[str]) -> list[datetime.date]:
    """The dates of the dated files in ``directory``'s year folders, which are
    one run with the folder's own - the way `clean-folder-structure -y` sorts a
    run into years."""
    dates: list[datetime.date] = []
    for name in folders:
        if not _YEAR_FOLDER.match(name):
            continue
        files, _folders = _listing(os.path.join(directory, name))
        if files:
            dates += _dated(_run_files(files)) or []
    return dates


def analyse(directory: str, name: str, in_years: bool = False) -> Folder:
    """The report's folder for ``directory`` and everything under it."""
    node = Folder(name)
    files, folders = _listing(directory)
    year_folders = [f for f in folders if _YEAR_FOLDER.match(f)]
    years = bool(year_folders) and len(year_folders) * 2 >= len(folders)
    year_dates = _year_dates(directory, folders) if years else []
    _file_gaps(files, year_dates, node, in_years)
    # Year folders are not a numbered run with gaps where a year had nothing:
    # the gaps in the dates of the files in them are what is missing.
    if not (years and year_dates):
        _folder_gaps(folders, node)
    for sub in folders:
        child = analyse(os.path.join(directory, sub), sub,
                        in_years=years and bool(year_dates)
                        and bool(_YEAR_FOLDER.match(sub)))
        if child.has_gaps():
            node.children.append(child)
            node.missing += child.missing
            node.date_gaps += child.date_gaps
    return node


# --- the report ---------------------------------------------------------------

def render(root: Folder) -> list[str]:
    """``tree``'s drawing of the folders with gaps, ascii glyphs, a folder
    marked with a trailing slash the way `tree -F` marks one - so it cannot be
    mistaken for a missing number of the same spelling."""
    lines = [root.name]

    def walk(node: Folder, indent: str) -> None:
        items = [(entry, None) for entry in node.entries] + [
            (child.name + "/", child) for child in node.children]
        for index, (label, child) in enumerate(items):
            last = index == len(items) - 1
            lines.append(indent + ("`-- " if last else "|-- ") + label)
            if child is not None:
                walk(child, indent + ("    " if last else "|   "))

    walk(root, "")
    return lines


def _count_folders(node: Folder) -> int:
    return (1 if node.entries else 0) + sum(map(_count_folders, node.children))


def report_name(directory: str) -> str:
    """What a folder's report is called after: its own name, "root" for /."""
    name = os.path.basename(os.path.realpath(directory))
    return name if name not in ("", "/") else "root"


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name))


def main(argv: list, program: str = "find-gaps",
         script_dir: str = "") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
        clioptions.settle_verbosity(result)
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

    argument = result.positionals[0]
    if not os.path.isdir(argument):
        sys.stderr.write(clioptions.missing_dir_text(declaration, argument))
        return 1
    if safety.is_empty_folder(argument):
        return safety.fail_no_relevant_input(
            argument, "numbered or dated files, or folders holding them")

    root = os.path.realpath(argument)
    report = commands.logs_file(script_dir or commands.script_dir(),
                                REPORT % report_name(root))
    if not overwrite.confirm_overwrite([report]):
        return 1

    tree = analyse(root, root)
    folders = _count_folders(tree)
    with open(report, "w", encoding="utf-8", errors="surrogateescape") as handle:
        for line in render(tree):
            handle.write(line + "\n")
        handle.write("\n%d folder(s) with gaps, %d missing, %d date gap(s)\n"
                     % (folders, tree.missing, tree.date_gaps))
    print('Wrote %d folder(s) with gaps (%d missing, %d date gap(s)) to "%s".'
          % (folders, tree.missing, tree.date_gaps, report))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
