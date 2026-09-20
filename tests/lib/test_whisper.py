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
            ("large-v3", "large-v3", 4122, 349),
            ("large-v3-turbo", "large-v3-turbo", 2522, 133),
            ("distil-large-v3.5", "medium", 2468, 203),
            ("medium.en", "medium", 2370, 203),
            ("small.en", "small", 1125, 82),
            ("base.en", "base", 907, 56),
        )

    def test_a_slot_is_never_dearer_than_the_run_it_widens(self):
        """Every row's fixed cost outweighs a slot's by a wide margin, which is
        why the plan fills slots before it starts a second run."""
        for _model, _multi, fixed, per_slot in whisper.WHISPER_GPU_MODELS:
            assert fixed > per_slot * 4


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


class TestPlan:
    """settleGpuPlan: how many runs at once and how many batch slots each, for
    a card of a given size. A slot is the cheap parallelism and a run the dear
    one, so the slots are filled first and a second run only taken when both
    can still be filled half way."""

    LARGE_V3 = (4122, 349)

    @pytest.mark.parametrize("free,expected", [
        # The cards this is sized for, by the free VRAM each leaves a desktop.
        ("30800", (2, 16)),   # 32 GB: two runs, both full
        ("22900", (2, 16)),   # 24 GB: still two
        ("14900", (1, 16)),   # 16 GB: one full run beats two narrow ones
        ("10900", (1, 14)),   # 12 GB: one run, as wide as it fits
        ("7000", (1, 5)),     #  8 GB: narrower still
        ("5671", (1, 2)),     # the narrowest batched run there is
        ("5670", (0, 0)),     # and a MiB below it, nothing
    ])
    def test_the_plan_for_a_card(self, free, expected):
        assert whisper.settle_gpu_plan(free, *self.LARGE_V3) == expected

    def test_a_second_run_is_passed_over_when_it_would_starve_both(self):
        """14900 MiB holds two runs of six slots, and one of sixteen. The
        second run is not worth twelve of the slots it costs."""
        fixed, per_slot = self.LARGE_V3
        usable = 14900 * whisper.WHISPER_VRAM_SHARE // 100
        assert (usable // 2 - fixed) // per_slot == 6
        assert whisper.settle_gpu_plan("14900", fixed, per_slot) == (1, 16)

    def test_a_plan_never_claims_more_than_its_share(self):
        for free in range(1200, 40000, 97):
            for _m, _multi, fixed, per_slot in whisper.WHISPER_GPU_MODELS:
                jobs, slots = whisper.settle_gpu_plan(str(free), fixed,
                                                      per_slot)
                claimed = jobs * (fixed + slots * per_slot)
                assert claimed <= free * whisper.WHISPER_VRAM_SHARE // 100

    @pytest.mark.parametrize("free", ["", "N/A", None])
    def test_a_figure_the_arithmetic_cannot_read_is_no_room(self, free):
        assert whisper.settle_gpu_plan(free, *self.LARGE_V3) == (0, 0)

    def test_a_model_with_no_slot_cost_is_no_room_either(self):
        """A row whose costs were never measured must not divide by zero."""
        assert whisper.settle_gpu_plan("30800", 4122, 0) == (0, 0)


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
                          "threads": "4", "jobs": whisper.WHISPER_JOBS,
                          "batchSlots": 0}
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

    def test_a_card_that_holds_large_v3_twice_over(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("30800"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0 0 0 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer == {"device": "cuda", "computeType": "float16",
                          "model": "large-v3", "modelMulti": "large-v3",
                          "threads": "4", "jobs": 2,
                          "batchSlots": whisper.WHISPER_BATCH_SLOTS}
        assert logs == [
            "GPU found with 30800 MiB free, looking for the best whisper model "
            "it can run ...",
            "Transcribing on the GPU (cuda, float16) with large-v3, 2 at a "
            "time, 16 batch slots each",
        ]
        assert calls == [_nvidia_listing(), _nvidia_query(),
                         _ffmpeg_call(w.ram),
                         _pipx_call(w.ram, "large-v3", "cuda", "float16", "4")]

    def test_a_card_that_holds_it_once_runs_one_wide_batch(self, w):
        """The slots are worth more than a second run, so a card that cannot
        fill two gives all of them to one."""
        logs = []
        answer, _ = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("14900"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0 0 0 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "large-v3"
        assert (answer["jobs"], answer["batchSlots"]) == (1, 16)

    def test_a_card_that_cannot_fill_even_one_narrows_the_batch(self, w):
        logs = []
        answer, _ = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("10900"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0 0 0 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "large-v3"
        assert (answer["jobs"], answer["batchSlots"]) == (1, 14)

    def test_the_narrowest_large_v3_a_card_can_hold(self, w):
        logs = []
        answer, _ = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("5671"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0 0 0 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "large-v3"
        assert (answer["jobs"], answer["batchSlots"]) == (
            1, whisper.WHISPER_BATCH_MIN_SLOTS)

    def test_but_one_mib_less_cannot_hold_it(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("5670"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0 0 0 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "large-v3-turbo"
        assert answer["modelMulti"] == "large-v3-turbo"
        assert calls == [_nvidia_listing(), _nvidia_query(),
                         _ffmpeg_call(w.ram),
                         _pipx_call(w.ram, "large-v3-turbo", "cuda", "float16", "4")]

    def test_an_english_only_winner_gets_its_counterpart_probed(self, w):
        # 4000 MiB rules large-v3 out, and the probe refuses turbo, which
        # leaves the first English-only row to win and have its counterpart
        # asked for as well.
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("4000"),
            ffmpeg="0 0 0 0 0 0", pipx="7 0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "distil-large-v3.5"
        assert answer["modelMulti"] == "medium"
        assert "Non-English work (detection, foreign transcripts, translations) " \
            "runs on medium" in logs
        assert calls == [
            _nvidia_listing(), _nvidia_query(),
            _ffmpeg_call(w.ram),
            _pipx_call(w.ram, "large-v3-turbo", "cuda", "float16", "4"),
            _ffmpeg_call(w.ram),
            _pipx_call(w.ram, "distil-large-v3.5", "cuda", "float16", "4"),
            _ffmpeg_call(w.ram),
            _pipx_call(w.ram, "medium", "cuda", "float16", "4"),
        ]

    def test_the_smaller_english_only_rows_settle_the_same_way(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("1400"),
            ffmpeg="0 0 0 0 0 0", pipx="0 0",
            install=("nvidia-smi", "ffmpeg", "pipx"))
        assert answer["model"] == "base.en"
        assert answer["modelMulti"] == "base"
        assert calls[-1] == _pipx_call(w.ram, "base", "cuda", "float16", "4")

    def test_a_card_that_holds_nothing_falls_back_with_a_warning(self, w):
        logs = []
        answer, calls = self._run(
            w, logs, "8", nvidia=_nvidia_stdout("1198"),
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
            w, logs, "8", nvidia=_nvidia_stdout("4000"),
            ffmpeg="0 0 0 0 0 0", pipx="7 0 7",
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
