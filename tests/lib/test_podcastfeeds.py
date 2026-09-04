"""The white box for medialib/lib/podcastfeeds.py.

The feed table and the yt-dlp call it builds. What is pinned here: the exact argv
each profile assembles, the fault report a malformed table earns line for line,
the counter read-increment behaviour, the two quoting styles a rendered call can
take, and that every table in the checkout parses.
"""

import glob
import io
import os

import pytest

from medialib import commands
from medialib.lib import podcastfeeds as pf
from tests import blackbox

pytestmark = pytest.mark.stubbed

VALID_ROW = "1\tAI/latent space\t\t40\t\thttps://example.test/latent"


# --- splitTabs / splitPodcastRow ---------------------------------------------

def test_split_tabs_keeps_every_column():
    fields = pf.split_tabs("1\tAI/x\t\t\t\thttps://example.test/a")
    assert fields == ["1", "AI/x", "", "", "", "https://example.test/a"]


def test_split_tabs_keeps_a_trailing_empty_column():
    assert pf.split_tabs("a\tb\t") == ["a", "b", ""]


def test_split_tabs_keeps_a_leading_empty_column():
    assert pf.split_tabs("\ta") == ["", "a"]


def test_split_tabs_without_a_separator():
    assert pf.split_tabs("one") == ["one"]


def test_split_podcast_row_pads_a_short_row():
    assert pf.split_podcast_row("1\tAI/x\thttps://u") == \
        ("1", "AI/x", "https://u", "", "", "")


def test_split_podcast_row_of_a_full_row():
    assert pf.split_podcast_row(VALID_ROW) == \
        ("1", "AI/latent space", "", "40", "", "https://example.test/latent")


# --- profiles ----------------------------------------------------------------

def test_youtube_audio_still_asks_for_everything_the_old_scripts_did():
    args = pf.podcast_profile_args("youtubeAudio")
    assert args == [
        "--sleep-interval", "2",
        "--sleep-request", "1",
        "--max-sleep-interval", "5",
        "--concurrent-fragments", "3",
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
        "--sponsorblock-remove", "all",
        "-x",
        "-f", "251/140/bestaudio/best",
        "--extractor-args", "youtube:player-client=android",
    ]


def test_every_listed_profile_builds_an_argument_list():
    for profile in pf.PODCAST_PROFILES:
        assert pf.podcast_profile_args(profile) is not None, profile


def test_an_unknown_profile_is_refused():
    assert pf.podcast_profile_args("notAProfile") is None
    assert pf.podcast_profile_args("") is None


def test_every_youtube_profile_asks_as_the_android_client_and_asks_last():
    for profile in pf.PODCAST_PROFILES:
        if not profile.startswith("youtube"):
            continue
        args = pf.podcast_profile_args(profile)
        assert args[-2:] == ["--extractor-args", "youtube:player-client=android"]


def test_rss_names_no_youtube_client_and_no_sponsorblock():
    args = pf.podcast_profile_args("rssAudio")
    assert "player-client" not in " ".join(args)
    assert not any("sponsorblock" in arg for arg in args)
    assert "-f" in args and "bestaudio/best" in args


def test_youtube_video_marks_rather_than_cuts():
    args = pf.podcast_profile_args("youtubeVideo")
    assert "--sponsorblock-mark" in args and "--sponsorblock-remove" not in args
    assert "-x" not in args and "--merge-output-format" in args


def test_the_media_profiles_ask_for_their_own_format_preference():
    audio = pf.podcast_profile_args("youtubeAudio")
    site = pf.podcast_profile_args("siteVideo")
    assert "251/140/bestaudio/best" in audio
    assert not any("sponsorblock" in arg for arg in site)
    assert "-f" not in site
    assert "--windows-filenames" in site and "--no-overwrites" in site


def test_the_provider_decides_which_tables_may_run_together():
    assert pf.podcast_profile_provider("youtubeAudio") == "youtube"
    assert pf.podcast_profile_provider("youtubeVideo") == "youtube"
    assert pf.podcast_profile_provider("rssAudio") == "rss"
    assert pf.podcast_profile_provider("siteVideo") == "site"
    assert pf.podcast_profile_provider("otherThing") == "other"


def test_the_media_is_derived_from_the_profile():
    assert pf.podcast_profile_media("youtubeVideo") == "video"
    assert pf.podcast_profile_media("rssVideo") == "video"
    assert pf.podcast_profile_media("youtubeAudio") == "audio"
    assert pf.podcast_profile_media("siteVideo") == "video"


def test_every_profile_is_one_of_the_two():
    """What the answer feeds is a two-way branch, so a third value is a profile
    nothing knows how to finish."""
    for profile in pf.PODCAST_PROFILES:
        assert pf.podcast_profile_media(profile) in ("audio", "video"), profile


def test_the_ingest_answer_is_keyed_on_the_conversion():
    assert pf.podcast_profile_ingest("rssAudio") == "Speech -c -m"
    assert pf.podcast_profile_ingest("youtubeAudio") == "Music -c -b 65"


def test_a_video_profile_is_not_ingested():
    for profile in pf.PODCAST_PROFILES:
        if pf.podcast_profile_media(profile) == "video":
            assert pf.podcast_profile_ingest(profile) is None, profile


