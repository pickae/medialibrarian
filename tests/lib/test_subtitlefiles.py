"""Tests for medialib.lib.subtitlefiles - the subtitle sidecar helpers.

What is pinned here: the sidecar lift and rename on real folders, the exact argv
each alignment hands ffsubsync, the skip records a rename makes, and the host-tool
edge cases - an absent pipx, an absent ffprobe, a conversion ffmpeg refuses.
"""

import os
import shutil
from types import SimpleNamespace

import pytest

from medialib import helpers
from medialib.lib import languages, subtitlefiles
from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLSTUB = blackbox.TOOLSTUB

_PLUMBING = ("bash", "awk", "cat", "find", "grep", "mktemp", "mv", "rm")

_HELPER = helpers.path_of("subliminal_download.py")

# The log a sync that applied the alignment leaves, and one that refused it.
_GOOD_LOG = "score: 44100.000\noffset seconds: 5.000\nwriting output"
_BAD_LOG = ("score: -109800.000\nlow-quality alignment (score -109800.0 < 0.0"
            "; |offset| 282.0s > 60.0s); leaving subtitles unmodified")


@pytest.fixture()
def w(tmp_path, monkeypatch):
    """A PATH holding only the named stubs and their plumbing, plus the knobs
    that decide what each tool prints, with which code it exits, and which
    file it writes.
    """
    bin_dir = tmp_path / "bin"
    out_dir = tmp_path / "out"
    state_dir = tmp_path / "state"
    for d in (bin_dir, out_dir, state_dir):
        d.mkdir()
    for tool in _PLUMBING:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    record = tmp_path / "calls"

    def install(name):
        shutil.copyfile(_TOOLSTUB, str(bin_dir / name))
        os.chmod(str(bin_dir / name), 0o755)

    def say(name, text):
        (out_dir / name).write_text(text)

    def rc(name, codes):
        (out_dir / (name + ".rc")).write_text(codes + "\n")

    def write(name, entries):
        (out_dir / (name + ".write")).write_text(entries + "\n")

    def calls():
        if not record.exists():
            return []
        return [line.rstrip("\n").split("\t")[1:]
                for line in record.read_text().splitlines() if line]

    def clear():
        if record.exists():
            record.unlink()

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("TOOLSTUB_LOG", str(record))
    monkeypatch.setenv("TOOLSTUB_OUT", str(out_dir))
    monkeypatch.setenv("TOOLSTUB_STATE", str(state_dir))
    return SimpleNamespace(install=install, say=say, rc=rc, write=write,
                           calls=calls, clear=clear, bin_dir=bin_dir,
                           tmp_path=tmp_path)


def _tree(w, *entries):
    """A folder holding the named files and (name, 'd') directories."""
    tree = w.tmp_path / "tree"
    tree.mkdir()
    for entry in entries:
        if isinstance(entry, tuple):
            (tree / entry[0]).mkdir(parents=True)
        else:
            path = tree / entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    return tree


