"""Tests for medialib.lib.whisper - the device, compute type and models this
host can actually transcribe with.

What is pinned here: the job/model/multi constants, the probe-and-init
settlement driven through the shared tool stub, the exact argv each probe hands
its tools, and the host-tool edge cases - an absent ffmpeg, an absent pipx.
"""

import os
import shutil
from types import SimpleNamespace

import pytest

from medialib.lib import whisper
from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLSTUB = blackbox.TOOLSTUB

_GPU_LINE = "GPU 0: NVIDIA GeForce RTX 5090"
_PLUMBING = ("bash", "awk", "cat", "grep", "head")


def _ffmpeg_call(ram):
    return ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-f", "lavfi",
            "-i", "anullsrc=r=16000:cl=mono", "-t", "0.5",
            os.path.join(ram, "whisperProbe.wav")]


def _pipx_call(ram, model, device, compute, threads):
    return ["pipx", "run", "whisper-ctranslate2",
            os.path.join(ram, "whisperProbe.wav"), "--output_dir", ram,
            "--model", model, "--language", "en", "--output_format", "srt",
            "--device", device, "--compute_type", compute, "--threads", threads]


def _nvidia_listing():
    return ["nvidia-smi", "-L"]


def _nvidia_query():
    return ["nvidia-smi", "--query-gpu=memory.free",
            "--format=csv,noheader,nounits"]


def _nvidia_stdout(vram, gpu=True):
    lines = [vram]
    if gpu:
        lines.append(_GPU_LINE)
    return "\n".join(lines) + "\n"


@pytest.fixture()
def w(tmp_path, monkeypatch):
    """A PATH holding only the named stubs and their plumbing, plus the knobs
    that decide what each tool prints and with which per-call status it exits.
    """
    bin_dir = tmp_path / "bin"
    out_dir = tmp_path / "out"
    state_dir = tmp_path / "state"
    ram_dir = tmp_path / "ram"
    for d in (bin_dir, out_dir, state_dir, ram_dir):
        d.mkdir()
    for tool in _PLUMBING:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    record = tmp_path / "calls"

    def install(name):
        shutil.copyfile(_TOOLSTUB, str(bin_dir / name))
        os.chmod(str(bin_dir / name), 0o755)

    def say(name, text):
        (out_dir / name).write_text(text)

    def rc(name, codes):
        (out_dir / (name + ".rc")).write_text(codes + "\n")

    def calls():
        if not record.exists():
            return []
        return [line.rstrip("\n").split("\t")[1:]
                for line in record.read_text().splitlines() if line]

    def clear():
        if record.exists():
            record.unlink()

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("TOOLSTUB_LOG", str(record))
    monkeypatch.setenv("TOOLSTUB_OUT", str(out_dir))
    monkeypatch.setenv("TOOLSTUB_STATE", str(state_dir))
    return SimpleNamespace(install=install, say=say, rc=rc, calls=calls,
                           clear=clear, bin_dir=bin_dir, ram=str(ram_dir),
                           tmp_path=tmp_path)


class TestConstants:
    def test_the_queue_runs_two_at_a_time(self):
        assert whisper.WHISPER_JOBS == 2

    def test_the_gpu_table_best_first(self):
        assert whisper.WHISPER_GPU_MODELS == (
            ("large-v3", 5500, "large-v3"),
            ("large-v3-turbo", 4000, "large-v3-turbo"),
            ("distil-large-v3.5", 3000, "medium"),
            ("medium.en", 3000, "medium"),
            ("small.en", 1500, "small"),
            ("base.en", 1000, "base"),
        )


class TestMultilingual:
    @pytest.mark.parametrize("model,expected", [
        ("large-v3", True),
        ("large-v3-turbo", True),
        ("base", True),
        ("medium", True),
        ("medium.en", False),
        ("large-v3.en", False),
        ("distil-large-v3.5", False),
        ("en", True),
        ("x.en.y", True),
        ("distil-", False),
        ("my-distil-x", True),
    ])
    def test_each_name(self, model, expected):
        assert whisper.whisper_is_multilingual(model) is expected


