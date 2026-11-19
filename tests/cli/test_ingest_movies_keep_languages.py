"""keepLanguages: the languages a remux keeps beside a film's own.

Read once at the run's start and on the run that remuxes - and on the -p
run's keeps too: a code the language table does not know refuses the run
before a film is touched, and a code it knows is what the improved copy
keeps by. The table's own spelling and the refusal's wording are pinned in
TestTheKeptLanguagesSetting in test_ingest_movies.py.
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
        monkeypatch.setenv("keepLanguages", "xx")
        assert run.main(["-O", "plex", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert "keepLanguages" in errors
        assert '"xx"' in errors
        assert "Nothing was changed." in errors

    def test_every_unknown_code_is_named(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "Films").mkdir()
        monkeypatch.setenv("keepLanguages", "xx,yy")
        assert run.main(["-O", "plex", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert '"xx"' in errors
        assert '"yy"' in errors
        assert "cs, da, de, el, en, es" in errors
        assert "ja, ko, nl, no, pl, pt" in errors


class TestTheRunThatRemuxes:
    """The full run's state carries the setting, so every film it remuxes is
    judged by it."""

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
        monkeypatch.setenv("keepLanguages", "en,de")
        assert run.main([str(tmp_path / "Films")]) == 0
        assert states[0].keep_languages == ("en", "de")

    def test_without_the_setting_the_run_carries_the_default(
            self, monkeypatch, tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        monkeypatch.delenv("keepLanguages", raising=False)
        assert run.main([str(tmp_path / "Films")]) == 0
        assert states[0].keep_languages == ("en",)


class TestThePlexKeeps:
    """-p compares with keeps the improved copy's: the run it compares by
    keeps by what the setting says, not by the default."""

    def _stubbed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(run.tooldeps, "require_tools", lambda *_a: 0)
        seen = {}
        monkeypatch.setattr(
            run.plexphase, "compare_roots",
            lambda roots, _log, **_kw: seen.update(_kw) or 0)
        return seen

    def test_the_keeps_run_carries_the_setting(self, monkeypatch, tmp_path):
        seen = self._stubbed(monkeypatch, tmp_path)
        (tmp_path / "Films").mkdir()
        monkeypatch.setenv("keepLanguages", "en,de")
        assert run.main(["-O", "plex", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 0
        assert seen["keeps_for"].__self__.keep_languages == ("en", "de")

    def test_and_the_default_where_the_setting_is_not(self, monkeypatch,
                                                      tmp_path):
        seen = self._stubbed(monkeypatch, tmp_path)
        (tmp_path / "Films").mkdir()
        monkeypatch.delenv("keepLanguages", raising=False)
        assert run.main(["-O", "plex", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 0
        assert seen["keeps_for"].__self__.keep_languages == ("en",)