class TestMoveSubs:
    def test_lifts_a_files_and_nested_folders_one_level(self, w):
        tree = _tree(w, ("The Movie/Subs/nested", "d"),
                     "The Movie/Subs/a.srt",
                     "The Movie/Subs/nested/c.srt",
                     "The Movie/left.srt")
        subtitlefiles.move_subs(str(tree))
        assert (tree / "The Movie/a.srt").is_file()
        assert (tree / "The Movie/nested/c.srt").is_file()
        assert not (tree / "The Movie/Subs/a.srt").exists()
        assert (tree / "The Movie/left.srt").is_file()

    @pytest.mark.parametrize("path", [
        "m/subs",           # lower-case
        "m/SUBS",           # upper-case
        "Subs",             # one level too shallow
        "m/deep/DeepSubs",  # one level too deep
    ])
    def test_the_match_is_exact(self, w, path):
        tree = _tree(w, (path, "d"), path + "/a.srt")
        subtitlefiles.move_subs(str(tree))
        assert (tree / path / "a.srt").is_file()

    def test_a_folder_onto_a_file_of_its_name_stays_put(self, w):
        # mv cannot overwrite a non-directory with a directory: the folder
        # stays, and the rest of the lift still happens
        tree = _tree(w, ("m/KeepSubs/block", "d"),
                     "m/KeepSubs/block/f.srt", "m/KeepSubs/lift.srt",
                     "m/block")
        subtitlefiles.move_subs(str(tree))
        assert (tree / "m/KeepSubs/block/f.srt").is_file()
        assert (tree / "m/block").is_file()
        assert (tree / "m/lift.srt").is_file()

    def test_a_folder_onto_a_nonempty_folder_of_its_name_stays_put(self, w):
        # mv refuses the rename (the target is a directory that is not
        # empty) and the entry stays where it is, the way `|| true` keeps
        # the lift going
        tree = _tree(w, ("m/KeepSubs/block", "d"), ("m/block", "d"),
                     "m/KeepSubs/block/f.srt", "m/KeepSubs/lift.srt",
                     "m/block/keep.srt")
        subtitlefiles.move_subs(str(tree))
        assert (tree / "m/KeepSubs/block/f.srt").is_file()
        assert (tree / "m/block/keep.srt").is_file()
        assert (tree / "m/lift.srt").is_file()

    def test_a_folder_replaces_an_empty_folder_of_its_name(self, w):
        # the rename succeeds onto an empty directory: the entry's content
        # lands directly in the level above
        tree = _tree(w, ("m/KeepSubs/block", "d"), ("m/block", "d"),
                     "m/KeepSubs/block/f.srt")
        subtitlefiles.move_subs(str(tree))
        assert (tree / "m/block/f.srt").is_file()
        assert not (tree / "m/KeepSubs/block").exists()

    def test_a_file_onto_a_folder_of_its_name_stays_put(self, w):
        # mv cannot overwrite a directory with a non-directory: the file
        # stays in the lifted folder
        tree = _tree(w, ("m/KeepSubs", "d"), ("m/notes.txt", "d"),
                     "m/KeepSubs/notes.txt")
        subtitlefiles.move_subs(str(tree))
        assert (tree / "m/KeepSubs/notes.txt").is_file()
        assert not (tree / "m/notes.txt/notes.txt").exists()

    def test_a_link_named_subsis_not_lifted(self, w):
        # find -P -type d: a link to a folder is a link, and its content is
        # not lifted through it
        tree = _tree(w, ("Deep/subs", "d"), "Deep/subs/x.srt", ("m", "d"))
        (tree / "m/Subs").symlink_to("../../Deep/subs")
        subtitlefiles.move_subs(str(tree))
        assert (tree / "Deep/subs/x.srt").is_file()
        assert (tree / "m/Subs").is_symlink()
        assert not (tree / "m/x.srt").exists()

    def test_a_linked_entry_moves_as_a_link(self, w):
        tree = _tree(w, ("m/Subs", "d"), "real.mp3")
        (tree / "m/Subs/linked.srt").symlink_to("../../real.mp3")
        subtitlefiles.move_subs(str(tree))
        assert (tree / "m/linked.srt").is_symlink()
        assert not (tree / "m/Subs/linked.srt").exists()
        assert (tree / "real.mp3").is_file()

    def test_a_file_named_subsis_not_lifted(self, w):
        tree = _tree(w, ("m", "d"), "m/Subs", "m/other.srt")
        subtitlefiles.move_subs(str(tree))
        assert (tree / "m/Subs").is_file()


class TestRenameSubs:
    def test_renames_to_the_convention(self, w):
        tree = _tree(w, ("The Movie", "d"), "The Movie/The Movie english.srt")
        from medialib.lib.safety import SkipLog
        subtitlefiles.rename_subs(str(tree), SkipLog())
        assert (tree / "The Movie/The Movie.en.srt").is_file()
        assert not (tree / "The Movie/The Movie english.srt").exists()

    def test_the_match_is_case_insensitive(self, w):
        tree = _tree(w, ("The Movie", "d"), "The Movie/THE.MOVIE.ENGLISH.SRT")
        subtitlefiles.rename_subs(str(tree))
        assert (tree / "The Movie/The Movie.en.srt").is_file()

    def test_a_nested_sidecar_names_its_movie(self, w):
        tree = _tree(w, ("The Movie/track 1", "d"),
                     "The Movie/track 1/movie german.srt")
        subtitlefiles.rename_subs(str(tree))
        assert (tree / "The Movie/The Movie.de.srt").is_file()

    def test_a_preexisting_target_is_skipped_and_recorded(self, w):
        tree = _tree(w, ("The Movie", "d"))
        (tree / "The Movie/The Movie.en.srt").write_text("old")
        (tree / "The Movie/The Movie english.srt").touch()
        from medialib.lib.safety import SkipLog
        skip_log = SkipLog()
        subtitlefiles.rename_subs(str(tree), skip_log)
        assert (tree / "The Movie/The Movie.en.srt").read_text() == "old"
        assert (tree / "The Movie/The Movie english.srt").is_file()
        assert skip_log.skips == [("./The Movie/The Movie english.srt",
                                   "The Movie/The Movie.en.srt")]

    def test_every_language_of_the_table(self, w):
        tree = _tree(w, ("The Movie", "d"),
                     "The Movie/a english.srt", "The Movie/b german.srt",
                     "The Movie/c french.srt", "The Movie/d dutch.srt",
                     "The Movie/e spanish.srt", "The Movie/f italian.srt",
                     "The Movie/g.txt")
        subtitlefiles.rename_subs(str(tree))
        for row in languages.LANGUAGES:
            assert (tree / "The Movie/The Movie.{}.srt".format(row.code2)).is_file()
        assert (tree / "The Movie/g.txt").is_file()

    def test_a_linked_movie_is_not_walked(self, w):
        # find -P -type d: a link to the movie folder is not a movie of its
        # own, so the sidecar renames under the movie's own name, never the
        # link's
        tree = _tree(w, ("The Movie", "d"), "The Movie/The Movie english.srt")
        (tree / "Link").symlink_to("The Movie")
        subtitlefiles.rename_subs(str(tree))
        assert (tree / "The Movie/The Movie.en.srt").is_file()
        assert not (tree / "The Movie/The Movie english.srt").exists()

    def test_a_linked_sidecar_is_left_alone(self, w):
        # find -P -type f: a link wearing a sidecar name is not a file
        tree = _tree(w, ("The Movie", "d"), "The Movie/real.mp3")
        (tree / "The Movie/english.srt").symlink_to("real.mp3")
        subtitlefiles.rename_subs(str(tree))
        assert (tree / "The Movie/english.srt").is_symlink()
        assert not (tree / "The Movie/The Movie.en.srt").exists()

    def test_a_linked_subfolder_is_not_walked(self, w):
        # a link to a folder inside the movie does not open a second view of
        # its content: the sidecar it holds belongs to the movie it is really
        # in
        tree = _tree(w, ("The Movie", "d"), ("Deep", "d"), "Deep/english.srt")
        (tree / "The Movie/box").symlink_to("../Deep")
        subtitlefiles.rename_subs(str(tree))
        assert (tree / "Deep/Deep.en.srt").is_file()
        assert not (tree / "The Movie/The Movie.en.srt").exists()


