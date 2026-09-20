"""The commentary-only run (-c): the transcription phase, and nothing else.

The full run's commentary phase is a phase of its own - the walk of every
folder given, the track test, the sidecar check and the queue drained over the
whisper workers - and -c exists to run it without the run around it. What is
pinned here is the boundary: the phase runs, per folder and in the order given,
with the run's own whisper settlement and drain, and no other phase of the run
touches the library.
"""

from __future__ import annotations

import os

import pytest

from medialib.cli import ingest_movies_run as run
from medialib.lib import runlog
from medialib.lib import whisper as whisper_lib

pytestmark = pytest.mark.fs


def _library(root, name: str, film: str = "The Movie (1999)"):
    """A folder holding one film, which is what makes it worth walking."""
    folder = root / name / film
    folder.mkdir(parents=True)
    (folder / (film + ".mkv")).touch()
    return root / name


def _never(*_args, **_kwargs):
    raise AssertionError("a phase of the full run ran that -c refuses")


class TestThePhaseRunsAndNothingElse:
    def _stubbed(self, monkeypatch, tmp_path):
        """Everything a run sets up around the phase, stood down - and every
        other phase of the run refused, so a -c that starts the opus pass or
        the remux fails the moment it crosses the line it exists to hold."""
        scratch = tmp_path / "ram"
        scratch.mkdir()
        monkeypatch.setattr(run.ffmpegselect, "select_ffmpeg",
                            lambda: None)
        monkeypatch.setattr(run.ffmpegselect, "report_ffmpeg_selection",
                            lambda: None)
        monkeypatch.setattr(run.tooldeps, "require_tools",
                            lambda *_a, **_k: False)
        monkeypatch.setattr(run, "_settle_subtitle_work", lambda: True)
        monkeypatch.setattr(run, "_settle_ffsubsync_quality",
                            lambda: "yes")
        monkeypatch.setattr(run.ramscratch, "init_ram_base",
                            lambda: None)
        monkeypatch.setattr(run.ramscratch, "ram_scratch_dir",
                            lambda _name: (str(scratch), 0))
        monkeypatch.setattr(run.ramscratch, "add_exit_cleanup",
                            lambda _p: None)
        monkeypatch.setattr(run.ramscratch, "run_exit_cleanup",
                            lambda: None)
        monkeypatch.setattr(run.safety, "trap_run_abort", lambda: None)
        monkeypatch.setattr(run.workerpool, "exit_status",
                            lambda status: status)
        monkeypatch.setattr(run, "log", lambda *a, **k: None)

        settled = {}

        def fake_init(cores, ram_root, log):
            settled["cores"] = cores
            settled["ram_root"] = ram_root
            return {"device": "cpu", "computeType": "int8",
                    "model": "base.en", "modelMulti": "base", "threads": "4",
                    "jobs": whisper_lib.WHISPER_JOBS, "batchSlots": 0}

        monkeypatch.setattr(whisper_lib, "init_whisper_model", fake_init)

        for name in ("cleanup", "mkv_mux", "movies_into_subfolders",
                     "extras_into_subfolders", "update_tags",
                     "rename_folders", "rename_movies"):
            monkeypatch.setattr(run.rules, name, _never)
        for name in ("move_subs", "rename_subs", "download_subs"):
            monkeypatch.setattr(run.subtitlefiles, name, _never)
        monkeypatch.setattr(run.tmdblookup, "tag_plex_ids", _never)
        for name in ("_transcode_opus", "improve_main_movies", "check_folders",
                     "_ingest"):
            monkeypatch.setattr(run, name, _never)

        exports = []

        def export_commentary(directory, read_track_info, is_bonus_folder,
                              rename, audio_stream_index, ram_root, whisper,
                              log, drain_queue, *rest, **kw):
            exports.append({"directory": directory, "whisper": whisper,
                            "drain": drain_queue,
                            "ram_root": ram_root, "rest": rest,
                            "same_commentary_name":
                                kw.get("same_commentary_name")})

        monkeypatch.setattr(run.commentarytranscription,
                            "export_commentary", export_commentary)
        return exports, settled, scratch

    def test_each_folder_is_walked_in_the_order_it_was_typed(
            self, monkeypatch, tmp_path):
        exports, _settled, _scratch = self._stubbed(monkeypatch, tmp_path)
        films = _library(tmp_path, "Films")
        docs = _library(tmp_path, "Documentaries")
        assert run.main(["-c", str(films), str(docs)]) == 0
        assert [entry["directory"] for entry in exports] \
            == [str(films), str(docs)]

    def test_a_folder_with_nothing_in_it_is_walked_not_refused(
            self, monkeypatch, tmp_path):
        """-c is pointed at a library the way -t is: a walk that finds no film
        does nothing, and is not the wrong-path refusal a full ingest gives a
        folder with no movies in it."""
        exports, _settled, _scratch = self._stubbed(monkeypatch, tmp_path)
        empty = tmp_path / "Empty"
        empty.mkdir()
        assert run.main(["-c", str(empty)]) == 0
        assert [entry["directory"] for entry in exports] == [str(empty)]

    def test_the_whisper_settlement_is_done_once_and_handed_to_the_phase(
            self, monkeypatch, tmp_path):
        exports, settled, scratch = self._stubbed(monkeypatch, tmp_path)
        films = _library(tmp_path, "Films")
        assert run.main(["-c", str(films)]) == 0
        # The GPU-to-CPU ladder runs, against the run's RAM scratch, and the
        # settled models are what the phase and its queue are handed.
        assert settled == {"cores": str(runlog.cpu_count()),
                           "ram_root": str(scratch)}
        entry = exports[0]
        assert entry["whisper"]["model"] == "base.en"
        assert callable(entry["drain"])
        # The phase syncs whisper transcripts with the tight offset and this
        # run's ffsubsync answer, and the last thing it is handed is the
        # verdict on an existing sidecar that is too small to keep.
        assert entry["rest"][:2] == (run.rules.MAX_WHISPER_SYNC_OFFSET,
                                     "yes")
        assert callable(entry["rest"][2])

    def test_the_drain_transcribes_with_this_run_s_settings(
            self, monkeypatch, tmp_path):
        exports, _settled, scratch = self._stubbed(monkeypatch, tmp_path)
        films = _library(tmp_path, "Films")
        assert run.main(["-c", str(films)]) == 0

        seen = []
        monkeypatch.setattr(run.commentarytranscription,
                            "transcribe_commentary",
                            lambda record, *a: seen.append((record, a)))
        # The width is whisper's, so the dynamic queue is what runs the
        # records: run them in this process, one at a time, to see what a
        # worker would get.
        def fake_queue(producer, jobs, target, arguments, log=None):
            assert jobs == whisper_lib.WHISPER_JOBS
            for record, _size in producer:
                target(*arguments(record))

        monkeypatch.setattr(run.dynamicqueue, "run", fake_queue)
        exports[0]["drain"](iter([("record", 0)]))
        assert [record for record, _args in seen] == ["record"]
        whisper, offset, quality, ram_root, _log = seen[0][1]
        assert whisper["model"] == "base.en"
        assert offset == run.rules.MAX_WHISPER_SYNC_OFFSET
        assert quality == "yes"
        assert ram_root == str(scratch)

    def test_the_discarder_is_handed_to_the_phase_to_judge_sidecars(
            self, monkeypatch, tmp_path):
        """The verdict on an existing sidecar is the run's own: the phase is
        handed a callable that decides, by the sidecar's size, whether to keep
        it or discard and re-transcribe."""
        exports, _settled, _scratch = self._stubbed(monkeypatch, tmp_path)
        films = _library(tmp_path, "Films")
        assert run.main(["-c", str(films)]) == 0
        discard_existing = exports[0]["rest"][2]
        seen = {}

        def fake_too_small(prefix, movie, durations):
            seen["prefix"] = prefix
            seen["movie"] = movie
            return True

        monkeypatch.setattr(run, "_sidecar_too_small", fake_too_small)
        assert discard_existing("P ", "M") is True
        assert seen["prefix"] == "P "
        assert seen["movie"] == "M"

    def test_the_name_rule_is_handed_to_the_phase_to_renumber_stale_transcripts(
            self, monkeypatch, tmp_path):
        """A transcript an older run numbered for a track that no longer stands
        where it numbered it is renumbered rather than transcribed again, and
        whether a name names the commentary is the run's own rule, handed down
        to the phase."""
        exports, _settled, _scratch = self._stubbed(monkeypatch, tmp_path)
        films = _library(tmp_path, "Films")
        assert run.main(["-c", str(films)]) == 0
        assert exports[0]["same_commentary_name"] is run.rules._same_commentary

    def test_without_ffsubsync_and_pipx_the_warning_is_the_run(
            self, monkeypatch, tmp_path):
        """There is nothing else in this run to fall back to, so the gate's
        warning is the whole of it: nothing is walked and nothing is queued."""
        _exports, _settled, _scratch = self._stubbed(monkeypatch, tmp_path)
        monkeypatch.setattr(run, "_settle_subtitle_work", lambda: False)
        exports = []

        def no_export(*_a, **_k):
            exports.append(1)

        monkeypatch.setattr(run.commentarytranscription,
                            "export_commentary", no_export)
        films = _library(tmp_path, "Films")
        assert run.main(["-c", str(films)]) == 0
        assert exports == []


