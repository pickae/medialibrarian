"""The p/r pause of `convert-video`, asserted where it matters - on the PARALLEL
encoders of a chunked file.

This is the half a unit test of the pause cannot cover. One file is cut into
chunks encoded by a worker pool, so the processes a keypress has to reach
are not the run's own children but its workers' - separate processes that cannot
be reached through any variable the run holds. What is asserted is that a pause
reaches ALL of them anyway, that they really stop doing work while it lasts, that
the run finishes normally afterwards, and that the waiting is reported rather
than being averaged into the run's throughput.

The keypress itself is not simulated: reading a key needs a terminal a test suite
does not have. The pause state is armed HERE instead and inherited by the run
(the way a wrapper's would be), so the test presses p and r by calling the very
functions the key reader calls.

An encode ends when the TEST says so: the stub ffmpeg reports its position and
then waits on a release file, so a worker cannot finish underneath an assertion
about it and a case that stops a run mid-encode is choosing the moment rather
than racing a sleep. The case that takes a run all the way through releases them
itself.
"""

import os
import signal
import time

import pytest

from medialib.lib import pausecontrol
from tests import blackbox

pytestmark = pytest.mark.stubbed

# An ENCODE is the call carrying -progress, and it is the only slow one: it
# reports its position the way ffmpeg does, for a few seconds, and then writes
# its output. Everything else - the capability probes, the chunk re-join, the mux
# - answers at once, so the only thing this run spends time in is the thing the
# pause is supposed to stop.
_FFMPEG = r"""#!/usr/bin/env bash
progress=""
prev=""
for a in "$@"; do
    [[ "$prev" == "-progress" ]] && progress="$a"
    prev="$a"
done
out="${!#}"
if [[ -n "$progress" ]]; then
    frame=0
    # Reports its position the way ffmpeg does, and keeps at it until the test
    # releases it. The count is a backstop for a case that never does - it has
    # to outlast the assertions, not the encode.
    for ((i = 0; i < 3000; i++)); do
        [[ -e "$ENCODE_RELEASE" ]] && break
        frame=$((frame + 10))
        printf 'frame=%d\nout_time_us=%d\n' "$frame" "$((frame * 100000))" >> "$progress"
        sleep 0.2
    done
fi
# A byte rather than an empty file: the run keeps a video encode only when there
# is one, and an empty intermediate is how a failed encode looks. Only a PATH is
# written to - the capability probes end in "-" or in an option value.
[[ "$out" == */* ]] && printf 'x' > "$out"
exit 0
"""

# The four questions this run asks: how long the source is, how big its video is,
# how many channels its audio has, and (nothing) about its HDR side data.
_FFPROBE = r"""#!/usr/bin/env bash
args="$*"
case "$args" in
*format=duration*)   echo "60.000000" ;;
*stream=channels*)   echo "2" ;;
*stream=width*)      echo '{"streams":[{"width":1920,"height":1080}]}' ;;
esac
exit 0
"""

_JQ = r"""#!/usr/bin/env bash
case "$*" in
*.width*) echo "1920 1080 progressive 1:1" ;;
esac
exit 0
"""