class TestSyncSubtitle:
    def _run(self, w, quality="yes", rc_code="0", log=None):
        w.install("ffsubsync")
        w.rc("ffsubsync", rc_code)
        if log is not None:
            w.say("ffsubsync", log)
            w.write("ffsubsync", "${--log-dir-path}/ffsubsync.log")
        return subtitlefiles.sync_subtitle("ref.mkv", "sub.srt", "600", "60",
                                           quality)

    def test_an_applied_alignment_is_zero(self, w):
        assert self._run(w, log=_GOOD_LOG) == 0

    def test_a_run_that_died_is_one(self, w):
        assert self._run(w, rc_code="1", log=_BAD_LOG) == 1

    def test_a_refused_alignment_is_two(self, w):
        assert self._run(w, log=_BAD_LOG) == 2

    def test_nothing_to_refuse_reads_as_a_clean_zero(self, w):
        assert self._run(w) == 0

    def test_the_verdict_is_the_log_file_and_never_the_console_copy(self, w):
        """ffsubsync's own console output WRAPS the rejection across three
        lines, so an implementation that scanned it would both miss a real
        refusal and read one into a run that aligned. The log file is the
        record; here it is empty and the console says the opposite."""
        w.install("ffsubsync")
        w.rc("ffsubsync", "0")
        w.say("ffsubsync",
              "[19:14:19] WARNING  low-quality alignment (score -109800.0 < "
              "0.0;   ffsubsync.py:269\n"
              "                    |offset| 282.0s > 60.0s); leaving subtitles"
              "\n                    unmodified")
        assert subtitlefiles.sync_subtitle("ref.mkv", "sub.srt", "600", "60",
                                           "yes") == 0

    def test_an_absent_ffsubsync_is_one(self, w):
        assert subtitlefiles.sync_subtitle("ref.mkv", "sub.srt", "600", "60",
                                           "yes") == 1

    def test_the_arguments_handed_ffsubsync(self, w):
        self._run(w, log=_GOOD_LOG)
        (argv,) = w.calls()
        assert argv[:7] == ["ffsubsync", "ref.mkv", "-i", "sub.srt", "-o",
                            "sub.srt", "--max-offset-seconds"]
        assert argv[7:11] == ["600", "--skip-sync-on-low-quality",
                              "--quality-max-offset-seconds", "60"]
        assert argv[11] == "--log-dir-path"
        assert os.path.dirname(argv[12]) == str(w.tmp_path) or argv[12]

    def test_an_old_ffsubsync_gets_no_quality_flags(self, w):
        self._run(w, quality="no", log=_GOOD_LOG)
        (argv,) = w.calls()
        assert "--skip-sync-on-low-quality" not in argv
        assert "--log-dir-path" in argv

    def test_the_log_directory_is_removed(self, w):
        self._run(w, log=_GOOD_LOG)
        (argv,) = w.calls()
        assert not os.path.exists(argv[-1])


class TestFfsubsyncPython:
    def _script(self, w, first_line):
        script = w.bin_dir / "ffsubsync"
        script.write_text(first_line + "\nexit 0\n")
        os.chmod(str(script), 0o755)
        return script

    def test_the_interpreter_its_own_line_names(self, w):
        self._script(w, "#!/opt/venvs/ffsubsync/bin/python")
        assert subtitlefiles.ffsubsync_python() == [
            "/opt/venvs/ffsubsync/bin/python"]

    def test_an_env_line_is_the_whole_argv(self, w):
        self._script(w, "#!/usr/bin/env python3")
        assert subtitlefiles.ffsubsync_python() == ["/usr/bin/env", "python3"]

    def test_a_shell_wrapper_falls_back_to_the_python_beside_it(self, w):
        self._script(w, "#!/bin/sh")
        python = w.bin_dir / "python"
        python.write_text("#!/bin/sh\n")
        os.chmod(str(python), 0o755)
        assert subtitlefiles.ffsubsync_python() == [str(python)]

    def test_nothing_to_go_on_is_none(self, w):
        self._script(w, "#!/bin/sh")
        assert subtitlefiles.ffsubsync_python() is None

    def test_no_ffsubsync_is_none(self, w):
        assert subtitlefiles.ffsubsync_python() is None


