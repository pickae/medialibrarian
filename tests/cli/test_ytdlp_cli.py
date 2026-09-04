"""`ytdlp` as a process: the argv each feed's call ends up with, the tidy-up `-c`
does afterwards, and the SponsorBlock probe.

`yt-dlp` is a stub that records the argv it was handed, one argument per line, one
file per call. That is what makes the interesting assertions possible with no
network: the point of this wrapper is WHICH ARGUMENTS a feed's call ends up with,
and an argument list surviving intact - a folder with a space in it arriving as one
argument rather than two - is not something a dry run's printed line can prove.

The call style is pinned to `linux` rather than detected, so the assertions can
spell out the paths they expect; the translation for a Windows host is
`tests/lib/test_podcastfeeds.py`'s business.
"""

from __future__ import annotations

import os
import re
import signal
import time

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

# One episode per call in the folder the output template names, sized so the byte
# total is assertable; the finished path on stdout behind the marker, the way
# yt-dlp answers `--print`; noise that quiet mode has to swallow; and a failure
# for one URL on purpose, so "one feed's failure is not the run's" can be checked.
_STUB = r"""
n=$(( $(ls "$CALL_LOG" | wc -l) + 1 ))
printf '%s\n' "$@" > "$CALL_LOG/call$n"
marker=""; outTemplate=""; prev=""
for a in "$@"; do
    [[ "$prev" == "--print" ]] && marker="${a#after_move:}"
    [[ "$prev" == "-o" ]] && outTemplate="$a"
    prev="$a"
done
marker="${marker%\%(filepath)s}"
for a in "$@"; do [[ "$a" == *"/fails" ]] && exit 1; done
if [[ -n "$marker" && -n "$outTemplate" ]]; then
    episode="${outTemplate%/*}/episode$n.opus"
    mkdir -p "${episode%/*}"
    printf '%*s' "$((100 * n))" '' > "$episode"
    printf '%s%s\n' "$marker" "$episode"
fi
printf '[download] noise that only -v should show\n'
exit 0
"""

# The wall-clock window each call was inside, so overlap is a fact rather than a
# hope.
_STUB_OVERLAP = r"""
marker=""; outTemplate=""; prev=""
for a in "$@"; do
    [[ "$prev" == "--print" ]] && marker="${a#after_move:}"
    [[ "$prev" == "-o" ]] && outTemplate="$a"
    prev="$a"
done
marker="${marker%\%(filepath)s}"
label="${outTemplate#"$LIBRARY/"}"; label="${label%%/*}"
printf '%s start %s\n' "$label" "$(date +%s%N)" >> "$OVERLAP_LOG"
sleep 1
printf '%s end %s\n' "$label" "$(date +%s%N)" >> "$OVERLAP_LOG"
episode="${outTemplate%/*}/e.opus"
mkdir -p "${episode%/*}"; printf 'x' > "$episode"
printf '%s%s\n' "$marker" "$episode"
exit 0
"""

_STUB_SLOW = r"""
marker=""; outTemplate=""; prev=""
for a in "$@"; do
    [[ "$prev" == "--print" ]] && marker="${a#after_move:}"
    [[ "$prev" == "-o" ]] && outTemplate="$a"
    prev="$a"
done
marker="${marker%\%(filepath)s}"
episode="${outTemplate%/*}/e.opus"
mkdir -p "${episode%/*}"; printf 'x' > "$episode"
printf '%s%s\n' "$marker" "$episode"
sleep 1
exit 0
"""

# The real bot-block message, curly apostrophe and plugin-tagged extractor
# included, then the refusals that would follow for every remaining episode -
# because -i, which every feed needs so one dead episode does not end a feed,
# makes yt-dlp walk into the refusal once per episode instead of stopping.
_STUB_BLOCKED = r"""
marker=""; outTemplate=""; prev=""
for a in "$@"; do
    [[ "$prev" == "--print" ]] && marker="${a#after_move:}"
    [[ "$prev" == "-o" ]] && outTemplate="$a"
    prev="$a"
done
marker="${marker%\%(filepath)s}"
label="${outTemplate%/*}"
printf '%s\n' "${label##*/}" >> "$RAN_LOG"
case "$label" in
*blocked*)
    printf 'ERROR: [youtube+GetPOT] a: Sign in to confirm you\xe2\x80\x99re not a bot.'  >&2
    printf ' Use --cookies-from-browser or --cookies for the authentication.\n' >&2
    for i in 1 2 3 4 5 6 7 8; do
        printf 'ERROR: [youtube] x%s: Sign in to confirm you\xe2\x80\x99re not a bot.\n' "$i" >&2
        sleep 0.2
    done
    ;;
*)
    episode="$label/e.opus"
    mkdir -p "$label"; printf 'x' > "$episode"
    printf '%s%s\n' "$marker" "$episode"
    ;;
esac
exit 0
"""