class _Run:
    """One armed pause, one stubbed PATH, and the runs started under them."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (("ffmpeg", _FFMPEG), ("ffprobe", _FFPROBE),
                           ("jq", _JQ)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)

        # Somewhere private for the run to work, so what it leaves behind is its
        # own. A real tmpfs is not what the assertion needs - privacy is - and a
        # shared /dev/shm root is what the suite's self-containment rule forbids.
        self.scratch = tmp_path / "scratch"
        self.scratch.mkdir()
        # The barrier the stub encoders wait on. Absent until a case creates it,
        # which is what makes "still encoding" a state a test can hold rather
        # than a few seconds it has to act inside of.
        self.barrier = tmp_path / "release-encoders"
        monkeypatch.setenv("ENCODE_RELEASE", str(self.barrier))
        monkeypatch.setenv("ramScratchBase", str(self.scratch))
        monkeypatch.setenv("PATH", str(self.bin) + os.pathsep
                           + os.environ["PATH"])

        # Armed before any run starts, so the test can press the keys: the run
        # inherits this state instead of arming its own.
        pausecontrol.init(str(tmp_path / "pause"))
        self.started = []

    def start(self, name, own_group=False):
        """A conversion of one file, cut into chunks by a pool of workers.

        -j 32 so the 1080p fixture is cut into several chunks whatever the core
        count of the machine running the suite: the parallel case is the point.
        """
        source = self.root / name
        source.mkdir()
        (source / "clip.mkv").write_text("")
        out = self.root / (name + ".out")
        log = self.root / (name + ".log")
        # To disk rather than to a pipe: this run is watched WHILE it goes, and a
        # pipe nobody is draining fills and blocks it. The child dups the
        # descriptor, so the parent's copy is closed at once and the log is read
        # back off disk rather than held open here.
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            proc = blackbox.start("convert-video", "-p", "av1Fast", "-j", "32",
                                  source, out, cwd=self.root,
                                  env=dict(os.environ), stdout=fd,
                                  new_session=own_group)
        finally:
            os.close(fd)
        proc._log = log  # noqa: SLF001 - the test's own bookkeeping
        proc._out = out  # noqa: SLF001
        self.started.append(proc)
        return proc

    def release_encoders(self):
        """Let every encode - the running ones and the ones still to start -
        finish. For the case that takes a run all the way through."""
        self.barrier.write_text("")

    def encoder_pids(self):
        """Every encoder of the moment, as the pause sees them."""
        jobs = os.environ["PAUSE_JOBS"]
        try:
            return sorted(int(n) for n in os.listdir(jobs))
        except FileNotFoundError:
            return []

    def progress_bytes(self):
        """The encoders' own progress files, which are what the status row reads
        and so the honest answer to "is anything still being encoded"."""
        total = 0
        for base, _dirs, files in os.walk(self.scratch):
            for name in files:
                if name.startswith("prog."):
                    total += os.path.getsize(os.path.join(base, name))
        return total

    def scratch_left(self):
        return sorted(p.name for p in self.scratch.iterdir())

    def text(self, proc):
        return proc._log.read_text(errors="replace")  # noqa: SLF001


@pytest.fixture()
def run(tmp_path, monkeypatch):
    r = _Run(tmp_path, monkeypatch)
    yield r
    # A test that failed mid-pause leaves SIGSTOPped processes, and the next test
    # then fails for reasons belonging to this one. kill_pausable_jobs continues
    # each job before it terms it, which is what reaches a stopped one.
    pausecontrol.kill_pausable_jobs()
    for proc in r.started:
        if proc.poll() is not None:
            continue
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=30)


def _proc_state(pid):
    """The kernel's state letter for a process: T while stopped."""
    try:
        with open("/proc/%d/status" % pid, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("State:"):
                    return line.split()[1]
    except OSError:
        return ""
    return ""


def _wait_for(predicate, limit=60.0, step=0.1):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def _alive(pid):
    return os.path.exists("/proc/%d" % pid)


def _now():
    return int(time.time())


class TestPausingParallelEncoders:
    """One run, taken through p and r and out the other side. The asserts are a
    sequence over one process rather than one case each: arriving at "resumed
    from where it stopped" means having paused a live pool first.
    """

    def test_a_pause_reaches_every_worker_and_a_resume_releases_them(self, run):
        proc = run.start("in")
        # Up to 60 s: the run has to get through its startup probes first.
        assert _wait_for(lambda: len(run.encoder_pids()) >= 2), \
            "the file is never encoded by several parallel workers"
        encoders = run.encoder_pids()

        pausecontrol.pause_jobs(_now())
        assert _wait_for(lambda: all(_proc_state(p) == "T" for p in encoders),
                         limit=10), \
            "pressing p does not stop every one of them"

        before = run.progress_bytes()
        assert before > 0, "the encoders had got nowhere before the pause"
        time.sleep(1.5)
        assert run.progress_bytes() == before, \
            "something was still encoded while the pause lasted"

        pausecontrol.resume_jobs(_now())
        assert _wait_for(lambda: run.progress_bytes() > before, limit=10), \
            "pressing r does not have them carry on"

        run.release_encoders()
        assert proc.wait(timeout=180) == 0, "the paused run did not finish"
        assert (proc._out / "clip.mkv").is_file()  # noqa: SLF001

        text = run.text(proc)
        assert "Of that, paused:" in text, \
            "the closing report does not account for the time spent paused"
        # A run whose output is not a terminal has nobody at a keyboard, so it
        # neither reads keys nor promises them.
        assert "Pause: press" not in text
        assert run.encoder_pids() == [], \
            "an encoder is left registered after the run"
        assert run.scratch_left() == [], "the run leaves scratch behind"


class TestStoppingARunWhileItIsPaused:
    """The failure this guards against is silent and expensive: a stopped process
    acts on nothing, so an interrupt queues up behind the pause and the encoders
    are left stopped, orphaned, holding their memory until somebody finds them by
    hand.
    """

    def test_the_interrupt_takes_the_paused_encoders_down_with_it(self, run):
        # Its own process group, so it can be stopped the way a console stops
        # one - a signal to the whole group. The group and not the pid, because a
        # PAUSED run acts on nothing else: it is blocked waiting on the pool, and
        # the pool's workers are the processes the pause has stopped. A console's
        # Ctrl+C reaches them too, which is what lets the run's handler run.
        #
        # SIGTERM rather than SIGINT because both go through the same handler and
        # a test runner's own SIGINT handling is not in the way of it.
        proc = run.start("in2", own_group=True)
        assert _wait_for(lambda: len(run.encoder_pids()) >= 2), \
            "the run is never encoding in parallel"
        encoders = run.encoder_pids()

        pausecontrol.pause_jobs(_now())
        assert _wait_for(lambda: all(_proc_state(p) == "T" for p in encoders),
                         limit=10)

        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        assert proc.wait(timeout=180) == 130, \
            "a paused run does not stop with the interrupt's status"

        assert _wait_for(lambda: not any(_alive(p) for p in encoders),
                         limit=10), \
            "the paused encoders are left stopped rather than taken down"

        assert "Interrupted" in run.text(proc), \
            "an interrupted pause does not say the run was stopped"
        assert run.scratch_left() == [], "the run leaves scratch behind"

    def test_an_interrupt_the_workers_never_saw_still_releases_them(self, run):
        """The same interrupt, delivered to the run ALONE.

        A group signal reaches the stopped workers directly and Linux wakes them
        to die of it, so it cannot tell whether the run released them or the
        kernel did. A signal to the run's own pid can: the workers are stopped,
        nothing else has told them anything, and they are still holding their
        memory. Releasing them is then the run's job and nobody else's.
        """
        proc = run.start("in3", own_group=True)
        assert _wait_for(lambda: len(run.encoder_pids()) >= 2)
        encoders = run.encoder_pids()

        pausecontrol.pause_jobs(_now())
        assert _wait_for(lambda: all(_proc_state(p) == "T" for p in encoders),
                         limit=10)

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=180) == 130

        assert _wait_for(lambda: not any(_alive(p) for p in encoders),
                         limit=10), \
            "the workers are left stopped, orphaned and holding their memory"
        assert run.scratch_left() == [], "the run leaves scratch behind"