class TestSyncByConfidence:
    """The confidence helper, run under the interpreter ffsubsync's own script
    names - here a stub that records the call and writes the log the case
    chooses."""

    _HELPER = helpers.path_of("ffsubsync_confidence.py")

    def _run(self, w, log, rc_code="0"):
        script = w.bin_dir / "ffsubsync"
        script.write_text("#!" + str(w.bin_dir / "fakepython") + "\n")
        os.chmod(str(script), 0o755)
        w.install("fakepython")
        w.rc("fakepython", rc_code)
        if log is not None:
            w.say("fakepython", log)
            w.write("fakepython", "${--log-dir-path}/ffsubsync.log")
        return subtitlefiles.sync_subtitle("ref.mkv", "sub.srt", "600", "60",
                                           "confidence")

    def _log(self, confidence):
        return _GOOD_LOG + "\nalignment confidence: %s" % confidence

    def test_the_helper_is_handed_the_whole_window_as_the_offset_limit(self, w):
        self._run(w, self._log("9.50"))
        (argv,) = w.calls()
        assert argv[:13] == ["fakepython", self._HELPER, "ref.mkv", "-i", "sub.srt", "-o",
                             "sub.srt", "--max-offset-seconds", "600",
                             "--skip-sync-on-low-quality",
                             "--quality-max-offset-seconds", "600",
                             "--log-dir-path"]

    def test_a_confident_alignment_is_zero(self, w):
        assert self._run(w, self._log(subtitlefiles.MIN_SYNC_CONFIDENCE)) == 0

    def test_one_that_does_not_stand_out_is_refused(self, w):
        assert self._run(w, self._log(
            subtitlefiles.MIN_SYNC_CONFIDENCE - 0.01)) == 2

    def test_ffsubsyncs_own_refusal_still_refuses(self, w):
        assert self._run(w, _BAD_LOG + "\nalignment confidence: 9.00") == 2

    def test_no_measurement_is_a_failure_not_a_pass(self, w):
        assert self._run(w, _GOOD_LOG) == 1
        assert self._run(w, None) == 1

    def test_a_helper_that_failed_is_one(self, w):
        assert self._run(w, self._log("9.50"), rc_code="1") == 1

    def test_without_an_interpreter_to_run_it_under_it_is_one(self, w):
        assert subtitlefiles.sync_subtitle("ref.mkv", "sub.srt", "600", "60",
                                           "confidence") == 1


