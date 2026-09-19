"""The dynamic queue: a worker pool whose items are prepared while it runs.

A queue of items that already exist - a list of files, a table of paths - is
handed to the worker pool ready to run. A queue whose items have to be
PREPARED first cannot be: the preparation is work of its own - probing,
extracting, deciding - and a run that prepares every item before the first
worker starts spends that work serially in front of the work it feeds, and
the run's wall clock is the two of them one after the other.

This is the queue for that shape. A producer - a generator that prepares one
item at a time - runs in this process, interleaved with the workers it feeds:
the queue loads a buffer of prepared items, starts the workers on it, and
after that only tops the buffer up as the workers spend it, so it holds an
adequate number of items at all times and not a mountain of them. The buffer
is proportional to the worker pool, twice its size by default: large enough
that a slow preparation rarely leaves a worker waiting for work, small enough
that the preparation does not run far ahead of the run.

The buffer is kept from LARGE to SMALL: the producer says how long each item
is expected to take, and a freed slot is given the largest of what is left.
The estimate may be rough - it is only the tail of the run it orders, where
the longest of what is left is what the wall clock waits for, and a rough
order still keeps that tail roughly as short as it can be.

What a worker is forked, named and joined with is the worker pool's, not a
second set of rules for a queue that happens to be fed dynamically: the same
processes, the same abnormal exits counted and said out loud, the same
Outcome. An interrupt stops the DISPATCH - the preparation included, which is
work as much as the transcription it prepares - while the workers already
running are waited for, the way :func:`workerpool.run` ends.
"""

import heapq
import multiprocessing

from medialib.lib import safety, workerpool

__all__ = ["run"]


def run(producer, jobs: int, target, arguments, buffer_factor: int = 2,
        log=None) -> "workerpool.Outcome":
    """Every prepared item through <target>, <jobs> of them at once.

    <producer> is a generator that yields ``(item, size)`` as it prepares the
    items, and it is consumed here, in this process, interleaved with the
    workers: a buffer of prepared items is loaded first, the workers start on
    it, and after that the producer is asked for more only as the buffer is
    spent, one item at a time. <size> is an estimate, in any unit the producer
    chooses, of how long the item will take, and the buffer is kept from the
    largest of it to the smallest, equal sizes in the order prepared.

    <arguments> turns one item into that worker's argument tuple, as in
    :func:`workerpool.run`. <buffer_factor> is the buffer in multiples of the
    worker pool - twice its size by default. <log> is the run's logger, for
    the queue's own lines: the pre-fill, the start, and a buffer that ran dry.
    """
    if log is None:
        log = _say_nothing
    if jobs < 1:
        jobs = 1
    buffer = jobs * buffer_factor
    if buffer < 1:
        buffer = 1

    heap: list = []
    order = 0
    running: list = []
    exhausted = False
    prefilled = False
    started = False
    waiting = False
    seen = len(workerpool.failed_workers())

    def prepared(item, size) -> None:
        # The size goes in NEGATIVE: heapq is a min-heap and the queue wants
        # the LARGEST item out first. The arrival number after it is what
        # keeps equal sizes in the order they were prepared, and it is what
        # keeps the item itself from ever being compared.
        nonlocal order
        heapq.heappush(heap, (-size, order, item))
        order += 1

    def take() -> bool:
        """One item from the producer; False when it has none left."""
        nonlocal exhausted, waiting
        try:
            item, size = next(producer)
        except StopIteration:
            exhausted = True
            return False
        prepared(item, size)
        waiting = False
        return True

    # The buffer, loaded before the first worker starts: the run waits for an
    # adequate number of prepared items and nothing more of the preparation,
    # so the start costs the buffer rather than the whole queue.
    while len(heap) < buffer and not safety.abort_requested():
        if not take():
            break
        if not prefilled:
            prefilled = True
            log("Queue: pre-filling the buffer of %d item(s) ..." % buffer)

    # The run ends when the buffer AND the preparation are spent and no
    # worker is left running: a buffer that empties while the preparation
    # still has items to give is the queue waiting on it, not the end of
    # the run.
    while heap or running or not exhausted:
        # Top the buffer up as the workers spend it, one item at a time and
        # only while there is room in it: the preparation is a job of its own,
        # and it runs here beside the workers rather than in front of them.
        if (not exhausted and not safety.abort_requested()
                and len(heap) < buffer):
            # The dry stretch, if there is one: the buffer was EMPTY with a
            # slot still free, which is the queue waiting on the preparation.
            # Said once a pull has proven the preparation still has items to
            # give - an empty buffer at the very tail of the run is the run
            # winding down, not a stall - and once per stretch of it.
            dry = not heap and len(running) < jobs and not waiting
            while len(heap) < buffer:
                if not take():
                    break
            if dry and not exhausted:
                waiting = True
                log("Queue: buffer ran dry, %d worker(s) waiting for preparation"
                    % (jobs - len(running)))

        # As many slots as there are items go to work at once: a freed slot is
        # given the next item straight away, and the LARGEST of what is left.
        while heap and len(running) < jobs and not safety.abort_requested():
            if not started:
                started = True
                log("Queue: buffer loaded, starting %d worker(s) on %d item(s)"
                    % (min(jobs, len(heap)), len(heap)))
            _size, _arrival, item = heapq.heappop(heap)
            worker = multiprocessing.Process(target=target,
                                             args=arguments(item),
                                             name=workerpool.label(item))
            worker.start()
            running.append(worker)

        if not running:
            break

        running = workerpool.reap_one(running)

    outcome = workerpool.Outcome()
    outcome.failed = workerpool.failed_workers()[seen:]
    outcome.aborted = bool(heap) or safety.abort_requested()
    return outcome


def _say_nothing(line: str) -> None:
    """The queue's voice for a caller that has no run to say it in."""
