"""The neural upscaler behind convert-video's -u: a local VapourSynth with
vs-mlrt's TensorRT plugin, run from a Python environment of its own.

None of it is a Python package this one can import. VapourSynth, its source
filter, TensorRT and the model live in their own environment under
``upscaleHome`` (default ``~/vapoursynth``), which is driven by starting its
programs - the same arrangement ``read-library`` has with its narration
checkout:

    <upscaleHome>/venv/bin/vspipe, venv/bin/python
        VapourSynth, vapoursynth-bestsource, TensorRT, onnx, onnxconverter-common
    <upscaleHome>/plugins/libvstrt.so     vs-mlrt's TensorRT plugin
    <upscaleHome>/models/<model>.onnx     the network (``upscaleModel`` picks one)
    <upscaleHome>/engines/                what TensorRT builds from it, kept

A TensorRT engine is compiled for ONE frame size, one GPU and one TensorRT
release, so an engine is built the first time a size is met - seconds, for the
small networks this is meant for - and kept under a name that says all three.
The network is converted to FP16 on the way: TensorRT builds strongly typed
networks only, so a half-precision engine needs a half-precision model, and
half precision is where the speed is. The two agree to a fraction of a 10-bit
code value.

A frame goes decode -> crop -> RGB -> network -> resize to the target -> 10-bit
4:2:0, all inside VapourSynth, and comes out of ``vspipe`` as y4m into the ffmpeg
that encodes it. The resize after the network is what turns the network's fixed
factor into the size the tier asks for, and it is also where an anamorphic
source gets its square pixels: the target is computed from the DISPLAYED shape.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess

__all__ = [
    "DEFAULT_HOME",
    "DEFAULT_MODEL",
    "STREAMS",
    "Stack",
    "home",
    "missing_parts",
    "settle",
    "engine_for",
    "index_source",
    "write_script",
    "source_matrix",
    "crop_edges",
    "frame_at",
    "pipeline_argv",
]

DEFAULT_HOME = "~/vapoursynth"

# A 2x SPAN network trained on the damage real SD and HD sources carry -
# compression, chroma subsampling, scaling blur, halos, bad deinterlacing - and
# deliberately NOT on noise, so it keeps grain rather than wiping it.
DEFAULT_MODEL = "2xLiveActionV1_SPAN_490000.onnx"

# CUDA streams per vspipe. Two is where a small network stops getting faster on
# one card (measured on an RTX 5090: 248 fps on one, 355 on two, 362 on four), and
# every chunk of a file runs a vspipe of its own.
STREAMS = 2

# The frame the setup probe pushes through the whole stack: small enough that its
# engine builds in a moment, and a multiple of everything a network divides by.
_PROBE_SIZE = 64

# ffprobe's colour-space names as VapourSynth's resizer spells the matrix.
_MATRICES = {
    "bt709": "709",
    "bt470bg": "470bg",
    "smpte170m": "170m",
    "bt2020nc": "2020ncl",
    "fcc": "fcc",
    "smpte240m": "240m",
}

# Run by the environment's own Python: the engine for one frame size, written
# beside its final name and moved onto it, so a run that dies half-way - or a
# second run building the same size - never leaves a torn engine to be loaded.
_BUILD_ENGINE = r"""
import os, sys
import onnx
import tensorrt as trt
from onnxconverter_common.float16 import convert_float_to_float16
source, target, width, height = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
model = convert_float_to_float16(onnx.load(source), keep_io_types=False)
logger = trt.Logger(trt.Logger.ERROR)
builder = trt.Builder(logger)
network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.STRONGLY_TYPED))
parser = trt.OnnxParser(network, logger)
if not parser.parse(model.SerializeToString()):
    sys.exit("\n".join(str(parser.get_error(i)) for i in range(parser.num_errors)))
config = builder.create_builder_config()
profile = builder.create_optimization_profile()
shape = (1, network.get_input(0).shape[1], height, width)
profile.set_shape(network.get_input(0).name, shape, shape, shape)
config.add_optimization_profile(profile)
plan = builder.build_serialized_network(network, config)
if plan is None:
    sys.exit("TensorRT could not build an engine")
partial = "%s.%d.partial" % (target, os.getpid())
with open(partial, "wb") as handle:
    handle.write(plan)
os.replace(partial, target)
"""

# The whole stack exercised once: VapourSynth, the source filter, the TensorRT
# plugin, the GPU and one engine. What it prints is what the run needs to know
# that it cannot read from a file: the TensorRT release, and the factor the
# network scales by.
_PROBE = r"""
import json, sys
import vapoursynth as vs
import tensorrt
core = vs.core
core.std.LoadPlugin(sys.argv[1])
clip = core.std.BlankClip(width=int(sys.argv[3]), height=int(sys.argv[3]), format=vs.RGBH, length=1)
out = core.trt.Model(clip, engine_path=sys.argv[2])
out.get_frame(0)
print(json.dumps({"tensorrt": tensorrt.__version__, "scale": out.width // clip.width,
                  "bestsource": hasattr(core, "bs")}))
