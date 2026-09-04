"""The device, compute type and models this host can actually transcribe with.

The settlement is functional rather than a guess from a driver version: a
broken or absent CUDA install only announces itself when a model is really
placed on the device, so ``init_whisper_model`` probes its way down the model
table and trusts only what answers. The probe - half a second of silence made
with ``ffmpeg`` and transcribed with ``pipx run whisper-ctranslate2`` - is the
one part that touches a tool, and a test drives it through the shared tool stub,
where the case names which probes fail.
"""

import os
import subprocess

__all__ = [
    "WHISPER_JOBS",
    "WHISPER_GPU_MODELS",
    "whisper_is_multilingual",
    "whisper_works",
    "init_whisper_model",
]

# Transcription parallelism: two runs at a time, on the CPU as well as on the
# GPU. It also caps how much VRAM a model may claim, so it is what the budget
# below is multiplied by.
WHISPER_JOBS = 2

# The GPU model candidates, best transcript first, as
# (model, VRAM per concurrent run in MiB, multilingual counterpart).
#
# The third field is the model the non-English work runs on when the first is
# English-only: the same size class, so it fits the same budget. For a row that
# is multilingual already it repeats the first field and nothing extra is ever
# downloaded or probed.
WHISPER_GPU_MODELS = (
    ("large-v3", 5500, "large-v3"),
    ("large-v3-turbo", 4000, "large-v3-turbo"),
    ("distil-large-v3.5", 3000, "medium"),
    ("medium.en", 3000, "medium"),
    ("small.en", 1500, "small"),
    ("base.en", 1000, "base"),
)


def whisper_is_multilingual(model: str) -> bool:
    """True for a model that can handle more than English.

    whisper-ctranslate2 silently rewrites ``--language`` to "en" for every
    model whose name ends in ``.en``, and the ``distil-`` family is
    English-only as well without saying so in its name.
    """
    return not (model.endswith(".en") or model.startswith("distil-"))


def whisper_works(device: str, compute_type: str, model: str,
                  ram_root: str, threads: str) -> int:
    """The functional probe of one device/compute/model combo.

    Makes half a second of silence in ``ram_root`` with ffmpeg, then asks
    whisper-ctranslate2 to transcribe it with exactly the arguments the bash
    does. Returns the probe's own exit status: 0 when it transcribed, 1 when
    the probe audio could not be made (whisper is never asked), the
    transcription tool's own status otherwise.
    """
    probe = os.path.join(ram_root, "whisperProbe.wav")
    try:
        made = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-f", "lavfi",
             "-i", "anullsrc=r=16000:cl=mono", "-t", "0.5", probe],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError:
        # An ffmpeg the host does not have is a failed probe, the bash's
        # ``ffmpeg ... || return 1`` on a 127.
        return 1
    if made.returncode != 0:
        return 1
    try:
        ran = subprocess.run(
            ["pipx", "run", "whisper-ctranslate2", probe, "--output_dir",
             ram_root, "--model", model, "--language", "en",
             "--output_format", "srt", "--device", device,
             "--compute_type", compute_type, "--threads", threads],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        # The bash's last command is the pipx itself, so an absent pipx answers
        # the shell's own 127, which the function passes straight back.
        return 127
    return ran.returncode


def _nvidia_smi(args) -> str:
    """nvidia-smi's stdout for these arguments, or ``""`` when the tool is
    absent - the bash's ``2>/dev/null`` on a command that cannot run."""
    try:
        proc = subprocess.run(["nvidia-smi"] + list(args),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    return proc.stdout.decode("utf-8", "replace")


def _fits(free_vram: str, model_vram: int) -> bool:
    """The bash's ``((freeVram >= modelVram * whisperJobs))``.

    A numeric figure is compared; anything the arithmetic cannot read - a
    query that printed a word instead of a number - is a failed comparison,
    the way bash's arithmetic reports it, and the row is skipped.
    """
    try:
        return int(free_vram) >= model_vram * WHISPER_JOBS
    except ValueError:
        return False


def init_whisper_model(cores: str, ram_root: str, log) -> dict:
    """Settle the transcription device, compute type and models for this host.

    ``cores`` is the CPU core count whisper's thread count is capped against,
    ``ram_root`` the scratch the probe writes its silence into, ``log`` the
    caller's log -
    a one-argument callable, the way the bash expects it. The settled values
    come back as a dict the way the bash's exports leave them: ``device``,
    ``computeType``, ``model`` (the best overall), ``modelMulti`` (the best
    MULTILINGUAL one - English-only models cannot do detection or translation)
    and ``threads``.
    """
    # whisper's thread count: the core count capped at 4, where whisper's own
    # default proved fastest on the CPU - int8 transcription is
    # memory-bandwidth bound, so a run does not get faster with more
    # intra-threads and in practice runs slower with them. The shell caps in
    # $(( )), which reads a word it cannot parse as 0.
    try:
        count = int(cores)
    except (TypeError, ValueError):
        count = 0
    threads = str(count if count < 4 else 4)
    device = "cpu"
    compute_type = "int8"
    model = "base.en"
    model_multi = "base"

    listing = _nvidia_smi(["-L"])
    if any(line.startswith("GPU") for line in listing.splitlines()):
        # First line of the memory query, the way the bash's ``head -n1``
        # reads it, with the bash's ``:-0`` default for a query that printed
        # nothing.
        query = _nvidia_smi(
            ["--query-gpu=memory.free", "--format=csv,noheader,nounits"])
        lines = query.splitlines()
        free_vram = lines[0] if lines else ""
        free_vram = free_vram if free_vram else "0"
        log("GPU found with {} MiB free, looking for the best whisper model "
            "it can run ...".format(free_vram))
        for candidate, model_vram, candidate_multi in WHISPER_GPU_MODELS:
            if not _fits(free_vram, model_vram):
                continue
            if whisper_works("cuda", "float16", candidate, ram_root, threads) == 0:
                device = "cuda"
                compute_type = "float16"
                model = candidate
                # The multilingual counterpart of the row that won, probed too
                # unless the winner is multilingual itself (then there is
                # nothing to pick).
                if whisper_is_multilingual(model):
                    model_multi = model
                elif whisper_works("cuda", "float16", candidate_multi,
                                    ram_root, threads) == 0:
                    model_multi = candidate_multi
                else:
                    # base is tiny and runs wherever the probe above just
                    # succeeded.
                    log("WARNING: the GPU cannot run whisper on {}, falling "
                        "back to base for the non-English work".format(
                            candidate_multi))
                    model_multi = "base"
                break
            log("WARNING: the GPU cannot run whisper on {}, trying a smaller "
                "model".format(candidate))
        if device != "cuda":
            log("WARNING: the GPU cannot run whisper at all (missing CUDA "
                "libraries?), falling back to the CPU")

    if device == "cuda":
        log("Transcribing on the GPU (cuda, float16) with {}, {} at a time"
            .format(model, WHISPER_JOBS))
    else:
        log("Transcribing on the CPU (int8, {} threads) with {}, {} at a time"
            .format(threads, model, WHISPER_JOBS))
    if model_multi != model:
        log("Non-English work (detection, foreign transcripts, translations) "
            "runs on {}".format(model_multi))

    return {
        "device": device,
        "computeType": compute_type,
        "model": model,
        "modelMulti": model_multi,
        "threads": threads,
    }