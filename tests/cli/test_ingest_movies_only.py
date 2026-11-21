"""-O: the single-phase sweeps named by one option, run in turn."""

from __future__ import annotations

import pytest

from medialib.cli import ingest_movies_run as run


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def one(_declaration, result, _program, _script_dir, phase):
        seen.append((phase, bool(result.values.get("writeTags"))))
        return 0

    monkeypatch.setattr(run, "_main_one", one)
    return seen


def test_one_phase_is_run(calls, tmp_path):
    assert run.main(["-O", "plex", str(tmp_path)]) == 0
    assert calls == [("plex", False)]


def test_a_list_runs_in_the_order_typed(calls, tmp_path):
    assert run.main(["-O", "subtitles,tagging", str(tmp_path)]) == 0
    assert [phase for phase, _ in calls] == ["subtitles", "tagging"]


def test_write_goes_only_to_the_phases_that_take_it(calls, tmp_path):
    assert run.main(["-wO", "tagging,plex", str(tmp_path)]) == 0
    assert calls == [("tagging", True), ("plex", False)]


def test_the_chain_stops_at_the_first_failure(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(run, "_main_one",
                        lambda *_a: seen.append(1) or 1)
    assert run.main(["-O", "tagging,plex", str(tmp_path)]) == 1
    assert seen == [1]


def test_an_unknown_phase_is_refused(calls, tmp_path, capsys):
    assert run.main(["-O", "bogus", str(tmp_path)]) == 1
    assert "-O phase must be one of" in capsys.readouterr().err
    assert calls == []


def test_write_with_no_phase_that_takes_it_is_refused(calls, tmp_path, capsys):
    assert run.main(["-wO", "plex,chapters", str(tmp_path)]) == 1
    assert "Drop the -w" in capsys.readouterr().err


def test_an_id_list_needs_the_tagging_phase(calls, tmp_path, capsys):
    assert run.main(["-O", "plex", "-i", "ids.tsv", str(tmp_path)]) == 1
    assert "tagging is not among" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("other", [["-o", "junk"], ["-k"], ["-r"]])
def test_a_full_ingest_option_is_refused(calls, tmp_path, capsys, other):
    assert run.main(["-O", "plex", *other, str(tmp_path)]) == 1
    assert "belongs to a full ingest" in capsys.readouterr().err
    assert calls == []


def test_the_old_single_phase_flags_are_gone(tmp_path, capsys):
    assert run.main(["-t", str(tmp_path)]) == 1
    assert "Unknown option: -t" in capsys.readouterr().err