class TestDownloadSrt:
    def _setup(self, w, tree, have=("pipx", "ffprobe", "ffmpeg", "ffsubsync"),
               pipx_write="-", ffprobe="-", ffmpeg_rc="-",
               ffmpeg_write="-", ffsubsync_rc="0", ffsubsync_log=None):
        for tool in have:
            w.install(tool)
        if pipx_write != "-":
            w.write("pipx", pipx_write)
        if ffprobe != "-":
            w.say("ffprobe", ffprobe)
        if ffmpeg_rc != "-":
            w.rc("ffmpeg", ffmpeg_rc)
        if ffmpeg_write != "-":
            w.write("ffmpeg", ffmpeg_write)
        if ffsubsync_rc != "-":
            w.rc("ffsubsync", ffsubsync_rc)
        if ffsubsync_log is not None:
            w.say("ffsubsync", ffsubsync_log)
            w.write("ffsubsync", "${--log-dir-path}/ffsubsync.log")
        (tree / "Movie.mkv").touch()
        return w.calls, tree

    def test_a_sidecar_that_exists_resumes(self, w, monkeypatch):
        tree = _tree(w, "Movie.en.srt")
        monkeypatch.chdir(tree)
        have_calls, _ = self._setup(w, tree)
        logs = []
        subtitlefiles.download_srt("Movie.mkv", "en", "u", "p", "600", "60",
                                   "yes", logs.append)
        assert have_calls() == []
        assert logs == []

    def test_without_credentials_it_warns_and_calls_nothing(self, w, monkeypatch):
        tree = _tree(w)
        monkeypatch.chdir(tree)
        have_calls, _ = self._setup(w, tree)
        logs = []
        subtitlefiles.download_srt("Movie.mkv", "en", "", "p", "600", "60",
                                   "yes", logs.append)
        assert have_calls() == []
        assert logs == ["WARNING: openSubtitlesUser/openSubtitlesPassword "
                        "not set, skipping subtitle download"]

    def test_the_full_walk_keeps_a_subtitle_that_aligned(self, w, monkeypatch):
        tree = _tree(w)
        monkeypatch.chdir(tree)
        have_calls, tree = self._setup(
            w, tree, pipx_write="Movie.en.srt", ffprobe="subrip",
            ffsubsync_log=_GOOD_LOG)
        logs = []
        subtitlefiles.download_srt("Movie.mkv", "en", "u", "p", "600", "60",
                                   "yes", logs.append)
        assert logs == ["Downloading en subtitles: Movie.mkv",
                        "Syncing en subtitles: Movie.mkv"]
        assert (tree / "Movie.en.srt").is_file()
        by_tool = {}
        for argv in have_calls():
            by_tool.setdefault(argv[0], []).append(argv)
        assert by_tool["pipx"] == [["pipx", "run", _HELPER, "en", "",
                                    "Movie.mkv"]]
        assert by_tool["ffprobe"] == [["ffprobe", "-v", "error",
                                       "-select_streams", "s:0",
                                       "-show_entries", "stream=codec_name",
                                       "-of", "default=nw=1:nk=1",
                                       "Movie.en.srt"]]
        assert "ffmpeg" not in by_tool
        (sync,) = by_tool["ffsubsync"]
        assert sync[:8] == ["ffsubsync", "Movie.mkv", "-i", "Movie.en.srt",
                            "-o", "Movie.en.srt", "--max-offset-seconds", "600"]
        assert sync[11] == "--log-dir-path"

    def test_a_mislabelled_sidecar_is_converted(self, w, monkeypatch):
        tree = _tree(w)
        monkeypatch.chdir(tree)
        have_calls, tree = self._setup(
            w, tree, pipx_write="Movie.en.srt", ffprobe="webvtt",
            ffmpeg_rc="0", ffmpeg_write="Movie.en.converted.srt",
            ffsubsync_log=_GOOD_LOG)
        logs = []
        subtitlefiles.download_srt("Movie.mkv", "en", "u", "p", "600", "60",
                                   "yes", logs.append)
        assert logs[1] == "Converting en subtitle from webvtt to subrip: Movie.mkv"
        assert (tree / "Movie.en.srt").is_file()
        assert not (tree / "Movie.en.converted.srt").exists()
        (ffmpeg,) = [a for a in have_calls() if a[0] == "ffmpeg"]
        assert ffmpeg == ["ffmpeg", "-y", "-loglevel", "error", "-nostats",
                          "-i", "Movie.en.srt", "Movie.en.converted.srt"]

    def test_a_failed_conversion_is_thrown_away(self, w, monkeypatch):
        tree = _tree(w)
        monkeypatch.chdir(tree)
        have_calls, tree = self._setup(
            w, tree, pipx_write="Movie.en.srt", ffprobe="webvtt", ffmpeg_rc="1",
            ffsubsync_log=_GOOD_LOG)
        logs = []
        subtitlefiles.download_srt("Movie.mkv", "en", "u", "p", "600", "60",
                                   "yes", logs.append)
        assert not (tree / "Movie.en.converted.srt").exists()
        assert (tree / "Movie.en.srt").is_file()
        assert any(a[0] == "ffsubsync" for a in have_calls())

    @pytest.mark.parametrize("rc,log,want,wording", [
        ("1", None, "failed", "WARNING: subtitle sync failed (en), "
                             "discarding: Movie.mkv"),
        ("0", _BAD_LOG, "rejected", "WARNING: subtitle sync rejected as "
                                   "low-quality (en), discarding: Movie.mkv"),
    ])
    def test_a_subtitle_that_cannot_be_synced_is_discarded(self, w, monkeypatch,
                                                           rc, log, want, wording):
        tree = _tree(w)
        monkeypatch.chdir(tree)
        have_calls, tree = self._setup(
            w, tree, pipx_write="Movie.en.srt", ffprobe="subrip",
            ffsubsync_rc=rc, ffsubsync_log=log)
        logs = []
        subtitlefiles.download_srt("Movie.mkv", "en", "u", "p", "600", "60",
                                   "yes", logs.append)
        assert logs[-1] == wording
        assert not (tree / "Movie.en.srt").exists()

    def test_without_ffprobe_nothing_is_converted(self, w, monkeypatch):
        tree = _tree(w)
        monkeypatch.chdir(tree)
        have_calls, tree = self._setup(
            w, tree, have=("pipx", "ffmpeg", "ffsubsync"),
            pipx_write="Movie.en.srt", ffsubsync_log=_GOOD_LOG)
        logs = []
        subtitlefiles.download_srt("Movie.mkv", "en", "u", "p", "600", "60",
                                   "yes", logs.append)
        assert not any(a[0] == "ffmpeg" for a in have_calls())
        assert (tree / "Movie.en.srt").is_file()

    def test_without_ffmpeg_the_converted_file_is_not_left(self, w, monkeypatch):
        tree = _tree(w)
        monkeypatch.chdir(tree)
        have_calls, tree = self._setup(
            w, tree, have=("pipx", "ffprobe", "ffsubsync"),
            pipx_write="Movie.en.srt", ffprobe="webvtt",
            ffsubsync_log=_GOOD_LOG)
        logs = []
        subtitlefiles.download_srt("Movie.mkv", "en", "u", "p", "600", "60",
                                   "yes", logs.append)
        assert logs[1] == "Converting en subtitle from webvtt to subrip: Movie.mkv"
        assert not (tree / "Movie.en.converted.srt").exists()
        assert (tree / "Movie.en.srt").is_file()