def test_rss_runs_wide_and_everything_else_one_at_a_time():
    assert pf.podcast_profile_jobs("rssAudio") == "10"
    assert pf.podcast_profile_jobs("rssVideo") == "10"
    assert pf.podcast_profile_jobs("youtubeAudio") == "1"
    assert pf.podcast_profile_jobs("siteVideo") == "1"


# --- the platform and the path it reads ---------------------------------------

def test_the_platform_can_be_forced():
    assert pf.podcast_platform("linux", "MINGW64_NT-10.0", "") == "linux"
    assert pf.podcast_platform("windows", "Linux", "") == "windows"


def test_the_emulation_layers_name_themselves():
    for uname in ("MINGW64_NT-10.0-18362", "MSYS_NT-10.0", "CYGWIN_NT-10.0"):
        assert pf.podcast_platform("", uname, "") == "windows"


def test_a_host_with_no_uname_is_named_by_its_os_variable():
    assert pf.podcast_platform("", "", "Windows_NT") == "windows"
    assert pf.podcast_platform("", "", "") == "linux"
    assert pf.podcast_platform("", "Linux", "Windows_NT") == "linux"


def test_a_linux_path_is_left_alone():
    assert pf.native_path("/mnt/phone/Podcasts", "linux", None) == \
        "/mnt/phone/Podcasts"


def test_a_drive_path_only_has_its_slashes_settled():
    assert pf.native_path("D:\\Podcasts", "windows", None) == "D:/Podcasts"


def test_cygpath_wins_when_it_answers():
    assert pf.native_path("/d/Podcasts", "windows", "X:/from/cygpath") == \
        "X:/from/cygpath"


@pytest.mark.parametrize("path,expected", [
    ("/d/Podcasts", "D:/Podcasts"),
    ("/cygdrive/d/Podcasts", "D:/Podcasts"),
    ("/d", "D:/"),
    ("/cygdrive/d", "D:/"),
    ("/D/sub", "D:/sub"),
])
def test_the_manual_fallback_covers_the_ordinary_mount_shapes(path, expected):
    assert pf.native_path(path, "windows", None) == expected
    # a cygpath that ran and printed nothing falls through too
    assert pf.native_path(path, "windows", "") == expected


def test_a_relative_path_stays_relative():
    assert pf.native_path("Podcasts/x", "windows", None) == "Podcasts/x"


# --- resolveYtdlp --------------------------------------------------------------
# The three sets are what the world materializes: present is what a name on PATH
# means to command -v (the executable bit is not tested), executable_paths what
# a slash candidate must pass [[ -x ]] for, importable which interpreter can
# `import yt_dlp`.

def test_an_explicit_answer_beats_every_guess():
    assert pf.resolve_ytdlp("/opt/yt", "linux", "/home/u", "/repo", "/w",
                            set(), set(), set()) == ["/opt/yt"]


def test_linux_consults_path_then_the_user_install():
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            {"yt-dlp"}, set(), set()) == ["yt-dlp"]
    # a name on PATH reports present even without the executable bit
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            {"yt-dlp"}, set(), set()) == ["yt-dlp"]
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            set(), {"/home/u/.local/bin/yt-dlp"}, set()) == \
        ["/home/u/.local/bin/yt-dlp"]


def test_linux_prefers_a_nightly_over_the_release():
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            {"yt-dlp-nightly", "yt-dlp"}, set(),
                            set()) == ["yt-dlp-nightly"]
    # a nightly under the user install still beats a release on PATH
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            {"yt-dlp"}, {"/home/u/.local/bin/yt-dlp-nightly"},
                            set()) == ["/home/u/.local/bin/yt-dlp-nightly"]
    # and an explicit answer still beats the nightly
    assert pf.resolve_ytdlp("/opt/yt", "linux", "/home/u", "/repo", "/w",
                            {"yt-dlp-nightly"}, set(), set()) == ["/opt/yt"]


def test_windows_does_not_look_for_a_nightly():
    assert pf.resolve_ytdlp("", "windows", "/home/u", "/repo", "/w",
                            {"yt-dlp-nightly", "yt-dlp"}, set(),
                            set()) == ["yt-dlp"]


def test_windows_consults_the_script_home_first():
    assert pf.resolve_ytdlp("", "windows", "/home/u", "/repo", "/w",
                            set(), {"/repo/yt-dlp.exe"}, set()) == \
        ["/repo/yt-dlp.exe"]
    assert pf.resolve_ytdlp("", "windows", "/home/u", "/repo", "/w",
                            set(), {os.path.normpath("/w/./yt-dlp.exe")},
                            set()) == ["./yt-dlp.exe"]
    assert pf.resolve_ytdlp("", "windows", "/home/u", "/repo", "/w",
                            {"yt-dlp.exe"}, set(), set()) == ["yt-dlp.exe"]
    assert pf.resolve_ytdlp("", "windows", "/home/u", "/repo", "/w",
                            {"yt-dlp"}, set(), set()) == ["yt-dlp"]


def test_an_importable_module_is_the_last_resort_in_order():
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            {"python3", "py"}, set(),
                            {"python3", "py"}) == ["python3", "-m", "yt_dlp"]
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            {"python3", "py"}, set(),
                            {"py"}) == ["py", "-m", "yt_dlp"]
    # present but not importable is not an answer
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            {"python3", "python", "py"}, set(),
                            {"python"}) == ["python", "-m", "yt_dlp"]
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            {"python3", "python", "py"}, set(),
                            set()) is None


