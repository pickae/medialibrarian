"""The worker pool every parallel command shares.

What a queue needs: hand each worker an item, then block until ANY ONE of them
finishes and give that slot the next item straight away.
On a folder of unevenly sized files the difference is most of the machine for
most of the run - a queue that instead waits for the LONGEST job of a set keeps
three idle workers for as long as the fourth lasts.
``multiprocessing.connection.wait`` over the workers' sentinels is that wait: it
returns as soon as the first of them ends, whichever one that is.

Nothing here decides how WIDE a queue is - that is the command's ``-P`` and its
own reasons. At width 1 :func:`map` does the work in the run's own process
rather than forking a worker per item.

There is one queue, whether its items already exist or have to be PREPARED
first - probed, extracted, decided. A run that prepares every item before the
first worker starts spends that work serially in front of the work it feeds,
so the items are always taken from a producer on a thread of its own, beside
the workers: a buffer of prepared items is loaded, the workers start on it,
and the producer only tops it up as they spend it - twice the pool by
default, large enough that a slow preparation rarely leaves a worker waiting,
small enough that it does not run far ahead of the run. A list of files is a
producer whose items are all ready at once. The thread is what keeps one slow
preparation from stalling the dispatch: without it a producer that spends
minutes on one item would hold the whole queue inside that one pull.

The buffer is kept from LARGE to SMALL: a producer says how long each item is
expected to take, and a freed slot is given the largest of what is left. The
estimate may be rough - it orders only the tail of the run, where the longest
of what is left is what the wall clock waits for. Items given no size go out
in the order they came.

A run whose items are worked one at a time in this process has the same shape
when each item is itself parallel inside - convert-video encodes one film at a
time across every chunk worker, and preparing the next film is probes of its
own. :func:`ahead` is the queue for that run: the same producer on the same
kind of thread, with the items handed to the caller's own loop.

What a worker's EXIT means is decided here too, because a worker is a process
and a process can end in ways its own code never sees: an uncaught exception, a
kill, a segfault in a library it called. Those are counted and said out loud,
and the run they belong to ends non-zero - an item that was asked for and never
made must not leave behind a command that printed "Done".
"""

import collections
import heapq
import os
import sys
import threading
import time

from medialib.lib import errorlog, runcontext, runlog, safety

# How often a wait with nothing to wake it looks at the abort flag.
_POLL_SECONDS = 0.5


def start_worker(target, args: tuple, name: str):
    """One worker process, started, from a forkserver where the platform's
    default would FORK this process.

    A fork copies every lock this process holds, and the thread that held one
    does not come along to release it: a worker forked while another thread was
    printing waits forever on the stream's lock the first time it prints. The
    queue prepares its items on a thread of its own, and some of that
    preparation runs worker pools of its own, so neither side may fork. A
    forkserver's children are forked from a process with no other threads, and
    it is what Python has defaulted to since 3.14; a platform that spawns
    already starts clean.
    """
    import multiprocessing
    import multiprocessing.process

    # Through run_in_worker, which hands the worker the run, names the item
    # for every error the worker records and keeps an exception it did not
    # catch.
    wrapped = (runcontext.snapshot(), target, tuple(args), name)
    worker: multiprocessing.process.BaseProcess
    if (multiprocessing.get_context().get_start_method() == "fork"
            and "forkserver" in multiprocessing.get_all_start_methods()):
        worker = multiprocessing.get_context("forkserver").Process(
            target=run_in_worker, args=wrapped, name=name)
    else:
        worker = multiprocessing.Process(target=run_in_worker,
                                         args=wrapped, name=name)
    worker.start()
    return worker


def run_in_worker(run, target, args: tuple, label: str) -> None:
    """A worker's body. The parent's run is adopted first - the one place a
    worker takes on the run's state (`medialib/lib/runcontext.py`) - then its
    item is named for every record made inside it, and an exception it did
    not catch is kept before the worker ends with status 1, which the parent
    then reports as the worker it lost.

    An interrupt that lands before the worker's own trap is in place arrives as
    a KeyboardInterrupt, and leaves the way the trap would have: the run marked
    as stopping and the queue told so. Let through, multiprocessing would print
    it to a stderr the interrupted parent may already have closed, and the
    BrokenPipeError from that would be kept as a crash."""
    try:
        runcontext.adopt(run)
        runlog.install_hooks()
        with errorlog.working_on(label if os.path.exists(label) else "",
                                 item=label):
            try:
                target(*args)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                runlog.record_crash(exc, where=label)
                raise SystemExit(1) from exc
    except KeyboardInterrupt:
        safety.request_abort()
        raise SystemExit(safety.XARGS_STOP_EXIT_STATUS) from None