class TestTheCombosItRefuses:
    def test_w_combined_with_c_is_refused(self, monkeypatch, tmp_path, capsys):
        """-w turns the dry run of -t or -i into real renames, and -c is the
        transcription, not a dry run, so there is nothing for -w to carry out."""
        _library(tmp_path, "Films")
        assert run.main(["-c", "-w", str(tmp_path / "Films")]) == 1
        errors = capsys.readouterr().err
        assert "-w has no part in -c" in errors
        assert "Nothing was changed." in errors

    def test_t_combined_with_c_is_refused(self, monkeypatch, tmp_path,
                                          capsys):
        """Two phases asked of the same folders at once would have to guess an
        order, and neither of them is answerable for the other's names."""
        _library(tmp_path, "Films")
        assert run.main(["-c", "-t", str(tmp_path / "Films")]) == 1
        errors = capsys.readouterr().err
        assert "two runs, not one" in errors
        assert "Nothing was changed." in errors

    def test_i_combined_with_c_is_refused(self, monkeypatch, tmp_path, capsys):
        _library(tmp_path, "Films")
        assert run.main(["-c", "-i", str(tmp_path / "nope.tsv"),
                         str(tmp_path / "Films")]) == 1
        errors = capsys.readouterr().err
        assert "two runs, not one" in errors
        assert "Nothing was changed." in errors


