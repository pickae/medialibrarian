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
"""

from medialib.lib import safety


def reap_one(running: list) -> list:
    """Block until one of the started workers has finished, and return the rest.

    The finished one is joined here, so a caller never has to remember to: an
    unjoined child stays a zombie for as long as the run lasts.
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
        else:
            alive.append(worker)
    return alive


def run(items, jobs: int, target, arguments) -> None:
    """Every item through <target> in a worker process, <jobs> of them at once.

    ``arguments`` turns one item into that worker's argument tuple, which is
    what differs between the commands - the loop around it does not.

    An interrupt stops the DISPATCH, not the workers: nothing further is handed
    out, and the ones already running are waited for. That is the shell's
    behaviour, and it is why a half-written output never outlives the run that
    was making it.
    """
    import multiprocessing

    pending = list(items)
    running: list = []
    while pending or running:
        while pending and len(running) < jobs and not safety.abort_requested():
            worker = multiprocessing.Process(target=target,
                                             args=arguments(pending.pop(0)))
            worker.start()
            running.append(worker)
        if not running:
            return
        running = reap_one(running)
