"""The podcast feed table and the yt-dlp call it builds.

WHAT to download is a table; HOW to call the tool for it is here, and the two
meet only in :func:`podcast_call`. Every function takes its settled values as
arguments and returns its answer, so nothing here reads or writes state a caller
has to remember to set.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Sequence

from medialib.lib.enums import shell_lower
from medialib.lib.formatting import fmt_bytes

__all__ = [
    "PODCAST_EPISODE_MARKER",
    "PODCAST_RSS_JOBS",
    "PODCAST_PROFILES",
    "PODCAST_DEFAULT_PROFILE",
    "PODCAST_DEFAULT_TEMPLATE",
    "PODCAST_DEFAULT_PLAYLIST_END",
    "PODCAST_UNLIMITED_PLAYLIST_END",
    "PODCAST_COLUMNS",
    "podcast_profile_args",
    "podcast_profile_provider",
    "podcast_profile_media",
    "podcast_profile_ingest",
    "podcast_profile_jobs",
    "podcast_platform",
    "native_path",
    "resolve_ytdlp",
    "is_nightly_version",
    "ytdlp_upgrade_command",
    "split_tabs",
    "split_podcast_row",
    "parse_date_range",
    "is_ytdlp_date",
    "podcast_call",
    "report_episodes",
    "podcast_bot_block_warning",
    "is_bot_block",
    "podcast_download_stats",
    "render_call",
    "quote_posix",
    "quote_powershell",
    "refused_extra_args",
]

# The marker no path can start with, prefixed to the finished file's path in the
# --print answer the run reads its episodes from.
PODCAST_EPISODE_MARKER = "__episode__"

# How many RSS feeds are fetched at once by default.
PODCAST_RSS_JOBS = 10

PODCAST_PROFILES = (
    "youtubeAudio",
    "youtubeVideo",
    "rssAudio",
    "rssVideo",
    "siteVideo",
)

PODCAST_DEFAULT_PROFILE = "youtubeAudio"

PODCAST_DEFAULT_TEMPLATE = "%(upload_date)s %(title)s.%(ext)s"

PODCAST_DEFAULT_PLAYLIST_END = 20

PODCAST_UNLIMITED_PLAYLIST_END = 0

PODCAST_COLUMNS = ("active", "subdir", "nameTemplate", "playlistEnd", "extraArgs",
                   "url")

# The yt-dlp options a row may not carry. A row describes ONE feed: the four
# that decide where a download is written would move it out of the output root
# the run was given, and the three that run something else are not a feed's to
# ask for. Everything yt-dlp can do about the download itself is still open.
PODCAST_REFUSED_ARGS = frozenset((
    "-o", "--output", "-P", "--paths",
    "--exec", "--exec-before-download", "--config-location",
))

# The arguments split into three parts, and only the first is truly universal:
# the core is about how this library is built, the pacing about the source the
# feeds come from, the media about what is wanted out of them.
_PODCAST_CORE_ARGS = (
    "--no-continue",
    "--no-overwrites",
    "--lazy-playlist",
    "--windows-filenames",
    "--convert-thumbnails", "jpg",
    "--embed-thumbnail",
    "--embed-metadata",
    "--retries", "35",
    "--fragment-retries", "35",
    "--file-access-retries", "5",
    "-i",
)

_PODCAST_YOUTUBE_PACING = (
    "--sleep-interval", "2",
    "--sleep-request", "1",
    "--max-sleep-interval", "5",
    "--concurrent-fragments", "3",
)

# It comes LAST in every YouTube profile's list, because a row's extraArgs are
# appended after the profile's and yt-dlp takes the last --extractor-args it is
# given for an extractor: a feed that needs a different client can still say so.
_PODCAST_YOUTUBE_CLIENT = ("--extractor-args", "youtube:player-client=android")

_PODCAST_RSS_PACING = (
    "--sleep-interval", "1",
    "--max-sleep-interval", "3",
)

_PODCAST_YOUTUBE_AUDIO = ("--sponsorblock-remove", "all", "-x",
                          "-f", "251/140/bestaudio/best")
_PODCAST_RSS_AUDIO = ("-x", "-f", "bestaudio/best")

_PODCAST_VIDEO = ("-f", "bv*+ba/b",
                  "--merge-output-format", "mkv",
                  "--embed-chapters")
_PODCAST_YOUTUBE_VIDEO = ("--sponsorblock-mark", "all", *_PODCAST_VIDEO)

_PODCAST_SITE_VIDEO = ("--embed-thumbnail", "--concurrent-fragments", "5")

_PROFILE_ARGS = {
    "youtubeAudio": _PODCAST_YOUTUBE_PACING + _PODCAST_CORE_ARGS
    + _PODCAST_YOUTUBE_AUDIO + _PODCAST_YOUTUBE_CLIENT,
    "youtubeVideo": _PODCAST_YOUTUBE_PACING + _PODCAST_CORE_ARGS
    + _PODCAST_YOUTUBE_VIDEO + _PODCAST_YOUTUBE_CLIENT,
    "rssAudio": _PODCAST_RSS_PACING + _PODCAST_CORE_ARGS + _PODCAST_RSS_AUDIO,
    "rssVideo": _PODCAST_RSS_PACING + _PODCAST_CORE_ARGS + _PODCAST_VIDEO,
    "siteVideo": _PODCAST_RSS_PACING + _PODCAST_CORE_ARGS + _PODCAST_SITE_VIDEO,
}

_INGEST = {
    "rssAudio": "Speech -c -m",
    "youtubeAudio": "Music -c -b 65",
}


def podcast_profile_args(profile: str) -> list[str] | None:
    """That profile's whole argument list, or None for a name that is not one of
    PODCAST_PROFILES."""
    args = _PROFILE_ARGS.get(profile)
    return list(args) if args is not None else None


def podcast_profile_provider(profile: str) -> str:
    """Who is being asked, which is what decides what may run alongside what."""
    if profile.startswith("youtube"):
        return "youtube"
    if profile.startswith("rss"):
        return "rss"
    if profile.startswith("site"):
        return "site"
    return "other"


def podcast_profile_media(profile: str) -> str:
    """"audio" or "video" - what a feed of it arrives as."""
    if "Video" in profile or "video" in profile:
        return "video"
    return "audio"


def podcast_profile_ingest(profile: str) -> str | None:
    """How a download of that profile becomes the library copy, as
    "<stagingFolder> <convertAudio arguments...>" - or None for a profile that
    is not converted at all."""
    return _INGEST.get(profile)


def podcast_profile_jobs(profile: str) -> str:
    """How many of that profile's feeds may be fetched at once, unless the table
    says otherwise."""
    return str(PODCAST_RSS_JOBS) if profile.startswith("rss") else "1"


def podcast_platform(platform_env: str, uname_s: str, os_env: str) -> str:
    """"windows" or "linux" for the host the call will run on.

    ``platform_env`` is PODCAST_PLATFORM (the host saying which other host it
    wants the calls for), ``uname_s`` what ``uname -s`` printed ("" when it
    failed), ``os_env`` the OS variable as the shell saw it.
    """
    if platform_env:
        return platform_env
    if uname_s.startswith(("MINGW", "MSYS", "CYGWIN")):
        return "windows"
    if uname_s == "" and os_env == "Windows_NT":
        return "windows"
    return "linux"


_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]")
_CYGDRIVE_RE = re.compile(r"^/cygdrive/([A-Za-z])(/.*)?$")
_DRIVE_RE = re.compile(r"^/([A-Za-z])(/.*)?$")


def native_path(path: str, platform: str, cygpath: str | None) -> str:
    """The path as the yt-dlp about to be run will read it.

    ``cygpath`` is the conversion ``cygpath -m -- <path>`` would print, None
    when the tool is absent and "" when it ran and printed nothing - both of
    which fall through to the manual shapes.
    """
    if platform != "windows":
        return path
    if _WIN_PATH_RE.match(path):
        return path.replace("\\", "/")
    if cygpath is not None and cygpath != "":
        return cygpath
    match = _CYGDRIVE_RE.match(path) or _DRIVE_RE.match(path)
    if match:
        drive = match.group(1).upper()
        rest = match.group(2) or "/"
        return f"{drive}:{rest}"
    # A relative path, or one under the emulated root with no Windows
    # equivalent: passed through unchanged, the way the shell version does it.
    return path


def resolve_ytdlp(ytdlp_env: str, platform: str, home: str, script_home: str,
                  cwd: str, present: set[str], executable_paths: set[str],
                  importable: set[str]) -> list[str] | None:
    """How to run yt-dlp here, as the argv prefix, or None when there is no way.

    ``present`` is the set of names ``command -v`` would find on PATH (a name
    with a non-directory entry in a PATH directory - the executable bit is not
    tested), ``executable_paths`` the absolute paths that pass ``[[ -x ]]``, and
    ``importable`` the interpreters whose ``import yt_dlp`` succeeds.

    On Linux a nightly install wins over the release one wherever both are
    there: an extractor a site broke is fixed in a nightly weeks before that
    fix is in a release. The nightly is looked for under its own name
    (``pipx install --suffix=-nightly --pip-args=--pre yt-dlp``), so the two
    coexist and ``YTDLP`` still overrides both.
    """
    if ytdlp_env:
        return [ytdlp_env]

    if platform == "windows":
        candidates = [f"{script_home}/yt-dlp.exe", "./yt-dlp.exe",
                      "yt-dlp.exe", "yt-dlp"]
    else:
        candidates = ["yt-dlp-nightly", f"{home}/.local/bin/yt-dlp-nightly",
                      "yt-dlp", f"{home}/.local/bin/yt-dlp"]

    for candidate in candidates:
        if "/" in candidate:
            if candidate == "./yt-dlp.exe":
                path = os.path.normpath(os.path.join(cwd, candidate))
            else:
                path = os.path.normpath(candidate)
            if path not in executable_paths:
                continue
        elif candidate not in present:
            continue
        return [candidate]

    for candidate in ("python3", "python", "py"):
        if candidate in present and candidate in importable:
            return [candidate, "-m", "yt_dlp"]

    return None


# A nightly's version carries the build time as a fourth component
# (2026.08.30.232658); a release stops at the date (2026.08.30). That is what
# tells the two apart at run time, whatever the command is called.
_NIGHTLY_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+")

# A pipx install lives at <pipx home>/venvs/<name>/bin/, and <name> is what
# pipx upgrades by - not the command name, which a --suffix install changes.
_PIPX_VENV_RE = re.compile(r"(?:^|/)pipx/venvs/([^/]+)/")


def is_nightly_version(version: str) -> bool:
    """Whether what ``yt-dlp --version`` printed is a nightly's."""
    return bool(_NIGHTLY_VERSION_RE.match(version.strip()))


