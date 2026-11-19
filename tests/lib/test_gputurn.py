"""The GPU's turns: work of two kinds shares the card only where both fit."""

from __future__ import annotations

import threading
import time

import pytest

from medialib.lib import gputurn, whisper

pytestmark = pytest.mark.fs


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(gputurn, "POLL_SECONDS", 0.01)


def _gate(tmp_path, free=10000, clock=time.time, alive=lambda pid: True):
    return gputurn.Gate(str(tmp_path / "turns"), free=lambda: free,
                        clock=clock, alive=alive)


def _waits(gate, kind, need, hold=None):
    """Whether claiming blocks: tried on a thread, given a moment."""
    entered = threading.Event()

    def run():
        with gate.claim(kind, need):
            entered.set()
            if hold is not None:
                hold.wait(5)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return entered, thread


class TestSharing:

    def test_the_same_kind_never_waits(self, tmp_path):
        gate = _gate(tmp_path, free=0)
        with gate.claim("whisper", 9000):
            with gate.claim("whisper", 9000):
                pass

    def test_another_kind_is_let_in_where_it_fits(self, tmp_path):
        # Counting the whisper just started, which nvidia-smi does not show yet.
        gate = _gate(tmp_path, free=12000)
        with gate.claim("whisper", 4000):
            with gate.claim("llm", 6000):
                pass

    def test_another_kind_waits_where_it_does_not_fit(self, tmp_path):
        gate = _gate(tmp_path, free=3000)
        with gate.claim("whisper", 4000):
            entered, thread = _waits(gate, "llm", 6000)
            assert not entered.wait(0.2)
        assert entered.wait(2)
        thread.join(2)

    def test_a_claim_that_needs_the_whole_card_waits_for_the_others(
            self, tmp_path):
        gate = _gate(tmp_path, free=40000)
        with gate.claim("whisper", 1000):
            entered, thread = _waits(gate, "llm", None)
            assert not entered.wait(0.2)
        assert entered.wait(2)
        thread.join(2)

    def test_a_card_held_by_the_whole_card_claim_keeps_others_out(
            self, tmp_path):
        gate = _gate(tmp_path, free=40000)
        with gate.claim("llm", None):
            entered, thread = _waits(gate, "whisper", 100)
            assert not entered.wait(0.2)
        assert entered.wait(2)
        thread.join(2)

    def test_a_job_only_just_started_is_counted_at_its_whole_need(
            self, tmp_path):
        now = [1000.0]
        gate = _gate(tmp_path, free=8000, clock=lambda: now[0])
        with gate.claim("whisper", 6000):
            # nvidia-smi still shows 8000 free: whisper has not loaded yet.
            entered, thread = _waits(gate, "llm", 6000)
            assert not entered.wait(0.2)
            now[0] += gputurn.SETTLE_SECONDS + 1
            assert entered.wait(2)
        thread.join(2)

    def test_no_card_means_no_waiting(self, tmp_path):
        gate = gputurn.Gate(str(tmp_path / "turns"), free=lambda: None)
        with gate.claim("whisper", 4000):
            with gate.claim("llm", None):
                pass

    def test_a_claim_of_a_dead_process_is_let_go_of(self, tmp_path):
        gate = _gate(tmp_path, free=0, alive=lambda pid: False)
        with gate.claim("whisper", 4000):
            with gate.claim("llm", 6000):
                pass


class TestFairness:

    def test_an_earlier_waiter_goes_first(self, tmp_path):
        gate = _gate(tmp_path, free=0)
        order: list = []
        release = threading.Event()
        with gate.claim("whisper", 4000):
            def llm():
                with gate.claim("llm", 6000):
                    order.append("llm")
                    release.wait(5)

            first = threading.Thread(target=llm, daemon=True)
            first.start()
            time.sleep(0.1)
            entered, second = _waits(gate, "whisper2", 100)
            # A third kind arriving after the llm waits behind it too.
            time.sleep(0.1)
            assert order == [] and not entered.is_set()
        time.sleep(0.2)
        assert order == ["llm"] and not entered.is_set()
        release.set()
        assert entered.wait(2)
        first.join(2)
        second.join(2)


class TestAbort:

    def test_a_waiting_claim_is_released_by_an_abort(self, tmp_path):
        gate = _gate(tmp_path, free=0)
        with gate.claim("whisper", 4000):
            with pytest.raises(gputurn.GateAborted):
                with gate.claim("llm", 6000, abort=lambda: True):
                    pass


class TestJobNeed:

    def test_a_gpu_plan_costs_its_weights_and_its_slots(self):
        need = whisper.job_vram_mib({"device": "cuda", "model": "large-v3",
                                     "modelMulti": "large-v3",
                                     "batchSlots": 8})
        assert need == 4122 + 8 * 349

    def test_the_dearer_of_the_two_models_counts(self):
        need = whisper.job_vram_mib({"device": "cuda",
                                     "model": "distil-large-v3.5",
                                     "modelMulti": "medium", "batchSlots": 4})
        assert need == 2468 + 4 * 203 or need == 2370 + 4 * 203

    def test_the_cpu_costs_the_card_nothing(self):
        assert whisper.job_vram_mib({"device": "cpu"}) == 0
