"""Tests for medialib.lib.upscale - the upscaler behind convert-video's -u.

The real stack is a GPU, TensorRT and a VapourSynth environment, none of which a
test can count on. What a test CAN stand up is one that answers - a venv whose
python reports a TensorRT release, writes the engine a build names, and prints
what the probe asks for - and so the settle path is checked refusal by refusal,
the way a working stack is checked by its answers. The rest is everything
around it: where the parts are looked for and how their absence is reported, the
numbers handed to the script - the crop, the frame range, the matrix - and the
one command a chunk runs.
"""

import os
import sys

import pytest

from medialib.lib import upscale

pytestmark = pytest.mark.pure

# The venv the settle path is proved through is stubbed as shell scripts - a
# python that answers its -c by pattern - and Windows cannot execute one.
_POSIX_STUBS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the venv's python is stubbed as a POSIX shell script, which "
           "Windows cannot run")


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


def _working(root, probe='{"tensorrt": "10.0", "scale": 2, "bestsource": true}',
             probe_status=0, build_ok=True, vspipe_status=0, counter=None):
    """The layout of <root>, with parts that answer like a working one: the
    python says it imports a TensorRT that reports 10.0, writes the engine a
    build names, and answers the probe with <probe>, exiting <probe_status>;
    the vspipe answers its version. <counter>, when given a path, counts the
    builds."""
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "vspipe").write_text(
        "#!/bin/bash\nexit %d\n" % vspipe_status, encoding="ascii")
    (root / "venv" / "bin" / "vspipe").chmod(0o755)
    (root / "venv" / "bin" / "python").write_text(
        "#!/bin/bash\n"
        '[ "${1:-}" = "-c" ] || exit 1\n'
        'case "${2:-}" in\n'
        "  *build_serialized_network*)\n"
        + ('echo x >> "%s"\n' % counter if counter else "")
        + ('echo "the build failed" >&2\nexit 1;;\n'
           if not build_ok else 'echo engine > "$4";;\n')
        + '  *json.dumps*) printf "%%s\\n" \'%s\'; exit %d;;\n'
        % (probe, probe_status)
        + '  *"tensorrt.__version__"*) echo "10.0";;\n'
        + "  *) exit 1;;\n"
        + "esac\n"
        + "exit 0\n",
        encoding="ascii")
    (root / "venv" / "bin" / "python").chmod(0o755)
    (root / "plugins").mkdir()
    (root / "plugins" / "libvstrt.so").write_text("", encoding="ascii")
    (root / "models").mkdir()
    (root / "models" / upscale.DEFAULT_MODEL).write_text("", encoding="ascii")
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
@_POSIX_STUBS
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

    def test_a_stack_that_proves_itself_is_the_answer(self, monkeypatch,
                                                     tmp_path):
        counter = tmp_path / "builds"
        monkeypatch.setenv(
            "upscaleHome", str(_working(tmp_path, counter=str(counter))))
        stack, problem = upscale.settle("The GPU")
        assert problem == ""
        assert stack.scale == 2
        assert stack.tensorrt == "10.0"
        # the probe engine was built, once
        assert counter.read_text().strip().count("x") == 1

    def test_a_second_settle_reuses_the_engine_the_first_built(
            self, monkeypatch, tmp_path):
        counter = tmp_path / "builds"
        monkeypatch.setenv(
            "upscaleHome", str(_working(tmp_path, counter=str(counter))))
        first, _ = upscale.settle("The GPU")
        assert first is not None
        second, problem = upscale.settle("The GPU")
        assert problem == ""
        assert second is not None
        assert counter.read_text().strip().count("x") == 1

    def test_a_build_that_fails_refuses_saying_what_the_builder_said(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("upscaleHome", str(_working(tmp_path,
                                                       build_ok=False)))
        stack, problem = upscale.settle("The GPU")
        assert stack is None
        assert problem.startswith("TensorRT could not build an engine")
        assert "the build failed" in problem

    def test_a_probe_that_prints_no_json_refuses(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "upscaleHome",
            str(_working(tmp_path, probe="Traceback (most recent call last):",
                         probe_status=0)))
        stack, problem = upscale.settle("The GPU")
        assert stack is None
        assert problem.startswith("a test frame did not make it through")

    def test_a_stack_without_bestsource_refuses(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "upscaleHome", str(_working(
                tmp_path,
                probe='{"tensorrt": "10.0", "scale": 2, "bestsource": false}')))
        stack, problem = upscale.settle("The GPU")
        assert stack is None
        assert "no bestsource" in problem

    def test_a_network_that_does_not_enlarge_refuses(self, monkeypatch,
                                                     tmp_path):
        monkeypatch.setenv(
            "upscaleHome", str(_working(
                tmp_path,
                probe='{"tensorrt": "10.0", "scale": 1, "bestsource": true}')))
        stack, problem = upscale.settle("The GPU")
        assert stack is None
        assert "does not enlarge" in problem

    def test_a_vspipe_that_does_not_run_refuses(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "upscaleHome", str(_working(tmp_path, vspipe_status=1)))
        stack, problem = upscale.settle("The GPU")
        assert stack is None
        assert "does not run" in problem


@pytest.mark.fs
@_POSIX_STUBS
class TestTheEngineKept:
    """An engine is compiled for one frame size, one GPU and one TensorRT
    release, and what is kept is named after all three: what a change of any
    of the three meets is a missing engine, not a stale one."""

    def _settled(self, monkeypatch, tmp_path):
        counter = tmp_path / "builds"
        monkeypatch.setenv(
            "upscaleHome", str(_working(tmp_path, counter=str(counter))))
        stack, problem = upscale.settle("The GPU")
        assert problem == ""
        return stack, counter

    def test_a_size_built_once_is_not_built_again(self, monkeypatch, tmp_path):
        stack, counter = self._settled(monkeypatch, tmp_path)
        before = counter.read_text().strip().count("x")
        first, problem = upscale.engine_for(stack, 1920, 1080)
        assert problem == ""
        second, problem = upscale.engine_for(stack, 1920, 1080)
        assert problem == ""
        assert second == first
        assert os.path.isfile(first)
        assert counter.read_text().strip().count("x") == before + 1
        # and another size is another engine
        other, problem = upscale.engine_for(stack, 1280, 720)
        assert problem == ""
        assert other != first
        assert counter.read_text().strip().count("x") == before + 2

    def test_a_new_tensorrt_release_builds_its_own_engine(self, monkeypatch,
                                                          tmp_path):
        stack, _counter = self._settled(monkeypatch, tmp_path)
        old, _ = upscale.engine_for(stack, 1920, 1080)
        stack.tensorrt = "11.0"
        new, problem = upscale.engine_for(stack, 1920, 1080)
        assert problem == ""
        assert new != old
        assert os.path.isfile(old)
        assert os.path.isfile(new)

    def test_a_new_gpu_builds_its_own_engine(self, monkeypatch, tmp_path):
        stack, _counter = self._settled(monkeypatch, tmp_path)
        old, _ = upscale.engine_for(stack, 1920, 1080)
        stack.gpu = "The Other GPU"
        new, problem = upscale.engine_for(stack, 1920, 1080)
        assert problem == ""
        assert new != old
        assert os.path.isfile(old)
        assert os.path.isfile(new)


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
