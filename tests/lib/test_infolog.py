"""The white box for medialib/lib/infolog.py - what a run said on the console,
kept under `logs/info/`, one file per run, at a verbosity of its own."""

import io
import multiprocessing
import os
import re
import sys
from pathlib import Path

import pytest

from medialib.lib import errorlog, infolog, runlog, statusline

pytestmark = pytest.mark.fs


@pytest.fixture(autouse=True)
def no_status_row(monkeypatch):
    monkeypatch.setattr(statusline.state, "row", "")


@pytest.fixture
def info_dir(tmp_path, monkeypatch):
    """The run's info log lands under the test's own directory."""
    monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
    monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)
    monkeypatch.delenv(infolog.LEVEL_VARIABLE, raising=False)
    monkeypatch.setattr(infolog, "_OPEN", [])
    return tmp_path / "logs" / infolog.INFO_DIR


def kept(info_dir: Path) -> list[str]:
    files = list(info_dir.iterdir())
    assert len(files) == 1, files
    text = files[0].read_text(encoding="utf-8")
    body = text.split("=" * 78 + "\n", 1)[1]
    return [re.sub(r"^\S+ \S+ \S+ \[\d+\] ", "", line)
            for line in body.splitlines()]


def say_one_of_each() -> None:
    runlog.detail("why it was so")
    runlog.log("working on a.flac")
    runlog.warn("a slight problem")
    runlog.result("would rename a to b")
    runlog.error("it broke", persist=False)


def test_nothing_is_made_before_the_first_line(info_dir):
    errorlog.start_run(["in"])
    assert not info_dir.exists()


def test_the_default_keeps_what_the_console_says_by_default(info_dir, capsys):
    errorlog.start_run(["in"])
    say_one_of_each()
    assert kept(info_dir) == ["working on a.flac", "WARNING: a slight problem",
                              "would rename a to b", "ERROR: it broke"]


@pytest.mark.parametrize("word, expected", [
    ("off", None),
    ("quiet", ["would rename a to b", "ERROR: it broke"]),
    ("verbose", ["why it was so", "working on a.flac", "WARNING: a slight problem",
                 "would rename a to b", "ERROR: it broke"]),
])
def test_the_setting_chooses_how_much(info_dir, monkeypatch, capsys, word, expected):
    monkeypatch.setenv(infolog.LEVEL_VARIABLE, word)
    errorlog.start_run(["in"])
    say_one_of_each()
    if expected is None:
        assert not info_dir.exists()
    else:
        assert kept(info_dir) == expected


def test_a_quiet_console_still_keeps_a_verbose_log(info_dir, monkeypatch, capsys):
    monkeypatch.setenv(infolog.LEVEL_VARIABLE, "verbose")
    runlog.settle_verbosity(runlog.QUIET)
    errorlog.start_run(["in"])
    say_one_of_each()
    assert "working on" not in capsys.readouterr().err
    assert "why it was so" in kept(info_dir)


def test_the_file_shares_the_error_logs_name_and_opens_with_the_run(info_dir, capsys):
    errorlog.start_run(["-j", "2", "/in"])
    runlog.log("hello")
    path = next(info_dir.iterdir())
    assert path.name == os.path.basename(errorlog.current_file())
    head = path.read_text(encoding="utf-8")
    assert "-j 2 /in" in head.splitlines()[0]
    assert "keeps:    normal" in head
    assert re.search(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} [+-]\d{4} \[%d\] hello$"
                     % os.getpid(), head, re.M)


def test_a_report_written_elsewhere_is_not_kept(info_dir):
    errorlog.start_run(["in"])
    runlog.log("into a report", stream=io.StringIO())
    assert not info_dir.exists()


def test_a_secret_is_starred_out(info_dir, capsys):
    errorlog.start_run(["in"])
    runlog.log("GET /x?api_key=abcdef123456")
    assert kept(info_dir) == ["GET /x?api_key=***"]


def test_an_unwritable_log_costs_the_run_nothing(info_dir, capsys):
    info_dir.parent.mkdir(parents=True)
    info_dir.write_text("a file where the folder should be")
    errorlog.start_run(["in"])
    runlog.log("still said")
    assert "still said" in capsys.readouterr().err


def _worker_says(line: str) -> None:
    runlog.log(line)


def test_a_worker_writes_into_the_runs_file(info_dir, capsys):
    errorlog.start_run(["in"])
    runlog.log("parent")
    context = multiprocessing.get_context("fork" if sys.platform != "win32" else "spawn")
    child = context.Process(target=_worker_says, args=("worker",))
    child.start()
    child.join()
    assert kept(info_dir) == ["parent", "worker"]