def ytdlp_upgrade_command(command: Sequence[str], real_path: str,
                          pipx_present: bool,
                          sibling_python: bool) -> list[str] | None:
    """The call that brings this yt-dlp up to date, or None when nothing here
    can bring it - which is not a fault, only a run that goes on with what it
    has.

    ``real_path`` is the command with its symlinks followed (``~/.local/bin``
    holds links into the venvs), ``pipx_present`` whether pipx is on PATH, and
    ``sibling_python`` whether an interpreter sits beside the resolved binary.

    Who owns the install decides the call. A pipx venv is upgraded by pipx, and
    with ``--pre`` spelled out: pipx does not remember the flag the nightly was
    installed with, so a plain upgrade would walk the install back to the
    release line the first time a release outranks the nightly. An interpreter
    beside the binary means a pip or distro install, whose package manager owns
    it and whose yt-dlp refuses to self-update anyway - deliberately the
    conservative reading, so a downloaded binary that happens to be parked in
    such a directory is left alone rather than upgraded by guess. Everything
    else is the single downloaded binary, which updates itself.
    """
    if len(command) != 1:
        # "python -m yt_dlp": the module's install is pip's business.
        return None
    venv = _PIPX_VENV_RE.search(real_path)
    if venv:
        if not pipx_present:
            return None
        return ["pipx", "upgrade", "--pip-args=--pre", venv.group(1)]
    if sibling_python:
        return None
    return [command[0], "-U", "--update-to", "nightly"]


