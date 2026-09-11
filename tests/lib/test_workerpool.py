"""The white box for medialib/lib/workerpool.py.

The property that matters here is that a slot which comes free is refilled AT
ONCE, not when the slowest worker of the current set finishes. It is not a
timing assertion - a queue that batches is not merely slower, it demonstrably
does not overlap - so the workers say so themselves. Three of them, two slots,
and a file each: the third can only find the first one still running if it was
started while the first still held a slot, which a batching queue never does.

The workers are real processes, because that is what the pool waits on: the
sentinel a fork leaves behind is the whole mechanism, and a thread or a stub
would test something else.
"""

import multiprocessing
import os
import signal
import time

import pytest

from medialib.lib import safety, workerpool
from tests import blackbox

pytestmark = pytest.mark.fs

# Long enough that a loaded machine does not trip it, short enough that a pool
# which has gone back to batching fails rather than hangs.
LIMIT = 20.0


def _wait_for(path: str) -> bool:
    deadline = time.time() + LIMIT
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.01)
    return False


def _mark(directory: str, name: str) -> None:
    open(os.path.join(directory, name), "w").close()


def _step(directory: str, kind: str) -> None:
    """One worker. Which of the three it is decides what it does.

    ``slow`` runs until ``late`` releases it, ``quick`` exists only to free a
    slot, and ``late`` records whether it got that slot while ``slow`` was still
    holding the other one.
    """
    if kind == "slow":
        _mark(directory, "slow.start")
        _wait_for(os.path.join(directory, "release"))
        _mark(directory, "slow.end")
    elif kind == "late":
        _wait_for(os.path.join(directory, "slow.start"))
        if not os.path.exists(os.path.join(directory, "slow.end")):
            _mark(directory, "overlapped")
        _mark(directory, "release")
    else:
        _mark(directory, "quick.end")


def _sleeper(seconds: float) -> None:
    time.sleep(seconds)


def test_a_freed_slot_is_refilled_while_the_slow_worker_still_runs(tmp_path):
    directory = str(tmp_path)
    workerpool.run(["slow", "quick", "late"], 2, _step,
                   lambda kind: (directory, kind))

    assert (tmp_path / "overlapped").exists(), (
        "the third item waited for the first to finish: the queue is batching")
    assert (tmp_path / "slow.end").exists()


def test_every_item_is_handed_out(tmp_path):
    directory = str(tmp_path)
    items = ["item%02d" % index for index in range(9)]
    workerpool.run(items, 3, _mark, lambda item: (directory, item))

    assert sorted(path.name for path in tmp_path.iterdir()) == items


def _census(directory: str, index: int) -> None:
    # Own pid included, which is why the count is of the children the parent
    # has, not of the siblings this one can see.
    time.sleep(0.05)
    with open(os.path.join(directory, "%d.width" % index), "w") as handle:
        handle.write("%d\n" % len(multiprocessing.active_children()))


def test_the_queue_never_runs_wider_than_it_was_asked_to(tmp_path):
    """Each worker records the workers alive beside it, itself included."""
    directory = str(tmp_path)
    workerpool.run(list(range(8)), 3, _census, lambda index: (directory, index))

    widths = [int((tmp_path / name).read_text())
              for name in os.listdir(str(tmp_path))]
    assert widths and max(widths) <= 3


def test_nothing_is_dispatched_once_the_run_is_aborting(tmp_path, monkeypatch):
    flag = tmp_path / "abort"
    flag.write_text("")
    monkeypatch.setenv("ABORT_FLAG", str(flag))
    work = tmp_path / "work"
    work.mkdir()

    workerpool.run(["a", "b", "c"], 2, _mark, lambda item: (str(work), item))

    assert list(work.iterdir()) == []


def test_reap_one_returns_the_survivors_and_buries_the_dead():
    finished = multiprocessing.Process(target=_sleeper, args=(0.0,))
    running = multiprocessing.Process(target=_sleeper, args=(LIMIT,))
    finished.start()
    running.start()
    try:
        alive = workerpool.reap_one([finished, running])

        assert alive == [running]
        assert finished.exitcode == 0
        assert not finished.is_alive()
    finally:
        running.terminate()
        running.join()


def test_reap_one_on_an_empty_pool_is_a_no_op():
    assert workerpool.reap_one([]) == []


# --- what a worker's EXIT says -------------------------------------------------
# The pool is the only place a worker's status exists: nothing else ever holds
# the Process object. A queue that drops it cannot tell an item that was made
# from one whose worker died before it wrote anything, and the command then
# prints its "Done" over a folder that is missing files.


def _explode(directory: str, name: str) -> None:
    _mark(directory, name)
    raise RuntimeError("the worker's own bug")


def _suicide(directory: str, name: str) -> None:
    _mark(directory, name)
    os.kill(os.getpid(), signal.SIGKILL)


def _mark_or_explode(directory: str, name: str) -> None:
    if name == "boom":
        _explode(directory, name)
    else:
        _mark(directory, name)


def _refuse_the_queue(_directory: str, _name: str) -> None:
    raise SystemExit(safety.XARGS_STOP_EXIT_STATUS)


