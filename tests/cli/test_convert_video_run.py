"""The per-file measurements ``convert_file`` takes before it encodes anything.

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
    """One file through ``convert_file``, stopping at the bitrate test's verdict.

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

        def worthwhile(relative, width, height, enc_width, enc_height,
                       source_grain, settings):
            judged.append(source_grain)
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
        status = state.convert_file("film.mkv")
        return settings, judged, status
    return run


def test_the_grain_probe_measures_the_source_it_is_given(measured):
    settings, judged, status = measured(grain_probe_wanted=True)
    assert status == 0
    assert settings.grain_level == "12"
    assert judged == ["12"]


def test_an_unmeasurable_source_synthesises_none(measured):
    settings, _judged, _status = measured(grain_probe_wanted=True, probe="0")
    assert settings.grain_level == "0"


def test_the_bitrate_test_measures_the_source_on_its_own(measured):
    settings, judged, _status = measured()
    # Nothing is being synthesised, so the run's own level is left alone and
    # only the verdict sees the measurement.
    assert judged == ["12"]
    assert settings.grain_level == "0"


def test_an_unmeasurable_source_is_judged_clean_by_the_bitrate_test(measured):
    _settings, judged, _status = measured(probe="0")
    assert judged == ["0"]


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