def test_a_candidate_on_path_beats_the_module():
    assert pf.resolve_ytdlp("", "linux", "/home/u", "/repo", "/w",
                            {"yt-dlp"}, set(), {"python3"}) == ["yt-dlp"]


# --- the nightly upgrade --------------------------------------------------------

def test_a_fourth_version_component_is_what_makes_it_a_nightly():
    assert pf.is_nightly_version("2026.08.30.232658")
    assert pf.is_nightly_version("  2026.08.30.232658\n")
    assert not pf.is_nightly_version("2026.08.30")
    assert not pf.is_nightly_version("")
    assert not pf.is_nightly_version("nightly")


def test_a_pipx_install_is_upgraded_by_pipx_under_its_venv_name():
    # the venv name, not the command name: --suffix parts the two
    assert pf.ytdlp_upgrade_command(
        ["yt-dlp-nightly"], "/home/u/.local/pipx/venvs/yt-dlp-nightly/bin/yt-dlp",
        True, True) == ["pipx", "upgrade", "--pip-args=--pre", "yt-dlp-nightly"]
    # the other pipx home, and the interpreter beside it does not overrule pipx
    assert pf.ytdlp_upgrade_command(
        ["yt-dlp"], "/home/u/.local/share/pipx/venvs/yt-dlp/bin/yt-dlp",
        True, True) == ["pipx", "upgrade", "--pip-args=--pre", "yt-dlp"]
    # no pipx to run it with is no upgrade, not a broken call
    assert pf.ytdlp_upgrade_command(
        ["yt-dlp"], "/home/u/.local/pipx/venvs/yt-dlp/bin/yt-dlp",
        False, True) is None


def test_a_standalone_binary_updates_itself_onto_the_nightly_line():
    assert pf.ytdlp_upgrade_command(["yt-dlp-nightly"], "/opt/bin/yt-dlp-nightly",
                                    True, False) == \
        ["yt-dlp-nightly", "-U", "--update-to", "nightly"]


def test_an_install_someone_else_owns_is_left_alone():
    # an interpreter beside it: a pip or distro install, whose yt-dlp refuses
    # to self-update anyway
    assert pf.ytdlp_upgrade_command(["yt-dlp"], "/usr/bin/yt-dlp",
                                    True, True) is None
    # and the module has no binary to upgrade at all
    assert pf.ytdlp_upgrade_command(["python3", "-m", "yt_dlp"],
                                    "/usr/bin/python3", True, True) is None


# --- the date range -------------------------------------------------------------

def test_an_empty_spec_is_no_range_at_all():
    assert pf.parse_date_range("") == ("", "")


def test_a_single_date_is_an_after():
    assert pf.parse_date_range("20260607") == ("20260607", "")


def test_a_range_sets_both_ends():
    assert pf.parse_date_range("20260607..20260707") == ("20260607", "20260707")


def test_an_open_start_sets_only_before():
    assert pf.parse_date_range("..20260707") == ("", "20260707")


def test_a_relative_date_survives_whole():
    assert pf.parse_date_range("today-2weeks") == ("today-2weeks", "")


def test_a_range_with_two_separators_cuts_at_the_first_one():
    # ${spec%%..*} strips the longest ".." suffix, which starts at the FIRST ..,
    # and ${spec#*..} the shortest prefix, which ends at the first: the spec is
    # cut at the first .. and the leftover "b..c" can never be a date, so the
    # flagged end is the one before it.
    err = io.StringIO()
    assert pf.parse_date_range("a..b..c", err) is None
    assert 'Not a date: "a".' in err.getvalue()
    err = io.StringIO()
    assert pf.parse_date_range("20260607..x..y", err) is None
    assert 'Not a date: "x..y".' in err.getvalue()


def test_a_date_that_is_not_one_is_refused_with_the_two_line_message():
    err = io.StringIO()
    assert pf.parse_date_range("last tuesday", err) is None
    assert err.getvalue() == (
        'Not a date: "last tuesday". Expected YYYYMMDD or a relative date '
        'like "today-2weeks",\n'
        "optionally as a range: <after>..<before>, ..<before> or <after>..\n")


@pytest.mark.parametrize("value", [
    "20260607", "now", "today", "yesterday",
    "+3days", "-1week", "now+1second", "today-2weeks", "-2weeks",
])
def test_the_date_shapes_yt_dlp_accepts(value):
    assert pf.is_ytdlp_date(value)


@pytest.mark.parametrize("value", [
    "2026-06-07", "2026067", "202606071", "2026060", "day", "today-2weeks-",
    "yesterday+5months", "+", "2026060a", "now-",
])
def test_the_shapes_it_does_not(value):
    assert not pf.is_ytdlp_date(value)


# --- building the call -----------------------------------------------------------

def _call(**kwargs):
    return pf.podcast_call(
        "/library", "/library/archive.log", "AI/latent space", "",
        "40", "", "https://example.test/latent",
        ytdlp_command=["yt-dlp"], **kwargs)


def test_the_call_starts_with_the_resolved_tool_and_the_output_path():
    call = _call()
    assert call[0] == "yt-dlp"
    assert call[1:3] == ["-o",
                         "/library/AI/latent space/%(upload_date)s "
                         "%(title)s.%(ext)s"]
    assert call[-1] == "https://example.test/latent"
    assert "--dateafter" not in call and "--datebefore" not in call
    assert "--no-warnings" in call and "--no-quiet" not in call
    assert "--print" in call
    assert f"after_move:{pf.PODCAST_EPISODE_MARKER}%(filepath)s" in call