# Exactly what yt-dlp leaves beside an episode: the thumbnail it embedded, the
# description and metadata sidecar the video profile asks for, a converted
# subtitle carrying its language between the stem and the extension, and the
# .part of a fragment it never finished. The episode's extension comes from the
# format the call asks for, so one stub serves an audio and a video table.
_STUB_LEFTOVERS = r"""
n=$(( $(ls "$CALL_LOG" | wc -l) + 1 ))
printf '%s\n' "$@" > "$CALL_LOG/call$n"
marker=""; outTemplate=""; video=0; prev=""
for a in "$@"; do
    [[ "$prev" == "--print" ]] && marker="${a#after_move:}"
    [[ "$prev" == "-o" ]] && outTemplate="$a"
    [[ "$a" == "--merge-output-format" ]] && video=1
    prev="$a"
done
marker="${marker%\%(filepath)s}"
folder="${outTemplate%/*}"
mkdir -p "$folder"
if (( video )); then episode="$folder/episode$n.MP4"
else episode="$folder/episode$n.opus"; fi
stem="${episode%.*}"
printf '%*s' 100 '' > "$episode"
printf 'thumbnail\n'   > "$stem.jpg"
printf 'description\n' > "$stem.description"
printf '{"id":"x"}\n'  > "$stem.info.json"
printf 'subtitle\n'    > "$stem.en.srt"
printf 'half a fragment\n' > "$folder/episode$n.f251.part"
printf '%s%s\n' "$marker" "$episode"
exit 0
"""

# `--help` is the probe's question, and answering it must not leave a call behind:
# the counts below are one per feed. SB_HELP=0 names no flags, 1 names them in a
# page that fits in a pipe, "big" in one that does not.
# A yt-dlp whose version is a file the "pipx" beside it rewrites: that is what
# makes "the run upgraded it" an observable fact rather than a claim about the
# network. --version and --help answer before the call log, so the two probes
# do not count as feed calls.
_STUB_NIGHTLY = r"""
if [[ "${1:-}" == "--version" ]]; then cat "$VERSION_FILE"; exit 0; fi
if [[ "${1:-}" == "--help" ]]; then printf '  --sponsorblock-remove CATEGORIES\n'; exit 0; fi
""" + _STUB

_STUB_PIPX = r"""
printf '%s\n' "$@" > "$PIPX_LOG"
[[ "${PIPX_FAILS:-0}" != 0 ]] && { printf 'could not reach the index\n'; exit 1; }
printf '%s\n' "$NEW_VERSION" > "$VERSION_FILE"
printf 'upgraded package yt-dlp-nightly\n'
exit 0
"""

_STUB_SPONSORBLOCK = r"""
if [[ "${1:-}" == "--help" ]]; then
    if [[ "${SB_HELP:-0}" != 0 ]]; then
        printf '  --sponsorblock-chapter-title TEXT\n'
        printf '  --sponsorblock-mark MARKS\n'
        printf '  --sponsorblock-remove CATEGORIES\n'
        if [[ "${SB_HELP}" == big ]]; then
            for i in $(seq 1 20000); do
                printf '  --option-%s VALUE  what option %s does\n' "$i" "$i"
            done
        fi
    fi
    exit 0
fi
n=$(( $(ls "$CALL_LOG" | wc -l) + 1 ))
printf '%s\n' "$@" > "$CALL_LOG/call$n"
marker=""; outTemplate=""; prev=""
for a in "$@"; do
    [[ "$prev" == "--print" ]] && marker="${a#after_move:}"
    [[ "$prev" == "-o" ]] && outTemplate="$a"
    prev="$a"
done
marker="${marker%\%(filepath)s}"
episode="${outTemplate%/*}/episode$n.opus"
mkdir -p "${episode%/*}"
printf '%*s' 100 '' > "$episode"
printf '%s%s\n' "$marker" "$episode"
exit 0
"""