class TestWorks:
    def test_a_combo_both_tools_accept(self, w):
        w.install("ffmpeg")
        w.install("pipx")
        w.rc("ffmpeg", "0")
        w.rc("pipx", "0")
        status = whisper.whisper_works("cuda", "float16", "large-v3", w.ram, "8")
        assert status == 0
        assert w.calls() == [_ffmpeg_call(w.ram),
                             _pipx_call(w.ram, "large-v3", "cuda", "float16", "8")]

    def test_the_combo_is_passed_through(self, w):
        w.install("ffmpeg")
        w.install("pipx")
        w.rc("ffmpeg", "0")
        w.rc("pipx", "0")
        status = whisper.whisper_works("cpu", "int8", "base.en", w.ram, "32")
        assert status == 0
        assert w.calls() == [_ffmpeg_call(w.ram),
                             _pipx_call(w.ram, "base.en", "cpu", "int8", "32")]

    def test_when_the_audio_cannot_be_made_whisper_is_never_asked(self, w):
        w.install("ffmpeg")
        w.install("pipx")
        w.rc("ffmpeg", "7")
        w.rc("pipx", "0")
        status = whisper.whisper_works("cuda", "float16", "large-v3", w.ram, "8")
        assert status == 1
        assert w.calls() == [_ffmpeg_call(w.ram)]

    def test_and_so_when_the_transcription_fails(self, w):
        w.install("ffmpeg")
        w.install("pipx")
        w.rc("ffmpeg", "0")
        w.rc("pipx", "7")
        status = whisper.whisper_works("cuda", "float16", "large-v3", w.ram, "8")
        assert status == 7
        assert w.calls() == [_ffmpeg_call(w.ram),
                             _pipx_call(w.ram, "large-v3", "cuda", "float16", "8")]

    def test_a_missing_ffmpeg_is_a_failed_probe_not_a_crash(self, w):
        w.install("pipx")
        status = whisper.whisper_works("cuda", "float16", "large-v3", w.ram, "8")
        assert status == 1
        assert w.calls() == []

    def test_a_missing_pipx_answers_the_shells_own_127(self, w):
        w.install("ffmpeg")
        w.rc("ffmpeg", "0")
        status = whisper.whisper_works("cuda", "float16", "large-v3", w.ram, "8")
        assert status == 127
        assert w.calls() == [_ffmpeg_call(w.ram)]


