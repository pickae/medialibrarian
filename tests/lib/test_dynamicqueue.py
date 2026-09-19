"""The white box for medialib/lib/dynamicqueue.py.

The property that matters is the one a queue of items that have to be prepared
cannot have: the preparation runs beside the run, not in front of it. The queue
loads a buffer of prepared items, starts the workers on it, and tops the buffer
up as it is spent - so a freed slot is refilled at once with the longest of
what is left, and a worker that goes idle on an empty buffer is said out loud.

The workers are real processes, because that is what the queue waits on: the
sentinel a fork leaves behind is the whole mechanism, and a thread or a stub
would test something else. The producer is a plain generator in this process,
the way the run that feeds it runs.
"""

import os
import time

import pytest

from medialib.lib import dynamicqueue, workerpool

pytestmark = pytest.mark.fs

# Long enough that a loaded machine does not trip it, short enough that a
# broken interleaving fails rather than hangs.
LIMIT = 20.0


def _wait_for(path: str, timeout: float = LIMIT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.01)
    return False


def _mark(directory: str, name: str) -> None:
    open(os.path.join(directory, name), "w").close()


@pytest.fixture(autouse=True)
def _a_fresh_tally():
    """The tally is the RUN's, and a test is not a run: each case starts with an
    empty one and leaves one behind."""
    workerpool.forget_failures()
    yield
    workerpool.forget_failures()


# --- the buffer, and what a freed slot is given --------------------------------


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


def test_a_freed_slot_is_refilled_while_the_slow_worker_still_runs(tmp_path):
    directory = str(tmp_path)
    prepared = []

    def producer():
        for kind in ("slow", "quick", "late"):
            prepared.append(kind)
            yield kind, 0

    dynamicqueue.run(producer(), 2, _step, lambda kind: (directory, kind))

    assert prepared == ["slow", "quick", "late"]
    assert (tmp_path / "overlapped").exists(), (
        "the third item waited for the first to finish: the queue is batching")
    assert (tmp_path / "slow.end").exists()


def _append(directory: str, name: str) -> None:
    with open(os.path.join(directory, "dispatched"), "a") as handle:
        handle.write(name + "\n")


def test_a_freed_slot_is_given_the_longest_item_left(tmp_path):
    """The buffer is kept from the largest of the prepared items to the
    smallest, so the longest of the run is done while the workers are still
    full rather than left to the tail of it."""
    directory = str(tmp_path)
    sizes = {"small": 1, "large": 30, "medium": 10}

    def producer():
        for name in ("small", "large", "medium"):
            yield name, sizes[name]

    dynamicqueue.run(producer(), 1, _append, lambda name: (directory, name))

    assert (tmp_path / "dispatched").read_text().split() \
        == ["large", "medium", "small"]


def _record_seen(directory: str, name: str) -> None:
    time.sleep(0.05)
    seen = sum(1 for entry in os.listdir(directory)
               if entry.startswith("prepared."))
    with open(os.path.join(directory, "seen.%s" % name), "w") as handle:
        handle.write("%d\n" % seen)


def test_no_worker_starts_before_the_buffer_is_loaded(tmp_path):
    """The start costs the buffer and nothing more of the preparation: no
    worker can find fewer prepared items than the buffer holds, and the item
    prepared while the workers run is the one that finds more."""
    directory = str(tmp_path)
    items = ["w%d" % index for index in range(1, 6)]

    def producer():
        for index, item in enumerate(items):
            if index == 4:
                # the last item is prepared while the workers already run:
                # slow enough that it cannot be ready before they are
                time.sleep(2.0)
            _mark(directory, "prepared.%d" % (index + 1))
            yield item, 0

    dynamicqueue.run(producer(), 2, _record_seen,
                     lambda item: (directory, item))

    counts = {}
    for item in items:
        path = tmp_path / ("seen.%s" % item)
        assert path.exists()
        counts[item] = int(path.read_text())
    # the buffer is four: nothing started before the fourth mark was down
    assert min(counts.values()) == 4
    # and the late item was prepared after the first workers had started
    assert counts["w5"] == 5


def _done_after(directory: str, name: str, seconds: float) -> None:
    time.sleep(seconds)
    _mark(directory, "done.%s" % name)


