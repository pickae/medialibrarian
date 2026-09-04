"""The white box for medialib/lib/pausecontrol.py.

The SIGSTOP/SIGCONT pause that holds a run's parallel jobs off the run queue,
the file state in the run's scratch that lets a keypress in one process reach
the jobs in every other, the second-counting that keeps a pause out of a run's
throughput figures, and the p/r key mapping that drives the whole thing. What
is pinned here: the exact signal each job's tree is handed and in what order,
the accounting arithmetic at its branch points, and the key-to-action mapping
with the line each says.

The signals never reach the kernel here: ``PAUSE_KILL_RECORD`` is set, so a signal
is appended to a log rather than sent, which is what lets a stopped job be tested
without leaving a real process stopped for ever. The jobs themselves are
real stand-in processes, because the liveness filter and the tree walk read the
real ``/proc`` - a pid that is alive and one that is gone must come out different.
"""

import os
import subprocess
import sys
import time

import pytest

from medialib.lib import pausecontrol as pc

pytestmark = pytest.mark.stubbed


@pytest.fixture
def pause_state(tmp_path, monkeypatch):
    """A fresh pause state: the three files armed the way ``init`` arms them, the
    signals recorded to a log instead of sent, and the environment pointed at them.
    Yields the paths so a test can inspect or stage the state directly."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    flag = tmp_path / "paused"
    accum = tmp_path / "pausedSeconds"
    accum.write_text("0\n", encoding="ascii")
    kill_log = tmp_path / "kill.log"
    env = {
        "PAUSE_DIR": str(tmp_path),
        "PAUSE_JOBS": str(jobs),
        "PAUSE_FLAG": str(flag),
        "PAUSE_ACCUM": str(accum),
        "PAUSE_KILL_RECORD": "1",
        "PAUSE_KILL_LOG": str(kill_log),
        "PAUSE_TTY_STATE": "",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ABORT_FLAG", raising=False)
    return {"jobs": jobs, "flag": flag, "accum": accum, "kill_log": kill_log}


def _signals(kill_log) -> list[tuple[str, list[str]]]:
    """The recorded signals, in send order: the signal name (no dash) and the pids."""
    if not kill_log.exists():
        return []
    out = []
    for line in kill_log.read_text(encoding="ascii").splitlines():
        if not line:
            continue
        sig, _, pids = line.partition("\t")
        out.append((sig.lstrip("-"), pids.split()))
    return out


def _register(jobs, *pids) -> None:
    for pid in pids:
        (jobs / str(pid)).write_text("", encoding="ascii")


# --- the accounting ------------------------------------------------------------


def test_paused_seconds_unarmed(pause_state):
    assert pc.paused_seconds(1_000_000) == 0


def test_paused_seconds_banks_the_accum_only_when_not_paused(pause_state):
    pause_state["accum"].write_text("40\n", encoding="ascii")
    assert pc.paused_seconds(1_000_000) == 40


def test_paused_seconds_adds_the_pause_in_progress(pause_state):
    pause_state["accum"].write_text("40\n", encoding="ascii")
    # the flag began 5 s before the canned now: 40 banked + 5 in progress
    pause_state["flag"].write_text("999995\n", encoding="ascii")
    assert pc.paused_seconds(1_000_000) == 45


def test_paused_seconds_a_pause_of_zero_seconds_banks_zero(pause_state):
    pause_state["flag"].write_text("1000000\n", encoding="ascii")
    assert pc.paused_seconds(1_000_000) == 0


def test_paused_seconds_a_flag_that_is_not_a_number_is_ignored(pause_state):
    pause_state["flag"].write_text("abc\n", encoding="ascii")
    assert pc.paused_seconds(1_000_000) == 0


def test_resume_banks_the_pause_into_the_accum(pause_state):
    pause_state["accum"].write_text("10\n", encoding="ascii")
    # the pause began 100 s before the canned now: 10 banked + 100 in progress
    pause_state["flag"].write_text("999900\n", encoding="ascii")
    pc.resume_jobs(1_000_000)
    assert not pc.pause_requested()
    assert pause_state["flag"].exists() is False
    assert pause_state["accum"].read_text().strip() == "110"


def test_resume_with_no_pause_is_a_noop(pause_state):
    pause_state["accum"].write_text("7\n", encoding="ascii")
    pc.resume_jobs(1_000_000)
    assert pause_state["accum"].read_text().strip() == "7"


def test_resume_unarmed_is_a_noop(pause_state, monkeypatch):
    monkeypatch.delenv("PAUSE_FLAG")
    pause_state["accum"].write_text("3\n", encoding="ascii")
    pc.resume_jobs(1_000_000)
    assert pause_state["accum"].read_text().strip() == "3"


# --- pausing and resuming ------------------------------------------------------


def test_pause_jobs_writes_the_flag_before_signalling(pause_state):
    assert not pc.pause_requested()
    pc.pause_jobs(500)
    assert pc.pause_requested()
    assert pause_state["flag"].read_text().strip() == "500"


def test_pause_jobs_while_paused_is_a_noop(pause_state):
    pause_state["flag"].write_text("100\n", encoding="ascii")
    pc.pause_jobs(500)
    # the earlier pause time is kept, not overwritten
    assert pause_state["flag"].read_text().strip() == "100"


def test_pause_jobs_unarmed_is_a_noop(pause_state, monkeypatch):
    monkeypatch.delenv("PAUSE_FLAG")
    pc.pause_jobs(500)
    assert not pc.pause_requested()


# --- the registry and the signals it reaches -----------------------------------


def test_register_pausable_job_stops_itself_when_the_run_is_paused(pause_state):
    pause_state["flag"].write_text("100\n", encoding="ascii")
    pc.register_pausable_job(4242)
    assert (pause_state["jobs"] / "4242").exists()
    assert _signals(pause_state["kill_log"]) == [("STOP", ["4242"])]


def test_register_pausable_job_runs_on_unpaused(pause_state):
    pc.register_pausable_job(4242)
    assert (pause_state["jobs"] / "4242").exists()
    assert _signals(pause_state["kill_log"]) == []


def test_unregister_pausable_job_drops_the_entry(pause_state):
    _register(pause_state["jobs"], 4242)
    pc.unregister_pausable_job(4242)
    assert not (pause_state["jobs"] / "4242").exists()


def _a_pid_with_no_process():
    """A pid the registry walks will find nothing behind.

    What these cases need is a STALE entry, and "one above /proc/sys/kernel/
    pid_max" was one way to be certain of that - a Linux way, resting on a file
    macOS does not have, where it raised before asserting anything. A pid this
    process started and then REAPED is the same certainty on any POSIX host, and
    it is the property the cases are actually about rather than a fact about how
    the kernel numbers things.

    Reaped and not merely exited: a child that has not been waited for is a
    zombie, which is still an entry in the process table and still answers
    signal 0 - so it would read as live and the case would assert the opposite
    of what it means to.
    """
    spent = subprocess.Popen(
        [sys.executable, "-c", ""],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    spent.wait()
    # The kernel hands pids out ascending and wraps, so the number is not about
    # to come round again - but a case that silently got a LIVE pid here would
    # assert nothing, so it is checked rather than assumed.
    assert not pc._kill_zero(spent.pid), (  # noqa: SLF001
        "pid %d was reused between being reaped and being used as a stale entry"
        % spent.pid)
    return spent.pid


def _live_job():
    """A real stand-in job, so the liveness filter keeps it: a pid that is not a live
    process is stale, and the registry walks drop it before any signal is sent."""
    job = subprocess.Popen(
        ["sleep", "300"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return job


def test_kill_pausable_jobs_continues_before_it_terms_and_drops_the_entry(pause_state):
    job = _live_job()
    try:
        _register(pause_state["jobs"], job.pid)
        pc.kill_pausable_jobs()
        assert _signals(pause_state["kill_log"]) == [
            ("CONT", [str(job.pid)]),
            ("TERM", [str(job.pid)]),
        ]
        assert not (pause_state["jobs"] / str(job.pid)).exists()
    finally:
        job.kill()
        job.wait()


# --- the liveness filter and the tree, on real processes -----------------------


def test_pausable_job_pids_drops_a_pid_that_has_gone(pause_state):
    live = subprocess.Popen(
        ["sleep", "300"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    gone = _a_pid_with_no_process()
    _register(pause_state["jobs"], live.pid, gone)
    try:
        assert pc.pausable_job_pids() == [live.pid]
        # the stale entry is removed as it is found, so a second asking sees only
        # the live one and no longer has to drop it
        assert pc.pausable_job_pids() == [live.pid]
        assert not (pause_state["jobs"] / str(gone)).exists()
        assert (pause_state["jobs"] / str(live.pid)).exists()
    finally:
        live.kill()
        live.wait()


def test_process_tree_reaches_the_job_descendants_first(pause_state):
    parent = subprocess.Popen(
        ["bash", "-c", "sleep 300 & sleep 300 & wait"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # the fork happens between two reads of the tree, so wait for it the way
        # the shell's own callers do: until the tree holds more than the parent
        tree = [parent.pid]
        for _ in range(100):
            tree = pc.process_tree(parent.pid)
            if len(tree) >= 3:
                break
            time.sleep(0.05)
        assert tree[0] == parent.pid  # parents first
        assert len(tree) == 3
        kids = set(tree[1:])
        assert kids == {
            int(word)
            for word in subprocess.run(
                ["pgrep", "-P", str(parent.pid)], capture_output=True
            ).stdout.split()
        }
    finally:
        for word in subprocess.run(
            ["pgrep", "-P", str(parent.pid)], capture_output=True
        ).stdout.split():
            os.kill(int(word), 9)
        parent.kill()
        parent.wait()


def test_signal_job_tree_never_fails_on_a_job_that_has_gone(pause_state):
    # a job that finished between being listed and being signalled is the ordinary
    # case: the signal reaches nothing and is not an error
    pc.signal_job_tree("STOP", _a_pid_with_no_process())
    assert True  # reaching here is the point: no exception


# --- the key mapping -----------------------------------------------------------


def _read_keys(state, keys: bytes) -> str:
    import io

    err = io.StringIO()
    pc.pause_key_reader(io.BytesIO(keys), 1_000_000, error=err)
    return err.getvalue()


def test_key_reader_p_and_r_and_their_announces(pause_state):
    out = _read_keys(pause_state, b"p")
    assert pc.pause_requested()
    assert out.count(pc.PAUSE_ANNOUNCE_PAUSE) == 1
    out = _read_keys(pause_state, b"r")
    assert not pc.pause_requested()
    assert "Resumed - " in out


def test_key_reader_a_second_p_says_nothing_more(pause_state):
    out = _read_keys(pause_state, b"pp")
    assert out.count(pc.PAUSE_ANNOUNCE_PAUSE) == 1


def test_key_reader_a_second_r_says_nothing_more(pause_state):
    out = _read_keys(pause_state, b"pr")
    assert "Resumed - " in out
    out = _read_keys(pause_state, b"r")  # already resumed
    assert "Resumed - " not in out


def test_key_reader_ignores_a_key_that_means_nothing_here(pause_state):
    out = _read_keys(pause_state, b"q")
    assert out == ""
    assert not pc.pause_requested()


def test_key_reader_takes_the_uppercase_forms(pause_state):
    out = _read_keys(pause_state, b"PR")
    assert pc.PAUSE_ANNOUNCE_PAUSE in out
    assert "Resumed - " in out
    assert not pc.pause_requested()


def test_key_reader_reports_the_time_banked(pause_state):
    # the pause began 10 s before the canned now
    pause_state["flag"].write_text("999990\n", encoding="ascii")
    out = _read_keys(pause_state, b"r")
    # 10 s in progress at the canned now, banked, and named in the resume line
    assert "Resumed - 0:00:10 spent paused so far." in out


def test_key_reader_leaves_when_the_stream_ends(pause_state):
    # an empty stream is a console that went away: the reader leaves quietly
    assert _read_keys(pause_state, b"") == ""
    assert not pc.pause_requested()


# --- arming --------------------------------------------------------------------


def test_init_arms_the_three_files(tmp_path, monkeypatch):
    for name in ("PAUSE_DIR", "PAUSE_JOBS", "PAUSE_FLAG", "PAUSE_ACCUM"):
        monkeypatch.delenv(name, raising=False)
    pc.init(str(tmp_path))
    assert (tmp_path / "jobs").is_dir()
    assert not (tmp_path / "paused").exists()
    assert (tmp_path / "pausedSeconds").read_text().strip() == "0"


def test_init_inherits_an_already_armed_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("PAUSE_DIR", str(tmp_path))
    pc.init(str(tmp_path / "elsewhere"))
    # the inherited dir wins: the inner script shares the wrapper's state
    assert os.environ["PAUSE_DIR"] == str(tmp_path)

def test_the_key_reader_is_started_on_a_readable_console(pause_state,
                                                         monkeypatch):
    """The one thing only a real start can show: that the console handle it
    opens is a stream the reader can read a key off.

    A run with a terminal is the only run that takes this path, so nothing that
    runs under the suite reaches it.
    """
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"p")
    os.close(write_fd)
    monkeypatch.setattr(pc, "_is_console", lambda: True)
    monkeypatch.setattr(pc.os, "open", lambda path, flags: read_fd)

    started = pc.start_pause_keys()
    try:
        assert started
        # The reader runs in its own thread; the key it acts on is the pause.
        for _ in range(200):
            if pc.pause_requested():
                break
            time.sleep(0.005)
        assert pc.pause_requested()
    finally:
        pc.stop_pause_keys()


def test_no_console_to_open_leaves_the_run_exactly_as_it_was(pause_state,
                                                             monkeypatch):
    monkeypatch.setattr(pc, "_is_console", lambda: True)

    def refuse(path, flags):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(pc.os, "open", refuse)
    assert pc.start_pause_keys() is False


class TestTheGateBetweenJobs:
    """``wait_while_paused`` is where a run sits BETWEEN two files, so that a
    pause holds the next file's probes off as well as the encoder that happened
    to be running."""

    def _gate(self, timeout=5.0):
        """The gate on a thread, and whether it came back."""
        import threading
        through = threading.Event()
        thread = threading.Thread(
            target=lambda: (pc.wait_while_paused(), through.set()),
            daemon=True)
        thread.start()
        return through, thread

    def test_it_holds_while_the_run_is_paused(self, pause_state):
        pc.pause_jobs(1000)
        through, _thread = self._gate()
        assert not through.wait(1.0)

    def test_and_lets_go_when_the_run_is_resumed(self, pause_state):
        pc.pause_jobs(1000)
        through, thread = self._gate()
        assert not through.wait(0.2)
        pc.resume_jobs(1001)
        assert through.wait(5.0)
        thread.join(1.0)

    def test_an_interrupted_run_is_not_held_at_it_either(self, pause_state,
                                                         tmp_path):
        """A Ctrl+C during a pause would otherwise never be acted on."""
        from medialib.lib import safety
        safety.init_abort_flag(str(tmp_path / "abortRequested"))
        pc.pause_jobs(1000)
        through, thread = self._gate()
        assert not through.wait(0.2)
        safety.request_abort()
        assert through.wait(5.0)
        thread.join(1.0)

    def test_an_unpaused_run_walks_straight_through(self, pause_state):
        through, thread = self._gate()
        assert through.wait(2.0)
        thread.join(1.0)


class TestRunningOneJob:
    """``run_pausable`` is the only way a job gets into the registry, and the one
    place a job's stderr can be filtered on its way to the console."""

    def _job(self, *lines, status=0):
        """A stand-in job that writes those lines to stderr and exits."""
        script = ("import sys\n"
                  + "".join("sys.stderr.write(%r)\n" % (line + "\n")
                            for line in lines)
                  + "sys.exit(%d)\n" % status)
        return [sys.executable, "-c", script]

    def test_it_passes_the_exit_status_back(self, pause_state):
        assert pc.run_pausable(self._job(status=3)) == 3

    def test_the_job_is_unregistered_when_it_ends(self, pause_state):
        pc.run_pausable(self._job())
        assert list(pause_state["jobs"].iterdir()) == []

    def test_without_a_filter_stderr_is_left_alone(self, pause_state, capfd):
        pc.run_pausable(self._job("chatter", "trouble"))
        assert capfd.readouterr().err.splitlines() == ["chatter", "trouble"]

    def test_a_filter_keeps_only_the_lines_it_accepts(self, pause_state, capfd):
        pc.run_pausable(self._job("chatter", "trouble"),
                        keep=lambda line: line != "chatter")
        assert capfd.readouterr().err.splitlines() == ["trouble"]

    def test_a_filtered_job_still_reports_its_status(self, pause_state):
        assert pc.run_pausable(self._job("chatter", status=4),
                               keep=lambda line: False) == 4