class TestSettlement:
    def _run(self, w, logs, cores, nvidia=None, ffmpeg=None, pipx=None,
             install=()):
        for tool in install:
            w.install(tool)
        if nvidia is not None:
            w.say("nvidia-smi", nvidia)
        if ffmpeg is not None:
            w.rc("ffmpeg", ffmpeg)
        if pipx is not None:
            w.rc("pipx", pipx)
        answer = whisper.init_whisper_model(cores, w.ram, logs.append)
        return answer, w.calls()

    def test_no_nvidia_smii_at_all(self, w):
        logs = []
        answer, calls = self._run(w, logs, "8")
        assert calls == []
        assert answer == {"device": "cpu", "computeType": "int8",
                          "model": "base.en", "modelMulti": "base",
                          "threads": "4"}
        assert logs == [
            "Transcribing on the CPU (int8, 4 threads) with base.en, 2 at a time",
            "Non-English work (detection, foreign transcripts, translations) "
            "runs on base",
        ]

    def test_and_so_does_an_nvidia_smii_with_no_gpu(self, w):
        logs = []
        answer, calls = self._run(w, logs, "8", nvidia="0\n", install=("nvidia-smi",))
        assert calls == [_nvidia_listing()]
        assert answer["device"] == "cpu"

    def test_a_card_that_holds_large_v3(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("11000"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0 0 0 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer == {"device": "cuda", "computeType": "float16",
                          "model": "large-v3", "modelMulti": "large-v3",
                          "threads": "4"}
        assert logs == [
            "GPU found with 11000 MiB free, looking for the best whisper model "
            "it can run ...",
            "Transcribing on the GPU (cuda, float16) with large-v3, 2 at a time",
        ]
        assert calls == [_nvidia_listing(), _nvidia_query(),
                         _ffmpeg_call(w.ram),
                         _pipx_call(w.ram, "large-v3", "cuda", "float16", "4")]

    def test_but_one_mib_less_cannot_hold_it(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("10999"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0 0 0 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "large-v3-turbo"
        assert answer["modelMulti"] == "large-v3-turbo"
        assert calls == [_nvidia_listing(), _nvidia_query(),
                         _ffmpeg_call(w.ram),
                         _pipx_call(w.ram, "large-v3-turbo", "cuda", "float16", "4")]

    def test_the_budget_is_doubled_inclusively(self, w):
        logs = []
        answer, _ = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("8000"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0 0 0 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        # 8000 holds exactly 2 x 4000 (turbo) but not 2 x 5500 (large-v3).
        assert answer["model"] == "large-v3-turbo"

    def test_an_english_only_winner_gets_its_counterpart_probed(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("7999"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "distil-large-v3.5"
        assert answer["modelMulti"] == "medium"
        assert "Non-English work (detection, foreign transcripts, translations) " \
            "runs on medium" in logs
        assert calls == [
            _nvidia_listing(), _nvidia_query(),
            _ffmpeg_call(w.ram),
            _pipx_call(w.ram, "distil-large-v3.5", "cuda", "float16", "4"),
            _ffmpeg_call(w.ram),
            _pipx_call(w.ram, "medium", "cuda", "float16", "4"),
        ]

    def test_the_smaller_english_only_rows_settle_the_same_way(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("2000"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "base.en"
        assert answer["modelMulti"] == "base"
        assert calls[-1] == _pipx_call(w.ram, "base", "cuda", "float16", "4")

    def test_a_card_that_holds_nothing_falls_back_with_a_warning(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("1999"),
            install=("nvidia-smi",))
        assert answer["device"] == "cpu"
        assert "WARNING: the GPU cannot run whisper at all (missing CUDA " \
            "libraries?), falling back to the CPU" in logs
        assert calls == [_nvidia_listing(), _nvidia_query()]

    def test_a_memory_query_that_prints_nothing_is_zero(self, w):
        logs = []
        answer, _ = self._run(
            w, logs, "8", nvidia=_nvidia_stdout(""),
            install=("nvidia-smi",))
        assert answer["device"] == "cpu"
        assert "GPU found with 0 MiB free" in logs[0]

    def test_a_figure_the_arithmetic_cannot_read_is_zero_too(self, w):
        logs = []
        answer, _ = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("N/A"),
            install=("nvidia-smi",))
        assert answer["device"] == "cpu"
        assert "GPU found with N/A MiB free" in logs[0]

    def test_a_failing_probe_moves_on_to_the_next_smaller(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "32", nvidia=_nvidia_stdout("12000"),
            ffmpeg="0 0 0 0 0 0", pipx="1 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "large-v3-turbo"
        assert "WARNING: the GPU cannot run whisper on large-v3, trying a " \
            "smaller model" in logs
        assert calls == [
            _nvidia_listing(), _nvidia_query(),
            _ffmpeg_call(w.ram),
            _pipx_call(w.ram, "large-v3", "cuda", "float16", "4"),
            _ffmpeg_call(w.ram),
            _pipx_call(w.ram, "large-v3-turbo", "cuda", "float16", "4"),
        ]

    def test_and_so_does_the_probe_audio_failing(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("12000"),
            ffmpeg="7 0 0 0 0 0", pipx="0 0 0 0 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "large-v3-turbo"
        # large-v3: ffmpeg fails (7), so pipx is never asked for it.
        assert calls == [
            _nvidia_listing(), _nvidia_query(),
            _ffmpeg_call(w.ram),
            _ffmpeg_call(w.ram),
            _pipx_call(w.ram, "large-v3-turbo", "cuda", "float16", "4"),
        ]

    def test_a_counterpart_the_gpu_cannot_run_falls_back_to_base(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("6000"),
            ffmpeg="0 0 0 0 0 0", pipx="0 7",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "distil-large-v3.5"
        assert answer["modelMulti"] == "base"
        assert "WARNING: the GPU cannot run whisper on medium, falling back " \
            "to base for the non-English work" in logs
        assert calls[-1] == _pipx_call(w.ram, "medium", "cuda", "float16", "4")


class TestThreadCap:
    """whisper's CPU throughput is memory-bandwidth bound: a run does not get
    faster with more intra-threads and in practice runs slower with them, so the
    count is capped at 4 - whisper's own default - however many cores there are.
    """

    @pytest.mark.parametrize("cores,expected", [
        ("16", "4"), ("32", "4"), ("4", "4"), ("2", "2"), ("1", "1"),
    ])
    def test_the_cap(self, w, cores, expected):
        answer = whisper.init_whisper_model(cores, w.ram, lambda _: None)
        assert answer["threads"] == expected

    @pytest.mark.parametrize("cores", ["", "x", None])
    def test_a_count_that_is_not_a_number_reads_as_none(self, w, cores):
        """The shell caps inside `$(( ))`, which reads a word it cannot parse
        as 0 rather than failing the run."""
        answer = whisper.init_whisper_model(cores, w.ram, lambda _: None)
        assert answer["threads"] == "0"
