"""Tests for medialib.lib.statusline - the live status row.

What is pinned here: the path shortening and the row's two rendering modes under a
controlled width, the settlement init_status_line makes from the terminal it is
handed (the stty-then-tput-then-80 chain and its floor, with the probes stubbed),
the background refresher's lifecycle, the lock/no-lock tick, and the settlement
a worker process adopts from the run that started it.
"""

import multiprocessing
import os

import pytest

from medialib.lib import statusline
from medialib.lib.statusline import state

pytestmark = pytest.mark.pure


@pytest.fixture(autouse=True)
def _fresh_state():
    """The module keeps its settlement in one shared state; start each test at the
    off-terminal default so one test's row never bleeds into the next."""
    state.row = ""
    state.cols = 1000
    state.interval = 30
    state.mon_pid = None
    state._mon_stop = None
    yield
    statusline.stop_status_monitor()


def _stderr(capsys):
    return capsys.readouterr().err


# --- the two output modes -----------------------------------------------------

def test_off_a_terminal_the_settlement_is_the_plain_heartbeat(monkeypatch, capsys):
    monkeypatch.setattr(statusline, "_is_tty", lambda: False)
    statusline.init_status_line()
    assert state.row == ""
    assert state.cols == 1000
    assert state.interval == 30
    statusline.draw_status("hello")
    statusline.clear_status()
    statusline.end_status()
    assert _stderr(capsys) == "hello\n"


def test_on_a_terminal_the_row_is_rewritten_in_place(monkeypatch, capsys):
    monkeypatch.setattr(statusline, "_is_tty", lambda: True)
    monkeypatch.setattr(statusline, "_stty_cols", lambda: "100")
    statusline.init_status_line()
    assert state.row == "1"
    assert state.interval == 2
    assert state.cols == 100
    statusline.draw_status("hello")
    assert _stderr(capsys) == "\rhello\033[K"


def test_on_a_terminal_the_row_keeps_off_the_last_column(capsys):
    state.row = "1"
    state.cols = 20
    statusline.draw_status("abcdefghijklmnopqrstuvwxyz")
    # 20 columns minus the 1 reserved = 19 of text
    assert _stderr(capsys) == "\rabcdefghijklmnopqrs\033[K"


def test_on_a_terminal_the_row_can_be_erased_and_ended(capsys):
    state.row = "1"
    state.cols = 20
    statusline.clear_status()
    statusline.end_status()
    assert _stderr(capsys) == "\r\033[K\n"


def test_repin_redraws_the_row_from_its_render(capsys):
    state.row = "1"
    state.cols = 20
    statusline.repin_status(lambda: "row: A")
    assert _stderr(capsys) == "\rrow: A\033[K"


def test_repin_does_nothing_off_a_terminal(capsys):
    state.row = ""
    statusline.repin_status(lambda: "row: A")
    assert _stderr(capsys) == ""


def test_repin_a_render_that_fails_draws_nothing(capsys):
    state.row = "1"
    state.cols = 20
    statusline.repin_status(lambda: None)
    assert _stderr(capsys) == ""


# --- the settlement's width chain --------------------------------------------

def test_stty_answers_and_is_wide_enough(monkeypatch):
    monkeypatch.setattr(statusline, "_is_tty", lambda: True)
    monkeypatch.setattr(statusline, "_stty_cols", lambda: "132")
    called = []
    monkeypatch.setattr(statusline, "_tput_cols",
                        lambda: called.append(1) or "80")
    statusline.init_status_line()
    assert state.cols == 132
    assert called == []


def test_a_stty_figure_below_the_floor_is_not_trusted(monkeypatch):
    monkeypatch.setattr(statusline, "_is_tty", lambda: True)
    monkeypatch.setattr(statusline, "_stty_cols", lambda: "30")
    monkeypatch.setattr(statusline, "_tput_cols", lambda: "80")
    statusline.init_status_line()
    assert state.cols == 80


def test_a_stty_it_cannot_vouch_for_falls_to_tput(monkeypatch):
    monkeypatch.setattr(statusline, "_is_tty", lambda: True)
    monkeypatch.setattr(statusline, "_stty_cols", lambda: "")
    monkeypatch.setattr(statusline, "_tput_cols", lambda: "120")
    statusline.init_status_line()
    assert state.cols == 120


def test_a_tput_figure_below_the_floor_is_not_trusted(monkeypatch):
    monkeypatch.setattr(statusline, "_is_tty", lambda: True)
    monkeypatch.setattr(statusline, "_stty_cols", lambda: "")
    monkeypatch.setattr(statusline, "_tput_cols", lambda: "39")
    statusline.init_status_line()
    assert state.cols == 80