_MKVMERGE = r"""
target=""; prev=""
for a in "$@"; do [[ "$prev" == "-o" ]] && target="$a"; prev="$a"; done
[[ -n "$target" ]] && printf 'matroska\n' > "$target"
printf '%s\n' "$*" >> "$MKV_LOG"
exit 0
"""

_MKVPROPEDIT = r"""
printf '%s\n' "$*" >> "$ATTACH_LOG"
exit 0
"""

_TABLE = (
    "# a comment\n\n"
    "1\tAI/latent space\t\t40\t\thttps://example.test/latent\n"
    "0\tMisc/paused\t\t\t\thttps://example.test/paused\n"
    "1\tMisc/Joe Rogan\t%(title)s.%(ext)s\t\t\thttps://example.test/rogan\n"
    "1\tMisc/broken\t\t\t\thttps://example.test/fails\n"
)


def _table(path, *rows, profile=""):
    text = ("#!profile %s\n" % profile) if profile else ""
    path.write_text(text + "".join(rows))
    return path


def _feed(active, folder, url, template="", days=""):
    return "%s\t%s\t%s\t%s\t\t%s\n" % (active, folder, template, days, url)


@pytest.fixture
def ytdlp(sandbox, tmp_path):
    """The stub, its call log, and a run pinned to the linux call style."""
    calls = tmp_path / "calls"
    calls.mkdir()
    sandbox.with_tool("ffmpeg", ":")
    sandbox.with_tool("yt-dlp", _STUB)

    environment = dict(os.environ, CALL_LOG=str(calls), YTDLP="yt-dlp")

    def run(*args, expect=0, env=None, **kwargs):
        # sandbox.env read now, not captured: a fixture that adds to it - the
        # tidy-up adds two log paths - would otherwise be handing them to nobody.
        done = sandbox.run("ytdlp", "-s", "linux", *args,
                           env=env or sandbox.env, **kwargs)
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    def argv(number=1):
        return (calls / ("call%d" % number)).read_text().splitlines()

    def clear():
        for call in calls.glob("call*"):
            call.unlink()

    sandbox.calls = calls
    sandbox.env = environment
    sandbox.ytdlp = run
    sandbox.argv = argv
    sandbox.clear = clear
    sandbox.table = _table(tmp_path / "podcasts.tsv", _TABLE)
    sandbox.library = tmp_path / "library"
    return sandbox


def _option(argv, flag):
    """The value that follows ``flag`` in a recorded argv, or None."""
    for index, argument in enumerate(argv[:-1]):
        if argument == flag:
            return argv[index + 1]
    return None


class TestARunOverATable:
    """One call per ACTIVE feed, and one feed's failure is not the run's."""

    @pytest.fixture
    def run(self, ytdlp):
        log = ytdlp.ytdlp("-t", ytdlp.table, ytdlp.library,
                          ytdlp.library / "archive.log", expect=1)
        return ytdlp, log

    def test_a_run_whose_feed_failed_exits_non_zero(self, run):
        ytdlp, log = run
        assert "Misc/broken" in log, log

    def test_one_call_per_active_feed_and_the_paused_one_is_reported(self, run):
        ytdlp, log = run
        assert len(list(ytdlp.calls.glob("call*"))) == 3
        assert "Skipped 1 inactive feed" in log, log

    def test_the_closing_count_is_of_episodes_and_not_of_feeds(self, run):
        """Two feeds produced an episode and the third failed, so it is two files
        and their exact size - not the three feeds it walked."""
        _, log = run
        found = re.search(
            r"Downloaded (\d+) file\(s\), ([^,]+), in (\d+:\d{2}:\d{2})", log)
        assert found, log
        assert found.group(1) == "2"
        assert found.group(2) == "300 B"

    def test_an_episode_is_one_numbered_line(self, run):
        """Count, outcome, podcast, episode, size."""
        _, log = run
        assert re.search(
            r"\[\s*1\]\s+ok\s+AI/latent space \| episode1\.opus \| 100 B", log), \
            log

    def test_the_numbering_runs_across_the_session_and_not_per_feed(self, run):
        _, log = run
        assert re.search(r"\[\s*2\]\s+ok\s+Misc/Joe Rogan", log), log

    def test_the_downloaders_own_output_is_silenced(self, run):
        _, log = run
        assert "noise that only -v should show" not in log

    def test_the_manifests_are_not_left_in_the_library(self, run):
        """They are the run's own bookkeeping, not something to leave beside the
        episodes."""
        ytdlp, _ = run
        assert list(ytdlp.library.rglob("podcastManifest*")) == []

    def test_verbose_puts_the_downloaders_output_back(self, ytdlp):
        log = ytdlp.ytdlp("-v", "-t", ytdlp.table, "-m", "latent",
                          ytdlp.library, ytdlp.library / "archive.log")
        assert "noise that only -v should show" in log


