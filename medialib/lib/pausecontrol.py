"""Making a long run pausable from the keyboard.

ffmpeg has no pause: its only interactive keys are ``q`` and the verbosity ones,
and every ffmpeg here runs with ``-nostdin`` so a worker cannot eat the console's
input. So the pause is the operating system's, not the encoder's: SIGSTOP takes a
job off the run queue wherever it is, SIGCONT puts it back, and nothing between has
to be encoder-aware. One keystroke reaches every encoder of a chunked file because
the state is FILES in the run's scratch, not shell variables - a worker in another
process cannot write back into the shell reading the keyboard, but it can read the
same flag and register itself in the same directory.

The two orderings that keep the flag and the registry agreeing without a lock:
``pause_jobs`` writes the flag BEFORE it walks the registry, and a job registers
itself BEFORE it looks at the flag. Whichever goes first, the other sees it - so a
job that starts exactly while the run is paused stops itself instead of running on.

The state lives in the environment the way the shell's globals do: ``PAUSE_JOBS``
a directory holding one empty file per running pausable job (named by its pid),
``PAUSE_FLAG`` a file that exists while the run is paused and holds the epoch second
the pause began, and ``PAUSE_ACCUM`` the seconds already spent paused. ``init``
arms them; a caller that never armed a pause finds them unset and every question
here answers "no, none, zero" rather than erroring.
"""

import glob
import os
import signal as _signal
import subprocess

__all__ = [
    "init",
    "pause_requested",
    "pausable_job_pids",
    "process_tree",
    "signal_job_tree",
    "register_pausable_job",
    "unregister_pausable_job",
    "run_pausable",
    "pause_jobs",
    "resume_jobs",
    "paused_seconds",
    "wait_while_paused",
    "kill_pausable_jobs",
    "pause_announce",
    "restore_pause_terminal",
    "pause_key_reader",
    "start_pause_keys",
    "stop_pause_keys",
    "pause_keys_active",
    "abort_requested",
    "PAUSE_ANNOUNCE_PAUSE",
    "PAUSE_ANNOUNCE_RESUME",
]

# The two lines the key reader says, the way the shell spells them. The resume line
# is a FORMAT - "$spent" is filled with the time banked so far - because what was
# spent depends on when the pause began and ended, which the reader measures.
PAUSE_ANNOUNCE_PAUSE = (
    'Paused - the video encode is stopped and the CPU/GPU is free; press "r" '
    'to resume (its memory is still held, and Ctrl+C still ends the run).'
)
PAUSE_ANNOUNCE_RESUME = "Resumed - %s spent paused so far."


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _jobs_dir() -> str:
    return _env("PAUSE_JOBS")


def _flag_path() -> str:
    return _env("PAUSE_FLAG")


def _accum_path() -> str:
    return _env("PAUSE_ACCUM")


def _tty_state() -> str:
    return _env("PAUSE_TTY_STATE")


def _set_state(name: str, value: str) -> None:
    os.environ[name] = value


def abort_requested() -> bool:
    """True once any process in this run recorded an interrupt - the flag
    ``fileSafety``'s ``requestAbort`` makes. Safe where no flag was armed: it is
    then simply always False, so a caller needs no second condition."""
    flag = _env("ABORT_FLAG")
    return bool(flag) and os.path.exists(flag)


# --- arming -------------------------------------------------------------------


def init(directory: str | None = None) -> None:
    """Arm the pause state. Inheritance wins, as it does for the abort flag: when a
    wrapper has already armed a pause for the run, the inner script shares THAT
    state, or a keypress would only reach one layer of it. Given a directory the
    state goes there (a caller that owns a RAM scratch passes one from there, so it
    goes back with the rest of the run's scratch); without one a private one is
    made."""
    if _env("PAUSE_DIR"):
        return
    if directory:
        pause_dir = directory
    else:
        import tempfile

        pause_dir = tempfile.mkdtemp(prefix="pauseControl.")
    _set_state("PAUSE_DIR", pause_dir)
    _set_state("PAUSE_JOBS", os.path.join(pause_dir, "jobs"))
    _set_state("PAUSE_FLAG", os.path.join(pause_dir, "paused"))
    _set_state("PAUSE_ACCUM", os.path.join(pause_dir, "pausedSeconds"))
    os.makedirs(_jobs_dir(), exist_ok=True)
    _remove(_flag_path())
    with open(_accum_path(), "w", encoding="ascii") as handle:
        handle.write("0\n")
    # The console's line settings as they were before anything read a keypress off
    # it, so they can be put back however the run ends. Off a terminal there is
    # nothing to capture, which is the empty state ``restore`` leaves alone.
    state = _stty_save()
    if state:
        _set_state("PAUSE_TTY_STATE", state)


