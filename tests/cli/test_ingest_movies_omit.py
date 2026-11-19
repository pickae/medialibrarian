"""The -o phases: what a full ingest is told to leave out.

Read once at the run's start: a phase the run will not omit is refused
with the page, like a bad option, and the phases it leaves out are what
the run is handed - a bare -o omitting them all, the way a bare -k keeps
them all. What each omitted phase costs is pinned in TestTheOmittedPhase:
the phase is not done, and is said so at its place, the way a phase
skipped for want of its tool is.
"""

from __future__ import annotations

import threading

import pytest

from medialib.cli import ingest_movies_lanes as lanes
from medialib.cli import ingest_movies_run as run
from medialib.lib import clioptions, tmdblookup

pytestmark = pytest.mark.fs


def _library(root, name: str, film: str = "The Movie (1999)"):
    """A folder holding one film, which is what makes it worth ingesting."""
    folder = root / name / film
    folder.mkdir(parents=True)
    (folder / (film + ".mkv")).touch()
    return root / name


class TestTheRefusal:
    """A phase the run will not omit refuses the run at its start, before
    a film is touched - like a bad option, with the page and the trailer."""

    def test_a_phase_the_run_will_not_omit_is_refused(
            self, tmp_path, capsys):
        (tmp_path / "Films").mkdir()
        assert run.main(["--omit=bogus", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert 'Option --omit does not take "bogus"' in errors
        assert "Nothing was changed." in errors

    def test_and_a_list_with_an_unknown_phase_in_it(self, tmp_path, capsys):
        (tmp_path / "Films").mkdir()
        assert run.main(["--omit=junk,bogus", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert 'Option --omit does not take "junk,bogus"' in errors
        assert "Nothing was changed." in errors


class TestTheOption:
    """The parser's arm: a word that is a phase goes to -o, and a folder
    that is not one stays a folder."""

    def _parse(self, argv):
        return clioptions.parse(run.rules.spec("ingest-movies"), argv)

    def test_a_bare_flag_is_a_bare_flag(self):
        result = self._parse(["-o"])
        assert "o" in result.given
        assert result.values["omitPhases"] == ""
        assert result.positionals == []

    def test_named_phases_stay_with_the_flag(self):
        result = self._parse(["-o", "junk,chapters", "/x"])
        assert result.values["omitPhases"] == "junk,chapters"
        assert result.positionals == ["/x"]

    def test_the_long_form_is_the_same(self):
        result = self._parse(["--omit=plex", "/x"])
        assert result.values["omitPhases"] == "plex"
        assert result.positionals == ["/x"]

    def test_a_folder_that_is_not_a_phase_stays_a_folder(self):
        result = self._parse(["-o", "/x"])
        assert result.values["omitPhases"] == ""
        assert result.positionals == ["/x"]

    def test_a_word_that_is_a_phase_goes_to_the_flag(self):
        """A folder literally named after one of them - "plex" is the
        likeliest - is written ./plex: the word after -o goes to -o when it
        is a phase."""
        result = self._parse(["-o", "plex"])
        assert result.values["omitPhases"] == "plex"
        assert result.positionals == []
        result = self._parse(["-o", "./plex"])
        assert result.values["omitPhases"] == ""
        assert result.positionals == ["./plex"]

    def test_the_flag_clings_to_the_next_letter(self):
        result = self._parse(["-ov", "/x"])
        assert "o" in result.given
        assert "v" in result.given
        assert result.values["omitPhases"] == ""
        assert result.positionals == ["/x"]


class TestTheRunThatOmits:
    """The full run's state carries the phases, so every phase entry the
    ingest does is guarded by them."""

    def _stubbed(self, monkeypatch, tmp_path):
        """Everything a full run sets up around the ingest itself, stood
        down: what is under test is the state the ingest is handed."""
        scratch = tmp_path / "ram"
        scratch.mkdir()
        monkeypatch.setattr(run.ffmpegselect, "select_ffmpeg", lambda: None)
        monkeypatch.setattr(run.ffmpegselect, "report_ffmpeg_selection",
                            lambda: None)
        monkeypatch.setattr(run.tooldeps, "require_tools",
                            lambda *_a, **_k: False)
        monkeypatch.setattr(run, "_settle_subtitle_work", lambda: False)
        monkeypatch.setattr(run, "_settle_ffsubsync_quality", lambda: "")
        monkeypatch.setattr(run.ramscratch, "init_ram_base", lambda: None)
        monkeypatch.setattr(run.ramscratch, "ram_scratch_dir",
                            lambda _name: (str(scratch), 0))
        monkeypatch.setattr(run.ramscratch, "add_exit_cleanup", lambda _p: None)
        monkeypatch.setattr(run.ramscratch, "run_exit_cleanup", lambda: None)
        monkeypatch.setattr(run.safety, "trap_run_abort", lambda: None)
        monkeypatch.setattr(run.workerpool, "exit_status",
                            lambda status: status)
        states = []
        monkeypatch.setattr(run, "_ingest",
                            lambda _state, _root, _subs, _name:
                            states.append(_state))
        return states

    def test_the_run_carries_the_named_phases(self, monkeypatch, tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        assert run.main(["-o", "junk,chapters", str(tmp_path / "Films")]) == 0
        assert states[0].omit_phases == frozenset({"junk", "chapters"})

    def test_a_bare_flag_omits_them_all(self, monkeypatch, tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        assert run.main(["-o", str(tmp_path / "Films")]) == 0
        assert states[0].omit_phases == frozenset(run.rules.OMIT_PHASES)

    def test_without_the_flag_the_run_does_every_phase(
            self, monkeypatch, tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        assert run.main([str(tmp_path / "Films")]) == 0
        assert states[0].omit_phases == frozenset()


class TestTheSweepRefusals:
    """Each sweep does its phase alone: there is no full ingest here to
    leave a phase out of."""




class TestTheOmittedPhase:
    """What an omitted phase costs: it is not done, and it is said so at
    its place, the way a phase skipped for want of its tool is."""

    def _ingested(self, monkeypatch, omit=frozenset(), subtitle_work=True):
        """One ingest with every phase stubbed out, reporting which phases
        ran and what the run said of the ones it did not."""
        seen = []

        def note(name):
            def _note(*_a, **_k):
                seen.append(name)
            return _note

        for name in ("cleanup", "mkv_mux", "movies_into_subfolders",
                     "extras_into_subfolders", "update_tags",
                     "conform_movie_names"):
            monkeypatch.setattr(run.rules, name, note(name))
        for name in ("rename_folders", "rename_movies"):
            monkeypatch.setattr(run.rules, name, lambda *a, **k: 0)
        monkeypatch.setattr(
            run.rules, "update_tags",
            lambda _root, roles=True: seen.append(
                "update_tags" if roles else "update_tags without roles"))
        monkeypatch.setattr(run.safety, "lower_case_extensions",
                            note("lower"))
        for name in ("move_subs", "rename_subs"):
            monkeypatch.setattr(run.subtitlefiles, name, note(name))
        monkeypatch.setattr(run.subtitlefiles, "download_subs",
                            note("download_subs"))
        monkeypatch.setattr(run.tmdblookup, "tag_plex_ids", note("tag"))
        lane_calls = []
        research_calls = []
        for name in ("_detect_commentaries", "_start_subtitle_lane",
                     "_research_again", "_commentary_name_phase",
                     "_chapter_phase", "check_folders"):
            monkeypatch.setattr(run, name, note(name))
        monkeypatch.setattr(
            run, "_start_research",
            lambda _state, _root, _omit: research_calls.append(_omit) or {})
        monkeypatch.setattr(
            run, "_films_in_lanes",
            lambda *a, **k: lane_calls.append(k))
        monkeypatch.setattr(run.plexphase, "compare_roots", note("plex"))
        monkeypatch.setattr(run, "_has_tool", lambda _name: False)
        monkeypatch.delenv("tmdbApiKey", raising=False)
        lines = []
        monkeypatch.setattr(
            run, "log",
            lambda *a, **k: lines.append(" ".join(str(x) for x in a)))
        state = run.Run(script_dir="", ram_root="", skips=None,
                        fragments_file="",
                        whisper={"jobs": 1},
                        whisper_said=[], ffsubsync_quality="yes",
                        long_names=tmdblookup.LongNames(),
                        unfixed_movies=[], omit_phases=omit)
        run._ingest(state, "/x", subtitle_work, "x")
        return seen, lines, lane_calls, research_calls

    def test_without_the_flag_every_phase_is_done(self, monkeypatch):
        seen, lines, lane_calls, _research = self._ingested(monkeypatch)
        assert seen.count("cleanup") == 3
        assert "_start_subtitle_lane" in seen
        assert "download_subs" in seen
        assert "_detect_commentaries" in seen
        assert "_commentary_name_phase" in seen
        assert "_chapter_phase" in seen
        assert "tag" in seen
        assert "plex" not in seen
        # No curl to compare with: the phase reached its own gate, which is
        # what distinguishes it from being left out.
        assert "comparing with the Plex library - SKIPPED (curl is not " \
            "installed)" in " ".join(lines)
        assert lane_calls and lane_calls[0]["commentary_work"] is True
        assert "OMITTED" not in " ".join(lines)

    def test_omitted_junk_is_left(self, monkeypatch):
        seen, lines, _lane, _research = self._ingested(
            monkeypatch, omit=frozenset({"junk"}))
        assert "cleanup" not in seen
        assert "cleaning up junk files - OMITTED (-o junk)" in " ".join(lines)

    def test_omitted_subtitles_are_not_fetched_or_downloaded(self,
                                                             monkeypatch):
        seen, lines, _lane, _research = self._ingested(
            monkeypatch, omit=frozenset({"subtitles"}))
        assert "_start_subtitle_lane" not in seen
        assert "download_subs" not in seen
        # Moving and renaming what is already beside the films is not
        # fetching: it is not in the phase.
        assert "move_subs" in seen
        assert "rename_subs" in seen
        joined = " ".join(lines)
        assert "Subtitles: not fetched (-o subtitles)" in joined
        assert "downloading missing subtitles - OMITTED (-o subtitles)" \
            in joined

    def test_omitted_roles_are_not_measured(self, monkeypatch):
        seen, lines, _lane, _research = self._ingested(
            monkeypatch, omit=frozenset({"roles"}))
        assert "update_tags without roles" in seen
        assert "update_tags" not in seen
        assert "refreshing mkv tags and track flags - subtitle roles " \
            "OMITTED (-o roles)" in " ".join(lines)

    def test_omitted_commentary_is_not_listened_for_or_transcribed(
            self, monkeypatch):
        seen, lines, lane_calls, _research = self._ingested(
            monkeypatch, omit=frozenset({"commentary"}))
        assert "_detect_commentaries" not in seen
        assert "_commentary_name_phase" not in seen
        assert lane_calls and lane_calls[0]["commentary_work"] is False
        assert "listening for commentaries the files do not flag - " \
            "OMITTED (-o commentary)" in " ".join(lines)

    def test_omitted_chapters_are_not_asked_of(self, monkeypatch):
        seen, lines, _lane, research_calls = self._ingested(
            monkeypatch, omit=frozenset({"chapters"}))
        assert "_chapter_phase" not in seen
        assert research_calls \
            and "chapters" in research_calls[0]
        assert "looking up chapters for films without named ones - " \
            "OMITTED (-o chapters)" in " ".join(lines)

    def test_omitted_plex_is_not_compared_with_or_carried_from(
            self, monkeypatch):
        seen, lines, _lane, _research = self._ingested(
            monkeypatch, omit=frozenset({"plex"}))
        assert "plex" not in seen
        joined = " ".join(lines)
        assert "comparing with the Plex library - OMITTED (-o plex)" in joined
        assert "settling what each film carries over from the Plex " \
            "library's copy - OMITTED (-o plex)" in joined

    def test_omitted_tagging_is_not_tagged(self, monkeypatch):
        seen, lines, _lane, _research = self._ingested(
            monkeypatch, omit=frozenset({"tagging"}))
        assert "tag" not in seen
        assert "tagging movies with IMDb ids (Plex/Jellyfin naming) - " \
            "OMITTED (-o tagging)" in " ".join(lines)

    def test_a_bare_flag_leaves_out_every_phase(self, monkeypatch):
        seen, lines, lane_calls, _research = self._ingested(
            monkeypatch, omit=frozenset(run.rules.OMIT_PHASES))
        for name in ("cleanup", "_start_subtitle_lane", "download_subs",
                     "_detect_commentaries", "_commentary_name_phase",
                     "_chapter_phase", "tag", "plex"):
            assert name not in seen
        assert lane_calls and lane_calls[0]["commentary_work"] is False
        joined = " ".join(lines)
        for part in ("junk", "subtitles", "commentary", "chapters", "plex",
                     "tagging"):
            assert "OMITTED (-o %s)" % part in joined


class TestTheResearchOmissions:
    """The thread of an omitted phase is not started: its research is not
    built, and the asking again reaches only the research there is."""

    def _started(self, monkeypatch, omit=frozenset()):
        built = []

        class Research:
            def __init__(self, name, _ask, keep=None):
                self.name = name
                built.append(name)

            def ahead(self, _work):
                pass

        monkeypatch.setattr(run.research, "Research", Research)
        monkeypatch.setattr(run, "_has_tool", lambda _name: True)
        state = run.Run(script_dir="")
        ahead = run._start_research(state, "/x", omit)
        return built, ahead

    def test_without_the_flag_every_research_is_built(self, monkeypatch):
        built, ahead = self._started(monkeypatch)
        assert built == ["chapter", "commentary name"]
        assert set(ahead) == {"chapters", "chapter pages", "names", "said"}

    def test_omitted_chapters_leave_the_disc_database_asked(self,
                                                            monkeypatch):
        built, ahead = self._started(
            monkeypatch, frozenset({"chapters"}))
        assert built == ["commentary name"]
        assert set(ahead) == {"names", "said"}

    def test_omitted_commentary_leaves_the_chapter_archive_asked(
            self, monkeypatch):
        built, ahead = self._started(
            monkeypatch, frozenset({"commentary"}))
        assert built == ["chapter"]
        assert set(ahead) == {"chapters", "chapter pages"}

    def test_everything_omitted_builds_neither(self, monkeypatch):
        built, ahead = self._started(
            monkeypatch, frozenset(run.rules.OMIT_PHASES))
        assert built == []
        assert ahead == {}

    def test_without_curl_the_omitted_phases_change_nothing(self,
                                                            monkeypatch):
        monkeypatch.setattr(run, "_has_tool", lambda _name: False)
        state = run.Run(script_dir="")
        assert run._start_research(
            state, "/x", frozenset(run.rules.OMIT_PHASES)) == {}


class TestTheLanePlacement:
    """The transcription decides where the opus transcode sits: a run told
    to omit the commentary is not one that transcribes, so the transcode
    goes first, even when whisper is on the GPU."""

    def test_the_omitted_commentary_does_not_move_the_transcode(
            self, monkeypatch, tmp_path):
        threads = {}

        def transcode(state, root, jobs=None, on_done=None,
                      exit_on_abort=True, row=None):
            threads["opus"] = threading.current_thread()
            threads["jobs"] = jobs

        monkeypatch.setattr(run, "_transcode_lossless", transcode)
        monkeypatch.setattr(run, "main_movies", lambda root: [])
        monkeypatch.setattr(run, "improve_main_movies",
                            lambda *a, **k: None)
        lines = []
        monkeypatch.setattr(
            run, "log", lambda *a, **k: lines.append(" ".join(
                str(x) for x in a)))
        state = run.Run(whisper={"device": "cuda", "jobs": 1})
        run._films_in_lanes(state, str(tmp_path), True,
                            lanes.FilmLocks(), commentary_work=False)
        assert threads["opus"] is threading.main_thread()
        assert threads["jobs"] is None
        joined = " ".join(lines)
        assert "beside the commentary transcription" not in joined
        assert "extracting and transcribing commentary tracks - " \
            "OMITTED (-o commentary)" in joined

    def test_the_transcription_is_the_one_that_moves_it(self, monkeypatch,
                                                        tmp_path):
        threads = {}

        def transcode(state, root, jobs=None, on_done=None,
                      exit_on_abort=True, row=None):
            threads["opus"] = threading.current_thread()

        def export_commentary(directory, *args, film_queued=None, **_kw):
            threads["whisper"] = threading.current_thread()

        monkeypatch.setattr(run, "_transcode_lossless", transcode)
        monkeypatch.setattr(run.commentarytranscription,
                            "export_commentary", export_commentary)
        monkeypatch.setattr(run, "main_movies", lambda root: [])
        monkeypatch.setattr(run, "improve_main_movies",
                            lambda *a, **k: None)
        monkeypatch.setattr(run, "log", lambda *a, **k: None)
        state = run.Run(whisper={"device": "cuda", "jobs": 1},
                        ram_root=str(tmp_path), fragments_file="",
                        ffsubsync_quality="no", unfixed_movies=[])
        # No subtitle work, but the commentary runs: the transcode still
        # goes beside it.
        run._films_in_lanes(state, str(tmp_path), False, lanes.FilmLocks(),
                            commentary_work=True)
        assert threads["opus"] is not threads["whisper"]