def test_a_trailing_slash_is_settled_before_the_path_is_built():
    call = pf.podcast_call("/library/", "/library/archive.log", "Misc/", "",
                           "", "", "https://u", ytdlp_command=["yt-dlp"])
    assert call[2] == "/library/Misc/" + pf.PODCAST_DEFAULT_TEMPLATE


def test_an_empty_playlist_end_falls_back_to_the_default():
    call = pf.podcast_call("/library", "a.log", "x", "", "", "", "https://u",
                           ytdlp_command=["yt-dlp"])
    assert call[3:5] == ["--playlist-end", "20"]


def test_unlimited_asks_for_no_playlist_end_at_all():
    call = pf.podcast_call("/library", "a.log", "x", "", "0", "", "https://u",
                           ytdlp_command=["yt-dlp"])
    assert "--playlist-end" not in call


def test_a_date_range_reaches_every_feed():
    call = _call(date_after="20260607", date_before="20260707")
    joined = " ".join(call)
    assert "--dateafter 20260607 --datebefore 20260707" in joined


def test_extra_args_land_between_the_archive_and_the_url():
    call = pf.podcast_call("/library", "a.log", "x", "", "20",
                           "--playlist-reverse --retries 3", "https://u",
                           ytdlp_command=["yt-dlp"])
    assert call[-4:] == ["--playlist-reverse", "--retries", "3", "https://u"]


def test_verbose_puts_the_firehose_back():
    call = _call(verbose="1")
    assert "--no-quiet" in call and "--verbose" in call
    assert "--no-warnings" not in call


def test_an_unknown_profile_refuses_the_call():
    assert _call(profile="nope") is None
    assert _call(profile="") is not None  # the default, not a refusal


def test_sponsorblock_unset_keeps_the_flags():
    assert "--sponsorblock-remove" in " ".join(_call())
    assert "--sponsorblock-remove" in " ".join(_call(sponsorblock="all"))


def test_sponsorblock_settled_absent_takes_flag_and_value_out():
    call = _call(sponsorblock="")
    assert "--sponsorblock-remove" not in call
    call = pf.podcast_call("/library", "a.log", "x", "", "", "", "https://u",
                           ytdlp_command=["yt-dlp"], profile="youtubeVideo",
                           sponsorblock="")
    assert "--sponsorblock-mark" not in call


def test_a_feed_s_own_no_sponsorblock_is_a_different_option():
    call = pf.podcast_call("/library", "a.log", "x", "", "",
                           "--no-sponsorblock", "https://u",
                           ytdlp_command=["yt-dlp"], sponsorblock="")
    assert "--no-sponsorblock" in call


# --- one line per episode ---------------------------------------------------------

def _episode_line(tmp_path, name, size):
    (tmp_path / name).write_bytes(b"\0" * size)
    return f"{pf.PODCAST_EPISODE_MARKER}{tmp_path / name}"


def test_one_line_per_episode_and_the_counter_behind_it(tmp_path, capsys):
    line = _episode_line(tmp_path, "ep1.opus", 10)
    stream = "\n".join(
        ["[download] noise nobody wants", line, "ERROR: Video unavailable"]) + "\n"
    rc = pf.report_episodes(stream, str(tmp_path / "counter"),
                            str(tmp_path / "manifest"), "AI/latent space")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ("[   1] ok   AI/latent space | ep1.opus | 10 B\n"
                   "[   2] FAIL AI/latent space | Video unavailable | -\n")
    assert (tmp_path / "counter").read_text() == "2\n"
    assert (tmp_path / "manifest").read_text() == f"{tmp_path / 'ep1.opus'}\n"


def test_the_numbering_carries_on_across_feeds(tmp_path, capsys):
    (tmp_path / "counter").write_text("5\n")
    pf.report_episodes(
        f"{_episode_line(tmp_path, 'e.opus', 10)}\n",
        str(tmp_path / "counter"), str(tmp_path / "manifest"), "AI/x")
    assert capsys.readouterr().out == \
        "[   6] ok   AI/x | e.opus | 10 B\n"


def test_the_counter_read_takes_the_whole_first_line(tmp_path, capsys):
    # read -r n with a single variable takes the whole line: a second line
    # never reaches the arithmetic, neither does a second field - a stale "7 8"
    # is not 7 (the shell's arithmetic would die on it with a syntax error) but
    # a non-number, and a non-number counts from one.
    for stale in ("7\n999\n", "  7\n"):
        (tmp_path / "counter").write_text(stale)
        pf.report_episodes(
            f"{_episode_line(tmp_path, 'e.opus', 10)}\n",
            str(tmp_path / "counter"), str(tmp_path / "manifest"), "AI/x")
        assert capsys.readouterr().out == \
            "[   8] ok   AI/x | e.opus | 10 B\n", stale
        capsys.readouterr()
    (tmp_path / "counter").write_text("7 8\n")
    pf.report_episodes(
        f"{_episode_line(tmp_path, 'e.opus', 10)}\n",
        str(tmp_path / "counter"), str(tmp_path / "manifest"), "AI/x")
    assert capsys.readouterr().out == \
        "[   1] ok   AI/x | e.opus | 10 B\n"