def split_tabs(line: str) -> list[str]:
    """The line's tab-separated fields, EMPTY ONES INCLUDED.

    Not a split on the separator as whitespace: a tab run must not collapse
    into one separator and a leading or trailing one must not drop, or a row
    with empty middle columns would arrive with its URL in the wrong field.
    """
    fields: list[str] = []
    rest = line
    while True:
        fields.append(rest.split("\t", 1)[0])
        if "\t" not in rest:
            break
        rest = rest.split("\t", 1)[1]
    return fields


def split_podcast_row(row: str) -> tuple[str, str, str, str, str, str]:
    """One tab-separated row into the six PODCAST_COLUMNS fields, in order."""
    fields = split_tabs(row)
    return tuple(fields[i] if i < len(fields) else "" for i in range(6))  # type: ignore[return-value]


_BLANK_RE = re.compile(r"^[ \t\r\v\f]*$")
_DIRECTIVE_RE = re.compile(
    r"^#![ \t\r\v\f]*([A-Za-z]+)[ \t\r\v\f]+([^ \t\r\v\f]+)[ \t\r\v\f]*$")
_JOBS_RE = re.compile(r"^[1-9][0-9]*$")
_PLAYLIST_END_RE = re.compile(r"^[0-9]+$")
_URL_RE = re.compile(r"^https?://")