def test_preparation_is_interleaved_with_the_run(tmp_path):
    """The third item is prepared only after the first worker has finished -
    a mark that could not exist if the preparation had run in front of the
    run, where no worker had run yet."""
    directory = str(tmp_path)

    def producer():
        yield "first", 0
        yield "second", 0
        if _wait_for(os.path.join(directory, "done.first"), timeout=5.0):
            _mark(directory, "interleaved")
        yield "third", 0

    dynamicqueue.run(producer(), 1, _done_after,
                     lambda name: (directory, name, 0.1))

    assert (tmp_path / "interleaved").exists()


# --- what the queue ends with --------------------------------------------------


def test_an_empty_queue_runs_nothing(tmp_path):
    directory = str(tmp_path)
    lines = []
    outcome = dynamicqueue.run(iter(()), 2, _mark,
                               lambda item: (directory, item),
                               log=lines.append)

    assert outcome.ok
    assert not outcome.aborted
    assert list(tmp_path.iterdir()) == []
    assert lines == []


def test_a_run_already_aborting_never_pulls_the_producer(tmp_path, monkeypatch):
    flag = tmp_path / "abort"
    flag.write_text("")
    monkeypatch.setenv("ABORT_FLAG", str(flag))
    directory = str(tmp_path)

    def producer():
        _mark(directory, "pulled")
        yield "a", 0

    outcome = dynamicqueue.run(producer(), 2, _mark,
                               lambda item: (directory, item))

    assert outcome.aborted
    assert not (tmp_path / "pulled").exists()


def _abort_if(directory: str, name: str, flag: str) -> None:
    _mark(directory, name + ".ran")
    if name == "a":
        # what safety.request_abort() writes in a worker that sees the run's
        # ABORT_FLAG - which the run sets before any worker is forked, and a
        # forkserver started before the test set it would not have inherited
        open(flag, "ab").close()


def test_an_abort_mid_run_stops_the_dispatch_not_the_workers(
        tmp_path, monkeypatch):
    """The interrupt stops the DISPATCH - the preparation included - and the
    item already running is waited for, not killed."""
    flag = tmp_path / "abort"
    monkeypatch.setenv("ABORT_FLAG", str(flag))
    directory = str(tmp_path)

    def producer():
        yield "a", 0
        yield "b", 0

    outcome = dynamicqueue.run(producer(), 1, _abort_if,
                               lambda name: (directory, name, str(flag)))

    assert (tmp_path / "a.ran").exists()
    assert not (tmp_path / "b.ran").exists()
    assert outcome.aborted


def _explode(_name: str) -> None:
    raise RuntimeError("the worker's own bug")


def test_a_dead_worker_is_counted_and_named():
    outcome = dynamicqueue.run(iter([("boom", 0)]), 1, _explode,
                               lambda item: (item,))

    assert outcome.failed == [("boom", "exited with status 1")]
    assert not outcome.ok
    assert not outcome.aborted


# --- what the queue says -------------------------------------------------------


def _sleeper(_directory: str, name: str) -> None:
    # the second item outlasts the first by far more than the fork gap, so
    # the dry slot is the first worker's and the count of waiters is one
    time.sleep(0.5 if name == "b" else 0.1)


def test_the_queue_says_its_prefill_its_start_and_a_buffer_that_ran_dry(
        tmp_path):
    """At a buffer of one per worker, the two workers finish before the next
    item is ready, and the empty slot waits on the preparation: the queue says
    so once, beside the pre-fill and the start it already said."""
    directory = str(tmp_path)
    lines = []

    def producer():
        for name in ("a", "b", "c", "d"):
            yield name, 0
            if name == "b":
                # long enough that the two workers are done - and their slot
                # is empty - before the next item is ready
                time.sleep(0.5)

    dynamicqueue.run(producer(), 2, _sleeper,
                     lambda name: (directory, name),
                     buffer_factor=1, log=lines.append)

    assert lines == [
        "Queue: pre-filling the buffer of 2 item(s) ...",
        "Queue: buffer loaded, starting 2 worker(s) on 2 item(s)",
        "Queue: buffer ran dry, 1 worker(s) waiting for preparation",
    ]