def test_a_vanished_episode_measures_zero(tmp_path, capsys):
    pf.report_episodes(
        f"{pf.PODCAST_EPISODE_MARKER}{tmp_path / 'gone.opus'}\n",
        str(tmp_path / "counter"), str(tmp_path / "manifest"), "AI/x")
    assert capsys.readouterr().out == \
        "[   1] ok   AI/x | gone.opus | 0 B\n"


def test_verbose_passes_everything_else_through(tmp_path, capsys):
    pf.report_episodes("[download] noise nobody wants\n",
                       str(tmp_path / "counter"), str(tmp_path / "manifest"),
                       "AI/x", verbose="1")
    assert capsys.readouterr().out == "[download] noise nobody wants\n"
    pf.report_episodes("[download] noise nobody wants\n",
                       str(tmp_path / "counter"), str(tmp_path / "manifest"),
                       "AI/x")
    assert capsys.readouterr().out == ""


def test_a_final_line_without_its_newline_is_not_read(tmp_path, capsys):
    pf.report_episodes(_episode_line(tmp_path, "e.opus", 10),
                       str(tmp_path / "counter"), str(tmp_path / "manifest"),
                       "AI/x")
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "manifest").exists()


def test_the_carriage_return_does_not_reach_the_path(tmp_path, capsys):
    line = _episode_line(tmp_path, "crlf.opus", 4)
    pf.report_episodes(line + "\r\n", str(tmp_path / "counter"),
                       str(tmp_path / "manifest"), "AI/x")
    assert capsys.readouterr().out == \
        "[   1] ok   AI/x | crlf.opus | 4 B\n"
    assert (tmp_path / "manifest").read_text() == f"{tmp_path / 'crlf.opus'}\n"


def test_an_error_without_its_space_keeps_the_prefix(tmp_path, capsys):
    pf.report_episodes("ERROR:Video unavailable\n", str(tmp_path / "counter"),
                       str(tmp_path / "manifest"), "AI/x")
    assert "ERROR:Video unavailable" in capsys.readouterr().out


def test_the_flock_branch_prints_the_same_lines(tmp_path, capsys):
    line = _episode_line(tmp_path, "e.opus", 10)
    pf.report_episodes(line + "\n", str(tmp_path / "counter"),
                       str(tmp_path / "manifest"), "AI/x", have_flock=True)
    assert capsys.readouterr().out == \
        "[   1] ok   AI/x | e.opus | 10 B\n"
    assert (tmp_path / "counter").read_text() == "1\n"


# --- the bot block -----------------------------------------------------------------

BOT_LINE = ("ERROR: [youtube+GetPOT] abc: "
            "Sign in to confirm you\u2019re not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication.")


def test_the_bot_block_stops_the_feed_saying_so(tmp_path, capsys):
    flag = tmp_path / "blocked.youtube"
    line = _episode_line(tmp_path, "ep1.opus", 10)
    stream = BOT_LINE + "\n" + line + "\n"
    rc = pf.report_episodes(stream, str(tmp_path / "counter"),
                            str(tmp_path / "manifest"), "AI/x",
                            block_flag=str(flag), provider="youtube")
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == f"[   1] STOP AI/x | {BOT_LINE[len('ERROR: '):]} | -\n"
    assert flag.exists()
    assert "WARNING: youtube answered" in err
    # nothing after the block is read: the episode never made the manifest
    assert not (tmp_path / "manifest").exists()
    assert (tmp_path / "counter").read_text() == "1\n"


@pytest.mark.parametrize("line", [
    "ERROR: [youtube] a: Sign in to confirm you're not a bot.",
    "ERROR: [youtube] a: Sign in to confirm you are not a bot.",
    "ERROR: [youtube] a: sign in to confirm you're not a bot",
])
def test_the_block_is_recognised_however_it_is_worded(line):
    assert pf.is_bot_block(line)


def test_the_age_gate_shares_the_first_fragment_and_not_the_second():
    assert not pf.is_bot_block(
        "ERROR: [youtube] zzz: Sign in to confirm your age. "
        "This video may be inappropriate for some users.")
    assert not pf.is_bot_block("ERROR: some other failure")


def test_the_age_gate_fails_alone_and_the_feed_carries_on(tmp_path, capsys):
    flag = tmp_path / "blocked.youtube"
    line = _episode_line(tmp_path, "ep1.opus", 10)
    stream = ("ERROR: [youtube] zzz: Sign in to confirm your age.\n"
              + line + "\n")
    pf.report_episodes(stream, str(tmp_path / "counter"),
                       str(tmp_path / "manifest"), "AI/x",
                       block_flag=str(flag), provider="youtube")
    out = capsys.readouterr().out
    assert not flag.exists()
    assert "FAIL" in out and "ok" in out


def test_without_a_block_flag_the_block_line_is_just_a_failure(
        tmp_path, capsys):
    line = _episode_line(tmp_path, "ep1.opus", 10)
    stream = BOT_LINE + "\n" + line + "\n"
    pf.report_episodes(stream, str(tmp_path / "counter"),
                       str(tmp_path / "manifest"), "AI/x")
    out = capsys.readouterr().out
    assert "FAIL" in out and "STOP" not in out
    assert (tmp_path / "manifest").exists()  # it carried on