def read_podcast_table(file: str, stderr=None) -> tuple[list[str], str,
                                                        str, int]:
    """The table's data rows, its profile and its jobs, or the refusal.

    Everything is validated before anything is downloaded, and every fault is
    reported at once: the rows are returned as they were read (a malformed row
    is among them, the way the shell's PODCAST_ROWS is on a refusal), and the
    status is 1 when any line is malformed.
    """
    if stderr is None:
        stderr = sys.stderr
    rows: list[str] = []
    profile = PODCAST_DEFAULT_PROFILE
    jobs = ""

    if not os.path.isfile(file):
        stderr.write(f"Podcast table not found: {file}\n")
        return ([], profile, jobs, 1)

    with open(file, encoding="utf-8") as handle:
        content = handle.read()
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]

    faults: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        line = line[:-1] if line.endswith("\r") else line
        if _BLANK_RE.match(line):
            continue

        if line.startswith("#!"):
            match = _DIRECTIVE_RE.match(line)
            if match:
                directive, value = match.group(1), match.group(2)
                if directive == "profile":
                    if podcast_profile_args(value) is not None:
                        profile = value
                    else:
                        faults.append(
                            f'line {line_no}: unknown profile "{value}" - '
                            "one of: " + " ".join(PODCAST_PROFILES))
                elif directive == "jobs":
                    if _JOBS_RE.match(value):
                        jobs = value
                    else:
                        faults.append(
                            f'line {line_no}: jobs is "{value}", expected a '
                            "positive number")
                else:
                    faults.append(
                        f'line {line_no}: unknown directive "#!{directive}" - '
                        "the directives are profile and jobs")
            else:
                faults.append(
                    f'line {line_no}: a #! directive reads "#!<name> '
                    f'<value>", not "{line}"')
            continue

        if line.startswith("#"):
            continue

        tabs = line.count("\t")
        if tabs != len(PODCAST_COLUMNS) - 1:
            faults.append(
                f"line {line_no}: {tabs} tab(s), expected "
                f"{len(PODCAST_COLUMNS) - 1} - the columns are "
                + " ".join(PODCAST_COLUMNS))
            continue

        active, subdir, template, playlist_end, extra, url = \
            split_podcast_row(line)
        if active not in ("0", "1"):
            faults.append(
                f'line {line_no}: active is "{active}", expected 0 or 1')
        if not subdir:
            faults.append(f"line {line_no}: the subdir column is empty")
        if subdir.startswith("/") or ".." in subdir:
            faults.append(
                f'line {line_no}: subdir "{subdir}" must stay under the '
                "output root")
        # The name half of the same path, and so the same rule.
        if template.startswith("/") or ".." in template:
            faults.append(
                f'line {line_no}: nameTemplate "{template}" must stay under '
                "the output root")
        for refused in refused_extra_args(extra):
            faults.append(
                f'line {line_no}: extraArgs may not carry "{refused}" - a row '
                "describes one feed, not where the run writes or what it runs")
        if playlist_end and not _PLAYLIST_END_RE.match(playlist_end):
            faults.append(
                f'line {line_no}: playlistEnd is "{playlist_end}", expected '
                "a number, "
                f"{PODCAST_UNLIMITED_PLAYLIST_END} for all of them, or nothing")
        if not _URL_RE.match(url):
            faults.append(
                f'line {line_no}: url "{url}" is not an http(s) address')

        rows.append(line)

    if faults:
        stderr.write("\n")
        stderr.write(f"Cannot read the podcast table {file}: "
                     f"{len(faults)} line(s) are malformed.\n\n")
        for fault in faults:
            stderr.write(f"  {fault}\n")
        stderr.write("\nFix the lines above and run again. "
                     "Nothing was downloaded.\n")
        return (rows, profile, jobs, 1)

    # A table that says nothing about how wide it may run gets what its
    # profile allows, which is the whole point of naming a profile.
    if not jobs:
        jobs = podcast_profile_jobs(profile)
    return (rows, profile, jobs, 0)


