"""The dynamic queue: a worker pool whose items are prepared while it runs.

A queue of items that already exist - a list of files, a table of paths - is
handed to the worker pool ready to run. A queue whose items have to be
PREPARED first cannot be: the preparation is work of its own - probing,
extracting, deciding - and a run that prepares every item before the first
worker starts spends that work serially in front of the work it feeds, and
the run's wall clock is the two of them one after the other.

This is the queue for that shape. A producer - a generator that prepares one
item at a time - runs on a thread of its own in this process, beside the
workers it feeds: the queue loads a buffer of prepared items, starts the
workers on it, and after that the producer only tops the buffer up as the
workers spend it, so it holds an adequate number of items at all times and not
a mountain of them. The buffer is proportional to the worker pool, twice its
size by default: large enough that a slow preparation rarely leaves a worker
waiting for work, small enough that the preparation does not run far ahead of
the run.

The thread is what keeps one slow preparation from stalling the dispatch. A
producer that spends minutes on a single item - convert-audio planning every
chunked file at once, say - would otherwise hold the whole queue inside that
one pull, and the buffer it had already loaded would sit unused while the
workers drained one by one.

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
import os
import threading

from medialib.lib import safety, workerpool

__all__ = ["run"]

# How often a wait with nothing to wake it looks at the abort flag.
_POLL_SECONDS = 0.5


def run(producer, jobs: int, target, arguments, buffer_factor: int = 2,
        log=None) -> "workerpool.Outcome":
    """Every prepared item through <target>, <jobs> of them at once.

    <producer> is a generator that yields ``(item, size)`` as it prepares the
    items, and it is consumed here, in this process, on a thread of its own
    beside the workers: a buffer of prepared items is loaded first, the workers start on
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
    running: list = []
    started = False
    seen = len(workerpool.failed_workers())
    # What the producer thread and the dispatch share, under <room>: the buffer
    # itself, whether the producer is finished, and what it raised if it did
    # not finish cleanly - handed to the dispatch to raise, as the pull that
    # raised it used to.
    room = threading.Condition()
    order = 0
    done = False
    failure: BaseException | None = None
    # Every item prepared, and the producer's end, is a byte down this pipe:
    # the dispatch waits on it beside the workers' sentinels, so a new item
    # wakes a free slot as promptly as a finished worker does.
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)

    def feed() -> None:
        nonlocal order, done, failure
        try:
            while True:
                with room:
                    while len(heap) >= buffer and not safety.abort_requested():
                        room.wait(_POLL_SECONDS)
                if safety.abort_requested():
                    break
                try:
                    item, size = next(producer)
                except StopIteration:
                    break
                with room:
                    # The size goes in NEGATIVE: heapq is a min-heap and the
                    # queue wants the LARGEST item out first. The arrival number
                    # after it is what keeps equal sizes in the order they were
                    # prepared, and it is what keeps the item itself from ever
                    # being compared.
                    heapq.heappush(heap, (-size, order, item))
                    order += 1
                _nudge(wake_write)
        except BaseException as raised:  # noqa: BLE001 - re-raised by run()
            failure = raised
        finally:
            done = True
            _nudge(wake_write)
            # Closed by this thread, never by the dispatch: a thread still
            # inside the producer when an interrupt ends the run would write
            # its last byte into whatever file had been given that number
            # since.
            os.close(wake_write)

    def raise_failure() -> None:
        if failure is not None:
            raise failure

    feeder = threading.Thread(target=feed, name="dynamicqueue-producer",
                              daemon=True)
    try:
        feeder.start()

        # The buffer, loaded before the first worker starts: the run waits for
        # an adequate number of prepared items and nothing more of the
        # preparation, so the start costs the buffer rather than the whole
        # queue.
        prefilled = False
        while True:
            with room:
                have = len(heap)
            if have and not prefilled:
                prefilled = True
                log("Queue: pre-filling the buffer of %d item(s) ..." % buffer)
            if have >= buffer or done or safety.abort_requested():
                break
            workerpool.reap_one([], wake_read, _POLL_SECONDS)
            _drain(wake_read)
        raise_failure()

        # The dry stretch, if there is one: the buffer EMPTY with a slot still
        # free, which is the queue waiting on the preparation. Said once an item
        # arriving has proven the preparation still had items to give - an
        # empty buffer at the very tail of the run is the run winding down, not
        # a stall - and once per stretch of it.
        dry_waiting = 0
        # The run ends when the buffer AND the preparation are spent and no
        # worker is left running: a buffer that empties while the preparation
        # still has items to give is the queue waiting on it, not the end of
        # the run.
        while True:
            raise_failure()
            # As many slots as there are items go to work at once: a freed slot
            # is given the next item straight away, and the LARGEST of what is
            # left.
            taken: list = []
            with room:
                if heap and dry_waiting:
                    log("Queue: buffer ran dry, %d worker(s) waiting for "
                        "preparation" % dry_waiting)
                    dry_waiting = 0
                if (heap and not started and len(running) < jobs
                        and not safety.abort_requested()):
                    started = True
                    workers = min(jobs, len(heap))
                    # A producer already finished before the first worker
                    # starts has handed over the WHOLE queue, and that is the
                    # more useful thing to say than that a buffer it never
                    # filled was loaded.
                    if done:
                        log("Queue: all %d item(s) prepared, starting %d "
                            "worker(s)" % (len(heap), workers))
                    else:
                        log("Queue: buffer loaded, starting %d worker(s) on "
                            "%d item(s)" % (workers, len(heap)))
                while (heap and len(running) + len(taken) < jobs
                       and not safety.abort_requested()):
                    taken.append(heapq.heappop(heap)[2])
                if taken:
                    room.notify()
                # Read together with the buffer: the producer's last item goes in
                # before it says it is done, so a finished producer seen here
                # has left nothing unseen.
                empty, finished = not heap, done
            for item in taken:
                running.append(workerpool.start_worker(
                    target, arguments(item), workerpool.label(item)))
            if (empty and not finished and len(running) < jobs and started
                    and not dry_waiting and not safety.abort_requested()):
                dry_waiting = jobs - len(running)

            if not running and (safety.abort_requested()
                                or (empty and finished)):
                break
            running = workerpool.reap_one(running, wake_read, _POLL_SECONDS)
            _drain(wake_read)
        raise_failure()
    finally:
        with room:
            room.notify_all()
        os.close(wake_read)

    outcome = workerpool.Outcome()
    outcome.failed = workerpool.failed_workers()[seen:]
    with room:
        outcome.aborted = bool(heap) or safety.abort_requested()
    return outcome


def _nudge(fd: int) -> None:
    """One byte down the wake pipe; a pipe whose reading end the run has
    already closed has nobody left to wake."""
    try:
        os.write(fd, b"x")
    except OSError:
        pass


def _drain(fd: int) -> None:
    try:
        while os.read(fd, 4096):
            pass
    except (BlockingIOError, OSError):
        pass


def _say_nothing(line: str) -> None:
    """The queue's voice for a caller that has no run to say it in."""
