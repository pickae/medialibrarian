"""The worker pool every parallel command shares.

``wait -n``, which is what a queue needs: hand each worker an item, then block
until ANY ONE of them finishes and give that slot the next item straight away.
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

from medialib.lib import safety

# Every abnormal worker exit this process has seen, as (owner, label, reason).
# The owner is the pid that recorded it: a worker inherits this list across the
# fork, and the entries it inherits are the parent's to report, not its own.
_FAILURES: list = []


class Outcome:
    """What one queue did: the workers that ended abnormally, and whether the
    dispatch was cut short before the queue drained."""

    def __init__(self) -> None:
        self.failed: list = []
        self.aborted = False

    @property
    def ok(self) -> bool:
        return not self.failed


def _label(item) -> str:
    """The name a worker is given, which is what a failure is reported BY.

    A pair or a record is named by its first field: the commands hand this
    tuples of (input, output) and objects of their own, and the first field is
    the one that names the work in every case.
    """
    if isinstance(item, tuple | list) and item:
        item = item[0]
    text = item if isinstance(item, str) else str(item)
    return text[:200]


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
    _FAILURES.append((os.getpid(), label, reason))
    sys.stderr.write("\nWARNING: a worker %s and its item was not finished:\n"
                     "  %s\n" % (reason, label))
    return reason


def failed_workers() -> list:
    """The abnormal exits THIS process has reaped, as (label, reason)."""
    mine = os.getpid()
    return [(label, reason) for owner, label, reason in _FAILURES
            if owner == mine]


def forget_failures() -> None:
    """Start the tally over. For a process that runs more than one run."""
    _FAILURES[:] = []


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


def reap_one(running: list) -> list:
    """Block until one of the started workers has finished, and return the rest.

    The finished one is joined here, so a caller never has to remember to: an
    unjoined child stays a zombie for as long as the run lasts. Its exit is
    classified here for the same reason - the join is the only moment the status
    exists, and a caller that skipped it would lose the crash with it.
    """
    import multiprocessing.connection

    if not running:
        return []
    # Taken once, up front: a worker's sentinel is not worth reading again
    # once it has been joined.
    sentinels = [worker.sentinel for worker in running]
    ready = set(multiprocessing.connection.wait(sentinels))
    alive = []
    for worker, sentinel in zip(running, sentinels, strict=True):
        if sentinel in ready or not worker.is_alive():
            worker.join()
            note_exit(worker)
        else:
            alive.append(worker)
    return alive


def join_all(running: list) -> None:
    """Wait for the workers still running, classifying each one's exit. What a
    caller that keeps its own queue does with the stragglers it is holding."""
    for worker in running:
        worker.join()
        note_exit(worker)


def run(items, jobs: int, target, arguments) -> Outcome:
    """Every item through <target> in a worker process, <jobs> of them at once.

    ``arguments`` turns one item into that worker's argument tuple, which is
    what differs between the commands - the loop around it does not.

    An interrupt stops the DISPATCH, not the workers: nothing further is handed
    out, and the ones already running are waited for. That is the shell's
    behaviour, and it is why a half-written output never outlives the run that
    was making it.

    The Outcome is what the run ended up with: the workers that died and whether
    the queue drained. Ignoring it is how a command comes to print "Done" over
    files it never made.
    """
    import multiprocessing

    # A deque, because the dispatch takes from the FRONT: a list's pop(0) copies
    # what is left over on every item, which a queue of tens of thousands of
    # cheap items pays for quadratically.
    pending = collections.deque(items)
    running: list = []
    outcome = Outcome()
    seen = len(failed_workers())
    while pending or running:
        while pending and len(running) < jobs and not safety.abort_requested():
            item = pending.popleft()
            worker = multiprocessing.Process(target=target,
                                             args=arguments(item),
                                             name=_label(item))
            worker.start()
            running.append(worker)
        if not running:
            break
        running = reap_one(running)
    outcome.failed = failed_workers()[seen:]
    outcome.aborted = bool(pending) or safety.abort_requested()
    return outcome
