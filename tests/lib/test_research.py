"""The white box for medialib/lib/research.py: questions asked ahead of need on
a thread, and answered from memory when the run asks them itself."""

import threading

import pytest

from medialib.lib import research

pytestmark = pytest.mark.pure


def _asking(asked, answer=lambda question: question.upper()):
    def ask(question):
        asked.append(question)
        return answer(question)
    return ask


def test_what_was_asked_ahead_is_answered_from_memory():
    asked = []
    ahead = research.Research("test", _asking(asked))
    ahead.ahead(lambda: [ahead(question) for question in ("a", "b")])
    ahead.finish(lambda _line: None)
    assert (ahead("a"), ahead("b"), ahead("c")) == ("A", "B", "C")
    assert asked == ["a", "b", "c"]


def test_an_answer_not_worth_keeping_is_asked_again():
    asked = []
    ahead = research.Research("test", _asking(asked, lambda _q: None),
                              keep=lambda answer: answer is not None)
    ahead("a")
    ahead("a")
    assert asked == ["a", "a"]


def test_the_work_runs_on_a_thread_of_its_own():
    released = threading.Event()
    ran = threading.Event()

    def work():
        released.wait(5)
        ran.set()

    ahead = research.Research("test", _asking([]))
    ahead.ahead(work)
    assert not ran.is_set()
    released.set()
    ahead.finish(lambda _line: None)
    assert ran.is_set()


def test_work_handed_over_later_runs_after_the_first():
    order = []
    ahead = research.Research("test", _asking([]))
    ahead.ahead(lambda: order.append(1))
    ahead.ahead(lambda: order.append(2))
    ahead.finish(lambda _line: None)
    ahead.finish(lambda _line: None)
    assert order == [1, 2]


def test_a_walk_that_trips_is_only_cut_short():
    def work():
        raise FileNotFoundError("moved from under it")

    ahead = research.Research("test", _asking([]))
    ahead.ahead(work)
    ahead.finish(lambda _line: None)
    ahead.ahead(lambda: ahead("again"))
    ahead.finish(lambda _line: None)
    assert ahead("again") == "AGAIN"


def test_waiting_for_it_is_said():
    released = threading.Event()
    said = []
    ahead = research.Research("chapter", _asking([]))
    ahead.ahead(lambda: released.wait(5))
    threading.Timer(0.2, released.set).start()
    ahead.finish(said.append)
    assert said == ["Waiting for the chapter research to finish"]