class TestTheArgvAFeedIsCalledWith:
    """Argument by argument, because a folder with a space in it has to arrive as
    ONE argument - which is the thing a hand-written command line gets wrong."""

    @pytest.fixture
    def run(self, ytdlp):
        ytdlp.ytdlp("-t", ytdlp.table, ytdlp.library,
                    ytdlp.library / "archive.log", expect=1)
        return ytdlp

    def test_the_output_template_arrives_as_one_argument(self, run):
        assert _option(run.argv(1), "-o") == str(
            run.library / "AI/latent space/%(upload_date)s %(title)s.%(ext)s")

    def test_the_url_is_the_last_argument(self, run):
        assert run.argv(1)[-1] == "https://example.test/latent"

    def test_the_archive_file_is_passed_through(self, run):
        assert "--download-archive" in run.argv(1)

    def test_no_date_argument_means_no_date_filter(self, run):
        assert "--dateafter" not in run.argv(1)

    def test_the_output_root_is_created(self, run):
        assert run.library.is_dir()

    def test_a_feeds_own_name_template_is_not_the_default(self, run):
        assert _option(run.argv(2), "-o") == str(
            run.library / "Misc/Joe Rogan/%(title)s.%(ext)s")


class TestTheDateRange:
    def test_a_range_becomes_dateafter_and_datebefore(self, ytdlp):
        ytdlp.ytdlp("-t", ytdlp.table, "-m", "latent", ytdlp.library,
                    ytdlp.library / "archive.log", "20260607..20260707")
        argv = ytdlp.argv(1)
        assert _option(argv, "--dateafter") == "20260607"
        assert _option(argv, "--datebefore") == "20260707"

    def test_a_date_that_is_not_one_is_refused_before_anything_runs(self, ytdlp):
        ytdlp.ytdlp("-t", ytdlp.table, ytdlp.library,
                    ytdlp.library / "archive.log", "last tuesday", expect=2)
        assert list(ytdlp.calls.glob("call*")) == []


class TestTheFlags:
    def test_all_fetches_the_paused_feed_too(self, ytdlp):
        ytdlp.ytdlp("-t", ytdlp.table, "-a", "-m", "paused", ytdlp.library,
                    ytdlp.library / "archive.log")
        assert len(list(ytdlp.calls.glob("call*"))) == 1

    def test_a_dry_run_runs_nothing_and_prints_the_whole_call(self, ytdlp):
        log = ytdlp.ytdlp("-t", ytdlp.table, "-n", ytdlp.library,
                          ytdlp.library / "archive.log")
        assert list(ytdlp.calls.glob("call*")) == []
        assert "--sponsorblock-remove all" in log

    @pytest.mark.parametrize("system", ["windows", "linux"])
    def test_a_dry_run_renders_either_systems_call_from_one_table(self, ytdlp,
                                                                 system):
        """Which is what makes one table serve both machines, so it is checked
        rather than assumed."""
        log = ytdlp.ytdlp("-t", ytdlp.table, "-n", "-s", system, "-m", "latent",
                          ytdlp.library, ytdlp.library / "archive.log")
        assert re.search(r"'[^']*AI/latent space[^']*'", log), log

    def test_a_missing_table_is_refused(self, ytdlp, tmp_path):
        ytdlp.ytdlp("-t", tmp_path / "nope.tsv", ytdlp.library,
                    ytdlp.library / "archive.log", expect=2)


