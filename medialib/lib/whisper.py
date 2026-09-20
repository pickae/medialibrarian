"""The device, compute type, models and batch plan this host can transcribe with.

The settlement is functional rather than a guess from a driver version: a
broken or absent CUDA install only announces itself when a model is really
placed on the device, so ``init_whisper_model`` probes its way down the model
table and trusts only what answers. The probe - half a second of silence made
with ``ffmpeg`` and transcribed with ``pipx run whisper-ctranslate2`` - is the
one part that touches a tool, and a test drives it through the shared tool stub,
where the case names which probes fail.

How WIDE the GPU work runs is settled here too, and the answer is not the
worker count it looks like. A run is a process of its own, so a second worker
reloads the whole model: gigabytes of weights for a card that one run already
keeps busy. A BATCH SLOT - a thirty-second window decoded alongside the others
in the same pass - costs a fraction of that and is what actually fills a GPU.
The plan therefore fills one run's slots before it considers a second run.
"""

import os
import subprocess

__all__ = [
    "WHISPER_JOBS",
    "WHISPER_BATCH_SLOTS",
    "WHISPER_BATCH_MIN_SLOTS",
    "WHISPER_VRAM_SHARE",
    "WHISPER_LINE_WIDTH",
    "WHISPER_LINE_COUNT",
    "WHISPER_GPU_MODELS",
    "whisper_is_multilingual",
    "whisper_works",
    "settle_gpu_plan",
    "init_whisper_model",
]

# The most runs at once. Two, on the CPU as well as on the GPU: on a card with
# room for a third it measured 2% and left no headroom for the desktop, which
# is a poor trade for work that answers an out-of-memory with status 0.
WHISPER_JOBS = 2

# The most batch slots one GPU run is given. Sixteen: past it the card stops
# answering faster while the memory goes on rising, so the slots above it are
# spent and not earned.
WHISPER_BATCH_SLOTS = 16

# The fewest slots worth batching for. A run this narrow still transcribes
# several times faster than an unbatched one, so the floor is low; below it the
# card is too small for the batched path at all and the run goes unbatched.
WHISPER_BATCH_MIN_SLOTS = 2

# The percentage of the free VRAM a plan may claim. The rest is the desktop's
# room to grow while the run lasts: a card filled exactly runs out of memory
# the moment a browser opens a tab, and whisper-ctranslate2 answers that with
# a traceback and an exit status of 0 - a transcript silently not made.
WHISPER_VRAM_SHARE = 85

# The subtitle shape the batched path is asked for. Batching decides its own
# segments from the voice detection, and left alone it writes one cue per
# speech run - half a minute of talk in a single unreadable block. Asked for
# word timestamps it can be cut where the words are, and these are the width
# and line count it is cut to.
WHISPER_LINE_WIDTH = 42
WHISPER_LINE_COUNT = 2

