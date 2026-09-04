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
import time

import pytest

from medialib.lib import workerpool
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
