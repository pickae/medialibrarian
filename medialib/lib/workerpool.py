"""The worker pool every parallel command shares.

What a queue needs: hand each worker an item, then block until ANY ONE of them
finishes and give that slot the next item straight away.
On a folder of unevenly sized files the difference is most of the machine for
most of the run - a queue that instead waits for the LONGEST job of a set keeps
three idle workers for as long as the fourth lasts.
``multiprocessing.connection.wait`` over the workers' sentinels is that wait: it
returns as soon as the first of them ends, whichever one that is.

Nothing here decides how WIDE a queue is - that is the command's ``-P`` and its
own reasons - and nothing here runs the serial path: at width 1 a command does
the work in its own process rather than forking a worker per item.

What a worker's EXIT means is decided here too, because a worker is a process
and a process can end in ways its own code never sees: an uncaught exception, a
kill, a segfault in a library it called. Those are counted and said out loud,
and the run they belong to ends non-zero - an item that was asked for and never
made must not leave behind a command that printed "Done".
"""

import collections
import os
import sys

from medialib.lib import errorlog, runcontext, runlog, safety


def start_worker(target, args: tuple, name: str):
    """One worker process, started, from a forkserver where the platform's
    default would FORK this process.

    A fork copies every lock this process holds, and the thread that held one
    does not come along to release it: a worker forked while another thread was
    printing waits forever on the stream's lock the first time it prints. The
    dynamic queue prepares its items on a thread of its own, and some of that
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


def run(items, jobs: int, target, arguments, on_done=None) -> Outcome:
    """Every item through <target> in a worker process, <jobs> of them at once.

    ``arguments`` turns one item into that worker's argument tuple, which is
    what differs between the commands - the loop around it does not.
    ``on_done``, when given, is called with each item once its worker has
    ended, however it ended.

    An interrupt stops the DISPATCH, not the workers: nothing further is handed
    out, and the ones already running are waited for, so a half-written output
    never outlives the run that was making it.

    The Outcome is what the run ended up with: the workers that died and whether
    the queue drained. Ignoring it is how a command comes to print "Done" over
    files it never made.
    """
    # A deque, because the dispatch takes from the FRONT: a list's pop(0) copies
    # what is left over on every item, which a queue of tens of thousands of
    # cheap items pays for quadratically.
    pending = collections.deque(items)
    running: list = []
    outcome = Outcome()
    seen = len(failed_workers())
    items_of: dict = {}

    def ended(worker, _reason: str) -> None:
        item = items_of.pop(id(worker), None)
        if on_done is not None:
            on_done(item)

    while pending or running:
        while pending and len(running) < jobs and not safety.abort_requested():
            item = pending.popleft()
            worker = start_worker(target, arguments(item), label(item))
            items_of[id(worker)] = item
            running.append(worker)
        if not running:
            break
        running = reap_one(running, on_done=ended)
    outcome.failed = failed_workers()[seen:]
    outcome.aborted = bool(pending) or safety.abort_requested()
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


def map(items, jobs: int, work, on_done=None) -> Outcome:  # noqa: A001
    """Every item through ``work``, up to ``jobs`` at once: the one queue the
    commands share.

    ``work`` is called with one item and must pickle - a bound method of the
    run's state, or a :func:`functools.partial` over a module-level function.
    At width 1 or less the items are worked through in this process, one
    after another, each named for the errors recorded meanwhile, and the
    queue stops at an interrupt; what ``work`` returns is then kept in the
    Outcome's ``results``. Wider than that each item gets a worker process
    (:func:`run`), and the work's result is the files and counters it leaves
    behind. ``on_done``, when given, is called with each item once it is done,
    however it ended.
    """
    if jobs <= 1:
        outcome = Outcome()
        for item in items:
            if safety.abort_requested():
                outcome.aborted = True
                break
            with errorlog.work_item(item):
                outcome.results.append(work(item))
            if on_done is not None:
                on_done(item)
        return outcome
    return run(items, jobs, in_worker, lambda item: (work, item),
               on_done=on_done)