class TestSeveralTablesAndWhatMayRunAlongsideWhat:
    """Each table brings its own profile and its own width, and the tables of ONE
    provider queue behind each other while different providers run together."""

    @pytest.fixture
    def run(self, ytdlp, tmp_path):
        ytdlp.with_tool("yt-dlp", _STUB_OVERLAP)
        library = tmp_path / "library3"
        overlaps = tmp_path / "overlap.log"
        overlaps.write_text("")
        tables = [
            _table(tmp_path / "yt1.tsv", _feed(1, "YT1/a",
                                               "https://example.test/y1")),
            _table(tmp_path / "yt2.tsv", _feed(1, "YT2/a",
                                               "https://example.test/y2")),
            _table(tmp_path / "rss.tsv", _feed(1, "RSS/a",
                                               "https://example.test/r1"),
                   profile="rssAudio"),
        ]
        arguments = []
        for table in tables:
            arguments += ["-t", table]
        log = ytdlp.ytdlp(*arguments, library, library / "archive.log",
                          env=dict(ytdlp.env, OVERLAP_LOG=str(overlaps),
                                   LIBRARY=str(library)))
        windows = {}
        for line in overlaps.read_text().splitlines():
            label, edge, stamp = line.split()
            windows.setdefault(label, {})[edge] = int(stamp)
        return log, windows

    def test_two_tables_of_one_provider_never_run_at_the_same_time(self, run):
        _, windows = run
        assert not _overlap(windows, "YT1", "YT2")

    def test_a_table_of_another_provider_does(self, run):
        _, windows = run
        assert _overlap(windows, "YT1", "RSS")

    def test_each_table_is_announced_with_its_own_profile(self, run):
        log, _ = run
        assert "profile rssAudio" in log
        assert "profile youtubeAudio" in log

    def test_the_width_option_lowers_how_wide_a_table_runs(self, ytdlp,
                                                           tmp_path):
        table = _table(tmp_path / "rssj.tsv",
                       _feed(1, "RSS/a", "https://example.test/r1"),
                       profile="rssAudio")
        library = tmp_path / "library4"
        log = ytdlp.ytdlp("-n", "-j", "3", "-t", table, library,
                          library / "archive.log")
        assert "profile rssAudio, 3 at a time" in log, log


def _overlap(windows, one, other):
    return (windows[one]["start"] < windows[other]["end"]
            and windows[other]["start"] < windows[one]["end"])


class TestStoppingTheRun:
    """What is asserted is the part that is this command's own doing: after the
    signal, the feeds it had not begun are NOT begun, it still reports what it
    managed, and it exits with the status that says a signal ended it rather than
    a fault."""

    @pytest.fixture
    def stopped(self, ytdlp, tmp_path):
        ytdlp.with_tool("yt-dlp", _STUB_SLOW)
        table = _table(tmp_path / "many.tsv",
                       *[_feed(1, "Slow/feed%d" % n,
                               "https://example.test/%d" % n)
                         for n in range(1, 9)])
        library = tmp_path / "library5"
        started = blackbox.start("ytdlp", "-s", "linux", "-t", table, library,
                                 library / "archive.log", cwd=ytdlp.work,
                                 path=ytdlp.path, env=ytdlp.env)
        time.sleep(3)
        started.send_signal(signal.SIGTERM)
        log, _ = started.communicate(timeout=180)
        return started.returncode, log

    def test_it_exits_with_the_signals_status(self, stopped):
        status, log = stopped
        assert status == 130, log

    def test_it_says_so_instead_of_claiming_it_finished(self, stopped):
        _, log = stopped
        assert "Interrupted" in log, log
        assert "Done:" not in log, log

    def test_it_still_reports_what_it_managed(self, stopped):
        _, log = stopped
        assert "Downloaded" in log, log

    def test_the_feeds_it_had_not_begun_are_not_begun(self, stopped):
        _, log = stopped
        started = len(re.findall(r"\] ok ", log))
        assert 0 < started < 8, log


