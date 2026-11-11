"""Tests for medialib.lib.console - the run's console.

What is pinned here: the row drawn, erased for a line and drawn again under it;
the refresher's lifecycle; the lock held across processes while a refresh
draws; the counters; the console a worker is handed; and the lanes - a lane's
lines about one item held as one block on a terminal, and written as they come
with the lane and item in front of them off one or under -v.
"""

import multiprocessing
import os
import pickle
import threading

import pytest

from medialib.lib import console, runcontext, runlog
from medialib.lib.statusline import state

pytestmark = pytest.mark.pure

needs_lock = pytest.mark.skipif(
    not runlog.can_lock(),
    reason="a host that cannot lock holds nothing to observe, and fcntl is "
           "not built into the Windows interpreter")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("LOG_VERBOSITY", raising=False)
    monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)
    state.row = ""
    state.cols = 1000
    state.interval = 3600
    yield
    console.hide_active()


def _on_a_terminal(cols: int = 100) -> None:
    state.row, state.cols = "1", cols


def _stderr(capsys) -> str:
    return capsys.readouterr().err


# --- the row ------------------------------------------------------------------

def test_showing_the_row_draws_it_once_straight_away(capsys):
    _on_a_terminal()
    ticks = []
    shown = console.Console()
    shown.show(lambda: ticks.append(1) or "counted")
    assert len(ticks) == 1
    assert _stderr(capsys) == "\rcounted\033[K"


def test_off_a_terminal_the_row_is_an_ordinary_line(capsys):
    console.Console().show(lambda: "hello")
    assert _stderr(capsys) == "hello\n"


def test_on_a_terminal_the_row_keeps_off_the_last_column(capsys):
    _on_a_terminal(cols=20)
    console.Console().show(lambda: "abcdefghijklmnopqrstuvwxyz")
    # 20 columns minus the 1 reserved = 19 of text
    assert _stderr(capsys) == "\rabcdefghijklmnopqrs\033[K"


def test_a_render_that_fails_draws_nothing(capsys):
    _on_a_terminal()
    console.Console().show(lambda: None)
    assert _stderr(capsys) == ""


def test_hiding_a_console_that_shows_nothing_is_safe(capsys):
    console.Console().hide()
    console.hide_active()
    assert _stderr(capsys) == ""


def test_a_log_line_while_the_row_is_pinned_lands_on_its_own_row(capsys):
    """Not on the end of the row's text, the way a line written over a pinned
    row without erasing it first would."""
    _on_a_terminal()
    run = console.Console()
    run.show(lambda: "encoding 0 jobs")
    runlog.log("Queue: pre-filling")
    run.hide()
    assert _stderr(capsys) == (
        "\rencoding 0 jobs\033[K"          # drawn when it is shown
        "\r\033[K==> Queue: pre-filling\n"  # erased, then the line
        "\rencoding 0 jobs\033[K"          # and the row back under it
        "\n")                              # finished off when it is hidden


def test_once_the_row_is_gone_lines_are_written_plainly(capsys):
    _on_a_terminal()
    run = console.Console()
    run.show(lambda: "x")
    run.hide()
    capsys.readouterr()
    runlog.log("after")
    assert _stderr(capsys) == "==> after\n"


def test_off_a_terminal_a_line_brings_no_row_with_it(capsys):
    """Off a terminal the row is a periodic heartbeat, so a line neither erases
    anything nor prints the row again after itself."""
    run = console.Console()
    run.show(lambda: "row")
    capsys.readouterr()
    runlog.log("line")
    assert _stderr(capsys) == "==> line\n"


def test_quiet_draws_no_row(monkeypatch, capsys):
    """-q prints errors only, and the row is progress."""
    monkeypatch.setenv("LOG_VERBOSITY", "quiet")
    _on_a_terminal()
    run = console.Console()
    run.show(lambda: "encoding 40%")
    run.write("an error\n")
    run.hide()
    assert _stderr(capsys) == "an error\n"


# --- the lock -----------------------------------------------------------------

