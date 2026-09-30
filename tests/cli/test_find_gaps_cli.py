"""`find-gaps` as a process: the report it leaves under logs/, and the question
it asks before replacing one an earlier run left."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.fs


def _library(tmp_path):
    library = tmp_path / "Library"
    for name in ("01.mp3", "02.mp3", "04.mp3"):
        (library / "Book").mkdir(parents=True, exist_ok=True)
        (library / "Book" / name).touch()
    return library


def _run(sandbox, tmp_path, *args, stdin=""):
    env = dict(os.environ, CLI_SCRIPT_DIR=str(tmp_path / "script"))
    return sandbox.run("find-gaps", *args, env=env, stdin=stdin)


def _report(tmp_path):
    return tmp_path / "script" / "logs" / "find-gaps" / "find-gaps-Library.tree"


def test_the_report_is_a_tree_of_what_is_missing(sandbox, tmp_path):
    library = _library(tmp_path)
    done = _run(sandbox, tmp_path, str(library))
    assert done.returncode == 0, done.stderr
    assert _report(tmp_path).read_text() == (
        "%s\n`-- Book/\n    `-- 03\n\n"
        "1 folder(s) with gaps, 1 missing, 0 date gap(s)\n" % library)
    assert str(_report(tmp_path)) in done.stdout


def test_the_input_is_not_touched(sandbox, tmp_path):
    library = _library(tmp_path)
    before = sorted(p.name for p in library.rglob("*"))
    _run(sandbox, tmp_path, str(library))
    assert sorted(p.name for p in library.rglob("*")) == before


def test_an_earlier_report_is_kept_without_an_answer(sandbox, tmp_path):
    library = _library(tmp_path)
    _run(sandbox, tmp_path, str(library))
    _report(tmp_path).write_text("earlier\n")
    done = _run(sandbox, tmp_path, str(library))
    assert done.returncode == 1
    assert "Overwrite it? [y/N]" in done.stderr
    assert "No answer on standard input" in done.stderr
    assert _report(tmp_path).read_text() == "earlier\n"


def test_no_keeps_it_and_yes_replaces_it(sandbox, tmp_path):
    library = _library(tmp_path)
    _run(sandbox, tmp_path, str(library))
    _report(tmp_path).write_text("earlier\n")
    assert _run(sandbox, tmp_path, str(library), stdin="n\n").returncode == 1
    assert _report(tmp_path).read_text() == "earlier\n"
    assert _run(sandbox, tmp_path, str(library), stdin="y\n").returncode == 0
    assert "`-- 03" in _report(tmp_path).read_text()


def test_an_empty_folder_is_refused(sandbox, tmp_path):
    (tmp_path / "Empty").mkdir()
    done = _run(sandbox, tmp_path, str(tmp_path / "Empty"))
    assert done.returncode == 1
    assert "is empty" in done.stderr
    assert not (tmp_path / "script" / "logs" / "find-gaps").exists()