def refused_extra_args(extra: str) -> list[str]:
    """The options in an extraArgs column that a row is not allowed to carry.

    Whitespace-split the way the column itself is, and the "=" form taken as the
    same option: --paths=/elsewhere is --paths.
    """
    return [word for word in extra.split()
            if word.split("=", 1)[0] in PODCAST_REFUSED_ARGS]


_DATE_RE = re.compile(r"^[0-9]{8}$")


def is_ytdlp_date(value: str) -> bool:
    """Whether the date shape is one yt-dlp itself accepts."""
    if _DATE_RE.match(value):
        return True
    # The two alternatives of the bash regex, kept as one test each the way
    # bash's || chain tests them.
    if re.match(r"^(now|today|yesterday)$", value):
        return True
    return re.match(
        r"^(now|today)?[+-][0-9]+(second|minute|hour|day|week|month|year)s?$",
        value) is not None


def parse_date_range(spec: str, stderr=None) -> tuple[str, str] | None:
    """The (after, before) ends of a date range, or None with a message when an
    end is not a date yt-dlp would take.

    An empty spec is no range at all: ("", "").
    """
    if stderr is None:
        stderr = sys.stderr
    after = ""
    before = ""
    if not spec:
        return ("", "")

    if ".." in spec:
        # ${spec%%..*} strips the longest ".." suffix, which starts at the FIRST
        # "..", and ${spec#*..} the shortest prefix, which ends at the first:
        # a spec with more than one ".." is cut at the first, and whatever is
        # left in the before end fails the date test and is named as such.
        after, before = spec.split("..", 1)
    else:
        after = spec

    for endpoint in (after, before):
        if not endpoint:
            continue
        if not is_ytdlp_date(endpoint):
            stderr.write(
                f'Not a date: "{endpoint}". Expected YYYYMMDD or a relative '
                'date like "today-2weeks",\n')
            stderr.write(
                "optionally as a range: <after>..<before>, ..<before> or "
                "<after>..\n")
            return None

    return (after, before)


def podcast_call(output_root: str, archive_file: str, subdir: str,
                 template: str, playlist_end: str, extra_args: str,
                 url: str, *, ytdlp_command: Sequence[str],
                 profile: str = "", date_after: str = "",
                 date_before: str = "", verbose: str = "",
                 sponsorblock: str | None = None) -> list[str] | None:
    """The complete argv for one feed, or None when the profile is unknown.

    ``sponsorblock`` carries the caller's settled answer as three states: None
    for never asked (the flags stay), "" for settled absent (they come out), a
    non-empty string for settled present (they stay).
    """
    output_root = output_root.removesuffix("/")
    subdir = subdir.removesuffix("/")
    if not template:
        template = PODCAST_DEFAULT_TEMPLATE
    if not playlist_end:
        playlist_end = str(PODCAST_DEFAULT_PLAYLIST_END)

    profile_args = podcast_profile_args(profile or PODCAST_DEFAULT_PROFILE)
    if profile_args is None:
        return None

    call: list[str] = list(ytdlp_command)
    call += ["-o", f"{output_root}/{subdir}/{template}"]
    # The unlimited row asks for no --playlist-end at all rather than for a very
    # large one.
    if playlist_end != str(PODCAST_UNLIMITED_PLAYLIST_END):
        call += ["--playlist-end", playlist_end]
    call += list(profile_args)
    if date_after:
        call += ["--dateafter", date_after]
    if date_before:
        call += ["--datebefore", date_before]
    call += ["--download-archive", archive_file,
             "--print", f"after_move:{PODCAST_EPISODE_MARKER}%(filepath)s"]
    if verbose:
        call += ["--no-quiet", "--verbose"]
    else:
        call += ["--no-warnings"]

    # Whitespace-split on purpose: the column holds yt-dlp arguments, not a
    # shell command line.
    if extra_args:
        call += extra_args.split()

    if sponsorblock is not None and sponsorblock == "":
        clean: list[str] = []
        skip_value = False
        for arg in call:
            if skip_value:
                skip_value = False
                continue
            if arg in ("--sponsorblock-remove", "--sponsorblock-mark"):
                skip_value = True
                continue
            clean.append(arg)
        call = clean

    call.append(url)
    return call