def test_no_width_anyone_vouches_for_is_the_floor(monkeypatch):
    monkeypatch.setattr(statusline, "_is_tty", lambda: True)
    monkeypatch.setattr(statusline, "_stty_cols", lambda: "")
    monkeypatch.setattr(statusline, "_tput_cols", lambda: "")
    statusline.init_status_line()
    assert state.cols == 80


# --- shortenPath: the file name is what gives way -----------------------------

def test_a_name_that_fits_is_left_alone():
    assert statusline.shorten_path("20", "movie.mkv") == "movie.mkv"


def test_a_name_at_exactly_the_limit_is_left_alone():
    assert statusline.shorten_path("20", "12345678901234567890") == "12345678901234567890"


def test_a_long_path_loses_its_front_not_its_file_name():
    assert statusline.shorten_path("20", "Some/Deep/Folder/Tree/movie name.mkv") \
        == "...ee/movie name.mkv"


def test_a_limit_too_small_for_the_marker_just_cuts():
    assert statusline.shorten_path("2", "abcdef") == "ab"


def test_a_non_numeric_limit_cuts_to_nothing():
    assert statusline.shorten_path("", "abcdef") == ""


def test_a_negative_limit_cuts_to_nothing():
    assert statusline.shorten_path("-5", "abcdef") == ""


# --- the background refresher's lifecycle -------------------------------------

def test_starting_the_refresher_draws_the_row_once_straight_away(capsys):
    ticks = []
    def render():
        return ticks.append(1) or "counted"
    state.interval = 3600
    state.row = ""
    statusline.start_status_monitor("", render)
    assert len(ticks) == 1
    assert state.mon_pid is not None
    statusline.stop_status_monitor()
    assert state.mon_pid is None


def test_stopping_a_refresher_that_is_not_running_is_safe():
    statusline.stop_status_monitor()
    assert state.mon_pid is None


def test_a_tick_under_the_progress_lock_draws_the_row(capsys, monkeypatch):
    state.row = "1"
    state.cols = 80
    # HAVE_FLOCK empty: the lock is a no-op, so a lock file is never opened and the
    # tick draws exactly the same row. Said here rather than left to whatever the
    # environment holds - UNSET is not "no", it is "nobody has asked", and the probe
    # that answers it says yes on every POSIX host, where the tick would then open
    # the path below for real.
    monkeypatch.setenv("HAVE_FLOCK", "")
    statusline.status_tick("/nonexistent/lock", lambda: "counted")
    assert _stderr(capsys) == "\rcounted\033[K"


def test_a_tick_whose_render_fails_draws_nothing(capsys):
    state.row = "1"
    state.cols = 80
    statusline.status_tick("", lambda: None)
    assert _stderr(capsys) == ""


# --- the settlement a worker adopts -------------------------------------------

def test_the_settled_row_travels_as_a_geometry():
    state.row, state.cols, state.interval = "1", 96, 2
    assert statusline.status_geometry() == ("1", 96, 2)


def test_a_worker_adopting_the_geometry_draws_the_row_the_run_settled():
    """A worker process is a fresh interpreter on any host whose start method is
    ``forkserver`` or ``spawn``, so it begins believing there is no terminal.
    Adopting the run's settlement is what lets it erase the row before printing."""
    statusline.adopt_status_geometry(("1", 96, 2))
    assert (state.row, state.cols, state.interval) == ("1", 96, 2)


def test_nothing_to_adopt_leaves_the_process_as_it_is():
    """A run that settled no row hands its workers none, and the serial path hands
    none either - neither is a reason to change what this process decided."""
    state.row, state.cols, state.interval = "1", 96, 2
    statusline.adopt_status_geometry(None)
    assert (state.row, state.cols, state.interval) == ("1", 96, 2)


def _report_whether_the_lock_is_free(lock_file: str, answer_file: str) -> None:
    """Whether the console lock can be taken RIGHT NOW, from another process.

    In another process on purpose: flock is held by an open file description, and
    a second open in the holder's own process would conflict with itself and say
    "held" whether the tick took the lock or not.
    """
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
def test_the_tick_holds_the_lock_while_it_draws(tmp_path, monkeypatch):
    """The whole point of the lock: a refresh must not land between a worker's
    erase and the line that erase made room for, which would put the row and the
    line on one console row."""
    monkeypatch.setenv("HAVE_FLOCK", "1")
    state.row, state.cols = "1", 80
    lock_file = str(tmp_path / "lock")
    answer_file = str(tmp_path / "answer")

    def draw_and_ask(text):
        prober = multiprocessing.Process(
            target=_report_whether_the_lock_is_free,
            args=(lock_file, answer_file))
        prober.start()
        prober.join()
        return 0

    monkeypatch.setattr(statusline, "draw_status", draw_and_ask)
    statusline.status_tick(lock_file, lambda: "counted")
    assert (tmp_path / "answer").read_text() == "held"