class Outcome:
    """What one queue did: the workers that ended abnormally, and whether the
    dispatch was cut short before the queue drained."""

    def __init__(self) -> None:
        self.failed: list = []
        self.aborted = False
        # What the work returned, item by item - on the serial path only.
        self.results: list = []

    @property
    def ok(self) -> bool:
        return not self.failed


# The name a worker is given, which is what a failure is reported BY.
label = runcontext.label


def _abnormal_exit(worker) -> str:
    """Why this worker's exit was not an ordinary finish, or ``""``.

    Two exits are ordinary. Zero is the item done. The queue-stopping status is
    the interrupt: the worker was asked to stop and said so, which is the run
    ending tidily and not an item that failed.
    """
    code = worker.exitcode
    if code is None or code == 0:
        return ""
    if code == safety.XARGS_STOP_EXIT_STATUS:
        return ""
    if code < 0:
        return "killed by signal %d" % -code
    return "exited with status %d" % code


def note_exit(worker) -> str:
    """Record and report an abnormal exit of a worker that has been joined.

    Said at once rather than gathered for the end: the line belongs beside the
    output of the item that is missing, and a run that is cut short after it
    still leaves the reason on screen.
    """
    if worker.exitcode == safety.XARGS_STOP_EXIT_STATUS:
        # Not a failure but a request: stop handing work out. The worker records
        # the interrupt itself before it goes, and this says it again where the
        # dispatch reads it - a flag that could not be written would otherwise
        # leave the queue working through the rest of the run.
        safety.request_abort()
        return ""
    reason = _abnormal_exit(worker)
    if not reason:
        return ""
    label = worker.name or "a worker"
    runcontext.current().failures.append((label, reason))
    sys.stderr.write("\nWARNING: a worker %s and its item was not finished:\n"
                     "  %s\n" % (reason, label))
    causes = [errorlog.signal_cause(-worker.exitcode)] if worker.exitcode < 0 else []
    errorlog.record("worker-died", "a worker %s and its item was not finished: %s"
                    % (reason, label), files=[label] if os.path.exists(label) else [],
                    causes=[cause for cause in causes if cause], stack=False)
    return reason


def failed_workers() -> list:
    """The abnormal exits THIS process has reaped, as (label, reason)."""
    return list(runcontext.current().failures)


def forget_failures() -> None:
    """Start the tally over. For a process that runs more than one run."""
    runcontext.current().failures[:] = []


def report_failures(stream=None) -> int:
    """The closing recap of the workers that ended abnormally, and how many."""
    failures = failed_workers()
    if not failures:
        return 0
    out = sys.stderr if stream is None else stream
    out.write("%d item(s) were not finished: a worker ended abnormally on "
              "each\n" % len(failures))
    for label, reason in failures:
        out.write("  %s (%s)\n" % (label, reason))
    return len(failures)


def exit_status(status: int = 0) -> int:
    """The status a run ends with: its own, unless a worker of it died.

    Called where a command would otherwise ``return 0``. A per-item failure the
    worker handled itself is not this - those are reported by the worker and are
    deliberately not fatal - only an exit that bypassed it.
    """
    if failed_workers():
        report_failures()
        return 1
    return status


def reap_one(running: list, wake=None, timeout=None, on_done=None) -> list:
    """Block until one of the started workers has finished, and return the rest.

    The finished one is joined here, so a caller never has to remember to: an
    unjoined child stays a zombie for as long as the run lasts. Its exit is
    classified here for the same reason - the join is the only moment the status
    exists, and a caller that skipped it would lose the crash with it.

    <wake>, a readable file descriptor, and <timeout> end the wait early too,
    with every worker still running handed back; draining <wake> is the
    caller's. <on_done>, when given, is called with each worker joined here
    and the reason its exit was abnormal, "" for an ordinary one.
    """
    import multiprocessing.connection

    if not running and wake is None:
        return []
    # Taken once, up front: a worker's sentinel is not worth reading again
    # once it has been joined.
    sentinels = [worker.sentinel for worker in running]
    waited = sentinels + ([wake] if wake is not None else [])
    ready = set(multiprocessing.connection.wait(waited, timeout))
    alive = []
    for worker, sentinel in zip(running, sentinels, strict=True):
        if sentinel in ready or not worker.is_alive():
            worker.join()
            reason = note_exit(worker)
            if on_done is not None:
                on_done(worker, reason)
        else:
            alive.append(worker)
    return alive


def join_all(running: list) -> None:
    """Wait for the workers still running, classifying each one's exit. What a
    caller that keeps its own queue does with the stragglers it is holding."""
    for worker in running:
        worker.join()
        note_exit(worker)


