"""The next file prepared while the current one encodes.

Preparing a file - its probes, the grain and crop measurements, the chunk
boundaries - is work of its own, and done between two encodes it is spent in
front of the second. So the run prepares one file ahead, on a thread beside the
encode. What that must not cost: a second Dolby Vision intermediate in the RAM
scratch beside the first, and the prepared file's lines printed over the one
encoding.
"""

import io
import threading

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_run as run_module

pytestmark = pytest.mark.fs

# Long enough for a thread that is going to move to have moved, on any host.
SETTLE = 0.3


@pytest.fixture
def settings(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        (source / name).write_text("")
    (tmp_path / "out").mkdir()
    return rules.Settings(input_dir=str(source), output_dir=str(tmp_path / "out"),
                          chunk_root=str(tmp_path / "work"), script_dir="")


def test_the_next_file_is_prepared_while_the_current_one_converts(
        settings, monkeypatch):
    prepared = set()
    order = []

    def prepare(self, relative):
        order.append("prepare " + relative)
        prepared.add(relative)
        return run_module.Plan(relative, rules.Settings(**settings.__dict__))

    def convert(self, plan):
        order.append("convert " + plan.relative)
        following = {"a.mkv": "b.mkv", "b.mkv": "c.mkv"}.get(plan.relative)
        if following:
            # Waited for inside the convert: a run that prepared after it would
            # never get here, and the wait would time out.
            for _ in range(50):
                if following in prepared:
                    break
                threading.Event().wait(0.1)
            assert following in prepared, "not prepared during the convert"
        return 0

    monkeypatch.setattr(run_module.Run, "prepare", prepare)
    monkeypatch.setattr(run_module.Run, "convert", convert)
    monkeypatch.setattr(run_module, "log", lambda *a, **k: None)

    assert run_module._run_all(settings) == 0
    assert [step for step in order if step.startswith("convert")] == [
        "convert a.mkv", "convert b.mkv", "convert c.mkv"]


def test_only_one_file_is_prepared_ahead(settings, monkeypatch):
    prepared = []
    converting = threading.Event()
    release = threading.Event()

    def prepare(self, relative):
        prepared.append(relative)
        return run_module.Plan(relative, rules.Settings(**settings.__dict__))

    def convert(self, plan):
        if plan.relative == "a.mkv":
            converting.set()
            release.wait(5)
        return 0

    monkeypatch.setattr(run_module.Run, "prepare", prepare)
    monkeypatch.setattr(run_module.Run, "convert", convert)
    monkeypatch.setattr(run_module, "log", lambda *a, **k: None)

    runner = threading.Thread(target=run_module._run_all, args=(settings,))
    runner.start()
    try:
        assert converting.wait(5)
        threading.Event().wait(SETTLE)
        assert prepared == ["a.mkv", "b.mkv"]
    finally:
        release.set()
        runner.join(10)


class TestOneDolbyVisionIntermediateAtATime:
    """A profile 7 source is converted to an 8.1 intermediate that is most of a
    film's video stream. The file prepared ahead waits for the encoding file to be
    done with its own before converting, rather than holding two."""

    @pytest.fixture
    def run(self, settings, monkeypatch):
        settings.dv_encoder_support = True
        monkeypatch.setattr(rules, "dolby_vision_profile",
                            lambda path: ("8", "") if path.endswith(".dv81")
                            else ("7", "1"))
        converted = []

        def normalise(relative, settings, output_dir):
            converted.append(relative)
            return relative + ".dv81", "/scratch/" + relative

        monkeypatch.setattr(run_module, "normalise_dolby_vision", normalise)
        monkeypatch.setattr(run_module, "dolby_vision_mode_for",
                            lambda *a, **k: "1")
        monkeypatch.setattr(run_module.ramscratch, "release_exit_cleanup",
                            lambda paths: None)
        state = run_module.Run(settings)
        state.converted_dv = converted
        return state

    def plan(self, run, relative):
        return run_module.Plan(relative,
                               rules.Settings(**run.settings.__dict__))

    def test_the_second_waits_until_the_first_is_released(self, run):
        first = self.plan(run, "a.mkv")
        run._settle_dolby_vision(first)
        assert first.holds_dv_slot

        second = self.plan(run, "b.mkv")
        ahead = threading.Thread(target=run._settle_dolby_vision,
                                 args=(second,))
        ahead.start()
        threading.Event().wait(SETTLE)
        assert run.converted_dv == ["a.mkv"]

        run._release_dv(first)
        ahead.join(5)
        assert run.converted_dv == ["a.mkv", "b.mkv"]
        assert second.settings.dolby_vision_source == "b.mkv.dv81"
        # Each file's intermediate is its own: the first's settings were not
        # handed the second's.
        assert first.settings.dolby_vision_source == ""

    def test_a_file_that_keeps_no_intermediate_hands_the_slot_back(
            self, run, monkeypatch):
        monkeypatch.setattr(run_module, "dolby_vision_mode_for",
                            lambda *a, **k: "0")
        first = self.plan(run, "a.mkv")
        run._settle_dolby_vision(first)
        assert not first.holds_dv_slot and not first.scratch

        second = self.plan(run, "b.mkv")
        run._settle_dolby_vision(second)
        assert run.converted_dv == ["a.mkv", "b.mkv"]


class TestTheHeldOutput:
    """The look-ahead's lines wait while the run is busy, and come out - in the
    order they were said - once the run is waiting on the look-ahead."""

    def test_the_look_ahead_is_held_and_the_run_is_not(self):
        stream = io.StringIO()
        output = run_module._HeldOutput(stream)
        output.live(False)

        def ahead():
            output.ahead = threading.get_ident()
            output.write("prepared b\n")

        thread = threading.Thread(target=ahead)
        thread.start()
        thread.join()
        output.write("encoded a\n")
        assert stream.getvalue() == "encoded a\n"

        output.live(True)
        assert stream.getvalue() == "encoded a\nprepared b\n"

    def test_a_live_look_ahead_is_written_straight_through(self):
        stream = io.StringIO()
        output = run_module._HeldOutput(stream)
        output.ahead = threading.get_ident()
        output.write("preparing\n")
        assert stream.getvalue() == "preparing\n"