class TestTheTooSmallSidecar:
    """A transcript already beside a film is skipped - unless it is too small to
    be the film's real transcript. The size is judged against the film's length:
    a real one runs to the order of a kilobyte per minute, and a sidecar under
    half of that is the one an older run wrote forcing a non-English commentary
    through the English model."""

    def _folder(self, tmp_path, film="The Movie (1999)"):
        folder = tmp_path / film
        folder.mkdir(parents=True)
        prefix = str(folder) + os.sep + film + " 0 "
        movie = str(folder / (film + ".mkv"))
        return prefix, movie, folder

    def _write(self, folder, name, size):
        (folder / name).write_bytes(b"x" * size)

    def test_well_under_half_a_kilobyte_per_minute_is_too_small(
            self, monkeypatch, tmp_path):
        prefix, movie, folder = self._folder(tmp_path)
        self._write(folder, "The Movie (1999) 0 Commentary.en.srt", 100)
        monkeypatch.setattr(run.rules, "_duration_of", lambda _m: 100 * 60)
        assert run._sidecar_too_small(prefix, movie, {}) is True

    def test_at_the_order_of_a_kilobyte_per_minute_is_kept(
            self, monkeypatch, tmp_path):
        prefix, movie, folder = self._folder(tmp_path)
        self._write(folder, "The Movie (1999) 0 Commentary.en.srt",
                    100 * 1024)
        monkeypatch.setattr(run.rules, "_duration_of", lambda _m: 100 * 60)
        assert run._sidecar_too_small(prefix, movie, {}) is False

    def test_the_two_language_sidecars_are_judged_together(
            self, monkeypatch, tmp_path):
        """A healthy supported-language commentary is two files, and their sum
        is what clears the bar."""
        prefix, movie, folder = self._folder(tmp_path)
        self._write(folder, "The Movie (1999) 0 Commentary.de.srt", 30 * 1024)
        self._write(folder, "The Movie (1999) 0 Commentary.en.srt", 30 * 1024)
        monkeypatch.setattr(run.rules, "_duration_of", lambda _m: 100 * 60)
        # 60 KB for 100 minutes clears the 50 KB bar.
        assert run._sidecar_too_small(prefix, movie, {}) is False

    def test_but_two_that_come_in_short_together_are_not(
            self, monkeypatch, tmp_path):
        prefix, movie, folder = self._folder(tmp_path)
        self._write(folder, "The Movie (1999) 0 Commentary.de.srt", 20 * 1024)
        self._write(folder, "The Movie (1999) 0 Commentary.en.srt", 20 * 1024)
        monkeypatch.setattr(run.rules, "_duration_of", lambda _m: 100 * 60)
        # 40 KB for 100 minutes is under the 50 KB bar.
        assert run._sidecar_too_small(prefix, movie, {}) is True

    def test_without_any_sidecar_there_is_nothing_to_discard(
            self, monkeypatch, tmp_path):
        prefix, movie, _folder = self._folder(tmp_path)
        monkeypatch.setattr(run.rules, "_duration_of", lambda _m: 100 * 60)
        assert run._sidecar_too_small(prefix, movie, {}) is False

    def test_a_film_whose_length_cannot_be_read_is_left_alone(
            self, monkeypatch, tmp_path):
        """An existing transcript is not thrown away over a length that is not
        there."""
        prefix, movie, folder = self._folder(tmp_path)
        self._write(folder, "The Movie (1999) 0 Commentary.en.srt", 100)
        monkeypatch.setattr(run.rules, "_duration_of", lambda _m: 0)
        assert run._sidecar_too_small(prefix, movie, {}) is False

    def test_the_film_s_length_is_read_once_and_not_per_track(
            self, monkeypatch, tmp_path):
        prefix, movie, folder = self._folder(tmp_path)
        self._write(folder, "The Movie (1999) 0 Commentary.en.srt", 100)
        reads = []

        def duration(_m):
            reads.append(1)
            return 100 * 60

        monkeypatch.setattr(run.rules, "_duration_of", duration)
        cache = {}
        assert run._sidecar_too_small(prefix, movie, cache) is True
        assert run._sidecar_too_small(prefix, movie, cache) is True
        assert reads == [1]
        assert movie in cache


