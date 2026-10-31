"""The white box for medialib/lib/encodercalibration.py.

The curves here are made up so that the answer is known in advance: an
encoder whose files are a fixed factor larger at every score has exactly that
factor as its BD-rate, and an encoder whose quality scale is the CPU's shifted
by ten must be asked for ten more. From those, what is pinned is the mapping a
run depends on - which preset an -s gets, and which quality an -l does - and
that a saved calibration comes back as it went, while a broken one is ignored
rather than trusted.
"""

from __future__ import annotations

import json
import os

import pytest

from medialib.lib import encodercalibration as ec

pytestmark = pytest.mark.pure


def _curve(factor=1.0, shift=0.0, qualities=range(20, 100, 10)):
    """Score rises with quality, size with score; <factor> scales the size at
    every score, <shift> moves the quality scale under it."""
    return [ec.Point(q + shift, factor * (0.05 + 0.01 * q), float(q))
            for q in qualities]


def _found(cpu_factors, gpu_factors, gpu_shift=0.0):
    found = {("cpu", str(s)): _curve(f) for s, f in cpu_factors.items()}
    found.update({("gpu", p): _curve(f, gpu_shift)
                  for p, f in gpu_factors.items()})
    return found


class TestTheBdRate:
    def test_the_same_curve_costs_nothing(self):
        assert ec.bd_rate(_curve(), _curve()) == pytest.approx(0)

    @pytest.mark.parametrize("factor", [0.8, 1.05, 1.3])
    def test_a_constant_factor_is_its_own_rate(self, factor):
        assert ec.bd_rate(_curve(factor), _curve()) == pytest.approx(
            factor - 1)

    def test_curves_that_never_meet_in_score_are_not_compared(self):
        low = _curve(qualities=range(0, 30, 10))
        high = _curve(qualities=range(50, 90, 10))
        assert ec.bd_rate(low, high) is None


class TestThePresetForASpeed:
    """-s's ranking carried over: the defaults tied together, and each side
    spread by the sizes measured on it."""

    # Sizes in the shape measured: every preset smaller than every CPU speed.
    GPU = {"slow": 0.873, "base": 0.885, "fast": 0.888, "faster": 0.899,
           "turbo": 0.909, "turbo2": 0.92, "turbo3": 0.934}
    CPU = {2: 0.981, 3: 0.983, 4: 0.992, 5: 1.0, 6: 1.068, 7: 1.214,
           8: 1.221, 9: 1.261}

    def test_the_defaults_are_tied_together(self):
        assert ec.derive(_found(self.CPU, self.GPU)).presets[5] == "turbo"

    def test_the_ends_meet_the_ends(self):
        presets = ec.derive(_found(self.CPU, self.GPU)).presets
        assert presets[2] == "slow" and presets[9] == "turbo3"

    def test_a_lower_speed_never_gets_a_faster_preset(self):
        presets = ec.derive(_found(self.CPU, self.GPU)).presets
        ranks = [ec.GPU_PRESETS.index(presets[s]) for s in sorted(presets)]
        assert ranks == sorted(ranks)

    def test_a_jump_in_the_cpu_s_sizes_does_not_drag_the_default(self):
        """Sizes alone would put -s 5 near the slowest end: 7's step is
        most of the CPU's range."""
        cpu = {**self.CPU, 7: 3.0, 8: 3.0, 9: 3.0}
        assert ec.derive(_found(cpu, self.GPU)).presets[5] == "turbo"

    def test_with_nothing_to_rank_by_every_speed_takes_the_anchor(self):
        flat = {p: 1.2 for p in self.GPU}
        presets = ec.derive(_found({2: 1.0, 5: 1.0, 9: 1.0}, flat)).presets
        assert set(presets.values()) == {ec.GPU_ANCHOR}


class TestTheQualityForALevel:
    def test_a_shifted_scale_is_asked_for_shifted(self):
        """The GPU scores at q+10 what the CPU does at q: ask it for +10."""
        found = _found({5: 1.0}, {"slow": 1.0}, gpu_shift=10)
        calibration = ec.derive(found)
        assert calibration.gpu_settings(5, 60) == ("slow", 70)
        assert calibration.gpu_settings(5, 35) == ("slow", 45)

    def test_past_the_measured_range_it_carries_on_and_is_held_to_0_100(self):
        calibration = ec.Calibration({5: "turbo"},
                                     {5: [(30, 40.0), (90, 100.0)]})
        assert calibration.gpu_settings(5, 20) == ("turbo", 30)
        assert calibration.gpu_settings(5, 95) == ("turbo", 100)
        assert calibration.gpu_settings(5, 0) == ("turbo", 10)

    def test_an_unmeasured_speed_takes_the_nearest(self):
        calibration = ec.Calibration(
            {2: "slow", 5: "turbo"},
            {2: [(30, 30.0), (90, 90.0)], 5: [(30, 40.0), (90, 100.0)]})
        assert calibration.gpu_settings(0, 60)[0] == "slow"
        assert calibration.gpu_settings(9, 60)[0] == "turbo"

    def test_the_default_answers_every_speed_and_quality(self):
        for speed in range(10):
            for quality in range(0, 101, 5):
                preset, matched = ec.DEFAULT.gpu_settings(speed, quality)
                assert preset in ec.GPU_PRESETS and 0 <= matched <= 100


class TestTheCurves:
    def test_bits_are_summed_before_dividing(self):
        """A large crop weighs what it costs."""
        rows = [("cpu", "5", 60, 100, 100, 70.0),
                ("cpu", "5", 60, 900, 100, 60.0)]
        (point,) = ec.curves(rows)[("cpu", "5")]
        assert point == ec.Point(60, 5.0, 65.0)

    def test_points_are_in_quality_order(self):
        rows = [("gpu", "turbo", q, 10, 10, float(q)) for q in (80, 20, 50)]
        assert [p.quality for p in ec.curves(rows)[("gpu", "turbo")]] == [
            20, 50, 80]


class TestTheSavedCalibration:
    pytestmark = pytest.mark.fs

    def test_it_comes_back_as_it_went(self):
        calibration = ec.Calibration({5: "turbo", 9: "turbo3"},
                                     {5: [(30, 41.5), (90, 88.0)],
                                      9: [(30, 40.0), (90, 87.5)]})
        ec.save(calibration)
        assert ec.load() == calibration
        assert ec.active() == calibration

    def test_it_lives_beside_the_settings_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLI_SETTINGS_FILE", str(tmp_path / "settings"))
        assert ec.path() == str(tmp_path / ec.FILE_NAME)

    def test_without_one_the_default_is_used(self):
        assert ec.load() is None
        assert ec.active() is ec.DEFAULT

    @pytest.mark.parametrize("text", [
        "not json",
        json.dumps({"presets": {}, "qualities": {}}),
        json.dumps({"presets": {"5": "warp"}, "qualities": {"5": [[30, 40]]}}),
        json.dumps({"presets": {"5": "turbo"}, "qualities": {"5": [[30, 40]]}}),
        json.dumps({"presets": {"5": "turbo"}}),
    ], ids=["garbage", "empty", "unknown-preset", "one-point", "half"])
    def test_a_broken_one_is_ignored_not_trusted(self, text):
        path = ec.path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
        assert ec.load() is None
        assert ec.active() is ec.DEFAULT
