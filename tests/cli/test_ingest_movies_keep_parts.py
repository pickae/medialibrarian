"""The -k parts: what a full ingest is told to keep.

Read once at the run's start: a part the run will not keep is refused with
the page, like a bad option, and the parts it keeps are what the run that
remuxes is handed - a bare -k keeping everything.
What each part settles is pinned where it settles it: the tracks in
TestKeepEverythingDropsNothing in test_ingest_movies.py, the refusals in
TestKeepEverything in test_ingest_movies_convert.py.
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
    """A part the run will not keep refuses the run at its start, before a
    film is touched - like a bad option, with the page and the trailer."""

    def test_a_part_the_run_will_not_keep_is_refused(
            self, monkeypatch, tmp_path, capsys):
        (tmp_path / "Films").mkdir()
        assert run.main(["--keep=bogus", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert 'Option --keep does not take "bogus"' in errors
        assert "Nothing was changed." in errors

    def test_and_a_list_with_an_unknown_part_in_it(self, monkeypatch,
                                                   tmp_path, capsys):
        (tmp_path / "Films").mkdir()
        assert run.main(["--keep=audio,bogus", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert 'Option --keep does not take "audio,bogus"' in errors
        assert "Nothing was changed." in errors


class TestTheRunThatRemuxes:
    """The full run's state carries the parts, so every film it remuxes is
    judged by them."""

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

    def test_the_run_carries_the_parts(self, monkeypatch, tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        assert run.main(["-k", "audio,dv", str(tmp_path / "Films")]) == 0
        assert states[0].keep_parts == frozenset({"audio", "dv"})

    def test_a_bare_k_keeps_every_part(self, monkeypatch, tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        assert run.main(["-k", str(tmp_path / "Films")]) == 0
        assert states[0].keep_parts \
            == frozenset({"audio", "tracks", "dv"})

    def test_and_the_clustered_flag_keeps_them_all_too(self, monkeypatch,
                                                       tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        assert run.main(["-kv", str(tmp_path / "Films")]) == 0
        assert states[0].keep_parts \
            == frozenset({"audio", "tracks", "dv"})

    def test_without_the_flag_the_run_keeps_none(self, monkeypatch, tmp_path):
        states = self._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "Films")
        assert run.main([str(tmp_path / "Films")]) == 0
        assert states[0].keep_parts == frozenset()


class TestTheWordAfterTheFlag:
    """-k claims the word after it only when that word is a part, the way
    -t claims a number: the part is claimed and the folder is left, and the
    folder named after one of them is written with the dot."""

    def test_a_folder_that_is_the_part_name_is_the_part(
            self, monkeypatch, tmp_path):
        _library(tmp_path, "audio")
        monkeypatch.chdir(tmp_path)
        # The word goes to -k, and the folder is left with no positional to
        # be a root.
        assert run.main(["-k", "audio"],
                        script_dir=str(tmp_path / "script")) == 1

    def test_and_written_with_the_dot_the_folder_is_the_root(
            self, monkeypatch, tmp_path):
        states = TestTheRunThatRemuxes()._stubbed(monkeypatch, tmp_path)
        _library(tmp_path, "audio")
        monkeypatch.chdir(tmp_path)
        assert run.main(["-k", "./audio"],
                        script_dir=str(tmp_path / "script")) == 0
        assert states[0].keep_parts \
            == frozenset({"audio", "tracks", "dv"})