class TestTheOrphanReport:
    """The transcripts the walk finds that name a track their film no longer
    numbers a commentary for. The -c run is the one that collects them and
    leaves them in a file, one absolute path per line, and it leaves no file
    at all for a run that found nothing."""

    def _stubbed(self, monkeypatch, tmp_path, export_commentary):
        script_dir = tmp_path / "script"
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(script_dir))
        scratch = tmp_path / "ram"
        scratch.mkdir()
        monkeypatch.setattr(run.ffmpegselect, "select_ffmpeg",
                            lambda: None)
        monkeypatch.setattr(run.ffmpegselect, "report_ffmpeg_selection",
                            lambda: None)
        monkeypatch.setattr(run.tooldeps, "require_tools",
                            lambda *_a, **_k: False)
        monkeypatch.setattr(run, "_settle_subtitle_work", lambda: True)
        monkeypatch.setattr(run, "_settle_ffsubsync_quality",
                            lambda: "yes")
        monkeypatch.setattr(run.ramscratch, "init_ram_base",
                            lambda: None)
        monkeypatch.setattr(run.ramscratch, "ram_scratch_dir",
                            lambda _name: (str(scratch), 0))
        monkeypatch.setattr(run.ramscratch, "add_exit_cleanup",
                            lambda _p: None)
        monkeypatch.setattr(run.ramscratch, "run_exit_cleanup",
                            lambda: None)
        monkeypatch.setattr(run.safety, "trap_run_abort", lambda: None)
        monkeypatch.setattr(run.workerpool, "exit_status",
                            lambda status: status)
        monkeypatch.setattr(run, "log", lambda *a, **k: None)

        def fake_init(cores, ram_root, log):
            return {"device": "cpu", "computeType": "int8",
                    "model": "base.en", "modelMulti": "base", "threads": "4",
                    "jobs": whisper_lib.WHISPER_JOBS, "batchSlots": 0}

        monkeypatch.setattr(whisper_lib, "init_whisper_model", fake_init)
        for name in ("_transcode_opus", "improve_main_movies", "check_folders",
                     "_ingest"):
            monkeypatch.setattr(run, name, _never)
        monkeypatch.setattr(run.commentarytranscription,
                            "export_commentary", export_commentary)
        return script_dir

    def test_a_run_that_found_nothing_writes_no_file(self, tmp_path):
        run._write_commentary_orphans(str(tmp_path), [])
        assert not (tmp_path / "logs" / "commentaryOrphans.txt").exists()

    def test_the_orphans_are_written_one_absolute_path_per_line(
            self, tmp_path, monkeypatch):
        logs = []
        monkeypatch.setattr(run, "log", logs.append)
        run._write_commentary_orphans(
            str(tmp_path),
            ["/root/Films/The Movie (1999)/The Movie (1999) 5 "
             "OldCommentary.en.srt"])
        report = tmp_path / "logs" / "commentaryOrphans.txt"
        assert report.read_text() == \
            "/root/Films/The Movie (1999)/The Movie (1999) 5 " \
            "OldCommentary.en.srt\n"
        assert any("commentaryOrphans.txt" in line for line in logs)

    def test_the_run_leaves_the_orphans_it_found_in_a_file(
            self, monkeypatch, tmp_path):
        """The walk collects the transcript, and the run hands it to the
        report with the folder it stood beside made absolute."""
        def export_commentary(directory, read_track_info, is_bonus_folder,
                              rename, audio_stream_index, ram_root, whisper,
                              log, drain_queue, *rest, **kw):
            orphans = kw.get("orphans")
            if orphans is not None:
                orphans.append(
                    "./The Movie (1999)/The Movie (1999) 5 "
                    "OldCommentary.en.srt")

        script_dir = self._stubbed(monkeypatch, tmp_path, export_commentary)
        films = _library(tmp_path, "Films")
        assert run.main(["-c", str(films)]) == 0
        report = script_dir / "logs" / "commentaryOrphans.txt"
        expected = str(films / "The Movie (1999)" /
                       "The Movie (1999) 5 OldCommentary.en.srt") + "\n"
        assert report.read_text() == expected

    def test_the_run_leaves_no_file_when_the_walk_found_nothing(
            self, monkeypatch, tmp_path):
        def export_commentary(directory, read_track_info, is_bonus_folder,
                              rename, audio_stream_index, ram_root, whisper,
                              log, drain_queue, *rest, **kw):
            return

        script_dir = self._stubbed(monkeypatch, tmp_path, export_commentary)
        films = _library(tmp_path, "Films")
        assert run.main(["-c", str(films)]) == 0
        assert not (script_dir / "logs" / "commentaryOrphans.txt").exists()


