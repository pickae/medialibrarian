"""-O layout, roles, ocr and extras-chapters: the phases of a full ingest that
can be run on their own."""

from __future__ import annotations

import os

import pytest

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_run as run
from medialib.lib import tooldeps


@pytest.fixture
def phases(monkeypatch):
    seen = []
    for name in ("_layout_only", "_roles_only", "_ocr_only",
                 "_extras_chapters_only"):
        monkeypatch.setattr(run, name,
                            lambda *a, _n=name: seen.append(_n) or 0)
    return seen


@pytest.mark.parametrize("phase, handler", [
    ("layout", "_layout_only"), ("roles", "_roles_only"),
    ("ocr", "_ocr_only"), ("extras-chapters", "_extras_chapters_only")])
def test_each_phase_reaches_its_own_handler(phases, tmp_path, phase, handler):
    assert run.main(["-O", phase, str(tmp_path)]) == 0
    assert phases == [handler]


def test_they_chain_in_the_order_typed(phases, tmp_path):
    assert run.main(["-O", "ocr,roles,layout", str(tmp_path)]) == 0
    assert phases == ["_ocr_only", "_roles_only", "_layout_only"]


def test_the_phases_are_named_in_only_phases():
    assert {"layout", "roles", "ocr", "extras-chapters"} <= set(
        rules.ONLY_PHASES)
    assert not {"layout", "roles", "ocr", "extras-chapters"} & set(
        rules.ONLY_WRITES)


def test_write_alone_with_them_is_refused(phases, tmp_path, capsys):
    assert run.main(["-wO", "layout,roles,ocr", str(tmp_path)]) == 1
    assert "Drop the -w" in capsys.readouterr().err
    assert phases == []


def test_instructions_go_with_extras_chapters(phases, tmp_path):
    assert run.main(["-O", "extras-chapters", "-P", "a chapter per scene",
                     str(tmp_path)]) == 0
    assert phases == ["_extras_chapters_only"]


def test_instructions_are_refused_without_extras_chapters(phases, tmp_path,
                                                          capsys):
    assert run.main(["-O", "roles", "-P", "x", str(tmp_path)]) == 1
    assert "extras-chapters is" in capsys.readouterr().err
    assert phases == []


def test_layout_deletes_junk_and_gives_a_loose_film_a_folder(
        monkeypatch, tmp_path):
    monkeypatch.setattr(tooldeps, "require_tools", lambda *_a: 0)
    library = tmp_path / "library"
    library.mkdir()
    (library / "Some Film (2001).mkv").write_bytes(
        b"\x1a\x45\xdf\xa3" + b"\0" * (1100 * 1024))
    (library / "release.nfo").write_text("junk")
    assert run.main(["-O", "layout", str(library)]) == 0
    assert not (library / "release.nfo").exists()
    assert not (library / "Some Film (2001).mkv").exists()
    folders = [entry for entry in library.iterdir() if entry.is_dir()]
    assert len(folders) == 1
    assert [f.name for f in folders[0].iterdir()] == ["Some Film (2001).mkv"]


class _Track:
    def __init__(self, id_, subtitle):
        self.id = id_
        self.is_subtitle = subtitle
        self.name = "null"


def test_roles_touches_only_the_films_with_a_wrong_subtitle_flag(
        monkeypatch, tmp_path):
    for name in ("a", "b"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / (name + ".mkv")).write_bytes(b"x")
    calls = []
    monkeypatch.setattr(rules, "_is_matroska", lambda _m: True)
    monkeypatch.setattr(rules, "_identify", lambda _m: [
        _Track("0", False), _Track("1", True)])
    monkeypatch.setattr(
        rules, "_measured_role_edits",
        lambda movie, _t: {"1": ("full", "forced")}
        if movie.endswith("a.mkv") else {})
    monkeypatch.setattr(rules, "_run_capture",
                        lambda argv: calls.append(argv) or "")
    assert rules.update_roles(str(tmp_path)) == 1
    assert len(calls) == 1
    assert calls[0][0] == "mkvpropedit"
    # Only the subtitle's own scope and flag: nothing else is rewritten.
    assert calls[0][2:] == ["--edit", "track:2", "--set", "flag-forced=1"]
    assert os.path.basename(calls[0][1]) == "a.mkv"