class TestAProviderRefusingTheRun:
    """The whole reason this wrapper watches its downloader's output: yt-dlp has
    no bot-block-specific stop, and `-i` - which every feed needs so one dead
    episode does not end a feed - makes it walk into the refusal once per
    remaining episode.

    The block has to stop the provider it came from and NOTHING else: the feeds
    queued behind it, and the TABLES queued behind it, are not started, while a
    table of other providers finishes normally.
    """

    @pytest.fixture
    def run(self, ytdlp, tmp_path):
        ytdlp.with_tool("yt-dlp", _STUB_BLOCKED)
        ran = tmp_path / "ran.log"
        ran.write_text("")
        first = _table(tmp_path / "b1.tsv",
                       _feed(1, "yt-first", "https://example.test/1"),
                       _feed(1, "yt-blocked", "https://example.test/2"),
                       _feed(1, "yt-behind", "https://example.test/3"))
        second = _table(tmp_path / "b2.tsv",
                        _feed(1, "yt-nexttable", "https://example.test/4"))
        third = _table(tmp_path / "b3.tsv",
                       _feed(1, "rss-unaffected", "https://example.test/r"),
                       profile="rssAudio")
        library = tmp_path / "library6"
        log = ytdlp.ytdlp("-t", first, "-t", second, "-t", third, library,
                          library / "archive.log", expect=1,
                          env=dict(ytdlp.env, RAN_LOG=str(ran)))
        return log, ran.read_text().split()

    @pytest.mark.parametrize("feed", ["yt-first", "yt-blocked",
                                      "rss-unaffected"])
    def test_what_is_reached_before_or_despite_the_block(self, run, feed):
        _, ran = run
        assert feed in ran, ran

    @pytest.mark.parametrize("feed", ["yt-behind", "yt-nexttable"])
    def test_what_is_not_started_after_the_block(self, run, feed):
        _, ran = run
        assert feed not in ran, ran

    def test_the_blocked_feed_is_reported_as_stopped_and_not_as_failed(
            self, run):
        log, _ = run
        assert "STOP" in log, log
        assert "feed(s) failed" not in log, log

    def test_the_block_is_warned_about_where_it_happens_and_in_the_summary(
            self, run):
        log, _ = run
        assert len(re.findall("not a bot", log)) >= 2, log


class TestTheTidyUp:
    """`-c` is told which files the run produced, and matches leftovers on the
    episode's STEM rather than on their extension. That is what keeps a folder's
    cover art - which shares its name with no episode - and it is the whole
    difference from deleting every image in the folder."""

    @pytest.fixture
    def tidy(self, ytdlp, tmp_path):
        ytdlp.with_tool("yt-dlp", _STUB_LEFTOVERS)
        ytdlp.with_tool("mkvmerge", _MKVMERGE)
        ytdlp.with_tool("mkvpropedit", _MKVPROPEDIT)
        ytdlp.mkv_log = tmp_path / "mkvmerge.log"
        ytdlp.attach_log = tmp_path / "attach.log"
        ytdlp.mkv_log.write_text("")
        ytdlp.attach_log.write_text("")
        ytdlp.env = dict(ytdlp.env, MKV_LOG=str(ytdlp.mkv_log),
                         ATTACH_LOG=str(ytdlp.attach_log))
        ytdlp.audio_table = _table(
            tmp_path / "audio.tsv",
            _feed(1, "AI/latent space", "https://example.test/latent",
                  days="40"),
            profile="youtubeAudio")
        ytdlp.video_table = _table(
            tmp_path / "video.tsv",
            _feed(1, "Serien/Broute", "https://example.test/broute", days="40"),
            profile="youtubeVideo")
        return ytdlp

    def test_without_it_nothing_is_touched(self, tidy, tmp_path):
        """The tidy-up is a decision, not a default: a library someone else's
        tooling also reads is left exactly as the downloader wrote it."""
        library = tmp_path / "untidied"
        tidy.ytdlp("-t", tidy.audio_table, library, library / "archive.log")
        folder = library / "AI" / "latent space"
        assert (folder / "episode1.opus").is_file()
        assert (folder / "episode1.jpg").is_file()
        assert (folder / "episode1.f251.part").is_file()

    @pytest.fixture
    def tidied(self, tidy, tmp_path):
        library = tmp_path / "audio"
        folder = library / "AI" / "latent space"
        folder.mkdir(parents=True)
        # A cover belonging to the folder rather than to any episode - what the
        # blanket delete took with it - and an empty folder left by a feed that
        # has gone away, which it removed correctly.
        (folder / "folder.jpg").write_text("cover\n")
        (library / "Misc" / "gone away").mkdir(parents=True)
        tidy.clear()
        log = tidy.ytdlp("-c", "-t", tidy.audio_table, library,
                         library / "archive.log")
        return library, folder, log

    def test_the_episode_survives_and_every_leftover_goes(self, tidied):
        _, folder, _ = tidied
        assert (folder / "episode1.opus").is_file()
        for leftover in ("episode1.jpg", "episode1.description",
                         "episode1.info.json", "episode1.en.srt",
                         "episode1.f251.part"):
            assert not (folder / leftover).exists(), leftover

    def test_the_folders_own_cover_is_not_a_leftover(self, tidied):
        _, folder, _ = tidied
        assert (folder / "folder.jpg").is_file()

    def test_an_empty_folder_is_pruned_depth_first_and_the_root_kept(
            self, tidied):
        library, _, _ = tidied
        assert not (library / "Misc" / "gone away").exists()
        assert not (library / "Misc").exists()
        assert library.is_dir()

    def test_the_run_says_what_it_tidied(self, tidied):
        _, _, log = tidied
        assert "Tidied 1 episode(s)" in log, log

    @pytest.fixture
    def video(self, tidy, tmp_path):
        library = tmp_path / "video"
        tidy.clear()
        tidy.ytdlp("-c", "-t", tidy.video_table, library,
                   library / "archive.log")
        return tidy, library / "Serien" / "Broute"

    def test_a_video_becomes_a_matroska(self, video):
        _, folder = video
        assert (folder / "episode1.mkv").is_file()
        assert not (folder / "episode1.MP4").exists()
        assert not (folder / "episode1.mp4").exists()
        assert not (folder / "episode1.description").exists()

    @pytest.mark.parametrize("sidecar", ["episode1.description",
                                         "episode1.info.json"])
    def test_what_disappears_with_the_video_is_attached_into_it(self, video,
                                                                sidecar):
        tidy, _ = video
        attachments = tidy.attach_log.read_text()
        assert "--add-attachment" in attachments, attachments
        assert sidecar in attachments, attachments