def test_a_flag_that_cannot_be_written_still_stops_the_run(tmp_path,
                                                           capsys):
    stream = BOT_LINE + "\n"
    pf.report_episodes(stream, str(tmp_path / "counter"),
                       str(tmp_path / "manifest"), "AI/x",
                       block_flag=str(tmp_path / "no" / "such" / "dir" / "f"),
                       provider="youtube")
    out, err = capsys.readouterr()
    assert "STOP" in out and "WARNING: youtube answered" in err


def test_the_warning_says_what_to_do(capsys):
    pf.podcast_bot_block_warning("youtube")
    err = capsys.readouterr().err
    assert err.splitlines() == [
        'WARNING: youtube answered "Sign in to confirm you are not a bot".',
        "         Every further request would be refused the same way and would only",
        "         confirm the pattern that got this address blocked, so the youtube feeds",
        "         are being stopped here. Feeds from other providers are unaffected.",
        "         What helps: wait, come back from a different address, or pass cookies -",
        "         https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies",
    ]


# --- what the run downloaded ----------------------------------------------------------

def test_the_stats_are_read_off_the_disk(tmp_path):
    (tmp_path / "one.opus").write_bytes(b"\0" * 10)
    (tmp_path / "two.opus").write_bytes(b"\0" * 5)
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "feed1").write_text(f"{tmp_path / 'one.opus'}\n")
    (manifests / "feed2").write_text(f"{tmp_path / 'two.opus'}\n")
    assert pf.podcast_download_stats(str(manifests)) == (2, 15)


def test_a_vanished_file_is_still_a_file(tmp_path):
    (tmp_path / "one.opus").write_bytes(b"\0" * 10)
    manifests = tmp_path / "manifests2"
    manifests.mkdir()
    (manifests / "feed1").write_text(
        f"{tmp_path / 'one.opus'}\n{tmp_path / 'gone.opus'}\n")
    assert pf.podcast_download_stats(str(manifests)) == (2, 10)


def test_a_run_that_fetched_nothing_counts_nothing(tmp_path):
    assert pf.podcast_download_stats(str(tmp_path / "neverWritten")) == (0, 0)


def test_dotfiles_and_subdirectories_are_not_manifests(tmp_path):
    (tmp_path / "one.opus").write_bytes(b"\0" * 10)
    manifests = tmp_path / "m"
    manifests.mkdir()
    (manifests / "feed").write_text(f"{tmp_path / 'one.opus'}\n")
    (manifests / ".hidden").write_text(f"{tmp_path / 'one.opus'}\n")
    (manifests / "subdir").mkdir()
    (manifests / "subdir" / "feed").write_text(f"{tmp_path / 'one.opus'}\n")
    assert pf.podcast_download_stats(str(manifests)) == (1, 10)


def test_a_manifest_line_without_its_newline_still_counts(tmp_path):
    (tmp_path / "one.opus").write_bytes(b"\0" * 10)
    manifests = tmp_path / "m"
    manifests.mkdir()
    (manifests / "feed").write_text(str(tmp_path / "one.opus"))  # no \n
    assert pf.podcast_download_stats(str(manifests)) == (1, 10)


def test_the_stats_skip_blank_and_cr_lines(tmp_path):
    (tmp_path / "one.opus").write_bytes(b"\0" * 10)
    manifests = tmp_path / "m"
    manifests.mkdir()
    (manifests / "feed").write_text(f"\r\n{tmp_path / 'one.opus'}\r\n\n")
    assert pf.podcast_download_stats(str(manifests)) == (1, 10)


# --- the table reader -------------------------------------------------------------------

def _read(content, tmp_path, name="table.tsv"):
    table = tmp_path / name
    table.write_text(content)
    err = io.StringIO()
    return pf.read_podcast_table(str(table), err), err


def test_a_well_formed_table_is_accepted(tmp_path):
    (rows, profile, jobs, status), _err = _read(
        "# a comment, and the blank line below, are grouping - not rows\n"
        "\n"
        "1\tAI/latent space\t\t40\t\thttps://example.test/latent\n"
        "0\tMisc/paused\t\t\t\thttps://example.test/paused\n"
        "1\tMisc/Joe Rogan\t%(title)s.%(ext)s\t\t--no-sponsorblock"
        "\thttps://example.test/rogan\n", tmp_path)
    assert status == 0 and profile == "youtubeAudio" and jobs == "1"
    assert len(rows) == 3
    assert pf.split_podcast_row(rows[1]) == \
        ("0", "Misc/paused", "", "", "", "https://example.test/paused")
    assert pf.split_podcast_row(rows[2])[2] == "%(title)s.%(ext)s"
    assert pf.split_podcast_row(rows[2])[4] == "--no-sponsorblock"


def test_a_crlf_row_does_not_carry_the_cr_into_the_url(tmp_path):
    (rows, _profile, _jobs, status), _err = _read(
        "1\tAI/crlf\t\t\t\thttps://example.test/crlf\r\n", tmp_path)
    assert status == 0
    assert pf.split_podcast_row(rows[0])[5] == "https://example.test/crlf"


def test_a_table_with_no_trailing_newline_is_read_whole(tmp_path):
    (rows, _profile, _jobs, status), _err = _read(VALID_ROW, tmp_path)
    assert status == 0 and rows == [VALID_ROW]


def test_a_missing_table_is_refused(tmp_path):
    err = io.StringIO()
    rows, profile, jobs, status = pf.read_podcast_table(
        str(tmp_path / "nope.tsv"), err)
    assert (rows, profile, jobs, status) == ([], "youtubeAudio", "", 1)
    assert err.getvalue() == f"Podcast table not found: {tmp_path / 'nope.tsv'}\n"


