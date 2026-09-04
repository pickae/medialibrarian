"""ytdlp: the feed tables fetched, one line per episode.

A table says what it is with a "#!profile" line, and its profile decides both
what its feeds are fetched as and how many at once. Tables are then grouped into
LANES by the provider their feeds come from: within a lane they run one after
another, and the lanes run alongside each other. That is the provider's rule
rather than the run's - two YouTube tables at once are one client asking YouTube
for twice as much at once, which is what gets a client throttled, while an RSS
table is dozens of unrelated servers who cannot see each other.

Anything counted across the whole run is counted in FILES, one line per
occurrence, because a lane and a parallel table's feeds run in separate
processes and nothing they add to a variable reaches the parent.
"""

import os
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from multiprocessing import Process

from medialib import commands
from medialib.lib import (
    clioptions,
    downloadcleanup,
    ffmpegselect,
    podcastfeeds,
    ramscratch,
    runlog,
    safety,
    tooldeps,
    workerpool,
)
from medialib.lib.formatting import fmt_bytes, fmt_hms
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <outputPath> <archiveFile> [<dateRange>]

    <outputPath>    the library root every feed's subdir hangs under - or, with
                    -i, the parent of the staging and the library trees
    <archiveFile>   yt-dlp's download archive: the record of what has already
                    been fetched, so a re-run only picks up what is new. It is
                    created if it does not exist.
    <dateRange>     optional upload-date filter, applied to every feed:
                        20260607             everything since that day
                        20260607..20260707   a window
                        ..20260707           everything up to that day
                        today-2weeks         relative dates work at either end
                        ..today-10days       and so a rolling window is one of
                                             them: nothing newer than ten days,
                                             which is how the video library was
                                             fetched before it had a table
                    Without it, --playlist-end and the archive decide alone.

Options:"""

OPT_SPEC = """
h |  | Print this help page.
t | <table> | A feed table to read. May be given more than once, and each
                    table brings its own profile and its own parallelism (see
                    below). Default: podcasts.tsv beside this script.
j | <jobs> | Cap how many feeds any one table may fetch at once. Lower
                    only; a table asking for less keeps its own number.
n |  | Dry run: print the yt-dlp call each feed would get and
                    download nothing.
v |  | Verbose: print yt-dlp's own output as well. Without it the
                    run prints exactly one line per episode and nothing else.
a |  | Also fetch the feeds whose active column is 0.
c |  | Tidy up after the run: the sidecars each finished episode
                    leaves (the thumbnail that was embedded, the description and
                    the metadata json, which are attached into a Matroska
                    first), anything that is not Matroska remuxed into one, the
                    leftovers of an interrupted download, and the folders left
                    empty. Only the episodes THIS run downloaded are touched;
                    the sweep for interrupted leftovers covers <outputPath>.
i |  | Build the phone's library from the run instead of leaving the
                    downloads as they came off the network. <outputPath> becomes
                    the parent of two trees: the raw downloads land in
                    <outputPath>/Staging/<kind>, and when the run is over their
                    names are cleaned (clean-folder-structure) and they are
                    converted into <outputPath>/Ingested (convert-audio) - one
                    staging folder per conversion, all of them into that one
                    library. The raw downloads are KEPT: they are what a re-run
                    would otherwise have to fetch again. Normally given with -c,
                    so the sidecars are gone before the library is built.
                    Every staging folder with files in it is converted, including
                    the folders of conversions no table in this run downloads into.
                    A video table ignores -i and downloads to <outputPath> as
                    usual - an audio converter would throw its picture away.
m | <match> | Only the feeds whose subdir or URL contains <match>
                    (case-insensitive).
s | <system> | Build the calls for "windows" or "linux" instead of for
                    this host. Only useful with -n, to read off what the other
                    machine would run.
"""

# -t is the one repeatable option here: every occurrence appends, so several
# tables can be read in one run. The rest assign once, last flag winning.
OPT_FLAGS = "repeat:t"
OPT_VARS = "t:tables j:jobCap n:dryRun a:includeInactive c:cleanUp i:ingest m:match"

# -j caps a table's own parallelism and 0 means "no cap", so zero belongs in the
# range; -s is the one option whose accepted values are words rather than a
# number. The choices are joined with an ESCAPED pipe, because a plain one
# separates the spec's own fields.
OPT_CHECKS = """
j | nonNegInt | feed cap
s | enum:windows\\|linux\\|auto | system
"""

OPT_COLUMN = 20
OPT_LONG = ("h:help t:table j:jobs n:dry-run v:verbose a:include-inactive "
            "c:clean-up i:ingest m:match s:system")

USAGE_TAIL = """

A table says what it is with a "#!profile <name>" line, one of:
    youtubeAudio    (the default) audio only, SponsorBlock segments cut out,
                    politely paced, one feed at a time
    youtubeVideo    audio and video, muxed into Matroska, with SponsorBlock
                    segments marked as chapters rather than cut
    rssAudio        ordinary podcast RSS: audio only, and {jobs} feeds at
                    once, because they are {jobs} different providers each
                    serving one client one feed
    rssVideo        the same for a video podcast feed
    siteVideo       a video site that is neither: no SponsorBlock and no format
                    codes, just the page's video and its poster frame