class TestFilmImdbId:
    @pytest.mark.parametrize("path,want", [
        ("Movie (1999) {imdb-tt0000003}.mkv", "tt0000003"),
        ("Movie (1999) {IMDB-TT0000003} {edition-Extended}.mkv", "tt0000003"),
        ("Movie (1999) {imdb-tt0000003}/Movie (1999).mkv", "tt0000003"),
        ("Movie (1999)/Movie (1999).mkv", ""),
        ("Movie (1999) {imdb-}.mkv", ""),
    ])
    def test_the_id_is_read_off_the_file_then_its_folder(self, path, want):
        assert subtitlefiles.film_imdb_id(path) == want

    def test_the_files_own_tag_wins_over_its_folders(self):
        assert subtitlefiles.film_imdb_id(
            "F {imdb-tt0000002}/F {imdb-tt0000001}.mkv") == "tt0000001"

    def test_a_tmdb_tag_is_looked_up_for_its_imdb_id(self, monkeypatch):
        asked = []

        def fake(tmdb_id):
            asked.append(tmdb_id)
            return "tt0000003"

        monkeypatch.setattr(subtitlefiles.tmdblookup, "imdb_of_tmdb", fake)
        assert subtitlefiles.film_imdb_id("Movie (1999) {tmdb-12345}.mkv") \
            == "tt0000003"
        assert asked == ["12345"]


class TestTheDownloadCall:
    """What the helper is handed: the film's id, and credentials it reads from
    its environment rather than from an argv every account can read."""

    def _pipx_that_reports_its_environment(self, w):
        report = w.tmp_path / "env"
        pipx = w.bin_dir / "pipx"
        pipx.write_text('#!/bin/bash\nprintf "%s\\n" "$*" '
                        '"$openSubtitlesUser" "$openSubtitlesPassword" > '
                        + str(report) + "\n")
        os.chmod(str(pipx), 0o755)
        return report

    def test_the_id_and_the_credentials(self, w, monkeypatch):
        tree = _tree(w, "Movie (1999) {imdb-tt0000003}.mkv")
        monkeypatch.chdir(tree)
        report = self._pipx_that_reports_its_environment(w)
        subtitlefiles.download_srt("Movie (1999) {imdb-tt0000003}.mkv", "en",
                                   "someone", "secret", "600", "60", "yes",
                                   lambda _line: None)
        argv, user, password = report.read_text().splitlines()
        assert argv == ("run " + _HELPER + " en tt0000003 "
                        "Movie (1999) {imdb-tt0000003}.mkv")
        assert "secret" not in argv
        assert (user, password) == ("someone", "secret")

    def test_an_untagged_film_hands_an_empty_id(self, w, monkeypatch):
        tree = _tree(w, "Movie.mkv")
        monkeypatch.chdir(tree)
        report = self._pipx_that_reports_its_environment(w)
        subtitlefiles.download_srt("Movie.mkv", "nl", "u", "p", "600", "60",
                                   "yes", lambda _line: None)
        argv = report.read_text().splitlines()[0]
        assert argv == "run " + _HELPER + " nl  Movie.mkv"

    def test_the_helper_is_shipped(self):
        assert os.path.isfile(_HELPER)