def test_the_refusal_names_every_fault_at_once(tmp_path):
    (rows, _profile, _jobs, status), err = _read(
        "1\tAI/ok\t\t\t\thttps://example.test/ok\n"
        "2\tAI/bad-active\t\t\t\thttps://example.test/bad\n"
        "1\tAI/too-few-columns\thttps://example.test/short\n"
        "1\tAI/bad-url\t\t\t\tnot-a-url\n"
        "1\t/absolute\t\t\t\thttps://example.test/abs\n"
        "1\tAI/bad-end\t\tmany\t\thttps://example.test/end\n"
        "1\tAI/all-of-it\t\t0\t\thttps://example.test/all\n",
        tmp_path, "bad.tsv")
    out = err.getvalue()
    assert status == 1
    for fault in ('line 2: active is "2", expected 0 or 1',
                  "line 3: 2 tab(s), expected 5 - the columns are "
                  "active subdir nameTemplate playlistEnd extraArgs url",
                  'line 4: url "not-a-url" is not an http(s) address',
                  'line 5: subdir "/absolute" must stay under the output root',
                  'line 6: playlistEnd is "many", expected a number, '
                  "0 for all of them, or nothing"):
        assert fault in out, fault
    assert "line 7" not in out  # playlistEnd 0 is a valid row
    assert "5 line(s) are malformed" in out
    # the refused rows are still returned as they were read - a row with a
    # field fault is among them, only a wrong tab count drops its row
    assert len(rows) == 6


def test_a_single_line_can_earn_two_faults(tmp_path):
    err = io.StringIO()
    table = tmp_path / "t.tsv"
    table.write_text("1\t/a..b\t\t\t\tnot-a-url\n")
    _rows, _profile, _jobs, status = pf.read_podcast_table(str(table), err)
    assert status == 1
    assert 'subdir "/a..b" must stay under the output root' in err.getvalue()
    assert 'url "not-a-url" is not an http(s) address' in err.getvalue()
    assert "2 line(s) are malformed" in err.getvalue()


def test_a_template_that_climbs_out_of_the_root_is_a_fault(tmp_path):
    """The name half of the same path the subdir column is kept inside. Only
    subdir was checked, so this row wrote wherever it liked."""
    (_rows, _profile, _jobs, status), err = _read(
        "1\tAI/ok\t../../%(title)s.%(ext)s\t\t\thttps://example.test/a\n",
        tmp_path)
    assert status == 1
    assert ('nameTemplate "../../%(title)s.%(ext)s" must stay under the '
            "output root") in err.getvalue()


def test_an_absolute_template_is_a_fault_too(tmp_path):
    (_rows, _profile, _jobs, status), err = _read(
        "1\tAI/ok\t/tmp/%(title)s.%(ext)s\t\t\thttps://example.test/a\n",
        tmp_path)
    assert status == 1
    assert "nameTemplate" in err.getvalue()


def test_a_template_field_with_a_format_spec_is_still_allowed(tmp_path):
    """The check must not cost the column what it is for."""
    (_rows, _profile, _jobs, status), err = _read(
        "1\tAI/ok\t%(upload_date>%Y-%m-%d)s %(title)s.%(ext)s\t\t\t"
        "https://example.test/a\n", tmp_path)
    assert status == 0, err.getvalue()


@pytest.mark.parametrize("argument", [
    "-o", "--output", "-P", "--paths", "--exec", "--exec-before-download",
    "--config-location",
])
def test_the_options_a_row_may_not_carry(argument, tmp_path):
    """Four decide where a download is written and would take it out of the
    output root; three run something else, which is not a feed's to ask for."""
    (_rows, _profile, _jobs, status), err = _read(
        f"1\tAI/ok\t\t\t{argument} x\thttps://example.test/a\n", tmp_path)
    assert status == 1
    assert f'extraArgs may not carry "{argument}"' in err.getvalue()


def test_the_equals_form_is_the_same_option(tmp_path):
    (_rows, _profile, _jobs, status), err = _read(
        "1\tAI/ok\t\t\t--paths=/elsewhere\thttps://example.test/a\n",
        tmp_path)
    assert status == 1
    assert 'may not carry "--paths=/elsewhere"' in err.getvalue()


def test_the_arguments_a_row_is_meant_to_carry_still_pass(tmp_path):
    (_rows, _profile, _jobs, status), err = _read(
        "1\tAI/ok\t\t\t--no-sponsorblock --write-subs\t"
        "https://example.test/a\n", tmp_path)
    assert status == 0, err.getvalue()


def test_directives_set_the_profile_and_the_width(tmp_path):
    (rows, profile, jobs, status), _err = _read(
        "#!profile rssAudio\n#!jobs 4\n"
        "1\tRSS/a\t\t\t\thttps://example.test/a\n", tmp_path)
    assert (status, profile, jobs) == (0, "rssAudio", "4")
    assert rows == ["1\tRSS/a\t\t\t\thttps://example.test/a"]


def test_a_table_that_says_nothing_about_width_gets_its_profiles_width(tmp_path):
    (_rows, profile, jobs, status), _err = _read(
        "#!profile rssAudio\n1\tRSS/a\t\t\t\thttps://example.test/a\n",
        tmp_path)
    assert (status, profile, jobs) == (0, "rssAudio", "10")