"""

# The index bestsource needs before it can seek, built once per file so the
# chunks that open it in parallel read it instead of each decoding the whole film.
_INDEX = r"""
import sys
import vapoursynth as vs
clip = vs.core.bs.VideoSource(sys.argv[1], cachemode=4, cachepath=sys.argv[2],
                              rff=0, fpsnum=int(sys.argv[3]), fpsden=int(sys.argv[4]))
print(clip.num_frames)
"""

# The script every chunk runs, its per-chunk values handed in with vspipe -a.
# rff=0 decodes the way ffmpeg does: the frames as stored, not with a DVD's
# soft-telecine flags applied, which would interleave fields into film frames.
_SCRIPT = r"""import vapoursynth as vs
core = vs.core
core.std.LoadPlugin(plugin)
clip = core.bs.VideoSource(source, cachemode=3, cachepath=cache, rff=0,
                           fpsnum=int(fpsnum), fpsden=int(fpsden))
last = int(end) if int(end) >= 0 else clip.num_frames
clip = clip[int(start):last]
left, right, top, bottom = (int(edge) for edge in crop.split(","))
if left or right or top or bottom:
    clip = core.std.Crop(clip, left, right, top, bottom)
clip = core.resize.Bicubic(clip, format=vs.RGBH, matrix_in_s=matrix,
                           range_in_s=range)
clip = core.trt.Model(clip, engine_path=engine, num_streams=int(streams))
clip = core.resize.Spline36(clip, int(width), int(height), format=vs.YUV420P10,
                            matrix_s="709", range_s="limited",
                            dither_type="error_diffusion")
clip = core.std.SetFrameProps(clip, _SARNum=1, _SARDen=1)
clip.set_output()
"""


class Stack:
    """The upscaler a run settled on: where its parts are and what it measured
    about them. Plain attributes, because it travels to the chunk workers inside
    the run's Settings."""

    def __init__(self, **values) -> None:
        self.home = ""
        self.vspipe = ""
        self.python = ""
        self.plugin = ""
        self.model = ""
        self.engines = ""
        self.gpu = ""
        self.tensorrt = ""
        self.scale = 0
        self.__dict__.update(values)

    def describe(self) -> str:
        return "%s (%dx) on TensorRT %s, %s" % (
            os.path.basename(self.model), self.scale, self.tensorrt,
            self.gpu or "unknown GPU")


def home() -> str:
    return os.path.abspath(os.path.expanduser(
        os.environ.get("upscaleHome", "") or DEFAULT_HOME))


def _model_path(root: str) -> str:
    """``upscaleModel`` as a path, or as a name under the models folder."""
    wanted = os.environ.get("upscaleModel", "") or DEFAULT_MODEL
    wanted = os.path.expanduser(wanted)
    if os.path.isabs(wanted):
        return wanted
    return os.path.join(root, "models", wanted)


def _parts(root: str) -> Stack:
    return Stack(home=root,
                 vspipe=os.path.join(root, "venv", "bin", "vspipe"),
                 python=os.path.join(root, "venv", "bin", "python"),
                 plugin=os.path.join(root, "plugins", "libvstrt.so"),
                 model=_model_path(root),
                 engines=os.path.join(root, "engines"))


def missing_parts(root: str) -> list[str]:
    """Each part of the layout that is not where it should be, as a line saying
    what it is for - every one at once, the way a missing tool is reported."""
    stack = _parts(root)
    wanted = ((stack.vspipe, "VapourSynth's vspipe, which runs the upscaler"),
              (stack.python, "the environment's Python, which builds engines"),
              (stack.plugin, "vs-mlrt's TensorRT plugin"),
              (stack.model, "the upscaling network (upscaleModel names another)"))
    return ["%s - %s" % (path, what) for path, what in wanted
            if not os.path.isfile(path)]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-") or "gpu"


def _engine_path(stack: Stack, width: int, height: int, tensorrt: str) -> str:
    model = os.path.splitext(os.path.basename(stack.model))[0]
    return os.path.join(stack.engines, "%s.%dx%d.fp16.trt%s.%s.engine" % (
        model, width, height, tensorrt, _slug(stack.gpu)))


