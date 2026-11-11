"""The files around the one encoding, worked while it encodes.

Preparing a file - its probes, the -t test, the grain and crop measurements,
the chunk boundaries - is work of its own, and done between two encodes it is
spent in front of the second. So is the previous file's tail: waiting for its
audio, the mux and the checks of the muxed file. So the run prepares files
ahead through the dynamic queue, on a thread beside the encode, and muxes the
previous file on another. What that must not cost: a second Dolby Vision
intermediate in the RAM scratch beside the first, more than one mux at a time,
and the prepared files' lines printed over the one encoding.
"""

import io
import threading

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_hdr as hdr
from medialib.cli import convert_video_run as run_module
from medialib.cli import convert_video_source as videosource

pytestmark = pytest.mark.fs

# Long enough for a thread that is going to move to have moved, on any host.
SETTLE = 0.3
LIMIT = 5


@pytest.fixture
def settings(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv", "e.mkv"):
        (source / name).write_text("")
    (tmp_path / "out").mkdir()
    return rules.Settings(input_dir=str(source), output_dir=str(tmp_path / "out"),
                          chunk_root=str(tmp_path / "work"), script_dir="")


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(run_module, "log", lambda *a, **k: None)


def _plan(settings, relative):
    return run_module.Plan(relative, rules.Settings(**settings.__dict__))


def _wait(predicate) -> bool:
    for _ in range(LIMIT * 10):
        if predicate():
            return True
        threading.Event().wait(0.1)
    return predicate()


def test_the_next_file_is_prepared_while_the_current_one_encodes(
        settings, monkeypatch):
    prepared = set()
    order = []

    def prepare(self, relative):
        prepared.add(relative)
        return _plan(settings, relative)

    def start_convert(self, plan):
        order.append(plan.relative)
        following = {"a.mkv": "b.mkv", "b.mkv": "c.mkv"}.get(plan.relative)
        if following:
            # Waited for inside the encode: a run that prepared after it would
            # never get here, and the wait would time out.
            assert _wait(lambda: following in prepared), \
                "not prepared during the encode"
        return lambda: 0

    monkeypatch.setattr(run_module.Run, "prepare", prepare)
    monkeypatch.setattr(run_module.Run, "start_convert", start_convert)

    assert run_module._run_all(settings) == 0
    assert order == ["a.mkv", "b.mkv", "c.mkv", "d.mkv", "e.mkv"]


def test_the_look_ahead_is_as_deep_as_the_queue_buffer(settings, monkeypatch):
    prepared = []
    encoding = threading.Event()
    release = threading.Event()

    def prepare(self, relative):
        prepared.append(relative)
        return _plan(settings, relative)

    def start_convert(self, plan):
        if plan.relative == "a.mkv":
            encoding.set()
            release.wait(LIMIT)
        return lambda: 0

    monkeypatch.setattr(run_module.Run, "prepare", prepare)
    monkeypatch.setattr(run_module.Run, "start_convert", start_convert)

    runner = threading.Thread(target=run_module._run_all, args=(settings,))
    runner.start()
    try:
        assert encoding.wait(LIMIT)
        threading.Event().wait(SETTLE)
        assert prepared == ["a.mkv", "b.mkv", "c.mkv"][:1 + run_module.LOOKAHEAD]
    finally:
        release.set()
        runner.join(10)


def test_files_passed_over_do_not_use_up_the_look_ahead(settings, monkeypatch):
    """A file the preparation skips is not waiting to be encoded, so a stretch
    of them is worked through while the current file encodes rather than
    leaving the encoder to wait on it afterwards."""
    prepared = []
    encoding = threading.Event()
    release = threading.Event()

    def prepare(self, relative):
        prepared.append(relative)
        if relative in ("b.mkv", "c.mkv", "d.mkv"):
            return None
        return _plan(settings, relative)

    def start_convert(self, plan):
        if plan.relative == "a.mkv":
            encoding.set()
            release.wait(LIMIT)
        return lambda: 0

    monkeypatch.setattr(run_module.Run, "prepare", prepare)
    monkeypatch.setattr(run_module.Run, "start_convert", start_convert)

    runner = threading.Thread(target=run_module._run_all, args=(settings,))
    runner.start()
    try:
        assert encoding.wait(LIMIT)
        assert _wait(lambda: "e.mkv" in prepared)
    finally:
        release.set()
        runner.join(10)


class TestTheMuxBesideTheNextEncode:
    """The previous file's audio wait, mux and checks run while the next file's
    video encodes - one at a time, and each still counted."""

    def test_the_next_encode_starts_while_the_previous_file_muxes(
            self, settings, monkeypatch):
        events = []
        muxed = threading.Event()

        def start_convert(self, plan):
            events.append("encode " + plan.relative)

            def tail():
                if plan.relative == "a.mkv":
                    # Held until the next file's encode has started: a run
                    # that muxed in front of it would never get there.
                    assert _wait(lambda: "encode b.mkv" in events)
                    muxed.set()
                events.append("muxed " + plan.relative)
                return 0
            return tail

        monkeypatch.setattr(run_module.Run, "prepare",
                            lambda self, relative: _plan(settings, relative))
        monkeypatch.setattr(run_module.Run, "start_convert", start_convert)

        assert run_module._run_all(settings) == 0
        assert muxed.is_set()
        assert events.index("encode b.mkv") < events.index("muxed a.mkv")
        # Every file muxed, the last one too, and before the run returned.
        assert sorted(e for e in events if e.startswith("muxed")) == [
            "muxed a.mkv", "muxed b.mkv", "muxed c.mkv", "muxed d.mkv",
            "muxed e.mkv"]

    def test_only_one_mux_runs_at_a_time(self, settings, monkeypatch):
        lock = threading.Lock()
        running = [0]
        most = [0]

        def start_convert(self, plan):
            def tail():
                with lock:
                    running[0] += 1
                    most[0] = max(most[0], running[0])
                threading.Event().wait(0.05)
                with lock:
                    running[0] -= 1
                return 0
            return tail

        monkeypatch.setattr(run_module.Run, "prepare",
                            lambda self, relative: _plan(settings, relative))
        monkeypatch.setattr(run_module.Run, "start_convert", start_convert)

        assert run_module._run_all(settings) == 0
        assert most[0] == 1

    def test_a_failed_mux_is_counted_and_the_run_goes_on(
            self, settings, monkeypatch):
        encoded = []
        state = {}
        original = run_module.Run.__init__

        def init(self, settings):
            original(self, settings)
            state["run"] = self

        def start_convert(self, plan):
            encoded.append(plan.relative)
            return lambda: 1 if plan.relative == "b.mkv" else 0

        monkeypatch.setattr(run_module.Run, "__init__", init)
        monkeypatch.setattr(run_module.Run, "prepare",
                            lambda self, relative: _plan(settings, relative))
        monkeypatch.setattr(run_module.Run, "start_convert", start_convert)

        assert run_module._run_all(settings) == 1
        assert state["run"].failed == 1
        assert len(encoded) == 5

    def test_what_the_mux_raises_is_raised_by_the_run(
            self, settings, monkeypatch):
        def start_convert(self, plan):
            def tail():
                raise RuntimeError("mux broke on " + plan.relative)
            return tail

        monkeypatch.setattr(run_module.Run, "prepare",
                            lambda self, relative: _plan(settings, relative))
        monkeypatch.setattr(run_module.Run, "start_convert", start_convert)

        with pytest.raises(RuntimeError, match="a.mkv"):
            run_module._run_all(settings)


class TestOneDolbyVisionIntermediateAtATime:
    """A profile 7 source is converted to an 8.1 intermediate that is most of a
    film's video stream. The file prepared ahead waits for the encoding file to be
    done with its own before converting, rather than holding two."""

    @pytest.fixture
    def run(self, settings, monkeypatch):
        settings.dv_encoder_support = True
        monkeypatch.setattr(videosource, "dolby_vision_profile",
                            lambda path: ("8", "") if path.endswith(".dv81")
                            else ("7", "1"))
        converted = []

        def normalise(relative, settings, output_dir):
            converted.append(relative)
            return relative + ".dv81", "/scratch/" + relative

        monkeypatch.setattr(hdr, "normalise_dolby_vision", normalise)
        monkeypatch.setattr(hdr, "dolby_vision_mode_for",
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
        monkeypatch.setattr(hdr, "dolby_vision_mode_for",
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