def _stty_save() -> str:
    try:
        with open("/dev/tty", "rb") as tty:
            out = subprocess.run(
                ["stty", "-g"],
                stdin=tty,
                capture_output=True,
                check=False,
            )
    except (OSError, ValueError):
        return ""
    return out.stdout.decode("utf-8", "replace").strip()


def _remove(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


# --- the pause question -------------------------------------------------------


def pause_requested() -> bool:
    """True while the run is paused. Safe in a script that never armed a pause (it
    is then simply always false), so callers need no second condition."""
    flag = _flag_path()
    return bool(flag) and os.path.exists(flag)


# --- the jobs a pause reaches -------------------------------------------------


def pausable_job_pids() -> list[int]:
    """Every pid this run has registered as pausable, in the registry's own order -
    the way the shell's glob lists them, which is the directory's sorted order.
    Entries whose process is gone, or that this run does not own, are dropped as
    they are found, so a stale or recycled pid is never signalled."""
    jobs = _jobs_dir()
    if not jobs or not os.path.isdir(jobs):
        return []
    live: list[int] = []
    for name in sorted(os.listdir(jobs)):
        if not name.isdigit():
            continue
        pid = int(name)
        entry = os.path.join(jobs, name)
        proc = "/proc/{}".format(pid)
        if os.path.isdir(proc):
            # -O rather than a bare liveness: a pid this run does not own is a
            # recycled number, and signalling somebody else's process is the one
            # mistake this must not make.
            try:
                owned = os.stat(proc).st_uid == os.geteuid()
            except OSError:
                owned = False
            if not owned:
                _remove(entry)
                continue
        elif not _kill_zero(pid):
            _remove(entry)
            continue
        live.append(pid)
    return live


def _kill_zero(pid: int) -> bool:
    """The ``kill -0`` liveness the shell falls back to off /proc: true when the
    pid is alive and signalable, false when it has gone. A pid we cannot signal for
    any other reason reads the same as one that is gone, the way the shell's does."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def process_tree(pid: int) -> list[int]:
    """That pid and every descendant of it, parents first - the tree and not the pid
    alone, because what gets registered is the shell's background child, and whether
    that child IS the encoder or a subshell that forked it depends on an optimisation
    in bash. Stopping the tree takes down the helper processes too (a stderr filter,
    a decoder feeding a pipe) that would otherwise spin on a pipe nobody is
    draining."""
    if not str(pid).isdigit():
        return []
    tree = [pid]
    kids = _children(pid)
    for kid in kids:
        tree.extend(process_tree(kid))
    return tree


def _children(pid: int) -> list[int]:
    """The direct children of a pid, in the order the kernel reports them. /proc's
    per-thread ``children`` files where there is one (the shell's own reading
    order, glob-sorted), ``pgrep -P`` where there is not."""
    task = "/proc/{}/task".format(pid)
    kids: list[int] = []
    if os.path.isdir(task):
        for path in sorted(glob.glob(os.path.join(task, "*", "children"))):
            if not os.access(path, os.R_OK):
                continue
            try:
                with open(path, encoding="ascii") as handle:
                    kids.extend(int(word) for word in handle.read().split())
            except (OSError, ValueError):
                continue
    else:
        try:
            out = subprocess.run(
                ["pgrep", "-P", str(pid)], capture_output=True, check=False
            )
            kids = [int(word) for word in out.stdout.split()]
        except (OSError, ValueError):
            kids = []
    return kids


def _signal_pids(sig_name: str, pids) -> None:
    """Send one signal to a set of pids - or, under the harness, record it. A job
    that finished between being listed and being signalled is the ordinary case, not
    an error, so a delivery that reaches nothing is not a failure.

    ``PAUSE_KILL_RECORD`` is the test switch: set, the call is appended to
    ``PAUSE_KILL_LOG``, so what is asserted is the dispatch and no real process is
    left stopped. Unset, the signal goes to the kernel for real."""
    if _env("PAUSE_KILL_RECORD"):
        log = _env("PAUSE_KILL_LOG")
        if log:
            with open(log, "a", encoding="ascii") as handle:
                handle.write("{}\t{}\n".format("-" + sig_name, " ".join(map(str, pids))))
        return
    num = _SIGNALS.get(sig_name.upper())
    if num is None:
        return
    for pid in pids:
        try:
            os.kill(int(pid), num)
        except OSError:
            pass


# Built by availability: a platform lacking a signal (Windows) gets it absent
# rather than an error at import time.
_SIGNALS = {
    name: getattr(_signal, "SIG" + name)
    for name in ("STOP", "CONT", "TERM", "INT", "KILL", "HUP")
    if hasattr(_signal, "SIG" + name)
}


def signal_job_tree(sig_name: str, pid: int) -> None:
    """Send one signal to a job and everything under it. Never fails: a job that
    finished between being listed and being signalled is the ordinary case."""
    tree = process_tree(pid)
    if not tree:
        return
    _signal_pids(sig_name, tree)


# --- registering --------------------------------------------------------------


def register_pausable_job(pid: int) -> None:
    """From now until it is unregistered, p and r reach this job. A job started while
    the run is ALREADY paused stops itself here, which is what keeps a pause holding
    as the parallel queue rolls on to its next item."""
    jobs = _jobs_dir()
    if not jobs or not os.path.isdir(jobs):
        return
    entry = os.path.join(jobs, str(pid))
    try:
        with open(entry, "w", encoding="ascii") as handle:
            handle.write("")
    except OSError:
        pass
    if pause_requested():
        signal_job_tree("STOP", pid)


def unregister_pausable_job(pid: int) -> None:
    """It has finished; p and r no longer concern it."""
    jobs = _jobs_dir()
    if not jobs:
        return
    _remove(os.path.join(jobs, str(pid)))


def run_pausable(command, keep=None) -> int:
    """Run one job so that p and r can stop and continue it, and return exactly what
    it returned. The job is run in the BACKGROUND and waited for rather than in the
    foreground, for the one reason that a foreground child's pid is not something the
    caller can learn: a pause has to know what to signal.

    ``keep`` is an optional line predicate. Given one, the job's stderr is read here
    and only the lines it accepts are passed on - the shell's ``2> >(grep -v ...)``,
    kept in this process so that the filter is not one more thing a pause has to
    stop. A job whose stderr is being read stays readable while it is stopped: it
    simply writes nothing until it is continued.
    """
    import subprocess as _sp
    import sys

    if keep is None:
        process = _sp.Popen(list(command))
    else:
        process = _sp.Popen(list(command), stderr=_sp.PIPE)
    register_pausable_job(process.pid)
    stderr = process.stderr
    if keep is not None and stderr is not None:
        for raw in stderr:
            line = raw.decode("utf-8", "replace")
            if keep(line.rstrip("\r\n")):
                sys.stderr.write(line)
                sys.stderr.flush()
        stderr.close()
    rc = process.wait()
    unregister_pausable_job(process.pid)
    return rc


# --- pausing and resuming -----------------------------------------------------


def pause_jobs(now: int) -> None:
    """Pause the run: every registered job stops where it is, and everything
    registered from here on stops as it starts. The flag is written BEFORE the jobs
    are walked, which is the half of the race note at the top of this module that
    lives here."""
    flag = _flag_path()
    if not flag:
        return
    if pause_requested():
        return
    with open(flag, "w", encoding="ascii") as handle:
        handle.write("{}\n".format(now))
    for pid in pausable_job_pids():
        signal_job_tree("STOP", pid)


def resume_jobs(now: int) -> None:
    """Resume the run: the pause is banked into ``PAUSE_ACCUM`` and the flag is
    dropped BEFORE anything is continued, so a job registering at this very moment
    cannot stop itself just after everything else was let go."""
    flag = _flag_path()
    if not flag:
        return
    if not pause_requested():
        return
    started = _read_number(flag)
    accum = _read_number(_accum_path())
    if started is None:
        started = now
    if accum is None:
        accum = 0
    with open(_accum_path(), "w", encoding="ascii") as handle:
        handle.write("{}\n".format(accum + now - started))
    _remove(flag)
    for pid in pausable_job_pids():
        signal_job_tree("CONT", pid)


def _read_number(path: str) -> int | None:
    """A file's whole number, the way ``<file`` and the shell's arithmetic read it:
    a value that is not all digits settles to None rather than being passed on."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="ascii") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def paused_seconds(now: int) -> int:
    """How long this run has spent paused, in whole seconds, including a pause that
    is still going on. Prints 0 for a run that never armed a pause, so a caller can
    subtract it unconditionally."""
    accum = _read_number(_accum_path())
    if accum is None:
        accum = 0
    if pause_requested():
        started = _read_number(_flag_path())
        if started is not None:
            accum = accum + now - started
    return accum


def wait_while_paused() -> None:
    """Block the caller for as long as the run is paused - for the points BETWEEN
    pausable jobs, so that pausing holds the whole run off rather than only the
    encoder that happened to be running. The wait is a series of short sleeps rather
    than one long one so that an interrupt is noticed promptly: a shell blocked in a
    foreground sleep runs no traps until it returns, and a paused run is exactly when
    somebody decides to stop it instead."""
    import time

    while pause_requested():
        if abort_requested():
            return
        time.sleep(0.5)


def kill_pausable_jobs() -> None:
    """Continue every job and then take it down. What an interrupt handler calls: it
    is the only thing that reaches the encoders of a paused run, whose own shells
    have already been stopped or have gone.

    The CONT is not politeness: a stopped process cannot act on the TERM that
    follows it - the signal queues up behind the SIGSTOP - so a job that is not
    continued first is left stopped for ever, still holding the memory the pause
    never released."""
    for pid in pausable_job_pids():
        signal_job_tree("CONT", pid)
        signal_job_tree("TERM", pid)
        unregister_pausable_job(pid)


# --- reading the keys ---------------------------------------------------------


def pause_announce(message: str, error=None) -> None:
    """Say something to the console without landing halfway along a live status row,
    when there is one. The dependency is one-way and optional on purpose: a script
    that keeps no status row simply prints the line."""
    from medialib.lib import statusline

    statusline.clear_status()
    if error is not None:
        error.write(message + "\n")
    else:
        import sys

        sys.stderr.write(message + "\n")


def restore_pause_terminal() -> None:
    """Put the console's line settings back the way ``init`` found them. Reading
    single keypresses turns off echoing and line buffering, and a reader killed
    mid-keypress would otherwise leave the terminal that way. Always returns None, so
    it can sit in a cleanup."""
    state = _tty_state()
    if not state:
        return
    try:
        with open("/dev/tty", "wb") as tty:
            subprocess.run(
                ["stty", state],
                stdin=tty,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except (OSError, ValueError):
        pass


def pause_key_reader(stream, now: int, error=None) -> None:
    """The key reader itself, run against a key stream: ``p`` holds the run off,
    ``r`` lets it carry on, and anything else is ignored rather than acted on. The
    read has a timeout in the shell so the loop comes up for air - it is how a reader
    notices that the run has been interrupted and stops reading a console it no
    longer owns; a stream that ends for any other reason means there is no console to
    read from any more, and the reader leaves quietly and the run carries on without
    the keys, which is the same thing that happens when there was no terminal to
    begin with."""
    from medialib.lib import formatting

    while True:
        if abort_requested():
            break
        key = stream.read(1)
        if not key:
            break
        key = key.decode("utf-8", "replace") if isinstance(key, bytes) else key
        if key in ("p", "P"):
            if not pause_requested():
                pause_jobs(now)
                pause_announce(PAUSE_ANNOUNCE_PAUSE, error)
        elif key in ("r", "R"):
            if pause_requested():
                was = paused_seconds(now)
                resume_jobs(now)
                spent = formatting.fmt_hms(was)
                pause_announce(PAUSE_ANNOUNCE_RESUME % spent, error)
    restore_pause_terminal()


def start_pause_keys() -> bool:
    """Start reading p and r from the console, in the background. A no-op - silently,
    and leaving ``pause_keys_active`` false - when there is no console to read from:
    a redirected or scripted run has nobody at a keyboard, and the run has to be
    exactly as it was before this module existed."""
    if not _env("PAUSE_DIR"):
        return False
    if not _is_console():
        return False
    stop_pause_keys()
    import threading

    try:
        # Unbuffered, so a key acts when it is pressed rather than when a
        # buffer fills. Opened here rather than in the thread because a console
        # that cannot be opened is what this answers False to.
        console = os.fdopen(os.open("/dev/tty", os.O_RDONLY), "rb",
                            buffering=0)
    except OSError:
        return False

    def _run() -> None:
        try:
            pause_key_reader(console, int(_time_now()))
        finally:
            console.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _set_state("PAUSE_KEY_PID", str(thread.ident or 0))
    return True


def _is_console() -> bool:
    import sys

    return sys.stderr.isatty() and os.access("/dev/tty", os.R_OK)


def _time_now() -> int:
    import time

    return int(time.time())


def stop_pause_keys() -> None:
    """Stop the reader and give the console back. Safe to call when none is running,
    so it can sit beside the other cleanups, and always returns None."""
    pid = _env("PAUSE_KEY_PID")
    if pid:
        _set_state("PAUSE_KEY_PID", "")
    restore_pause_terminal()


def pause_keys_active() -> bool:
    """True while the keys are being read, so a startup summary can promise them only
    where they will actually work."""
    return bool(_env("PAUSE_KEY_PID"))