class TestTheUnfixedReport:
    """The movies the walk left untranscribed because their name carried no dot
    before its extension. The -c run is the one that collects them and leaves
    them in a file, one absolute path per line, and it leaves no file at all for
    a run that found nothing. A mangled name the folder's spelling does answer
    is conformed before the walk, so it is transcribed and not reported."""

    def _stubbed(self, monkeypatch, tmp_path, export_commentary):
        script_dir = tmp_path / "script"
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(script_dir))
        scratch = tmp_path / "ram"
        scratch.mkdir()
        monkeypatch.setattr(run.ffmpegselect, "select_ffmpeg",
                            lambda: None)
        monkeypatch.setattr(run.ffmpegselect, "report_ffmpeg_selection",
                            lambda: None)
        monkeypatch.setattr(run.tooldeps, "require_tools",
                            lambda *_a, **_k: False)
        monkeypatch.setattr(run, "_settle_subtitle_work", lambda: True)
        monkeypatch.setattr(run, "_settle_ffsubsync_quality",
                            lambda: "yes")
        monkeypatch.setattr(run.ramscratch, "init_ram_base",
                            lambda: None)
        monkeypatch.setattr(run.ramscratch, "ram_scratch_dir",
                            lambda _name: (str(scratch), 0))
        monkeypatch.setattr(run.ramscratch, "add_exit_cleanup",
                            lambda _p: None)
        monkeypatch.setattr(run.ramscratch, "run_exit_cleanup",
                            lambda: None)
        monkeypatch.setattr(run.safety, "trap_run_abort", lambda: None)
        monkeypatch.setattr(run.workerpool, "exit_status",
                            lambda status: status)
        monkeypatch.setattr(run, "log", lambda *a, **k: None)

        def fake_init(cores, ram_root, log):
            return {"device": "cpu", "computeType": "int8",
                    "model": "base.en", "modelMulti": "base", "threads": "4",
                    "jobs": whisper_lib.WHISPER_JOBS, "batchSlots": 0}

        monkeypatch.setattr(whisper_lib, "init_whisper_model", fake_init)
        for name in ("_transcode_opus", "improve_main_movies", "check_folders",
                     "_ingest"):
            monkeypatch.setattr(run, name, _never)
        monkeypatch.setattr(run.commentarytranscription,
                            "export_commentary", export_commentary)
        return script_dir

    def test_a_run_that_found_nothing_writes_no_file(self, tmp_path):
        run._write_unfixed_movies(str(tmp_path), [])
        assert not (tmp_path / "logs" / "unfixedMovies.txt").exists()

    def test_the_unfixed_movies_are_written_one_absolute_path_per_line(
            self, tmp_path, monkeypatch):
        logs = []
        monkeypatch.setattr(run, "log", logs.append)
        run._write_unfixed_movies(
            str(tmp_path),
            ["/root/Films/Hollow Ridge (1981) {imdb-tt0000001}/"
             "Hollow Ridge (1981) {imdb-tt0000001} 1981mkv"])
        report = tmp_path / "logs" / "unfixedMovies.txt"
        assert report.read_text() == \
            "/root/Films/Hollow Ridge (1981) {imdb-tt0000001}/" \
            "Hollow Ridge (1981) {imdb-tt0000001} 1981mkv\n"
        assert any("unfixedMovies.txt" in line for line in logs)

    def test_the_run_leaves_the_unfixed_movies_it_found_in_a_file(
            self, monkeypatch, tmp_path):
        """The walk collects the movie, and the run hands it to the report with
        the folder it stood beside made absolute."""
        def export_commentary(directory, read_track_info, is_bonus_folder,
                              rename, audio_stream_index, ram_root, whisper,
                              log, drain_queue, *rest, **kw):
            unfixed = kw.get("unfixed")
            if unfixed is not None:
                unfixed.append(
                    "./Hollow Ridge (1981) {imdb-tt0000001}/"
                    "Hollow Ridge (1981) {imdb-tt0000001} 1981mkv")

        script_dir = self._stubbed(monkeypatch, tmp_path, export_commentary)
        films = _library(tmp_path, "Films")
        assert run.main(["-c", str(films)]) == 0
        report = script_dir / "logs" / "unfixedMovies.txt"
        expected = (str(films / "Hollow Ridge (1981) {imdb-tt0000001}" /
                        "Hollow Ridge (1981) {imdb-tt0000001} 1981mkv")
                    + "\n")
        assert report.read_text() == expected

    def test_the_run_leaves_no_file_when_the_walk_found_nothing(
            self, monkeypatch, tmp_path):
        def export_commentary(directory, read_track_info, is_bonus_folder,
                              rename, audio_stream_index, ram_root, whisper,
                              log, drain_queue, *rest, **kw):
            return

        script_dir = self._stubbed(monkeypatch, tmp_path, export_commentary)
        films = _library(tmp_path, "Films")
        assert run.main(["-c", str(films)]) == 0
        assert not (script_dir / "logs" / "unfixedMovies.txt").exists()

    def test_a_mangled_name_the_folder_answers_is_conformed_not_reported(
            self, monkeypatch, tmp_path):
        """The conform runs before the walk reads the tracks, so a mangled name
        the folder's spelling answers is put right on disk and never reaches the
        report - what is reported is only what the conform could not answer."""
        def export_commentary(directory, read_track_info, is_bonus_folder,
                              rename, audio_stream_index, ram_root, whisper,
                              log, drain_queue, *rest, **kw):
            return

        script_dir = self._stubbed(monkeypatch, tmp_path, export_commentary)
        folder = (tmp_path / "Films" / "Hollow Ridge (1981) {imdb-tt0000001}")
        folder.mkdir(parents=True)
        mangled = folder / "Hollow Ridge (1981) {imdb-tt0000001} 1981mkv"
        mangled.touch()
        assert run.main(["-c", str(tmp_path / "Films")]) == 0
        assert not mangled.exists()
        assert (folder / "Hollow Ridge (1981) {imdb-tt0000001}.mkv").is_file()
        assert not (script_dir / "logs" / "unfixedMovies.txt").exists()