@pytest.fixture(autouse=True)
def _a_fresh_tally():
    """The tally is the RUN's, and a test is not a run: each case starts with an
    empty one and leaves one behind."""
    workerpool.forget_failures()
    yield
    workerpool.forget_failures()


class TestAnAbnormalExitIsNotAFinishedItem:
    def test_an_uncaught_exception_is_counted_and_named(self, tmp_path):
        directory = str(tmp_path)
        outcome = workerpool.run(["boom"], 2, _explode,
                                 lambda name: (directory, name))

        assert [label for label, _reason in outcome.failed] == ["boom"]
        assert "status 1" in outcome.failed[0][1]
        assert not outcome.ok

    def test_a_worker_killed_by_a_signal_is_counted_as_the_signal(self,
                                                                  tmp_path):
        directory = str(tmp_path)
        outcome = workerpool.run(["cut"], 2, _suicide,
                                 lambda name: (directory, name))

        assert [label for label, _reason in outcome.failed] == ["cut"]
        assert "signal %d" % int(signal.SIGKILL) in outcome.failed[0][1]

    def test_the_items_that_did_finish_are_not_counted_with_it(self, tmp_path):
        """A crash is one item, not the run: the rest of the queue is dispatched
        and the failure names only its own."""
        work = tmp_path / "work"
        work.mkdir()

        outcome = workerpool.run(["one", "boom", "two"], 2, _mark_or_explode,
                                 lambda name: (str(work), name))

        assert [label for label, _reason in outcome.failed] == ["boom"]
        assert sorted(path.name for path in work.iterdir()) == [
            "boom", "one", "two"]

    def test_the_queue_stopping_status_is_an_interrupt_and_not_a_failure(
            self, tmp_path):
        """The status a worker exits with to stop the QUEUE is the run ending
        tidily. Counting it would turn every Ctrl+C into a list of failures."""
        directory = str(tmp_path)
        outcome = workerpool.run(["a", "b"], 2, _refuse_the_queue,
                                 lambda name: (directory, name))

        assert outcome.failed == []
        assert outcome.ok

    def test_that_status_stops_the_dispatch(self, tmp_path, monkeypatch):
        """What the status MEANS is "stop the queue", so the rest of it is not
        handed out - the worker records the interrupt and the pool says it
        again where the dispatch reads it."""
        monkeypatch.setenv("ABORT_FLAG", str(tmp_path / "abort"))
        work = tmp_path / "work"
        work.mkdir()

        workerpool.run(["a", "b", "c", "d"], 1, _refuse_the_queue,
                       lambda name: (str(work), name))

        assert safety.abort_requested()

    def test_a_failure_is_said_at_once_and_names_the_item(self, tmp_path,
                                                          capsys):
        workerpool.run(["the item that died"], 1, _explode,
                       lambda name: (str(tmp_path), name))

        said = capsys.readouterr().err
        assert "the item that died" in said
        assert "WARNING" in said

    def test_a_pair_is_named_by_its_first_field(self, tmp_path):
        """The commands queue (input, output) pairs and records of their own;
        what names the work is the first field in every one of them."""
        outcome = workerpool.run([("/in/Book One", "/out/Book One")], 1,
                                 _explode, lambda pair: (str(tmp_path),
                                                         pair[0]))

        assert [label for label, _reason in outcome.failed] == ["/in/Book One"]


class TestWhatTheRunEndsWith:
    def test_a_clean_run_keeps_the_status_it_was_going_to_have(self):
        assert workerpool.exit_status() == 0
        assert workerpool.exit_status(3) == 3

    def test_a_run_that_lost_a_worker_cannot_report_success(self, tmp_path):
        workerpool.run(["boom"], 1, _explode,
                       lambda name: (str(tmp_path), name))

        assert workerpool.exit_status() == 1
        assert workerpool.exit_status(0) == 1

    def test_the_closing_recap_lists_them(self, tmp_path, capsys):
        workerpool.run(["boom"], 1, _explode,
                       lambda name: (str(tmp_path), name))
        capsys.readouterr()

        count = workerpool.report_failures()

        assert count == 1
        assert "boom" in capsys.readouterr().err

    def test_the_recap_of_a_clean_run_says_nothing_at_all(self, capsys):
        assert workerpool.report_failures() == 0
        assert capsys.readouterr().err == ""


class TestJoiningTheStragglers:
    def test_their_exits_are_classified_too(self, tmp_path):
        """A command that keeps its own queue hands the ones it is still holding
        here, and a crash among them counts exactly as one in the pool does."""
        worker = multiprocessing.Process(target=_explode,
                                         args=(str(tmp_path), "left over"),
                                         name="left over")
        worker.start()

        workerpool.join_all([worker])

        assert [label for label, _ in workerpool.failed_workers()] == [
            "left over"]


def test_no_command_keeps_a_queue_of_its_own():
    """Waiting for a worker is this module's job, and only this module's.

    A command that caps its own fan-out has to reap it too, and the reap is the
    part that is easy to get wrong: waiting on ``running[0]`` looks like waiting
    for a free slot and is waiting for the OLDEST worker.
    """
    private = []
    for path in sorted((blackbox.REPO / "medialib").rglob("*.py")):
        if path.name == "workerpool.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "running[0]" in source or (
                "len(running)" in source and "workerpool." not in source):
            private.append(path.name)
    assert private == []
