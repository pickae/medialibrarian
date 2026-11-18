"""mediaServerNaming: the server the names are written for.

Read once at the run's start: a server the naming table does not know
refuses the run before a film is touched, and a run set for jellyfin
writes its ids and editions the way that server reads them - while the
ones written either way are read back whatever the setting says.
"""

from __future__ import annotations

import pytest

from medialib.cli import ingest_movies_run as run

pytestmark = pytest.mark.fs


def _library(root, name: str, film: str = "The Movie (1999)"):
    """A folder holding one film, which is what makes it worth ingesting."""
    folder = root / name / film
    folder.mkdir(parents=True)
    (folder / (film + ".mkv")).touch()
    return root / name


class TestTheRefusal:
    """A server the table does not know refuses the run at its start, before
    a film is touched - like a bad option, with the page and the trailer."""

    def test_a_server_the_table_does_not_know_is_refused(
            self, monkeypatch, tmp_path, capsys):
        (tmp_path / "Films").mkdir()
        monkeypatch.setenv("mediaServerNaming", "kodi")
        assert run.main(["-s", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert "mediaServerNaming" in errors
        assert '"kodi"' in errors
        assert "Nothing was changed." in errors

    def test_the_refusal_holds_before_a_phase_of_any_kind(
            self, monkeypatch, tmp_path, capsys):
        """The setting is read where spokenLanguages is read: once, at the
        run's start, on a run asked for nothing but the tags."""
        _library(tmp_path, "Films")
        monkeypatch.setenv("mediaServerNaming", "plex  plex")
        assert run.main(["-t", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert "mediaServerNaming" in errors
        assert "Nothing was changed." in errors


class TestTheRunThatIngests:
    """A run set for either server goes on, with the setting's style the one
    its names come out in."""

    def _stubbed(self, monkeypatch, tmp_path):
        """Everything a full run sets up around the ingest itself, stood
        down: what is under test is that the run goes on at all."""
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
        monkeypatch.setattr(run, "_ingest",
                            lambda _state, _root, _subs, _name: None)

    def test_a_run_set_for_jellyfin_goes_on(self, monkeypatch, tmp_path):
        self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        monkeypatch.setenv("mediaServerNaming", "jellyfin")
        assert run.main([str(tmp_path / "Films")]) == 0

    def test_without_the_setting_the_run_writes_for_plex(
            self, monkeypatch, tmp_path):
        self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        monkeypatch.delenv("mediaServerNaming", raising=False)
        assert run.main([str(tmp_path / "Films")]) == 0
