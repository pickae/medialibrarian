"""Tests for medialib.lib.upscale - the upscaler behind convert-video's -u.

The real stack is a GPU, TensorRT and a VapourSynth environment, none of which a
test can count on, so what is here is everything around it: where the parts are
looked for and how their absence is reported, the numbers handed to the script -
the crop, the frame range, the matrix - and the one command a chunk runs.
"""

import os

import pytest

from medialib.lib import upscale

pytestmark = pytest.mark.pure


def _layout(root, skip=()):
    """The layout under <root>, every part present except those in <skip>, each
    part an executable that fails."""
    parts = {"vspipe": "venv/bin/vspipe", "python": "venv/bin/python",
             "plugin": "plugins/libvstrt.so",
             "model": "models/" + upscale.DEFAULT_MODEL}
    for name, relative in parts.items():
        if name in skip:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 1\n")
        path.chmod(0o755)
    return root


class TestWhereItLives:

    def test_upscaleHome_names_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv("upscaleHome", str(tmp_path / "vs"))
        assert upscale.home() == str(tmp_path / "vs")

    def test_without_it_the_default_is_under_home(self, monkeypatch, tmp_path):
        """Home is HOME on POSIX and USERPROFILE on Windows, so both are set."""
        monkeypatch.delenv("upscaleHome", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert upscale.home() == str(tmp_path / "vapoursynth")

    @pytest.mark.fs
    def test_every_missing_part_is_named_at_once(self, tmp_path):
        missing = upscale.missing_parts(str(tmp_path))
        assert len(missing) == 4
        assert any("vspipe" in line for line in missing)
        assert any("libvstrt.so" in line for line in missing)
        assert any(upscale.DEFAULT_MODEL in line for line in missing)

    @pytest.mark.fs
    def test_a_whole_layout_is_missing_nothing(self, tmp_path):
        assert upscale.missing_parts(str(_layout(tmp_path))) == []

    @pytest.mark.fs
    def test_upscaleModel_is_a_name_under_models_or_a_path(self, monkeypatch,
                                                            tmp_path):
        _layout(tmp_path, skip=("model",))
        monkeypatch.setenv("upscaleModel", "other.onnx")
        assert upscale.missing_parts(str(tmp_path)) == [
            "%s - the upscaling network (upscaleModel names another)"
            % (tmp_path / "models" / "other.onnx")]
        elsewhere = tmp_path / "elsewhere.onnx"
        elsewhere.write_text("")
        monkeypatch.setenv("upscaleModel", str(elsewhere))
        assert upscale.missing_parts(str(tmp_path)) == []


@pytest.mark.fs
class TestSettle:
    """Refused, with the reason, before anything is encoded."""

    def test_a_missing_part_refuses_and_says_which(self, monkeypatch, tmp_path):
        monkeypatch.setenv("upscaleHome", str(_layout(tmp_path,
                                                      skip=("plugin",))))
        stack, problem = upscale.settle("GPU")
        assert stack is None
        assert "not installed under %s" % tmp_path in problem
        assert "libvstrt.so" in problem

    def test_parts_that_do_not_work_refuse_too(self, monkeypatch, tmp_path):
        """A python that cannot import TensorRT looks installed."""
        monkeypatch.setenv("upscaleHome", str(_layout(tmp_path)))
        stack, problem = upscale.settle("GPU")
        assert stack is None
        assert problem.startswith("TensorRT cannot be imported")


class TestTheNumbersTheScriptIsHanded:

    def test_a_crop_becomes_the_four_edges(self):
        assert upscale.crop_edges("720:360:0:60", 720, 480) == "0,0,60,60"
        assert upscale.crop_edges("1904:800:8:140", 1920, 1080) == "8,8,140,140"

    @pytest.mark.parametrize("crop", ["", "junk", "1:2:3"])
    def test_no_crop_is_no_edges(self, crop):
        assert upscale.crop_edges(crop, 720, 480) == "0,0,0,0"

    def test_the_chunks_tile_the_frames_exactly(self):
        """Each chunk ends at the frame the next begins at, so a boundary that
        falls between two frames is decided once, the same way from both sides."""
        fps = "24000/1001"
        bounds = [0.0, 13.347, 27.110, 41.708, 60.0]
        frames = [upscale.frame_at(bound, fps) for bound in bounds]
        assert frames == sorted(frames)
        assert frames[0] == 0 and frames[-1] == 1439
        assert upscale.frame_at(1.0, "25/1") == 25

    @pytest.mark.parametrize("space,height,matrix", [
        ("bt709", 480, "709"), ("smpte170m", 1080, "170m"),
        ("bt470bg", 576, "470bg"), ("", 480, "170m"), ("", 576, "170m"),
        ("", 720, "709"), ("unknown", 1080, "709"), ("", "", "709"),
    ])
    def test_the_matrix_is_the_sources_or_the_one_its_size_implies(
            self, space, height, matrix):
        assert upscale.source_matrix(space, height) == matrix


class TestThePipeline:

    STACK = upscale.Stack(vspipe="/vs/venv/bin/vspipe",
                          plugin="/vs/plugins/libvstrt.so")

    def test_one_bash_command_under_pipefail(self):
        argv = upscale.pipeline_argv(self.STACK, "/tmp/up.vpy",
                                     {"start": "0"}, ["ffmpeg", "-i", "pipe:0"])
        assert argv[:2] == ["bash", "-c"]
        assert argv[2].startswith("set -o pipefail; /vs/venv/bin/vspipe -c y4m ")
        assert argv[2].endswith(" /tmp/up.vpy - | ffmpeg -i pipe:0")

    def test_every_value_travels_as_a_script_argument(self):
        line = upscale.pipeline_argv(
            self.STACK, "/tmp/up.vpy", {"width": "1920", "crop": "0,0,60,60"},
            ["ffmpeg"])[2]
        assert "-a crop=0,0,60,60 -a width=1920 " in line
        assert "-a plugin=/vs/plugins/libvstrt.so" in line
        assert "-a streams=%d" % upscale.STREAMS in line

    def test_a_path_with_spaces_and_quotes_is_quoted(self):
        line = upscale.pipeline_argv(
            self.STACK, "/tmp/up.vpy", {"source": "/films/Your Film's Name (1999).mkv"},
            ["ffmpeg", "-progress", "/tmp/prog 0"])[2]
        assert "'source=/films/Your Film'\"'\"'s Name (1999).mkv'" in line
        assert "'/tmp/prog 0'" in line

    @pytest.mark.fs
    def test_the_script_is_written_where_it_is_asked(self, tmp_path):
        path = upscale.write_script(str(tmp_path))
        assert path == os.path.join(str(tmp_path), "upscale.vpy")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert "core.trt.Model(" in text and "rff=0" in text