class TestTheNightlyUpgrade:
    """A nightly is brought up to date before the feeds are fetched, because the
    extractor fix that is the whole reason for running one lands daily. A
    release is left alone - nobody asked for the version to move under them -
    and an upgrade that cannot happen is a warning, never the end of the run.

    The stub's version is a file, and the "pipx" beside it rewrites that file:
    an upgrade is then something the run can be shown to have done. These cases
    leave the suite-wide preflight switch OFF.
    """

    @pytest.fixture
    def nightly(self, ytdlp, tmp_path):
        # The realpath of the command has to land inside a pipx venv, because
        # that is what tells the run pipx owns this install.
        ytdlp.with_tool("yt-dlp", _STUB_NIGHTLY)
        venv = tmp_path / "pipx" / "venvs" / "yt-dlp-nightly" / "bin"
        venv.mkdir(parents=True)
        real = venv / "yt-dlp"
        real.write_text((ytdlp.bin / "yt-dlp").read_text(), encoding="ascii")
        real.chmod(0o755)
        (ytdlp.bin / "yt-dlp").unlink()
        (ytdlp.bin / "yt-dlp").symlink_to(real)
        ytdlp.with_tool("pipx", _STUB_PIPX)
        version = tmp_path / "version"
        pipx_log = tmp_path / "pipx-argv"
        table = _table(tmp_path / "n.tsv",
                       _feed(1, "AI/latent space",
                             "https://example.test/latent", days="40"))

        def run(installed, offered, *args, **extra):
            version.write_text(installed + "\n")
            library = tmp_path / ("library-%s%s" % (installed, "".join(args)))
            log = ytdlp.ytdlp(*args, "-t", table, library,
                              library / "archive.log",
                              env=dict(ytdlp.env, VERSION_FILE=str(version),
                                       NEW_VERSION=offered,
                                       PIPX_LOG=str(pipx_log),
                                       SKIP_TOOL_PREFLIGHT="", **extra))
            argv = (pipx_log.read_text().splitlines()
                    if pipx_log.exists() else None)
            pipx_log.unlink(missing_ok=True)
            return log, argv, library

        return run

    def test_a_newer_nightly_is_taken_before_the_feeds_are(self, nightly):
        log, argv, library = nightly("2026.08.30.232658", "2026.08.31.010203")
        assert "2026.08.30.232658 is a nightly" in log, log
        assert "upgraded: 2026.08.30.232658 -> 2026.08.31.010203" in log, log
        # --pre spelled out: pipx does not remember it, and without it the
        # first release to outrank the nightly walks the install back
        assert argv == ["upgrade", "--pip-args=--pre", "yt-dlp-nightly"]
        # and the run went on to do what it was called for
        assert len(list(library.rglob("episode*.opus"))) == 1

    def test_a_nightly_that_is_current_is_checked_and_left(self, nightly):
        log, argv, library = nightly("2026.08.30.232658", "2026.08.30.232658")
        assert "is a nightly" in log, log
        assert "upgraded" not in log, log
        assert argv == ["upgrade", "--pip-args=--pre", "yt-dlp-nightly"]
        assert len(list(library.rglob("episode*.opus"))) == 1

    def test_a_release_install_is_not_touched(self, nightly):
        log, argv, library = nightly("2026.06.09", "2026.08.31.010203")
        assert "is a nightly" not in log, log
        assert argv is None
        assert len(list(library.rglob("episode*.opus"))) == 1

    def test_an_upgrade_that_fails_warns_and_the_run_goes_on(self, nightly):
        log, _, library = nightly("2026.08.30.232658", "2026.08.31.010203",
                                  PIPX_FAILS="1")
        assert "the nightly upgrade failed" in log, log
        assert "could not reach the index" in log, log
        assert len(list(library.rglob("episode*.opus"))) == 1

    def test_the_check_can_be_switched_off(self, nightly):
        log, argv, library = nightly("2026.08.30.232658", "2026.08.31.010203",
                                     SKIP_YTDLP_UPGRADE="1")
        assert "is a nightly" not in log, log
        assert argv is None
        assert len(list(library.rglob("episode*.opus"))) == 1

    def test_a_dry_run_leaves_the_install_where_it_found_it(self, nightly):
        # -n prints the calls a run WOULD make; it must not move the install
        # that would have made them.
        log, argv, _ = nightly("2026.08.30.232658", "2026.08.31.010203", "-n")
        assert "is a nightly" not in log, log
        assert argv is None