def _report_whether_the_lock_is_free(lock_file: str, answer_file: str) -> None:
    """Whether the console lock can be taken RIGHT NOW, from another process:
    flock is held by an open file description, and a second open in the
    holder's own process would say "held" whether the tick took it or not."""
    import fcntl
    handle = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
        free = "free"
    except OSError:
        free = "held"
    finally:
        os.close(handle)
    with open(answer_file, "w") as answer:
        answer.write(free)


@pytest.mark.fs
@needs_lock
def test_a_refresh_holds_the_lock_while_it_draws(tmp_path, monkeypatch):
    """A refresh must not land between a worker's erase and the line that
    erase made room for, which would put the row and the line on one row."""
    monkeypatch.setenv("HAVE_FLOCK", "1")
    _on_a_terminal()
    answer_file = str(tmp_path / "answer")
    run = console.Console(str(tmp_path))

    def render():
        prober = multiprocessing.Process(
            target=_report_whether_the_lock_is_free,
            args=(run.lock_path, answer_file))
        prober.start()
        prober.join()
        return "counted"

    run.render = render
    run.tick()
    assert (tmp_path / "answer").read_text() == "held"


def test_without_flock_the_lock_opens_no_file(tmp_path, monkeypatch, capsys):
    """HAVE_FLOCK empty is the run's "no", and the lock is then this process's
    alone."""
    monkeypatch.setenv("HAVE_FLOCK", "")
    run = console.Console(str(tmp_path))
    with run.locked():
        pass
    assert not os.path.exists(run.lock_path)