A "#!jobs <n>" line overrides how wide that table runs."""


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        flags=OPT_FLAGS,
        checks=OPT_CHECKS,
        tail=USAGE_TAIL.format(jobs=podcastfeeds.PODCAST_RSS_JOBS),
        column=OPT_COLUMN,
        # The no-argument refusal goes to stderr: a run of this script is usually
        # the tail of a pipeline, and its usage belongs with the other
        # diagnostics rather than in whatever is reading its stdout.
        no_args_stream="stderr",
    )


class Counters:
    """Anything counted across the whole run.

    In FILES when there is a scratch, because a lane and a parallel table's
    feeds are separate processes; in plain attributes for a dry run, which has
    no scratch and never forks.
    """

    def __init__(self, status_dir: str):
        self.status_dir = status_dir
        self.skipped_inactive = 0
        self.skipped_unmatched = 0
        self.feed_index = 0

    def record_skip(self, kind: str) -> None:
        if self.status_dir:
            with open(os.path.join(self.status_dir, "skip." + kind),
                      "a") as handle:
                handle.write("x\n")
        elif kind == "inactive":
            self.skipped_inactive += 1
        else:
            self.skipped_unmatched += 1

    def next_feed_index(self) -> int:
        """The feed's position in the run, taken under a lock so two lanes can
        never be given the same one."""
        if not self.status_dir:
            self.feed_index += 1
            return self.feed_index
        lock = os.path.join(self.status_dir, "feedIndex.lockdir")
        _take_dir_lock(lock)
        try:
            path = os.path.join(self.status_dir, "feedIndex")
            count = _count_lines(path) + 1
            with open(path, "a") as handle:
                handle.write("x\n")
            return count
        finally:
            try:
                os.rmdir(lock)
            except OSError:
                pass

    def count(self, name: str) -> int:
        if not self.status_dir:
            return {"feedIndex": self.feed_index,
                    "skip.inactive": self.skipped_inactive,
                    "skip.unmatched": self.skipped_unmatched}.get(name, 0)
        return _count_lines(os.path.join(self.status_dir, name))


def _count_lines(path: str) -> int:
    try:
        with open(path, "rb") as handle:
            return handle.read().count(b"\n")
    except OSError:
        return 0


def _take_dir_lock(path: str) -> None:
    """mkdir is atomic, which is the whole lock: whoever creates the directory
    holds it."""
    for _ in range(10000):
        try:
            os.mkdir(path)
            return
        except FileExistsError:
            time.sleep(0.05)
        except OSError:
            return


class Run:
    """The settled world one run works in: what was asked for, where things go,
    and the scratch its processes speak back through."""

    # Handed over once the tables have been read, by the run that read them.
    rows_of: list[list[str]]

    def __init__(self, options, ytdlp_command, platform, native_output,
                 native_archive, manifest_dir, native_manifest_dir, status_dir,
                 counter_file, counters, staging_for_profile, script_dir):
        self.options = options
        self.ytdlp_command = ytdlp_command
        self.platform = platform
        self.native_output = native_output
        self.native_archive = native_archive
        self.manifest_dir = manifest_dir
        self.native_manifest_dir = native_manifest_dir
        self.status_dir = status_dir
        self.counter_file = counter_file
        self.counters = counters
        self.staging_for_profile = staging_for_profile
        self.script_dir = script_dir
        self.have_flock = runlog.have_flock()

    # --- what stops a run -------------------------------------------------
    def provider_blocked(self, provider: str) -> bool:
        """True once a provider has refused this run outright. One flag per
        PROVIDER: YouTube blocking us says nothing about the forty unrelated
        servers an RSS table is talking to."""
        return bool(self.status_dir) and os.path.exists(
            os.path.join(self.status_dir, "blocked." + provider))

    # --- one feed ---------------------------------------------------------
    def run_feed(self, index: int, podcast: str, row: str, provider: str,
                 root: str) -> None:
        """Make one feed's call and record its outcome where the parent reads
        it - a file, because this may run in a background process whose exit
        code the parent only ever sees through a join."""
        if safety.abort_requested() or self.provider_blocked(provider):
            return

        fields = podcastfeeds.split_podcast_row(row)
        # active, subdir, nameTemplate, playlistEnd, extraArgs, url - the
        # column order the table is written in.
        argv = podcastfeeds.podcast_call(
            root, self.native_archive, fields[1], fields[2], fields[3],
            fields[4], fields[5],
            ytdlp_command=self.ytdlp_command,
            profile=os.environ.get("PODCAST_PROFILE", ""),
            date_after=os.environ.get("PODCAST_DATE_AFTER", ""),
            date_before=os.environ.get("PODCAST_DATE_BEFORE", ""),
            verbose=os.environ.get("PODCAST_VERBOSE", ""),
            sponsorblock=os.environ.get("PODCAST_SPONSORBLOCK"))
        if argv is None:
            return

        manifest = os.path.join(self.manifest_dir, "feed%d" % index)
        block_flag = os.path.join(self.status_dir, "blocked." + provider)
        status = self._download(argv, manifest, podcast, block_flag, provider)

        # A feed the reader cut short did not fail - it was stopped, and saying
        # otherwise would put it in the list of feeds to go and look at.
        if self.provider_blocked(provider) and status != 0:
            status = "blocked"

        # An interrupt can take the scratch away underneath a feed that was
        # already dispatched; its status is then simply absent, which reads as
        # "never finished" rather than as a failure - and is exactly right.
        try:
            part = os.path.join(self.status_dir, "%d.part" % index)
            with open(part, "w") as handle:
                handle.write("%s\n" % status)
            # Renamed into place as the last act: the rename is atomic, so the
            # presence of <index> is a reliable "this feed is finished" signal.
            os.replace(part, os.path.join(self.status_dir, str(index)))
        except OSError:
            return

    def _download(self, argv, manifest: str, podcast: str, block_flag: str,
                  provider: str):
        """Run one call, reporting its episodes AS THEY ARRIVE.

        The shell pipes yt-dlp straight into the reporter, so a feed's lines
        appear while it downloads rather than when it finishes; the reporter is
        stateless between lines - everything it counts lives in files - so it is
        fed one complete line at a time and the liveness survives the port.
        """
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL)
        except OSError:
            return 1
        if process.stdout is None:
            # stdout=PIPE was asked for, so this is the same "could not start"
            # the OSError above answers.
            return 1
        with process.stdout as stream:
            for raw in stream:
                line = raw.decode("utf-8", "replace")
                if not line.endswith("\n"):
                    # The shell's `read` does not deliver a final partial line.
                    break
                podcastfeeds.report_episodes(
                    line, self.counter_file, manifest, podcast, block_flag,
                    provider, os.environ.get("PODCAST_VERBOSE", ""),
                    self.have_flock)
                if os.path.exists(block_flag):
                    # Carrying on through the remaining refusals is precisely
                    # the behaviour that deepens the block.
                    break
        return process.wait()

    # --- one table --------------------------------------------------------
    def run_table(self, profile: str, jobs: int, label: str, rows) -> None:
        """One table's feeds, at that table's width: its profile decides what
        they are fetched as and its jobs how many at a time, so a table is the
        unit both settings belong to."""
        if not rows:
            return
        os.environ["PODCAST_PROFILE"] = profile
        provider = podcastfeeds.podcast_profile_provider(profile)

        # Where this table's feeds land: its staging folder when the run is
        # building a library out of them, and the output path otherwise - which
        # is also what a video table gets in an -i run, since it is not
        # converted.
        root = self.staging_for_profile.get(profile) or self.native_output
        log("%s: %d feed(s), profile %s, %d at a time"
            % (label, len(rows), profile, jobs))

        running: list[Process] = []
        match = (self.options["match"] or "").lower()
        for row in rows:
            if safety.abort_requested() or self.provider_blocked(provider):
                break
            fields = podcastfeeds.split_podcast_row(row)
            active, subdir, url = fields[0], fields[1], fields[5]

            if active == "0" and not self.options["includeInactive"]:
                self.counters.record_skip("inactive")
                continue
            if match and match not in subdir.lower() and match not in url.lower():
                self.counters.record_skip("unmatched")
                continue

            index = self.counters.next_feed_index()
            if self.status_dir:
                with open(os.path.join(self.status_dir, "%d.name" % index),
                          "w") as handle:
                    handle.write(subdir + "\n")

            if self.options["dryRun"]:
                argv = podcastfeeds.podcast_call(
                    root, self.native_archive, subdir, fields[2], fields[3],
                    fields[4], url, ytdlp_command=self.ytdlp_command,
                    profile=profile,
                    date_after=os.environ.get("PODCAST_DATE_AFTER", ""),
                    date_before=os.environ.get("PODCAST_DATE_BEFORE", ""),
                    verbose=os.environ.get("PODCAST_VERBOSE", ""),
                    sponsorblock=os.environ.get("PODCAST_SPONSORBLOCK"))
                if argv is not None:
                    print(podcastfeeds.render_call(argv, self.platform))
                continue

            if jobs <= 1:
                self.run_feed(index, subdir, row, provider, root)
                continue

            running.append(_spawn(_feed_worker,
                                  (self, index, subdir, row, provider, root)))
            if len(running) >= jobs:
                running = workerpool.reap_one(running)

        # This table's stragglers, before the next table in the same lane
        # starts: that is what "one table at a time within a lane" means.
        for job in running:
            job.join()

    def run_lane(self, lane_tables, tables, profiles, jobs_of) -> None:
        """The tables of one lane, one after another."""
        for index in lane_tables:
            if safety.abort_requested():
                break
            profile = profiles[index]
            provider = podcastfeeds.podcast_profile_provider(profile)
            # The lane's whole point: once this provider has refused us, the
            # tables still queued behind this one are not started either.
            if self.provider_blocked(provider):
                break
            jobs = jobs_of[index]
            cap = self.options["jobCap"]
            # -j lowers only: a table that asked for one feed at a time asked
            # for a reason, and a cap is about this machine's connection.
            if cap > 0 and jobs > cap:
                jobs = cap
            rows = [line for line in self.rows_of[index] if line]
            self.run_table(profile, jobs, tables[index], rows)


def _spawn(target, args) -> "Process":
    import multiprocessing

    process = multiprocessing.Process(target=target, args=args)
    process.start()
    return process


def _feed_worker(state, index, subdir, row, provider, root) -> None:
    """One feed, in a worker process.

    bash sets SIGINT to ignored in the background jobs of a non-interactive
    shell, so the shell's own worker has to ask for it back; here the handler is
    installed for the same reason - recording the flag is what stops the REST of
    the run.
    """
    safety.trap_worker_abort()
    state.run_feed(index, subdir, row, provider, root)


def _default_tables(script_dir: str):
    """The one table that has always been there, so the common call stays as
    short as it was. Looked for in data/podcasts first, which is where the
    tables actually live - they are one machine's library rather than code."""
    first = os.path.join(script_dir, "data", "podcasts", "podcasts.tsv")
    if os.path.isfile(first):
        return [first]
    return [os.path.join(script_dir, "podcasts.tsv")]