class TestDownloadSubs:
    def test_every_movie_every_language_and_the_extras_alone(self, w, monkeypatch):
        tree = _tree(w, ("Featurettes", "d"), "Movie.mkv",
                     "Featurettes/Clip.mkv", "Show.mp4")
        monkeypatch.chdir(tree)
        w.install("pipx")
        w.install("ffsubsync")
        w.write("pipx", "Movie.en.srt - - - - -")
        w.rc("ffsubsync", "0")
        w.say("ffsubsync", _GOOD_LOG)
        w.write("ffsubsync", "${--log-dir-path}/ffsubsync.log")
        logs = []
        subtitlefiles.download_subs(str(tree), "u", "p", "600", "60", "yes",
                                    logs.append)
        assert (tree / "Movie.en.srt").is_file()
        assert not (tree / "Featurettes/Clip.en.srt").exists()
        pipx_calls = [a for a in w.calls() if a[0] == "pipx"]
        assert [a[3] for a in pipx_calls] == [
            row.code2 for row in languages.LANGUAGES]
        assert all(a[-1].endswith("/Movie.mkv") for a in pipx_calls)
        assert len(pipx_calls) == len(languages.LANGUAGES)

    @pytest.mark.parametrize("folder", [
        "Others", "Scenes", "Interviews", "Shorts", "Trailers", "Extras",
        "Featurettes",
    ])
    def test_the_extras_words_are_substrings(self, w, monkeypatch, folder):
        tree = _tree(w, (folder, "d"), folder + "/Clip.mkv")
        monkeypatch.chdir(tree)
        w.install("pipx")
        w.write("pipx", "Clip.en.srt")
        w.rc("ffsubsync", "0")
        logs = []
        subtitlefiles.download_subs(str(tree), "u", "p", "600", "60", "yes",
                                    logs.append)
        assert w.calls() == []
        assert logs == []

    @pytest.mark.parametrize("kept", [
        "Movie (old).mkv",
        "Movie {imdb-tt0120737} {edition-Colorized} (old).mkv"])
    def test_the_copy_an_improvement_kept_is_not_downloaded_for(
            self, w, monkeypatch, kept):
        """Its living sibling is getting these same subtitles; a second set
        beside the copy only asks OpenSubtitles for what the folder has. The
        suffix comes last, after every tag, so a copy of an already-tagged film
        is passed over as readily as one made before the tagging."""
        tree = _tree(w, kept)
        monkeypatch.chdir(tree)
        w.install("pipx")
        w.write("pipx", "Movie.en.srt")
        logs = []
        subtitlefiles.download_subs(str(tree), "u", "p", "600", "60", "yes",
                                    logs.append)
        assert w.calls() == []
        assert logs == []

    def test_without_credentials_it_says_so_once_for_the_folder(self, w,
                                                                 monkeypatch):
        """Once, with the count, rather than once per film and language."""
        tree = _tree(w, "One.mkv", "Two.mkv", "Two.en.srt")
        monkeypatch.chdir(tree)
        w.install("pipx")
        logs = []
        subtitlefiles.download_subs(str(tree), "", "", "600", "60", "yes",
                                    logs.append)
        assert w.calls() == []
        missing = 2 * len(languages.LANGUAGES) - 1
        assert logs == ["WARNING: openSubtitlesUser/openSubtitlesPassword "
                        "not set, skipping subtitle download (%d missing)"
                        % missing]

    def test_without_credentials_and_nothing_missing_it_is_silent(
            self, w, monkeypatch):
        tree = _tree(w, "Movie.mkv", *["Movie.%s.srt" % row.code2
                                       for row in languages.LANGUAGES])
        monkeypatch.chdir(tree)
        logs = []
        subtitlefiles.download_subs(str(tree), "", "", "600", "60", "yes",
                                    logs.append)
        assert logs == []

    def test_the_movie_match_is_case_sensitive(self, w, monkeypatch):
        tree = _tree(w, "Movie.MKV")
        monkeypatch.chdir(tree)
        w.install("pipx")
        w.write("pipx", "Movie.en.srt")
        logs = []
        subtitlefiles.download_subs(str(tree), "u", "p", "600", "60", "yes",
                                    logs.append)
        assert w.calls() == []
        assert not (tree / "Movie.en.srt").exists()