class TestTheSponsorBlockProbe:
    """The YouTube profiles pass `--sponsorblock-remove`, which an older yt-dlp
    reads as an unknown option and fails the whole download over - not just the
    segment cutting. So the resolved command is probed at startup and, when the
    flags are not there, they are dropped - flag and value - from every feed's
    argv, and the episodes download with their sponsor segments intact instead of
    the run dying on its first feed.

    The probe is what is under test and it is answered by the stub's `--help`, so
    these runs leave the suite-wide preflight switch OFF: the stubs satisfy the
    preflight the same way they satisfy the runs.
    """

    @pytest.fixture
    def probe(self, ytdlp, tmp_path):
        ytdlp.with_tool("yt-dlp", _STUB_SPONSORBLOCK)
        table = _table(tmp_path / "sb.tsv",
                       _feed(1, "AI/latent space",
                             "https://example.test/latent", days="40"))

        def run(answer):
            ytdlp.clear()
            library = tmp_path / ("library-%s" % answer)
            log = ytdlp.ytdlp("-t", table, library, library / "archive.log",
                              env=dict(ytdlp.env, SB_HELP=str(answer),
                                       SKIP_TOOL_PREFLIGHT=""))
            return log, ytdlp.argv(1), library

        return run

    def test_without_the_flags_the_download_goes_on_and_they_come_out(
            self, probe):
        log, argv, library = probe(0)
        assert len(list(library.rglob("episode*.opus"))) == 1
        assert len(re.findall("no SponsorBlock support", log)) == 1, log
        assert "keep their sponsor" in log, log
        assert "--sponsorblock-remove" not in argv
        # The value went out with its flag rather than being orphaned behind it.
        assert "all" not in argv

    def test_the_rest_of_the_profile_is_untouched(self, probe):
        _, argv, _ = probe(0)
        assert "-x" in argv
        assert "251/140/bestaudio/best" in argv

    def test_with_the_flags_nothing_changes(self, probe):
        log, argv, _ = probe(1)
        assert "no SponsorBlock support" not in log, log
        assert _option(argv, "--sponsorblock-remove") == "all"

    def test_a_help_page_too_big_for_a_pipe_still_reads_as_support(self, probe):
        """Piped into a reader that stops at its first match, the writes still to
        come hit a closed pipe - and a probe that took that death for its answer
        read a yt-dlp that HAS the flags as one that has not. A page larger than
        the pipe's 64 KiB meets that every run rather than only when the
        scheduler interleaves it that way."""
        log, argv, _ = probe("big")
        assert "no SponsorBlock support" not in log, log
        assert "--sponsorblock-remove" in argv