def _resolve_ytdlp(platform: str, script_dir: str):
    """How to run yt-dlp here, resolved rather than assumed: a downloaded .exe
    beside the script on Windows, and on Linux a nightly install ahead of the
    apt/pip/pipx release one."""
    present, executable, importable = set(), set(), set()
    for name in ("yt-dlp-nightly", "yt-dlp", "yt-dlp.exe",
                 "python3", "python", "py"):
        if shutil.which(name):
            present.add(name)
    local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
    for candidate in (os.path.join(script_dir, "yt-dlp.exe"), "./yt-dlp.exe",
                      os.path.join(local_bin, "yt-dlp-nightly"),
                      os.path.join(local_bin, "yt-dlp")):
        if os.access(candidate, os.X_OK):
            executable.add(candidate)
    for name in ("python3", "python", "py"):
        if name not in present:
            continue
        try:
            if subprocess.run([name, "-c", "import yt_dlp"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                importable.add(name)
        except OSError:
            pass
    return podcastfeeds.resolve_ytdlp(
        os.environ.get("YTDLP", ""), platform, os.path.expanduser("~"),
        script_dir, os.getcwd(), present, executable, importable)


def _ytdlp_version(ytdlp_command) -> str:
    """What this yt-dlp calls itself, or "" when it will not say.

    A yt-dlp that cannot even print its version is not one to upgrade behind
    the user's back; the run goes on and fails, or not, on its own terms.
    """
    try:
        proc = subprocess.run(list(ytdlp_command) + ["--version"],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else ""


def _upgrade_nightly(ytdlp_command) -> None:
    """A nightly is brought up to date before the run, a release is left alone.

    The whole point of running the nightly is the extractor fix that landed
    after the last release, and a fix that arrived this morning is no use to a
    binary from last month. So the install's own upgrade path is taken, which
    is also the test for whether there is anything to take: both pipx and
    yt-dlp's self-update check the index and do nothing when the install is
    current.

    CAN this be upgraded is settled first, because it costs nothing, and only
    then IS it a nightly - which costs a process.

    Nothing here can fail the run. Offline, an index that is down, a venv the
    user cannot write to: each is a warning and the run goes on with the
    yt-dlp it already has, because a podcast fetch that refuses to start over
    a missed upgrade is worse than one that fetches with last night's build.
    """
    real = os.path.realpath(shutil.which(ytdlp_command[0]) or ytdlp_command[0])
    beside = os.path.dirname(real)
    upgrade = podcastfeeds.ytdlp_upgrade_command(
        ytdlp_command, real, bool(shutil.which("pipx")),
        any(os.access(os.path.join(beside, name), os.X_OK)
            for name in ("python", "python3")))
    if upgrade is None:
        return
    version = _ytdlp_version(ytdlp_command)
    if not podcastfeeds.is_nightly_version(version):
        return
    log("yt-dlp %s is a nightly: checking for a newer one" % version)
    try:
        proc = subprocess.run(upgrade, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              timeout=600)
    except (OSError, subprocess.SubprocessError) as failure:
        log("WARNING: the nightly upgrade could not run (%s) - going on with "
            "%s" % (failure.__class__.__name__, version))
        return
    now = _ytdlp_version(ytdlp_command)
    if now and now != version:
        log("yt-dlp upgraded: %s -> %s" % (version, now))
        return
    if proc.returncode != 0:
        log("WARNING: the nightly upgrade failed (exit %d) - going on with %s"
            % (proc.returncode, version))
        for line in proc.stdout.splitlines()[-3:]:
            log("         %s" % line)


def _probe_sponsorblock(ytdlp_command) -> None:
    """Whether this yt-dlp knows the SponsorBlock flags.

    Captured rather than piped into a match: a matcher that stops at its first
    hit leaves the help page's next write hitting a closed pipe, and pipefail
    then reads that SIGPIPE as the probe's answer - a yt-dlp that HAS the flags
    read as one that has not.
    """
    try:
        proc = subprocess.run(list(ytdlp_command) + ["--help"],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        text = proc.stdout.decode("utf-8", "replace")
    except OSError:
        text = ""
    if "sponsorblock" in text.lower():
        os.environ["PODCAST_SPONSORBLOCK"] = "1"
        return
    os.environ["PODCAST_SPONSORBLOCK"] = ""
    log("WARNING: this yt-dlp has no SponsorBlock support - the YouTube feeds "
        "keep their sponsor")
    log("         and ad segments, which newer yt-dlp releases would cut out. "
        "Everything else is unaffected.")


def _lanes(profiles):
    """Tables grouped by the PROVIDER their feeds come from.

    YouTube tables share one lane so they queue behind one another; every other
    table gets a lane to itself so it does not queue behind anything.
    """
    order, lanes = [], {}
    for index, profile in enumerate(profiles):
        provider = podcastfeeds.podcast_profile_provider(profile)
        lane = "youtube" if provider == "youtube" else "%s:%d" % (provider,
                                                                  index)
        if lane not in lanes:
            order.append(lane)
            lanes[lane] = []
        lanes[lane].append(index)
    return order, lanes


def main(argv: list, program: str = "ytdlp", script_dir: str = "") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv, _on_opt)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        sys.stderr.write(clioptions.usage_error_text(declaration,
                                                     error.message))
        return 1

    script_dir = script_dir or commands.script_dir()

    tables = result.values["tables"] or _default_tables(script_dir)
    if clioptions.args_out_of_range(len(result.positionals), 2, 3):
        sys.stderr.write(clioptions.no_args_text(declaration))
        return 1

    output_path = result.positionals[0].rstrip("/")
    archive_file = result.positionals[1]
    date_range = result.positionals[2] if len(result.positionals) > 2 else ""

    options = {
        "dryRun": bool(result.values["dryRun"]),
        "includeInactive": bool(result.values["includeInactive"]),
        "cleanUp": bool(result.values["cleanUp"]),
        "ingest": bool(result.values["ingest"]),
        "match": result.values["match"],
        "jobCap": int(result.values["jobCap"] or 0),
        "outputPath": output_path,
    }

    # EVERY table, the date range and the tools are checked up front and each
    # reports every fault at once: the alternative is finding out about the
    # second one an hour into the run.
    settled = podcastfeeds.parse_date_range(date_range)
    if settled is None:
        return 2
    after, before = settled
    if after:
        os.environ["PODCAST_DATE_AFTER"] = after
    if before:
        os.environ["PODCAST_DATE_BEFORE"] = before

    profiles, jobs_of, rows_of, feed_count, bad = [], [], [], 0, False
    for table in tables:
        # The third value is the table's OWN width: a "#!jobs" line overrides
        # what the profile would run at, so recomputing it from the profile
        # would quietly drop the override.
        rows, profile, jobs, status = podcastfeeds.read_podcast_table(table)
        if status != 0:
            bad = True
        profiles.append(profile)
        # A refused table answers with an empty width; every fault is collected
        # and reported at once, so this must not raise on the way there.
        jobs_of.append(int(jobs) if str(jobs).strip().isdigit() else 0)
        rows_of.append(rows)
        feed_count += len(rows)
    if bad:
        return 2

    platform = podcastfeeds.podcast_platform(
        os.environ.get("PODCAST_PLATFORM", ""), _uname(), os.environ.get("OS", ""))
    ytdlp_command = _resolve_ytdlp(platform, script_dir)
    skip_preflight = bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))
    if ytdlp_command is None:
        if tooldeps.require_tools("the podcast downloads", ["yt-dlp"],
                                  skip_preflight=skip_preflight):
            return 1
        # Only reachable with the preflight switched off (the test suite):
        # resolve to the plain name so the run has a defined command.
        ytdlp_command = ["yt-dlp"]

    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()
    if tooldeps.require_tools("the podcast downloads", ["ffmpeg"],
                              skip_preflight=skip_preflight):
        return 1

    if not skip_preflight and not tooldeps.tool_present("AtomicParsley"):
        log("WARNING: AtomicParsley not installed (apt install atomicparsley) "
            "- the feeds that offer no Opus")
        log("         are fetched as m4a and will arrive without embedded "
            "cover art. Everything else is unaffected.")
    # Before the probe, because an upgrade can be what puts the flags there,
    # and before the run, because that is the build the episodes are fetched
    # with. Never on a dry run, which changes nothing on this machine.
    if (not skip_preflight and not options["dryRun"]
            and not os.environ.get("SKIP_YTDLP_UPGRADE", "")):
        _upgrade_nightly(ytdlp_command)
    if not skip_preflight:
        _probe_sponsorblock(ytdlp_command)

    if options["cleanUp"] and not options["dryRun"]:
        kind = "audio"
        for profile in profiles:
            if podcastfeeds.podcast_profile_media(profile) == "video":
                kind = "video"
        tools = downloadcleanup.download_cleanup_tools(kind).split()
        if tools and tooldeps.require_tools(
                "the tidy-up (-c) of the video tables", tools,
                skip_preflight=skip_preflight):
            return 1

    return _run(options, tables, profiles, jobs_of, rows_of, feed_count,
                output_path, archive_file, ytdlp_command, platform, script_dir)


def _on_opt(letter: str, value: str) -> None:
    """-v and -s name no variable of their own: both settle an environment
    variable the feed library reads."""
    if letter == "v":
        os.environ["PODCAST_VERBOSE"] = "1"
    elif letter == "s":
        if value in ("windows", "linux"):
            os.environ["PODCAST_PLATFORM"] = value
        elif value == "auto":
            os.environ.pop("PODCAST_PLATFORM", None)


def _uname() -> str:
    try:
        return subprocess.run(["uname", "-s"], stdout=subprocess.PIPE
                              ).stdout.decode("utf-8", "replace").strip()
    except OSError:
        return ""




def _plan_ingest(output_path: str, profiles, script_dir: str):
    """The two trees -i makes.

    What comes off the network and what the phone plays are not the same file,
    so with -i they are not the same TREE either: the downloads land in a
    staging folder and the library is built next to it. The raw downloads are
    KEPT - the archive says they will not be fetched again.
    """
    base = output_path if output_path.startswith("/") else os.path.join(
        os.getcwd(), output_path)
    # Heterogeneous on purpose: it is the shell's associative arrays, one run's
    # worth of them, carried together.
    plan: dict[str, Any] = {
        "base": base,
        "root": os.path.join(base, "Ingested"),
        "args": {},
        "staging": {},
        "kinds": [],
        "carried": [],
        "notIngested": [],
        "forProfile": {},
    }
    # EVERY staging folder this script knows how to convert, not only the ones
    # this run downloads into: where a folder is and what converts it follows
    # from the conversion alone.
    all_kinds = []
    for profile in podcastfeeds.PODCAST_PROFILES:
        spec_line = podcastfeeds.podcast_profile_ingest(profile)
        if not spec_line:
            continue
        kind, _, arguments = spec_line.partition(" ")
        if kind not in plan["args"]:
            all_kinds.append(kind)
        plan["args"][kind] = arguments
        plan["staging"][kind] = os.path.join(base, "Staging", kind)

    this_run = set()
    for profile in profiles:
        if profile in plan["forProfile"]:
            continue
        spec_line = podcastfeeds.podcast_profile_ingest(profile)
        if not spec_line:
            # A profile with no conversion is a video one, and a video table
            # keeps the behaviour it has without -i. Collected rather than
            # refused: six audio tables and one video table is an ordinary
            # night, not a mistake.
            plan["notIngested"].append(profile)
            continue
        kind = spec_line.partition(" ")[0]
        plan["forProfile"][profile] = plan["staging"][kind]
        if kind not in this_run:
            plan["kinds"].append(kind)
            this_run.add(kind)

    # A staging folder no table in this run downloads into, left with files by
    # an earlier one: converted as well. What -i ingests is the FOLDER, not the
    # run.
    for kind in all_kinds:
        if kind in this_run:
            continue
        staging = plan["staging"][kind]
        if os.path.isdir(staging) and _holds_a_file(staging):
            plan["carried"].append(kind)
    return plan


def _holds_a_file(directory: str) -> bool:
    return any(files for _parent, _dirs, files in os.walk(directory))


def _run(options, tables, profiles, jobs_of, rows_of, feed_count, output_path,
         archive_file, ytdlp_command, platform, script_dir) -> int:
    ingest = None
    if options["ingest"]:
        # No preflight for the two commands the library copy runs: they are
        # modules of this package rather than files that can be missing from a
        # checkout, which is what the check that stood here was for.
        ingest = _plan_ingest(output_path, profiles, script_dir)

    if not options["dryRun"]:
        os.makedirs(output_path, exist_ok=True)
        os.makedirs(os.path.dirname(archive_file) or ".", exist_ok=True)
        if ingest:
            for kind in ingest["kinds"]:
                os.makedirs(ingest["staging"][kind], exist_ok=True)

    native_output = podcastfeeds.native_path(output_path, platform,
                                             shutil.which("cygpath"))
    native_archive = podcastfeeds.native_path(archive_file, platform,
                                              shutil.which("cygpath"))
    staging_for_profile = {}
    if ingest:
        for profile, staging in ingest["forProfile"].items():
            staging_for_profile[profile] = podcastfeeds.native_path(
                staging, platform, shutil.which("cygpath"))

    manifest_dir = native_manifest_dir = status_dir = counter_file = ""
    try:
        if not options["dryRun"]:
            ramscratch.init_ram_base()
            manifest_dir, ok_one = ramscratch.ram_scratch_dir(
                "podcastManifests")
            status_dir, ok_two = ramscratch.ram_scratch_dir("podcastStatus")
            if ok_one != 0 or ok_two != 0 or not manifest_dir or not status_dir:
                sys.stderr.write("\nError: no scratch directory could be made "
                                 "for this run.\nNothing was changed.\n")
                return 1
            ramscratch.add_exit_cleanup([manifest_dir, status_dir])
            counter_file = os.path.join(status_dir, "episodeCounter")
            native_manifest_dir = podcastfeeds.native_path(
                manifest_dir, platform, shutil.which("cygpath"))
            if "%" in native_manifest_dir:
                # yt-dlp reads this path as an OUTPUT TEMPLATE, so a "%" would
                # be read as a field name and fail every call. The statistics
                # are given up rather than the run.
                log('WARNING: the scratch path holds a "%", which yt-dlp would '
                    "read as an output-template field -")
                log("         the run is unaffected, but it will not be able "
                    "to report what it downloaded.")
                native_manifest_dir = ""
            safety.init_abort_flag(os.path.join(status_dir, "abortRequested"))
        # RECORDS the interrupt and carries on, rather than leaving on it: this
        # run's summary is written by the code below, and a handler that exited
        # would take "Downloaded N file(s)" and the blocked-provider warnings
        # with it. The shell's trap here is `requestAbort` alone for the same
        # reason, where the other scripts use trapRunAbort.
        _trap_record_only()
        runlog.settle_flock()

        counters = Counters(status_dir)
        state = Run(options, ytdlp_command, platform, native_output,
                    native_archive, manifest_dir, native_manifest_dir,
                    status_dir, counter_file, counters, staging_for_profile,
                    script_dir)
        state.rows_of = rows_of

        started = time.time()
        _announce(state, tables, feed_count, ingest, native_output)

        order, lanes = _lanes(profiles)
        if len(order) == 1 or options["dryRun"]:
            # One provider, or a dry run whose printed calls should come out in
            # table order: no reason to fork.
            for lane in order:
                state.run_lane(lanes[lane], tables, profiles, jobs_of)
        else:
            log("%d provider(s) fetched alongside each other: %s"
                % (len(order), " ".join(order)))
            running = [_spawn(_lane_worker,
                              (state, lanes[lane], tables, profiles, jobs_of))
                       for lane in order]
            for job in running:
                job.join()

        return _report(state, ingest, started, feed_count)
    finally:
        ramscratch.run_exit_cleanup()


def _trap_record_only() -> None:
    """Record an interrupt for the whole run and carry on to the summary."""
    import signal

    def handler(_number, _frame):
        safety.request_abort()

    for number in safety.interrupt_signals():
        try:
            signal.signal(number, handler)
        except (OSError, ValueError):
            pass


def _lane_worker(state, lane_tables, tables, profiles, jobs_of) -> None:
    safety.trap_worker_abort()
    state.run_lane(lane_tables, tables, profiles, jobs_of)


def _announce(state, tables, feed_count, ingest, native_output) -> None:
    log("yt-dlp: %s (%s call style)" % (" ".join(state.ytdlp_command),
                                        state.platform))
    log("Output: " + native_output)
    log("Archive: " + state.native_archive)
    if ingest:
        for kind in ingest["kinds"]:
            log("Staging %s: %s (convert-audio %s)"
                % (kind, ingest["staging"][kind], ingest["args"][kind]))
        for kind in ingest["carried"]:
            log("Also converting %s, left full by an earlier run: %s"
                % (kind, ingest["staging"][kind]))
        if ingest["kinds"] or ingest["carried"]:
            log("Library built in: " + ingest["root"])
        if ingest["notIngested"]:
            log("Not converted, downloaded straight to %s: %s"
                % (native_output, " ".join(ingest["notIngested"])))
    if os.environ.get("PODCAST_DATE_AFTER"):
        log("Only uploads after " + os.environ["PODCAST_DATE_AFTER"])
    if os.environ.get("PODCAST_DATE_BEFORE"):
        log("Only uploads before " + os.environ["PODCAST_DATE_BEFORE"])
    log("%d table(s), %d feed(s) in all" % (len(tables), feed_count))


def _report(state, ingest, started, feed_count) -> int:
    """What happened, read back from the scratch rather than from variables: a
    lane and a parallel table's feeds are separate processes, and one file per
    counted thing is what makes the count the same whether the run forked."""
    options = state.options
    selected = state.counters.count("feedIndex")
    skipped_inactive = state.counters.count("skip.inactive")
    skipped_unmatched = state.counters.count("skip.unmatched")

    failed, blocked = 0, 0
    failed_feeds, blocked_providers, ingest_failures = [], [], []
    if not options["dryRun"]:
        for name in sorted(os.listdir(state.status_dir), key=os.fsencode):
            if name.endswith((".name", ".part")) or not name.isdigit():
                continue
            path = os.path.join(state.status_dir, name)
            try:
                with open(path) as handle:
                    status = handle.read().strip()
            except OSError:
                status = "1"
            if status == "0":
                continue
            if status == "blocked":
                blocked += 1
                continue
            failed += 1
            feed_name = "?"
            name_file = os.path.join(state.status_dir, name + ".name")
            if os.path.isfile(name_file):
                with open(name_file) as handle:
                    feed_name = handle.read().strip() or "?"
            failed_feeds.append(feed_name)
        for entry in sorted(os.listdir(state.status_dir), key=os.fsencode):
            if entry.startswith("blocked."):
                blocked_providers.append(entry[len("blocked."):])

    if options["dryRun"]:
        if ingest:
            # The two calls each staging folder would end up in as well: they
            # are the rest of what -i does, and a dry run that showed only the
            # downloads would answer half the question.
            for kind in ingest["kinds"] + ingest["carried"]:
                print(podcastfeeds.render_call(
                    ["clean-folder-structure", ingest["staging"][kind]],
                    state.platform))
                print(podcastfeeds.render_call(
                    ["convert-audio"]
                    + ingest["args"][kind].split()
                    + [ingest["staging"][kind], ingest["root"]],
                    state.platform))
        log("Dry run: %d call(s) printed for %s, nothing was downloaded"
            % (selected, state.platform))
        _skips(skipped_inactive, skipped_unmatched, options["match"])
        return 0

    interrupted = safety.abort_requested()
    if interrupted:
        log("Interrupted: %d feed(s) started, %d failed, the rest was not "
            "begun" % (selected, failed))
    elif blocked_providers:
        log("Stopped: %d feed(s) started, %d failed, %d cut short by a provider"
            % (selected, failed, blocked))
    else:
        log("Done: %d feed(s) fetched, %d failed" % (selected, failed))

    # What the run actually produced, which the feed count cannot say: sixty
    # feeds all up to date and sixty with a new episode take the same line above
    # and a very different amount of time and disk.
    elapsed = int(time.time() - started)
    if state.native_manifest_dir:
        files, byte_count = podcastfeeds.podcast_download_stats(
            state.manifest_dir)
        rate = ""
        if elapsed > 0 and byte_count > 0:
            rate = " at %s/s" % fmt_bytes(byte_count // elapsed)
        log("Downloaded %d file(s), %s, in %s%s"
            % (files, fmt_bytes(byte_count), fmt_hms(elapsed), rate))
    else:
        log("Ran for " + fmt_hms(elapsed))

    if options["cleanUp"]:
        _tidy_up(state)

    if ingest and not interrupted:
        ingest_failures = _build_library(state, ingest)
    elif ingest and (ingest["kinds"] or ingest["carried"]):
        # An interrupted run is a run the user stopped. Starting an hour of
        # encoding at that moment is the opposite of what was asked.
        log("Interrupted before the library copy - the downloads are in "
            "%s/Staging, -i again ingests them" % ingest["base"])

    _skips(skipped_inactive, skipped_unmatched, options["match"])

    # Said again, last, because the first time was an hour of output ago and a
    # run that stopped early has to end by saying why.
    if blocked_providers:
        sys.stderr.write("\n")
        for provider in blocked_providers:
            podcastfeeds.podcast_bot_block_warning(provider)

    if failed:
        sys.stderr.write("\n%d feed(s) failed:\n" % failed)
        for name in failed_feeds:
            sys.stderr.write("  %s\n" % name)
        return 1
    if ingest_failures:
        sys.stderr.write("\n%d step(s) of the library copy (-i) failed:\n"
                         % len(ingest_failures))
        for name in ingest_failures:
            sys.stderr.write("  %s\n" % name)
        return 1
    if interrupted:
        return safety.INTERRUPTED_EXIT_STATUS
    # A run cut short by a provider did not do what it was asked to do, so it
    # does not get to report success - a nightly cron that only looks at the
    # status would otherwise never learn that half the library stopped arriving.
    return 1 if blocked_providers else 0


def _skips(inactive: int, unmatched: int, match: str) -> None:
    if inactive > 0:
        log("Skipped %d inactive feed(s) (-a includes them)" % inactive)
    if unmatched > 0:
        log('Skipped %d feed(s) not matching "%s"' % (unmatched, match))


def _tidy_up(state) -> None:
    """Driven by the MANIFESTS, so it touches the episodes this run produced
    and nothing else in a library of forty thousand files. Deliberately after
    the figures above: those are about what came off the network, and this is
    about to remux some of it and delete files alongside."""
    files = sidecars = remuxed = 0
    if state.native_manifest_dir:
        for name in sorted(os.listdir(state.manifest_dir), key=os.fsencode):
            manifest = os.path.join(state.manifest_dir, name)
            if not os.path.isfile(manifest):
                continue
            with open(manifest, encoding="utf-8",
                      errors="surrogateescape") as handle:
                for episode in handle:
                    episode = episode.rstrip("\n").rstrip("\r")
                    if not episode:
                        continue
                    # (where it ended up, sidecars removed, WAS it remuxed) -
                    # the third is a flag, so the run's total counts the files
                    # that were remuxed rather than summing a count.
                    _where, these_sidecars, was_remuxed = \
                        downloadcleanup.clean_downloaded_file(episode)
                    files += 1
                    sidecars += these_sidecars
                    remuxed += 1 if was_remuxed else 0
    else:
        log("The tidy-up has no manifest to work from, so only the leftovers "
            "of interrupted")
        log("downloads and the empty folders are cleaned up.")

    swept = downloadcleanup.sweep_partial_downloads(state.options["outputPath"])
    pruned = downloadcleanup.prune_empty_folders(state.options["outputPath"])
    log("Tidied %d episode(s): %d remuxed into Matroska, %d sidecar(s) "
        "removed," % (files, remuxed, sidecars))
    log("        %d interrupted download(s) swept, %d empty folder(s) removed"
        % (swept, pruned))


def _build_library(state, ingest):
    """The library copy, last and after the tidy-up: with -c the sidecars are
    gone by now, so the thumbnail that was embedded is not copied into the
    library beside the episode it is already inside.

    A staging FOLDER rather than the manifests, unlike the tidy-up: the two
    scripts this delegates to consider the names of a file's siblings and skip
    the outputs that are already up to date, and handing them only this run's
    new episodes would break both.
    """
    failures, ingested = [], 0
    for kind in ingest["kinds"] + ingest["carried"]:
        if safety.abort_requested():
            break
        staging = ingest["staging"][kind]
        # Nothing downloaded into it yet - the first run of a table whose every
        # feed was up to date, not a fault.
        if not os.path.isdir(staging) or not _holds_a_file(staging):
            continue
        ingested += 1
        log("Cleaning the names in " + staging)
        if commands.run_command("clean-folder-structure", [staging],
                                script_dir=state.script_dir).returncode != 0:
            failures.append(kind + " names")
        log("Converting %s into %s (%s)" % (staging, ingest["root"],
                                            ingest["args"][kind]))
        if commands.run_command(
                "convert-audio",
                ingest["args"][kind].split() + [staging, ingest["root"]],
                script_dir=state.script_dir).returncode != 0:
            failures.append(kind + " audio")
    if failures:
        log("The library copy did not finish: " + " ".join(failures))
    elif ingested > 0:
        log("Library copy up to date in %s; the downloads are kept in %s/Staging"
            % (ingest["root"], ingest["base"]))
    return failures


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
