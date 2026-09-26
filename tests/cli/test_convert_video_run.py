"""The per-file measurements ``prepare`` takes before it encodes anything.

The grain probe is measured HERE rather than once per run, because a clean file
must not be given grain it never had; the bitrate test asks the same measurement
a different question. Both reach `medialib/lib/videograin.py`, whose own white
box covers what a measurement means - what is pinned here is the CALL, which is
the one thing a test of the library alone can never see.
"""

import os

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_run as run_module

pytestmark = pytest.mark.fs


@pytest.fixture
def measured(monkeypatch, tmp_path):
    """One file through ``prepare``, stopping at the bitrate test's verdict.

    A refused conversion returns before any encode, which leaves the two grain
    measurements as the whole of what the call did.
    """
    def run(grain_probe_wanted=False, probe="12 0.3400"):
        source_dir = tmp_path / "in"
        source_dir.mkdir(exist_ok=True)
        (source_dir / "film.mkv").write_text("")

        monkeypatch.setattr(run_module.pausecontrol, "wait_while_paused",
                            lambda *a, **k: None)
        monkeypatch.setattr(run_module, "_media_duration", lambda path: 600.0)
        monkeypatch.setattr(rules, "video_dimensions",
                            lambda path: ("1920", "1080", "progressive", "1:1"))
        monkeypatch.setattr(run_module.videograin, "grain_probe_level",
                            lambda *a, **k: probe)
        monkeypatch.setattr(run_module, "log", lambda *a, **k: None)

        judged = []
        measured_with = []

        def worthwhile(relative, width, height, enc_width, enc_height,
                       source_grain, settings):
            judged.append(source_grain)
            measured_with.append(settings)
            return False

        monkeypatch.setattr(rules, "conversion_worthwhile", worthwhile)

        settings = rules.Settings(
            input_dir=str(source_dir), output_dir=str(tmp_path / "out"),
            chunk_root=str(tmp_path / "chunks"), cores=1,
            grain_probe_wanted=grain_probe_wanted,
            # Its refusal is what stops the run before an encoder is reached.
            test_source_bitrate=True,
        )
        os.makedirs(settings.output_dir, exist_ok=True)
        state = run_module.Run(settings)
        plan = state.prepare("film.mkv")
        # The file's own settings are the ones measured; the run's are left
        # alone, since the next file prepared would otherwise inherit them.
        assert settings.grain_level == "0"
        return measured_with[0], judged, plan
    return run


def test_the_grain_probe_measures_the_source_it_is_given(measured):
    settings, judged, plan = measured(grain_probe_wanted=True)
    assert plan is None
    assert settings.grain_level == "12"
    assert judged == ["12"]


def test_an_unmeasurable_source_synthesises_none(measured):
    settings, _judged, _plan = measured(grain_probe_wanted=True, probe="0")
    assert settings.grain_level == "0"


def test_the_bitrate_test_measures_the_source_on_its_own(measured):
    settings, judged, _plan = measured()
    # Nothing is being synthesised, so the run's own level is left alone and
    # only the verdict sees the measurement.
    assert judged == ["12"]
    assert settings.grain_level == "0"


def test_an_unmeasurable_source_is_judged_clean_by_the_bitrate_test(measured):
    _settings, judged, _plan = measured(probe="0")
    assert judged == ["0"]


class TestTheSizeAFileIsAnnouncedAt:
    """A crop and a scale do quite different things to a picture - one throws
    bands away, the other resamples what is left - so the line names each step it
    really took rather than collapsing them into one arrow."""

    @pytest.fixture
    def size_text(self):
        return run_module.Run(rules.Settings())._size_text

    def test_an_untouched_file_is_named_once(self, size_text):
        assert size_text("1920", "1080", "1920", "1080",
                         "1920", "1080") == "1920x1080"

    def test_a_crop_names_the_source_and_what_is_left_of_it(self, size_text):
        assert size_text("1920", "1080", "1920", "804", "1920", "804") == (
            "1920x1080 -> cropped 1920x804")

    def test_a_scale_alone_still_reads_as_it_always_did(self, size_text):
        assert size_text("3840", "2160", "3840", "2160", 1920, 1080) == (
            "3840x2160 -> 1920x1080")

    def test_both_are_named_in_the_order_the_filters_apply(self, size_text):
        """The ceiling is a ceiling on the PICTURE, so the crop is the middle
        step and the encoded size follows from it, not from the source."""
        assert size_text("1920", "1080", "1920", "804", 1280, 536) == (
            "1920x1080 -> cropped 1920x804 -> 1280x536")

    def test_a_size_that_could_not_be_read_says_so(self, size_text):
        assert size_text("", "", "", "", "", "") == "size unknown"

    def test_a_half_read_size_names_the_axis_that_came_through(self, size_text):
        assert size_text("", "1080", "", "1080", "", "1080") == "?x1080"


