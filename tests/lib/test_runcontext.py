"""Tests for medialib.lib.runcontext - the run's state in one object.

What is pinned here: a value settled is read back and handed down to the
children through the environment; a value the run did not settle is the
parent's hand-down; and a worker gets the run from the pickled context it is
handed - never from an environment it would share with a forkserver started
before the value was settled.
"""

import os
import pickle

import pytest

from medialib.lib import ramscratch, runcontext, statusline, workerpool

pytestmark = pytest.mark.pure


def test_a_settled_value_is_read_back_and_handed_down(monkeypatch):
    monkeypatch.delenv("PODCAST_PLATFORM", raising=False)
    runcontext.settle("PODCAST_PLATFORM", "native")
    assert runcontext.get("PODCAST_PLATFORM") == "native"
    assert os.environ["PODCAST_PLATFORM"] == "native"
    runcontext.forget("PODCAST_PLATFORM")
    assert runcontext.get("PODCAST_PLATFORM", "none") == "none"
    assert "PODCAST_PLATFORM" not in os.environ


def test_what_the_run_did_not_settle_is_the_parent_s_hand_down(monkeypatch):
    monkeypatch.setenv("LOG_VERBOSITY", "verbose")
    assert runcontext.get("LOG_VERBOSITY") == "verbose"


def test_a_settled_value_outlives_a_change_in_the_environment(monkeypatch):
    runcontext.settle("HAVE_MKVTOOLNIX", "1")
    monkeypatch.setenv("HAVE_MKVTOOLNIX", "")
    assert runcontext.get("HAVE_MKVTOOLNIX") == "1"


def test_the_pickled_run_carries_the_hand_down_read_once(monkeypatch):
    """A worker reads none of it from the environment itself."""
    monkeypatch.setenv("LOG_TIMESTAMPS", "1")
    handed = pickle.loads(pickle.dumps(runcontext.snapshot()))
    monkeypatch.setenv("LOG_TIMESTAMPS", "")
    runcontext.adopt(handed)
    assert runcontext.get("LOG_TIMESTAMPS") == "1"
    # and written into the environment, for what the worker starts
    assert os.environ["LOG_TIMESTAMPS"] == "1"


def test_the_pickled_run_carries_its_scratch_its_disk_turns_and_its_row():
    run = runcontext.current()
    run.ram_base = "/scratch/run.1"
    run.disk_turns = "/scratch/run.1/turns"
    statusline.state.row, statusline.state.cols = "1", 96
    handed = pickle.loads(pickle.dumps(run))
    runcontext.reset()
    runcontext.adopt(handed)
    assert ramscratch.ram_base() == "/scratch/run.1"
    assert runcontext.current().disk_turns == "/scratch/run.1/turns"
    assert statusline.status_geometry()[:2] == ("1", 96)


def test_a_worker_is_not_handed_the_failures_its_parent_reaped():
    runcontext.current().failures.append(("film.mkv", "killed by signal 9"))
    handed = pickle.loads(pickle.dumps(runcontext.snapshot()))
    assert handed.failures == []
    assert workerpool.failed_workers() == [("film.mkv", "killed by signal 9")]


def _report(answer_file: str) -> None:
    with open(answer_file, "w") as answer:
        answer.write("%s|%s" % (runcontext.get("PODCAST_DATE_AFTER"),
                                os.environ.get("PODCAST_DATE_AFTER", "")))


@pytest.mark.fs
def test_a_worker_sees_a_value_settled_after_the_first_worker_started(
        tmp_path):
    """Under forkserver every worker is forked from a server started with the
    first one, and shares that server's environment - the parent's as it was
    then. What the run settles later reaches the worker with the run."""
    first = workerpool.start_worker(_report, (str(tmp_path / "first"),),
                                    "first")
    first.join()
    runcontext.settle("PODCAST_DATE_AFTER", "20260101")
    second = workerpool.start_worker(_report, (str(tmp_path / "second"),),
                                     "second")
    second.join()
    assert second.exitcode == 0
    assert (tmp_path / "second").read_text() == "20260101|20260101"
