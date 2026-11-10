"""Tests for medialib.lib.statusline - the status row's geometry.

What is pinned here: the path shortening, the settlement init_status_line makes
from the terminal it is handed (the stty-then-tput-then-80 chain and its floor,
with the probes stubbed).
Drawing the row is the console's (tests/lib/test_console.py).
"""


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


# --- the two output modes -----------------------------------------------------

def test_off_a_terminal_the_settlement_is_the_plain_heartbeat(monkeypatch):
    monkeypatch.setattr(statusline, "_is_tty", lambda: False)
    statusline.init_status_line()
    assert state.row == ""
    assert state.cols == 1000
    assert state.interval == 30


def test_on_a_terminal_the_row_is_rewritten_in_place(monkeypatch):
    monkeypatch.setattr(statusline, "_is_tty", lambda: True)
    monkeypatch.setattr(statusline, "_stty_cols", lambda: "100")
    statusline.init_status_line()
    assert state.row == "1"
    assert state.interval == 2
    assert state.cols == 100


def test_the_settled_row_travels_as_a_geometry():
    state.row, state.cols, state.interval = "1", 96, 2
    assert statusline.status_geometry() == ("1", 96, 2)


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