class TestTheHardwareDecodeLadder:
    """Which interface a run decodes through, and what it says it chose.

    One rung per platform's own way of reaching the same silicon. The rungs are
    probed in order and each probe opens the device for real, so what is
    pinned here is the ORDER and the fall-through - the probes themselves are
    an ffmpeg away and belong to the media tier.
    """

    pytestmark = pytest.mark.pure

    def test_an_intel_igpu_is_taken_first(self, monkeypatch):
        monkeypatch.setattr(run_module, "intel_render_node",
                            lambda: "/dev/dri/renderD128")
        flags, said = run_module._decode_accel()
        assert flags == "-hwaccel vaapi -hwaccel_device /dev/dri/renderD128"
        assert "VAAPI" in said

    def test_a_mac_reaches_the_same_silicon_through_videotoolbox(self,
                                                                 monkeypatch):
        """There is no /dev/dri to walk there, and on Apple Silicon there is no
        discrete GPU to fall back on either."""
        monkeypatch.setattr(run_module, "intel_render_node", lambda: "")
        monkeypatch.setattr(run_module.hostos, "is_macos", lambda *_a: True)
        monkeypatch.setattr(run_module, "videotoolbox_works", lambda: True)
        flags, said = run_module._decode_accel()
        assert flags == "-hwaccel videotoolbox"
        assert "VideoToolbox" in said

    def test_a_mac_whose_ffmpeg_was_built_without_it_decodes_in_software(
            self, monkeypatch):
        """Every Mac has the hardware, so the probe is really asking about the
        BUILD - and a static one fetched from elsewhere may not have it."""
        monkeypatch.setattr(run_module, "intel_render_node", lambda: "")
        monkeypatch.setattr(run_module.hostos, "is_macos", lambda *_a: True)
        monkeypatch.setattr(run_module, "videotoolbox_works", lambda: False)
        assert run_module._decode_accel() == (
            "", "Hardware decode: no usable iGPU found, decoding in software.")

    def test_no_other_platform_pays_for_the_macos_probe(self, monkeypatch):
        """It costs an ffmpeg start, and nothing but a Mac has the framework
        to find."""
        probed = []
        monkeypatch.setattr(run_module, "intel_render_node", lambda: "")
        monkeypatch.setattr(run_module.hostos, "is_macos", lambda *_a: False)
        monkeypatch.setattr(run_module, "videotoolbox_works",
                            lambda: probed.append(1) or True)
        flags, _said = run_module._decode_accel()
        assert (flags, probed) == ("", [])


class TestTheArgumentsOfOneChunk:
    """Where a chunk starts and stops. The frames each argv selects are asserted
    against real video in `test_convert_video_chunks_media.py`; this pins the
    argv itself."""

    @pytest.fixture
    def chunk_argv(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rules, "build_video_args",
                            lambda base, path, settings: "-c:v libx265")
        monkeypatch.setattr(run_module.safety, "trap_worker_abort",
                            lambda: None)
        seen = []
        monkeypatch.setattr(run_module, "run_quiet_encode",
                            lambda argv: seen.append(argv) or 0)
        settings = rules.Settings(input_dir=str(tmp_path / "in"),
                                  chunk_root=str(tmp_path / "chunks"))

        def run(index, total, start, span):
            run_module.encode_video_chunk(settings, rules.UNIT.join(
                ["film.mkv", str(index), str(total), start, span]))
            return seen[-1]
        return run

    def test_a_chunk_is_cut_by_trim_and_not_by_minus_t(self, chunk_argv):
        argv = chunk_argv(1, 3, "40.000", "40.000")
        assert "-t" not in argv
        assert argv[argv.index("-ss") + 1] == "40.000"
        assert argv.index("-ss") < argv.index("-i")
        assert argv[argv.index("-vf") + 1] == "trim=end=40.000"

    def test_the_last_chunk_runs_to_the_end_of_the_stream(self, chunk_argv):
        argv = chunk_argv(2, 3, "80.000", "40.000")
        assert "-t" not in argv
        assert "-vf" not in argv
        assert argv[argv.index("-ss") + 1] == "80.000"


class TestTheResumeSkip:
    """An output already in place is skipped when it spans its source - measured
    with the slack the intermediate was allowed, so a finished file whose audio
    ends a few milliseconds early is recognised, and one cut short is not."""

    SOURCE = 40.023

    class Encoded(Exception):
        """``prepare`` got past the resume check and on to the encode's own
        measurements - raised there so nothing else has to be stubbed."""

    @pytest.fixture
    def resume(self, monkeypatch, tmp_path):
        def run(output_seconds):
            (tmp_path / "in").mkdir()
            (tmp_path / "in" / "clip.mkv").write_text("")
            (tmp_path / "out").mkdir()
            (tmp_path / "out" / "clip.mkv").write_text("")

            monkeypatch.setattr(run_module.pausecontrol, "wait_while_paused",
                                lambda *a, **k: None)
            monkeypatch.setattr(
                run_module, "_media_duration",
                lambda path: self.SOURCE if "/in/" in path else output_seconds)

            def encoded(path):
                raise self.Encoded()

            monkeypatch.setattr(rules, "video_dimensions", encoded)
            lines = []
            monkeypatch.setattr(run_module, "log", lines.append)

            state = run_module.Run(rules.Settings(
                input_dir=str(tmp_path / "in"), output_dir=str(tmp_path / "out"),
                chunk_root=str(tmp_path / "chunks"), cores=1))
            try:
                state.prepare("clip.mkv")
            except self.Encoded:
                return False
            assert state.skipped == 1
            assert lines == ["Up to date, skipping: clip.mkv"]
            return True
        return run

    def test_an_output_as_long_as_its_source_is_skipped(self, resume):
        assert resume(self.SOURCE)

    def test_an_opus_track_ending_milliseconds_early_is_still_up_to_date(
            self, resume):
        # The case that was re-encoded on every run: 40.008 against 40.023.
        assert resume(40.008)

    def test_up_to_the_slack_short_is_still_up_to_date(self, resume):
        assert resume(self.SOURCE - rules.LENGTH_SLACK_SECONDS)

    def test_an_output_cut_short_is_converted_again(self, resume):
        assert not resume(self.SOURCE - rules.LENGTH_SLACK_SECONDS - 0.1)

    def test_a_truncated_output_is_converted_again(self, resume):
        assert not resume(self.SOURCE / 2)