@pytest.mark.fs
@needs_lock
def test_the_lock_can_be_taken_again_by_the_thread_that_holds_it(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HAVE_FLOCK", "1")
    run = console.Console(str(tmp_path))
    with run.locked(), run.locked():
        run.count()
    assert run.value("position") == 1


# --- the counters -------------------------------------------------------------

@pytest.mark.fs
@needs_lock
def test_a_counted_line_carries_its_position(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HAVE_FLOCK", "1")
    run = console.Console(str(tmp_path))
    run.counted("one", 2)
    run.counted("two", 2)
    assert _stderr(capsys) == "[1/2] one\n[2/2] two\n"
    assert run.value("position") == 2


@pytest.mark.fs
def test_quiet_counts_without_printing_unless_the_line_must_be_said(
        tmp_path, monkeypatch, capsys):
    """Without flock, so it runs on every host: the line carries no
    position then, which the test above covers."""
    monkeypatch.setenv("HAVE_FLOCK", "")
    monkeypatch.setenv("LOG_VERBOSITY", "quiet")
    run = console.Console(str(tmp_path))
    run.counted("done", 2)
    run.counted("FAILED", 2, always=True, after="  why\n")
    assert _stderr(capsys) == "FAILED\n  why\n"
    assert run.value("position") == 2


def test_a_counter_nobody_bumped_reads_as_nought(tmp_path):
    assert console.Console(str(tmp_path)).value("anything") == 0
    assert console.Console().value("anything") == 0


# --- the console a worker is handed ---------------------------------------------

def test_a_worker_draws_against_the_row_its_parent_settled():
    """A worker process is a fresh interpreter on any host whose start method
    is ``forkserver`` or ``spawn``, so it begins believing there is no
    terminal; the run it is handed carries the parent's settlement and its
    console."""
    state.row, state.cols, state.interval = "1", 96, 2
    run = console.Console().activate()
    run.pinned = True
    handed = pickle.loads(pickle.dumps(runcontext.snapshot()))
    runcontext.adopt(handed)
    assert console.active() is not run
    assert console.active().geometry == ("1", 96, 2)
    assert console.active().pinned


def test_a_row_drawn_from_this_process_s_memory_does_not_travel(capsys):
    """A worker cannot draw it, but still erases it for its own lines."""
    _on_a_terminal()
    run = console.Console()
    run.show(lambda: "lanes", travels=False)
    handed = pickle.loads(pickle.dumps(run))
    capsys.readouterr()
    handed.write("worker line\n")
    assert _stderr(capsys) == "\r\033[Kworker line\n"


# --- lanes and blocks -----------------------------------------------------------

def test_on_a_terminal_a_lane_s_lines_about_a_film_are_one_block(capsys):
    _on_a_terminal()
    console.Console().activate()
    with console.about("/films/Film (2001)/Film (2001).mkv", "remux"):
        runlog.log("first")
        runlog.log("second")
        assert _stderr(capsys) == ""
    assert _stderr(capsys) == "==> first\n==> second\n"


def test_another_thread_s_lines_go_straight_out(capsys):
    _on_a_terminal()
    console.Console().activate()
    with console.about("Film.mkv", "remux"):
        runlog.log("held")
        other = threading.Thread(target=runlog.log, args=("other",))
        other.start()
        other.join()
        assert _stderr(capsys) == "==> other\n"
    assert _stderr(capsys) == "==> held\n"


def test_a_block_inside_a_block_is_the_outer_one(capsys):
    _on_a_terminal()
    console.Console().activate()
    with console.about("Film.mkv", "commentary"):
        with console.about("Film track 2.mka", "whisper"):
            runlog.log("inner")
        assert _stderr(capsys) == ""
    assert _stderr(capsys) == "==> inner\n"


def test_an_exception_still_writes_the_block(capsys):
    _on_a_terminal()
    console.Console().activate()
    with pytest.raises(RuntimeError), console.about("Film.mkv", "opus"):
        runlog.log("before")
        raise RuntimeError
    assert _stderr(capsys) == "==> before\n"


def test_a_stream_of_its_own_is_not_held(capsys):
    import io
    _on_a_terminal()
    console.Console().activate()
    out = io.StringIO()
    with console.about("Film.mkv", "remux"):
        runlog.log("aside", stream=out)
        assert out.getvalue() == "==> aside\n"


def test_off_a_terminal_each_line_names_its_lane_and_film(capsys):
    console.Console().activate()
    with console.about("/films/Film (2001)/Film (2001).mkv", "commentary"):
        runlog.log("  track 2: extracting")
        assert _stderr(capsys) == \
            "==> [commentary] Film (2001):   track 2: extracting\n"


def test_under_verbose_a_terminal_prefixes_too(monkeypatch, capsys):
    monkeypatch.setenv("LOG_VERBOSITY", "verbose")
    _on_a_terminal()
    console.Console().activate()
    with console.about("Film.mkv", "subs"):
        runlog.log("downloading")
        assert _stderr(capsys) == "==> [subs] Film: downloading\n"


def test_an_item_without_a_lane_changes_nothing(capsys):
    console.Console().activate()
    with console.about("Film.mkv"):
        runlog.log("plain")
    assert _stderr(capsys) == "==> plain\n"
    assert console.subject() == ("", "")


def test_a_timestamped_line_keeps_its_clock_first(monkeypatch, capsys):
    monkeypatch.setenv("LOG_TIMESTAMPS", "1")
    console.Console().activate()
    with console.about("Film.mkv", "opus"):
        runlog.log("encoding")
    err = _stderr(capsys)
    assert err.startswith("[") and err.endswith("] [opus] Film: encoding\n")


def test_a_block_closed_on_another_thread_leaves_this_one_s_stack_alone():
    """A generator holding a block across its yields may be closed anywhere."""
    def walk():
        with console.about("Film.mkv", "commentary"):
            yield 1
            yield 2

    made = walk()
    next(made)
    closer = threading.Thread(target=made.close)
    closer.start()
    closer.join()
    assert console.subject() == ("", "")


# --- the lane row ---------------------------------------------------------------

def test_the_lane_row_says_what_each_lane_has_done():
    row = console.LaneRow(("opus", "whisper", "remux", "subs"))
    assert row.render() is None
    row.total("opus", 40)
    row.total("whisper", add=7)
    row.total("remux")
    row.total("subs", 40)
    for _ in range(12):
        row.done("opus")
    for _ in range(3):
        row.done("whisper")
    row.done("remux", 2)
    row.done("subs", 18)
    assert row.render() == "  opus 12/40 · whisper 3/7 · remux 2 · subs 18/40"


def test_a_lane_that_never_started_is_left_off_the_row():
    row = console.LaneRow(("opus", "whisper", "remux", "subs"))
    row.total("opus", 3)
    row.total("remux")
    assert row.render() == "  opus 0/3 · remux 0"

