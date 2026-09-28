"""The white box for medialib/lib/politepacing.py: requests to one site spaced
a gap apart, the gap drawn afresh each time within the jitter."""

import threading

import pytest

from medialib.lib import politepacing

pytestmark = pytest.mark.pure


class _Clock:
    def __init__(self):
        self.time = 100.0
        self.slept = []

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.time += seconds


def _pacer(clock, seconds=5.0, jitter=0.0, draws=None):
    drawn = iter(draws or [])
    return politepacing.Pacer(seconds, jitter, now=clock.now,
                              sleep=clock.sleep,
                              draw=lambda low, high: next(drawn))


def test_the_first_request_goes_straight_out():
    clock = _Clock()
    _pacer(clock).wait()
    assert clock.slept == []


def test_each_request_after_it_waits_out_its_gap():
    clock = _Clock()
    pacer = _pacer(clock, 5.0, 2.0, draws=[1.5, -2.0])
    for _request in range(3):
        pacer.wait()
    assert clock.slept == [6.5, 3.0]


def test_the_jitter_is_drawn_within_its_bounds():
    bounds = []
    pacer = politepacing.Pacer(5.0, 2.0, now=_Clock().now, sleep=lambda _s: None,
                               draw=lambda low, high: bounds.append(
                                   (low, high)) or 0.0)
    pacer.wait()
    pacer.wait()
    assert bounds == [(-2.0, 2.0)]


def test_time_already_spent_is_not_waited_again():
    clock = _Clock()
    pacer = _pacer(clock, 5.0)
    pacer.wait()
    clock.time += 4.0
    pacer.wait()
    assert clock.slept == [pytest.approx(1.0)]


def test_a_reset_lets_the_next_request_straight_out():
    clock = _Clock()
    pacer = _pacer(clock, 5.0)
    pacer.wait()
    politepacing.reset_all()
    pacer.wait()
    assert clock.slept == []


def test_two_threads_queue_for_the_same_site():
    """The wait is taken under the lock, so the second thread's gap is counted
    from the first thread's request and not from nothing."""
    clock = _Clock()
    lock = threading.Lock()

    def sleep(seconds):
        with lock:
            clock.slept.append(seconds)
            clock.time += seconds

    pacer = politepacing.Pacer(5.0, now=clock.now, sleep=sleep)
    threads = [threading.Thread(target=pacer.wait) for _thread in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert clock.slept == [5.0, 5.0]