def run(items, jobs: int, target, arguments, on_done=None, *,
        prepared: bool = False, buffer_factor: int = 2, log=None,
        start_after: int = 0) -> Outcome:
    """Every item through <target> in a worker process, <jobs> of them at once.

    <items> is a plain iterable of items, or - with <prepared> - a producer:
    a generator that yields ``(item, size)`` as it prepares the items. Either
    way it is consumed on a thread of its own beside the workers, into a
    buffer of <buffer_factor> times the worker pool, kept from the largest
    <size> to the smallest, equal sizes in the order they came. A plain
    iterable's items all have the same size, so they go out in its order.

    ``arguments`` turns one item into that worker's argument tuple, which is
    what differs between the commands - the loop around it does not.
    ``on_done``, when given, is called with each item once its worker has
    ended, however it ended - on the dispatch's thread, which is the caller's.
    <log> is the run's logger, for the queue's own lines: the pre-fill, the
    start, and a buffer that ran dry. <start_after>, when given, starts the
    workers once that many items are prepared rather than the whole buffer -
    for a queue whose first items are slow to prepare and whose workers each
    keep busy a while on one; the rest of the buffer fills while they work.

    An interrupt stops the DISPATCH, the preparation included, not the
    workers: nothing further is handed out, and the ones already running are
    waited for, so a half-written output never outlives the run that was
    making it. What the producer raised is raised here once they have ended.

    The Outcome is what the run ended up with: the workers that died and whether
    the queue drained. Ignoring it is how a command comes to print "Done" over
    files it never made.
    """
    if log is None:
        log = _say_nothing
    if not prepared:
        items = ((item, 0) for item in items)
    if jobs < 1:
        jobs = 1
    buffer = jobs * buffer_factor
    if buffer < 1:
        buffer = 1

    heap: list = []
    running: list = []
    started = False
    seen = len(failed_workers())
    items_of: dict = {}

    def ended(worker, _reason: str) -> None:
        item = items_of.pop(id(worker), None)
        if on_done is not None:
            on_done(item)
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
                    item, size = next(items)
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

    feeder = threading.Thread(target=feed, name="workerpool-producer",
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
            if have and not prefilled and not start_after:
                prefilled = True
                log("Queue: pre-filling the buffer of %d item(s) ..." % buffer)
            if (have >= (start_after or buffer) or done
                    or safety.abort_requested()):
                break
            reap_one([], wake_read, _POLL_SECONDS)
            _drain(wake_read)
        raise_failure()

        # The dry stretch, if there is one: the buffer EMPTY with a slot still
        # free, which is the queue waiting on the preparation. Said once an item
        # arriving has proven the preparation still had items to give - an
        # empty buffer at the very tail of the run is the run winding down, not
        # a stall - and once per stretch of it.
        dry_waiting = 0
        # An early start (<start_after>) fills its slots as the items arrive,
        # and a slot not yet filled once is the ramp-up, not a stall: dry
        # stretches count from the moment every slot has been busy.
        ramped = not start_after
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
                    elif start_after:
                        log("Queue: starting on the first %d prepared "
                            "item(s); the buffer of %d fills meanwhile"
                            % (len(heap), buffer))
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
                worker = start_worker(target, arguments(item), label(item))
                items_of[id(worker)] = item
                running.append(worker)
            ramped = ramped or len(running) >= jobs
            if (empty and not finished and len(running) < jobs and started
                    and ramped and not dry_waiting
                    and not safety.abort_requested()):
                dry_waiting = jobs - len(running)

            if not running and (safety.abort_requested()
                                or (empty and finished)):
                break
            running = reap_one(running, wake_read, _POLL_SECONDS, ended)
            _drain(wake_read)
        raise_failure()
    finally:
        with room:
            room.notify_all()
        os.close(wake_read)

    outcome = Outcome()
    outcome.failed = failed_workers()[seen:]
    with room:
        outcome.aborted = bool(heap) or safety.abort_requested()
    return outcome


def in_worker(work, item):
    """One item through ``work``, in a worker process: the worker's interrupt
    handling installed first. Here and not in the work, because the serial
    path runs that same work in the RUN's own process, where the worker's
    handler would replace the run's and a Ctrl+C would leave with the
    queue-stopping status instead of the run's own and without its closing
    report. The run itself - its scratch, its disk turns, its console - the
    worker was handed as it started (:func:`run_in_worker`)."""
    safety.trap_worker_abort()
    return work(item)


def map(items, jobs: int, work, on_done=None, *,  # noqa: A001
        prepared: bool = False, look_ahead: int = 0, discard=None,
        buffer_factor: int = 2, log=None, start_after: int = 0) -> Outcome:
    """Every item through ``work``, up to ``jobs`` at once: the one queue the
    commands share.

    ``work`` is called with one item and must pickle - a bound method of the
    run's state, or a :func:`functools.partial` over a module-level function.
    <items> is a plain iterable, or with <prepared> a producer of
    ``(item, size)``, as in :func:`run`.

    At width 1 or less the items are worked through in this process, one
    after another, each named for the errors recorded meanwhile, and the
    queue stops at an interrupt; what ``work`` returns is then kept in the
    Outcome's ``results``. A producer is pulled in step with the work there,
    unless <look_ahead> asks for that many items prepared ahead of it on a
    thread (:func:`ahead`, which hands what it never gave out to <discard>).
    Wider than that each item gets a worker process (:func:`run`, which the
    remaining arguments are for), and the work's result is the files and
    counters it leaves behind. ``on_done``, when given, is called with each
    item once it is done, however it ended.
    """
    if jobs <= 1:
        if prepared and look_ahead:
            queue = ahead(items, look_ahead, log=log, discard=discard)
        elif prepared:
            queue = (item for item, _size in items)
        else:
            queue = iter(items)
        outcome = Outcome()
        try:
            for item in queue:
                if safety.abort_requested():
                    outcome.aborted = True
                    if discard is not None:
                        discard(item)
                    break
                with errorlog.work_item(item):
                    outcome.results.append(work(item))
                if on_done is not None:
                    on_done(item)
        finally:
            close = getattr(queue, "close", None)
            if close is not None:
                close()
        return outcome
    return run(items, jobs, in_worker, lambda item: (work, item),
               on_done=on_done, prepared=prepared,
               buffer_factor=buffer_factor, log=log, start_after=start_after)


def ahead(producer, buffer: int = 2, log=None, discard=None):
    """The items of <producer>, prepared up to <buffer> ahead of the caller.

    The serial twin of :func:`run`: <producer> yields ``(item, size)`` the same
    way and is consumed on a thread of its own, but the items are handed out
    here, one at a time, for the caller to work in its own process. The thread
    keeps up to <buffer> prepared items waiting and prepares the next as soon
    as one is taken, so a preparation that is slow for one item - or a stretch
    of items it prepares only to pass over - is spent while the caller is still
    busy with an earlier one rather than in front of the next.

    The items come out in the order they were prepared and <size> is not used:
    with one item worked at a time the wall clock is their sum whatever the
    order, and a run that works through a folder is easier to follow in the
    folder's own order.

    The generator ends when the producer does, or on an interrupt, and its end
    - an interrupt, the caller breaking off, or an error - waits for the
    producer to stop: nothing the preparation registers may land after the
    run's cleanup. What was prepared and never handed out is given to
    <discard>, one item at a time, so whatever it holds is let go of. An
    exception the producer raised is raised here, where the item it was
    preparing would have been handed out.

    <log> is the run's logger, for the one line the queue says itself: how long
    the caller waited on an empty buffer, said once the item it waited for
    arrives.
    """
    if log is None:
        log = _say_nothing
    if buffer < 1:
        buffer = 1

    held: collections.deque = collections.deque()
    room = threading.Condition()
    done = False
    stopping = False
    failure: BaseException | None = None

    def feed() -> None:
        nonlocal done, failure
        try:
            while True:
                with room:
                    while (len(held) >= buffer and not stopping
                           and not safety.abort_requested()):
                        room.wait(_POLL_SECONDS)
                    if stopping or safety.abort_requested():
                        break
                try:
                    item, _size = next(producer)
                except StopIteration:
                    break
                with room:
                    held.append(item)
                    room.notify_all()
        except BaseException as raised:  # noqa: BLE001 - re-raised by ahead()
            failure = raised
        finally:
            with room:
                done = True
                room.notify_all()

    feeder = threading.Thread(target=feed, name="workerpool-ahead",
                              daemon=True)
    feeder.start()
    handed = 0
    try:
        while True:
            waited_from = 0.0
            with room:
                while not held and not done and not safety.abort_requested():
                    if not waited_from:
                        waited_from = time.monotonic()
                    room.wait(_POLL_SECONDS)
                if safety.abort_requested():
                    return
                if not held:
                    if failure is not None:
                        raise failure
                    return
                item = held.popleft()
                room.notify_all()
            # The first item is the run's start, which waits on a preparation
            # by definition; any later wait is the caller idle on the buffer.
            waited = time.monotonic() - waited_from if waited_from else 0.0
            if handed and waited >= 1:
                log("Queue: buffer ran dry, waited %d s for the next item's "
                    "preparation" % waited)
            handed += 1
            yield item
    finally:
        with room:
            stopping = True
            room.notify_all()
        feeder.join()
        if discard is not None:
            while held:
                discard(held.popleft())


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