# The GPU model candidates, best transcript first, as
# (model, multilingual counterpart, fixed MiB per run, MiB per batch slot).
#
# The second field is the model the non-English work runs on when the first is
# English-only: the same size class, so it fits the same budget. For a row that
# is multilingual already it repeats the first field and nothing extra is ever
# downloaded or probed.
#
# The last two fields are what one run of the model costs, measured on an
# RTX 5090 by running the same audio at two batch sizes and reading the peak
# off nvidia-smi: a fixed part - the weights and the CUDA context, paid once
# per PROCESS - and a per-slot part. The per-slot figure tracks the DECODER
# rather than the encoder, which is why large-v3-turbo and distil-large-v3.5
# cost a third of large-v3 a slot while sharing its encoder: they carry four
# decoder layers where large-v3 carries thirty-two.
#
# Each pair is budgeted on the DEARER of its two models, because a row buys
# room for whichever of them a given track ends up on: distil-large-v3.5 costs
# 131 MiB a slot but hands its non-English work to medium, which costs 203, and
# a plan sized on the first would run the second out of memory.
WHISPER_GPU_MODELS = (
    ("large-v3", "large-v3", 4122, 349),
    ("large-v3-turbo", "large-v3-turbo", 2522, 133),
    ("distil-large-v3.5", "medium", 2468, 203),
    ("medium.en", "medium", 2370, 203),
    ("small.en", "small", 1125, 82),
    ("base.en", "base", 907, 56),
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
    whisper-ctranslate2 to transcribe it. Returns the probe's own exit status:
    0 when it transcribed, 1 when the probe audio could not be made (whisper
    is never asked), the transcription tool's own status otherwise.
    """
    probe = os.path.join(ram_root, "whisperProbe.wav")
    try:
        made = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-f", "lavfi",
             "-i", "anullsrc=r=16000:cl=mono", "-t", "0.5", probe],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError:
        # An ffmpeg the host does not have is a failed probe.
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
        # An absent pipx answers 127, which the function passes straight
        # back.
        return 127
    return ran.returncode


def _nvidia_smi(args) -> str:
    """nvidia-smi's stdout for these arguments, or ``""`` when the tool is
    absent."""
    try:
        proc = subprocess.run(["nvidia-smi"] + list(args),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    return proc.stdout.decode("utf-8", "replace")


def settle_gpu_plan(free_vram: str, fixed: int, per_slot: int) -> tuple:
    """How many runs at once, and how many batch slots each, this card affords.

    ``free_vram`` is nvidia-smi's free figure in MiB, ``fixed`` and
    ``per_slot`` the model's two costs from :data:`WHISPER_GPU_MODELS`. Comes
    back as ``(jobs, slots)``, and as ``(0, 0)`` for a card that cannot hold
    even the narrowest batched run - the caller's signal to try a smaller
    model.

    The slots are filled before a second run is considered, because they are
    the cheaper parallelism by a wide margin: a slot costs a few hundred MiB
    where a run costs the whole model again. A second run is still worth its
    weight once each has a substantial batch of its own - it overlaps one
    run's voice detection and word alignment, which are the CPU's, with the
    other's decoding - so the pair is taken when both can be filled at least
    half way, and passed over when it would leave them narrower than that.

    A figure the arithmetic cannot read - a query that printed a word instead
    of a number - is no room at all.
    """
    try:
        free = int(free_vram)
    except (TypeError, ValueError):
        return (0, 0)
    if per_slot <= 0:
        return (0, 0)
    usable = free * WHISPER_VRAM_SHARE // 100
    worthwhile = max(WHISPER_BATCH_SLOTS // 2, WHISPER_BATCH_MIN_SLOTS)
    for jobs in range(WHISPER_JOBS, 1, -1):
        slots = (usable // jobs - fixed) // per_slot
        if slots >= WHISPER_BATCH_SLOTS:
            return (jobs, WHISPER_BATCH_SLOTS)
        if slots >= worthwhile:
            return (jobs, slots)
    slots = (usable - fixed) // per_slot
    if slots >= WHISPER_BATCH_SLOTS:
        return (1, WHISPER_BATCH_SLOTS)
    if slots >= WHISPER_BATCH_MIN_SLOTS:
        return (1, slots)
    return (0, 0)


def init_whisper_model(cores: str, ram_root: str, log) -> dict:
    """Settle the transcription device, compute type, models and plan.

    ``cores`` is the CPU core count whisper's thread count is capped against,
    ``ram_root`` the scratch the probe writes its silence into, ``log`` the
    caller's log - a one-argument callable. The settled values come back as a
    dict: ``device``, ``computeType``, ``model`` (the best overall),
    ``modelMulti`` (the best MULTILINGUAL one - English-only models cannot do
    detection or translation), ``threads``, ``jobs`` (how many runs the queue
    may have going) and ``batchSlots`` (how wide each run decodes, 0 for the
    unbatched path the CPU takes).
    """
    # whisper's thread count: the core count capped at 4, where whisper's own
    # default proved fastest on the CPU - int8 transcription is
    # memory-bandwidth bound, so a run does not get faster with more
    # intra-threads and in practice runs slower with them. A core count that
    # cannot be parsed reads as 0.
    try:
        count = int(cores)
    except (TypeError, ValueError):
        count = 0
    threads = str(count if count < 4 else 4)
    device = "cpu"
    compute_type = "int8"
    model = "base.en"
    model_multi = "base"
    jobs = WHISPER_JOBS
    slots = 0

    listing = _nvidia_smi(["-L"])
    if any(line.startswith("GPU") for line in listing.splitlines()):
        # First line of the memory query, with a 0 default for a query that
        # printed nothing.
        query = _nvidia_smi(
            ["--query-gpu=memory.free", "--format=csv,noheader,nounits"])
        lines = query.splitlines()
        free_vram = lines[0] if lines else ""
        free_vram = free_vram if free_vram else "0"
        log("GPU found with {} MiB free, looking for the best whisper model "
            "it can run ...".format(free_vram))
        for row in WHISPER_GPU_MODELS:
            candidate, candidate_multi, fixed, per_slot = row
            plan_jobs, plan_slots = settle_gpu_plan(free_vram, fixed, per_slot)
            if not plan_jobs:
                continue
            if whisper_works("cuda", "float16", candidate, ram_root, threads) == 0:
                device = "cuda"
                compute_type = "float16"
                model = candidate
                jobs, slots = plan_jobs, plan_slots
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
        log("Transcribing on the GPU (cuda, float16) with {}, {} at a time, "
            "{} batch slots each".format(model, jobs, slots))
    else:
        log("Transcribing on the CPU (int8, {} threads) with {}, {} at a time"
            .format(threads, model, jobs))
    if model_multi != model:
        log("Non-English work (detection, foreign transcripts, translations) "
            "runs on {}".format(model_multi))

    return {
        "device": device,
        "computeType": compute_type,
        "model": model,
        "modelMulti": model_multi,
        "threads": threads,
        "jobs": jobs,
        "batchSlots": slots,
    }