class TestCheckSubs:
    """The subtitles already beside a film, put to the test a download is kept
    by. A dry run tests a copy and leaves the sidecar exactly as it was; a real
    one syncs the sidecar in place and throws out the one that was refused. One
    ffsubsync could not align at all is left alone either way."""

    _SRT = "Films/Movie/Movie.en.srt"

    def _library(self, w, monkeypatch, *entries, rc="0", log=_GOOD_LOG,
                 calls=1):
        tree = _tree(w, "Films/Movie/Movie.mkv", *entries)
        monkeypatch.setenv("TMPDIR", str(w.tmp_path))
        w.install("ffsubsync")
        # the stub's lists are consumed one entry per call
        w.rc("ffsubsync", " ".join([rc] * calls))
        w.say("ffsubsync", log)
        w.write("ffsubsync",
                " ".join(["${--log-dir-path}/ffsubsync.log"] * calls))
        return tree

    def _check(self, tree, write):
        logs = []
        verdicts = subtitlefiles.check_subs(str(tree), "600", "60", "yes",
                                            write, logs.append)
        return verdicts, logs

    def _synced(self, w):
        return [argv[3] for argv in w.calls() if argv[0] == "ffsubsync"]

    def test_the_dry_run_tests_a_copy_and_touches_nothing(self, w, monkeypatch):
        tree = self._library(w, monkeypatch, self._SRT, log=_BAD_LOG)
        srt = str(tree / self._SRT)
        (tree / self._SRT).write_text("1\n")
        verdicts, logs = self._check(tree, write=False)
        assert verdicts["discarded"] == [srt]
        assert (tree / self._SRT).read_text() == "1\n"
        (tested,) = self._synced(w)
        assert tested != srt
        assert os.path.basename(tested) == "Movie.en.srt"
        assert not os.path.exists(os.path.dirname(tested))
        assert logs == ["Subtitle out of step (en), would be thrown out: "
                        + srt]

    def test_the_dry_run_says_which_would_be_kept(self, w, monkeypatch):
        tree = self._library(w, monkeypatch, self._SRT)
        srt = str(tree / self._SRT)
        verdicts, logs = self._check(tree, write=False)
        assert verdicts == {"kept": [srt], "discarded": [], "untested": []}
        assert logs == ["Subtitle in step (en), would be kept: " + srt]

    def test_w_syncs_the_one_in_step_in_place(self, w, monkeypatch):
        tree = self._library(w, monkeypatch, self._SRT)
        srt = str(tree / self._SRT)
        verdicts, logs = self._check(tree, write=True)
        assert verdicts["kept"] == [srt]
        assert self._synced(w) == [srt]
        assert (tree / self._SRT).is_file()
        assert logs == ["Subtitle in step (en), synced and kept: " + srt]

    def test_w_throws_out_the_one_out_of_step(self, w, monkeypatch):
        tree = self._library(w, monkeypatch, self._SRT, log=_BAD_LOG)
        srt = str(tree / self._SRT)
        verdicts, logs = self._check(tree, write=True)
        assert verdicts["discarded"] == [srt]
        assert not (tree / self._SRT).exists()
        assert logs == ["WARNING: subtitle sync rejected as low-quality (en), "
                        "discarding: " + srt]

    @pytest.mark.parametrize("write", [False, True])
    def test_one_ffsubsync_could_not_align_is_left_alone(self, w, monkeypatch,
                                                         write):
        """Unlike a download, it may be a subtitle nobody can fetch again, and
        a tool that failed says nothing about whether it is in step."""
        tree = self._library(w, monkeypatch, self._SRT, rc="1")
        verdicts, logs = self._check(tree, write=write)
        assert verdicts["untested"] == [str(tree / self._SRT)]
        assert (tree / self._SRT).is_file()
        assert logs == ["WARNING: subtitle could not be tested (en), left "
                        "alone: " + str(tree / self._SRT)]

    def test_every_language_and_nothing_but_the_movie_s_own_sidecars(
            self, w, monkeypatch):
        """A commentary transcript carries the track after the movie's name,
        and a forced subtitle or one in a language outside the table is not
        one a download would write, so none of them is tested. Nor is a
        subtitle in an extras folder, nor one beside the copy an improvement
        kept."""
        own = ["Films/Movie/Movie.%s.srt" % row.code2
               for row in languages.LANGUAGES]
        tree = self._library(
            w, monkeypatch, *own,
            "Films/Movie/Movie 2 Audio Commentary.en.srt",
            "Films/Movie/Movie.en.forced.srt",
            "Films/Movie/Movie.pt.srt",
            "Films/Movie/Featurettes/Clip.mkv",
            "Films/Movie/Featurettes/Clip.en.srt",
            "Films/Movie/Movie (old).mkv",
            "Films/Movie/Movie (old).en.srt",
            calls=len(own))
        verdicts, _logs = self._check(tree, write=False)
        assert verdicts["kept"] == [str(tree / path) for path in own]
        assert len(self._synced(w)) == len(own)

    def test_the_walk_reaches_a_nested_library(self, w, monkeypatch):
        nested = "Films/Westerns/Classics/Old/Old"
        tree = self._library(w, monkeypatch, nested + ".mkv",
                             nested + ".de.srt")
        verdicts, _logs = self._check(tree, write=False)
        assert verdicts["kept"] == [str(tree / (nested + ".de.srt"))]

    def test_a_linked_sidecar_is_not_tested(self, w, monkeypatch):
        tree = self._library(w, monkeypatch, "elsewhere.srt")
        (tree / self._SRT).symlink_to(tree / "elsewhere.srt")
        verdicts, _logs = self._check(tree, write=True)
        assert verdicts == {"kept": [], "discarded": [], "untested": []}
        assert w.calls() == []

    def _mislabelled(self, w):
        w.install("ffprobe")
        w.say("ffprobe", "webvtt")
        w.install("ffmpeg")
        w.rc("ffmpeg", "0")
        w.write("ffmpeg", "$LAST")

    def test_the_dry_run_converts_only_its_copy_and_says_nothing(
            self, w, monkeypatch):
        tree = self._library(w, monkeypatch, self._SRT)
        (tree / self._SRT).write_text("WEBVTT\n")
        self._mislabelled(w)
        _verdicts, logs = self._check(tree, write=False)
        (ffmpeg,) = [a for a in w.calls() if a[0] == "ffmpeg"]
        assert ffmpeg[6] != str(tree / self._SRT)
        assert (tree / self._SRT).read_text() == "WEBVTT\n"
        assert not any("Converting" in line for line in logs)

    def test_w_converts_the_sidecar_itself(self, w, monkeypatch):
        tree = self._library(w, monkeypatch, self._SRT)
        self._mislabelled(w)
        _verdicts, logs = self._check(tree, write=True)
        (ffmpeg,) = [a for a in w.calls() if a[0] == "ffmpeg"]
        assert ffmpeg[6] == str(tree / self._SRT)
        assert logs[0] == ("Converting en subtitle from webvtt to subrip: "
                           + str(tree / "Films/Movie/Movie.mkv"))