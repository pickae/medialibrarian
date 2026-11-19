"""spokenLanguages: the languages a run fetches and tests subtitles for.

Read once at the run's start: a code the language table does not know
refuses the run before a film is touched, and the codes it knows, in the
order the setting names them, are what the run fetches and tests. The
table's own codes and the refusal's wording are pinned in
TestWantedSubtitles in test_languages.py.
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
    """A code the table does not know refuses the run at its start, before
    a film is touched - like a bad option, with the page and the trailer."""

    def test_a_code_the_table_does_not_know_is_refused(
            self, monkeypatch, tmp_path, capsys):
        (tmp_path / "Films").mkdir()
        monkeypatch.setenv("spokenLanguages", "xx")
        assert run.main(["-O", "subtitles", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert "spokenLanguages" in errors
        assert '"xx"' in errors
        assert "Nothing was changed." in errors

    def test_every_unknown_code_is_named(self, monkeypatch, tmp_path,
                                         capsys):
        (tmp_path / "Films").mkdir()
        monkeypatch.setenv("spokenLanguages", "xx,yy")
        assert run.main(["-O", "subtitles", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert '"xx"' in errors
        assert '"yy"' in errors

    def test_the_refusal_holds_before_a_phase_of_any_kind(
            self, monkeypatch, tmp_path, capsys):
        """The setting is read where keepLanguages is read: once, at the
        run's start, on a run asked for nothing but the tags."""
        _library(tmp_path, "Films")
        monkeypatch.setenv("spokenLanguages", "xx")
        assert run.main(["-O", "tagging", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert "spokenLanguages" in errors
        assert "Nothing was changed." in errors


class TestTheRunThatIngests:
    """The full run's state carries the setting, so every subtitle it
    fetches or tests is fetched and tested for it."""

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

    def test_the_run_carries_the_setting(self, monkeypatch, tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        monkeypatch.setenv("spokenLanguages", "pt,en")
        assert run.main([str(tmp_path / "Films")]) == 0
        wanted = states[0].subtitle_languages
        assert [row.code2 for row in wanted] == ["pt", "en"]

    def test_without_the_setting_the_run_carries_the_default(
            self, monkeypatch, tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        monkeypatch.delenv("spokenLanguages", raising=False)
        assert run.main([str(tmp_path / "Films")]) == 0
        wanted = states[0].subtitle_languages
        assert [row.code2 for row in wanted] \
            == ["en", "de", "fr", "nl", "es", "it"]