def _run(argv: list, timeout: float | None = None) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, "", str(error)
    return (done.returncode, done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"))


def _tensorrt_version(stack: Stack) -> str:
    status, out, _err = _run([stack.python, "-c",
                              "import tensorrt; print(tensorrt.__version__)"])
    return out.strip() if status == 0 else ""


def _build(stack: Stack, width: int, height: int, target: str) -> str:
    """Build that engine; "" on success, else what went wrong."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    status, _out, err = _run([stack.python, "-c", _BUILD_ENGINE, stack.model,
                              target, str(width), str(height)])
    if status != 0 or not os.path.isfile(target):
        return (err.strip().splitlines() or ["exit status %d" % status])[-1]
    return ""


def settle(gpu: str) -> tuple[Stack | None, str]:
    """The upscaler this run will use, proven to work, or (None, why not).

    Proven by use rather than by looking: a TensorRT that cannot see the GPU, a
    plugin built against another TensorRT, a model TensorRT cannot parse all
    look installed. So one small engine is built and one frame is pushed through
    VapourSynth, the plugin and the GPU, which is also how the network's scale
    factor is learned - from what it made of that frame.
    """
    root = home()
    missing = missing_parts(root)
    if missing:
        return None, ("the upscaler is not installed under %s (upscaleHome); "
                      "missing:\n    %s" % (root, "\n    ".join(missing)))
    stack = _parts(root)
    stack.gpu = gpu
    tensorrt = _tensorrt_version(stack)
    if not tensorrt:
        return None, "TensorRT cannot be imported by %s" % stack.python
    engine = _engine_path(stack, _PROBE_SIZE, _PROBE_SIZE, tensorrt)
    if not os.path.isfile(engine):
        problem = _build(stack, _PROBE_SIZE, _PROBE_SIZE, engine)
        if problem:
            return None, "TensorRT could not build an engine: " + problem
    status, out, err = _run([stack.python, "-c", _PROBE, stack.plugin, engine,
                             str(_PROBE_SIZE)], timeout=300)
    try:
        answer = json.loads(out.strip().splitlines()[-1]) if status == 0 else {}
    except (ValueError, IndexError):
        answer = {}
    if not answer:
        last = (err.strip().splitlines() or ["exit status %d" % status])[-1]
        return None, "a test frame did not make it through the upscaler: " + last
    if not answer.get("bestsource"):
        return None, ("VapourSynth has no bestsource, which reads the videos "
                      "(pip install vapoursynth-bestsource)")
    stack.tensorrt = str(answer.get("tensorrt", tensorrt))
    stack.scale = int(answer.get("scale", 0) or 0)
    if stack.scale < 2:
        return None, ("%s does not enlarge (it scaled a test frame by %d)"
                      % (os.path.basename(stack.model), stack.scale))
    status, _out, _err = _run([stack.vspipe, "--version"])
    if status != 0:
        return None, "%s does not run" % stack.vspipe
    return stack, ""


def engine_for(stack: Stack, width: int, height: int) -> tuple[str, str]:
    """(engine, "") for a frame of that size - built now if this is the first
    time the size is met - or ("", what went wrong)."""
    target = _engine_path(stack, width, height, stack.tensorrt)
    if os.path.isfile(target):
        return target, ""
    problem = _build(stack, width, height, target)
    return ("", problem) if problem else (target, "")


def index_source(stack: Stack, source: str, cache: str, fps: str) -> int:
    """Index <source> into <cache> and answer its frame count, or 0 when it
    cannot be read. <fps> is the nominal "num/den" it is read at."""
    num, _sep, den = fps.partition("/")
    status, out, _err = _run([stack.python, "-c", _INDEX, source, cache,
                              num, den or "1"])
    lines = out.strip().splitlines()
    return int(lines[-1]) if status == 0 and lines and lines[-1].isdigit() else 0


def write_script(directory: str) -> str:
    path = os.path.join(directory, "upscale.vpy")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_SCRIPT)
    return path


def source_matrix(color_space: str, height) -> str:
    """The YUV matrix a source is decoded with, in the resizer's spelling.

    Its own when it states one; otherwise the one a player would assume from its
    size - 601 below HD, 709 from 720 lines up - which is what it was seen as all
    along."""
    if color_space in _MATRICES:
        return _MATRICES[color_space]
    try:
        return "709" if int(height) >= 720 else "170m"
    except (TypeError, ValueError):
        return "709"


def crop_edges(crop: str, width, height) -> str:
    """A ``w:h:x:y`` crop as the "left,right,top,bottom" VapourSynth cuts, or
    "0,0,0,0" for no crop."""
    fields = crop.split(":") if crop else []
    try:
        cw, ch, x, y = (int(field) for field in fields)
        return "%d,%d,%d,%d" % (x, int(width) - cw - x, y, int(height) - ch - y)
    except (TypeError, ValueError):
        return "0,0,0,0"


def frame_at(seconds, fps: str) -> int:
    """The first frame at or after <seconds> at the nominal "num/den" rate.

    Every chunk boundary goes through here, so the frame one chunk ends before is
    the frame the next one starts at, and the chunks tile the film exactly."""
    num, _sep, den = fps.partition("/")
    return int(float(seconds) * int(num) / int(den or 1) + 0.5)


def pipeline_argv(stack: Stack, script: str, values: dict,
                  encode: list) -> list:
    """The one command that upscales a range of frames and encodes it: vspipe's
    y4m into <encode>, an ffmpeg command line reading it from stdin.

    One command rather than two processes, so the pause keys, which stop a job
    and everything under it, stop the upscaler and the encoder together. Under
    pipefail, so a vspipe that dies fails the command even though the ffmpeg
    reading from it ends cleanly on what it was given.
    """
    args = []
    for key, value in sorted(values.items()):
        args += ["-a", "%s=%s" % (key, value)]
    args += ["-a", "plugin=%s" % stack.plugin, "-a", "streams=%d" % STREAMS]
    upscale = [stack.vspipe, "-c", "y4m"] + args + [script, "-"]
    line = "set -o pipefail; %s | %s" % (shlex.join(upscale), shlex.join(encode))
    return ["bash", "-c", line]