def _file_size(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def is_bot_block(line: str) -> bool:
    """True for the "Sign in to confirm you're not a bot" refusal, and false for
    everything else - including the age gate, which shares the first fragment
    and not the second.

    Matched on two fragments of the lowercased line rather than on the
    sentence, because every other part of it is unstable: the apostrophe is a
    curly U+2019, the wording appears both as "you're" and as "you are", the
    extractor tag is not always [youtube], and the advice that follows has
    changed more than once.
    """
    lower = shell_lower(line)
    return ("sign in to confirm you" in lower
            and "not a bot" in lower)


def podcast_bot_block_warning(provider: str, stderr=None) -> None:
    """What to say when a provider refuses us, printed where it happens."""
    if stderr is None:
        stderr = sys.stderr
    stderr.write(
        'WARNING: %s answered "Sign in to confirm you are not a bot".\n'
        % provider)
    stderr.write(
        "         Every further request would be refused the same way and would only\n"
        "         confirm the pattern that got this address blocked, so the %s feeds\n"
        % provider)
    stderr.write(
        "         are being stopped here. Feeds from other providers are unaffected.\n"
        "         What helps: wait, come back from a different address, or pass cookies -\n"
        "         https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies\n")


def _read_counter(counter_file: str) -> int:
    """``read -r n <file``: one variable takes the WHOLE line, so a second line
    does not reach the arithmetic, and neither does a second field - the run
    writes a single integer, and that is what this reads. A first line that is
    not a number is fatal in the shell under the set -u the module runs in -
    an unbound variable in the arithmetic, with the crash naming the bash file
    and line - so the corpus stops short of it; this answers with the idiom's
    nounset value, the variable by that name, unset of which is zero."""
    try:
        with open(counter_file, encoding="utf-8") as handle:
            line = handle.readline()
    except OSError:
        return 0
    try:
        return int(line.strip()) if line.strip() else 0
    except ValueError:
        # The shell's arithmetic takes the value of a variable by that name,
        # unset of which is zero: garbage in the file still counts from one.
        return 0


def _emit_line(counter_file: str, status: str, podcast: str, episode: str,
               size: str) -> None:
    n = _read_counter(counter_file) + 1
    with open(counter_file, "w", encoding="utf-8") as handle:
        handle.write(f"{n}\n")
    sys.stdout.write(f"[{n:4d}] {status:<4} {podcast} | {episode} | {size}\n")


def _take_dir_lock(directory: str) -> None:
    """mkdir succeeds for exactly one caller and fails for every other, which
    is the whole lock. Bounded rather than infinite: a process killed between
    the mkdir and the rmdir would otherwise stop the run for ever, and
    printing one line out of order is a far smaller price than a download that
    never resumes."""
    tries = 0
    while True:
        try:
            os.mkdir(directory)
            return
        except OSError:
            tries += 1
            if tries > 200:
                return
            time.sleep(0.05)


def report_episodes(stream: str, counter_file: str, manifest_file: str,
                    podcast: str, block_flag: str = "",
                    provider: str = "the provider", verbose: str = "",
                    have_flock: bool = False) -> int:
    """Filter a feed's output into the run's own progress: one line per
    episode, numbered across the whole run.

    ``stream`` is yt-dlp's output as read from its pipe: a line without a
    trailing newline at the very end is not read, the way the shell's
    ``read`` leaves it. Reads on a bot block stop where they happen - the
    remaining lines are the block deepening, and the function returns 0 there
    the way the shell's does.
    """
    lines = stream.split("\n")[:-1]

    for line in lines:
        line = line[:-1] if line.endswith("\r") else line
        if line.startswith(PODCAST_EPISODE_MARKER):
            path = line[len(PODCAST_EPISODE_MARKER):]
            episode = path.rsplit("/", 1)[-1]
            size = _file_size(path)
            with open(manifest_file, "a", encoding="utf-8") as handle:
                handle.write(f"{path}\n")
            if have_flock:
                _locked(_emit_line, f"{counter_file}.lock", counter_file,
                        "ok", podcast, episode, fmt_bytes(size))
            else:
                _dir_locked(_emit_line, f"{counter_file}.lockdir",
                            counter_file, "ok", podcast, episode,
                            fmt_bytes(size))
        elif line.startswith("ERROR:"):
            # ${line#ERROR: } strips the literal prefix only when it is there;
            # a line that starts with the marker but not the space keeps it.
            detail = line[len("ERROR: "):] if line.startswith("ERROR: ") else line
            if block_flag and is_bot_block(line):
                # Record it for the rest of the run, say so where it happened,
                # and then stop reading: carrying on through the remaining
                # refusals is precisely the behaviour that deepens the block.
                try:
                    with open(block_flag, "w", encoding="utf-8"):
                        pass
                except OSError:
                    # `: >"$blockFlag" 2>/dev/null || true`: a flag that cannot
                    # be written (its folder missing) still stops the run.
                    pass
                if have_flock:
                    _locked(_emit_line, f"{counter_file}.lock", counter_file,
                            "STOP", podcast, detail, "-")
                else:
                    _dir_locked(_emit_line, f"{counter_file}.lockdir",
                                counter_file, "STOP", podcast, detail, "-")
                podcast_bot_block_warning(provider)
                return 0
            if have_flock:
                _locked(_emit_line, f"{counter_file}.lock", counter_file,
                        "FAIL", podcast, detail, "-")
            else:
                _dir_locked(_emit_line, f"{counter_file}.lockdir",
                            counter_file, "FAIL", podcast, detail, "-")
        else:
            if verbose:
                sys.stdout.write(f"{line}\n")
    return 0


def _locked(emit, lock_file: str, *args) -> None:
    """The flock branch: read-increment-write plus the print under one lock."""
    import fcntl
    with open(lock_file, "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            emit(*args)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _dir_locked(emit, lock_dir: str, *args) -> None:
    """The mkdir branch: the same, under a mkdir lock for a host without
    flock."""
    _take_dir_lock(lock_dir)
    try:
        emit(*args)
    finally:
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass


def podcast_download_stats(manifest_dir: str) -> tuple[int, int]:
    """The (file count, byte count) of the episodes the manifests list.

    A line whose file is no longer there still counts as a file (it WAS
    fetched) but adds no bytes, so a missing one can never make the run look
    like it failed.
    """
    file_count = 0
    byte_count = 0
    if not os.path.isdir(manifest_dir):
        return (0, 0)

    # The shell's glob is the locale's collation order; under the C locale that
    # is byte order, and UTF-8 byte order is code point order, so a plain sort
    # walks the same sequence.
    for name in sorted(os.listdir(manifest_dir)):
        if name.startswith("."):
            continue  # the shell's * glob never names a dotfile
        manifest = os.path.join(manifest_dir, name)
        if not os.path.isfile(manifest):
            continue
        with open(manifest, encoding="utf-8") as handle:
            content = handle.read()
        # read || [[ -n ]]: a final line without its newline is read too.
        paths = content.split("\n")
        if paths and paths[-1] == "":
            paths = paths[:-1]
        for path in paths:
            path = path[:-1] if path.endswith("\r") else path
            if not path:
                continue
            file_count += 1
            size = _file_size(path)
            byte_count += size
    return (file_count, byte_count)


_SAFE_POSIX = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_SAFE_POWERSHELL = re.compile(r"^[A-Za-z0-9_@%+=:,./\\-]+$")


def quote_posix(arg: str) -> str:
    """The argument quoted only if it has to be quoted, for a POSIX shell."""
    if _SAFE_POSIX.match(arg):
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"


def quote_powershell(arg: str) -> str:
    """The argument quoted only if it has to be quoted, for PowerShell - which
    ends a single-quoted string by doubling the quote rather than escaping
    it."""
    if _SAFE_POWERSHELL.match(arg):
        return arg
    return "'" + arg.replace("'", "''") + "'"


def render_call(argv: Sequence[str], platform: str) -> str:
    """The argv as a command line that can be pasted into a shell on the
    platform named - which is what the dry run prints."""
    out = ""
    if platform == "windows" and argv and re.search(r"[/\\]", argv[0]):
        out = "& "

    for arg in argv:
        if out and out != "& ":
            out += " "
        if platform == "windows":
            out += quote_powershell(arg)
        else:
            out += quote_posix(arg)
    return out