def test_a_table_with_no_directive_keeps_the_old_behaviour(tmp_path):
    (_rows, profile, jobs, status), _err = _read(
        "1\tYT/a\t\t\t\thttps://example.test/a\n", tmp_path)
    assert (status, profile, jobs) == (0, "youtubeAudio", "1")


def test_a_mistyped_directive_is_a_fault_not_a_comment(tmp_path):
    err = io.StringIO()
    table = tmp_path / "t.tsv"
    table.write_text("#!profil rssAudio\n"
                     "#!profile notAProfile\n"
                     "#!jobs many\n"
                     "1\tRSS/a\t\t\t\thttps://example.test/a\n")
    _rows, _profile, _jobs, status = pf.read_podcast_table(str(table), err)
    out = err.getvalue()
    assert status == 1
    assert 'unknown directive "#!profil" - the directives are ' \
        "profile and jobs" in out
    assert 'unknown profile "notAProfile" - one of: ' in out
    assert 'jobs is "many", expected a positive number' in out


def test_a_directive_with_the_wrong_shape_is_a_fault(tmp_path):
    for line in ("#!profile\n", "#!profile rssAudio extra\n",
                 "#!Profile rssAudio\n", "#!profile \n"):
        err = io.StringIO()
        table = tmp_path / "t.tsv"
        table.write_text(line)
        _rows, _profile, _jobs, status = pf.read_podcast_table(str(table), err)
        assert status == 1, line
        assert "#!<name> <value>" in err.getvalue() or \
            'unknown directive "#!Profile"' in err.getvalue(), line


# --- rendering the call ------------------------------------------------------------------

def test_posix_quoting_only_where_needed():
    assert pf.render_call(["yt-dlp", "-o", "/a b/c", "--verbose"], "linux") == \
        "yt-dlp -o '/a b/c' --verbose"


def test_posix_quoting_escapes_a_quote():
    assert pf.quote_posix("it's") == "'it'\\''s'"
    assert pf.quote_posix("-o") == "-o"
    assert pf.quote_posix("") == "''"


def test_powershell_doubles_an_inner_quote():
    assert pf.quote_powershell("it's") == "'it''s'"
    # a backslash is safe for PowerShell, where it would be work for POSIX
    assert pf.quote_powershell("a\\b") == "a\\b"
    assert pf.quote_posix("a\\b") == "'a\\b'"


def test_powershell_needs_the_call_operator_for_a_path():
    assert pf.render_call(["./yt-dlp.exe", "-o", "D:/a b/c"], "windows") == \
        "& ./yt-dlp.exe -o 'D:/a b/c'"


def test_powershell_without_a_path_needs_no_operator():
    assert pf.render_call(["yt-dlp.exe", "-o", "D:/x"], "windows") == \
        "yt-dlp.exe -o D:/x"


def test_a_backslash_path_earns_the_operator_too():
    assert pf.render_call(["C:\\x\\yt-dlp.exe"], "windows") == \
        "& C:\\x\\yt-dlp.exe"


def test_an_empty_argv_renders_to_nothing():
    assert pf.render_call([], "linux") == ""
    assert pf.render_call([], "windows") == ""


# --- the constants -----------------------------------------------------------------------

def test_the_constants_the_call_is_built_from():
    assert pf.PODCAST_EPISODE_MARKER == "__episode__"
    assert pf.PODCAST_RSS_JOBS == 10
    assert pf.PODCAST_DEFAULT_PROFILE == "youtubeAudio"
    assert pf.PODCAST_DEFAULT_TEMPLATE == "%(upload_date)s %(title)s.%(ext)s"
    assert pf.PODCAST_DEFAULT_PLAYLIST_END == 20
    assert pf.PODCAST_UNLIMITED_PLAYLIST_END == 0
    assert pf.PODCAST_COLUMNS == ("active", "subdir", "nameTemplate",
                                  "playlistEnd", "extraArgs", "url")
    assert pf.PODCAST_PROFILES == (
        "youtubeAudio", "youtubeVideo", "rssAudio", "rssVideo", "siteVideo")

# --- the tables that ship with the repository --------------------------------
# They are data, and data is where a typo hides. EVERY table found, not one named
# table: they are edited by hand and by the importer, and a check of only the
# first would be quietest about the ones nobody looks at. The real ones live in a
# folder the repository does not track - one machine's library, not code - so a
# checkout without them is normal. The sample beside the package is always
# there, and a fault in it would be copied into every table made from it.

_REPO = str(blackbox.REPO)
_SAMPLE = os.path.join(commands.config_dir(), "podcasts.example.tsv")


def _shipped_tables():
    found = [_SAMPLE]
    for pattern in (os.path.join(_REPO, "data", "podcasts", "*.tsv"),
                    os.path.join(_REPO, "*.tsv")):
        found += sorted(glob.glob(pattern))
    return [p for p in found if os.path.isfile(p)]


def test_the_sample_table_ships_with_the_repository():
    assert os.path.isfile(_SAMPLE)


@pytest.mark.parametrize("table", _shipped_tables(),
                         ids=lambda p: os.path.basename(p))
def test_a_table_in_this_checkout_is_valid(table):
    faults = io.StringIO()
    _rows, _profile, _jobs, status = pf.read_podcast_table(table,
                                                           stderr=faults)
    assert status == 0, faults.getvalue()
