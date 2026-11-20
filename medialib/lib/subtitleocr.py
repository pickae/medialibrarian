"""A film's picture subtitles read into text, by Subtitle Edit's ``seconv``.

A Blu-ray's subtitles (PGS) and a DVD's (VobSub) are pictures of the lines,
which a player can show but nothing can search, sync or restyle. seconv
(https://subtitleedit.github.io/subtitleedit/reference/command-line.html)
reads them with an OCR engine and writes SubRip. Its cue times are the
picture subtitle's own, so what it writes is in step with the film it came
out of and needs no alignment.

The tracks are taken out of the film with mkvextract first, every one wanted
in one pass: seconv handed the film itself holds the whole of it in memory.

A film may carry its subtitles in any language, so an engine that reads them
all is preferred wherever the machine can run it at speed. Unless the
``subtitleOcrEngines`` setting names the engines and their order, they are
tried, each where it is ready and the next one where it fails:

1. ``vision``, a vision model from the roster's ``subtitle-ocr`` task
   (:mod:`medialib.lib.llmroster`) - any language. Each model in the ladder
   is tried through :meth:`medialib.lib.llmclient.Llm.serving`, which handles
   fallback servers, starting a server on demand where the GPU has room, and
   stopping it again once idle. seconv only takes the lines' pictures out and
   their times; the pictures are batched up to :data:`VISION_GROUP` per
   request (scaled to the model's context window, :func:`group_size`) and
   held to the model's ``jobs`` concurrency;
2. ``paddle`` (PP-OCR, a hundred-odd languages) on the GPU, where its install
   has CUDA and the card has :data:`PADDLE_GPU_MIB` free - asked afresh for
   each subtitle, since another program may hold the card;
3. ``tesseract`` on the CPU, fast and good, but only in the languages it has
   data installed for;
4. ``paddle`` on the CPU, with the GPU hidden from it: any language, but about
   a second a line.

A GPU too full for paddle after all is one more failure the next engine
answers, so the run never stops over it.

What the vision model was asked is counted into a :class:`Usage`: its
requests, the tokens it read and wrote where the server reports them, and the
wall-clock it was at work - from a subtitle's first request to its last, with
time two subtitles were read at once counted once, so the tokens a second are
the model's real pace however many requests ran side by side.

It is optional: without seconv a film keeps its picture subtitles as they are.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor

from medialib.lib import (
    enums,
    llmclient,
    llmroster,
    modelmeter,
    modelserver,
    subtitletracks,
    toolcapture,
    tooldeps,
)

__all__ = ["TOOL", "ENGINES", "VISION_GROUP", "Usage", "available", "engines",
           "extract", "group_size", "recognise"]

TOOL = "seconv"

# Every engine this step knows: ``vision`` is asked here, the rest by seconv.
ENGINES = ("vision", "paddle", "tesseract")

# How many of a subtitle's pictures go in one vision request at a 4,096-token
# context window. A server admits a request at a time, so a track goes as fast
# as it can be put in few requests rather than many small concurrent ones: on a
# French Blu-ray's 1,342 lines with a 35B-A3B model, one picture a request took
# 210 s whatever the concurrency, 4 a request 64 s, 8 a request 40 s and 16 a
# request 20 s. Sixteen pictures and their answers fit a 4,096-token context;
# 32 overflowed it.
VISION_GROUP = 16
_VISION_CONTEXT = 4096
# The most a line's answer may run to, in tokens: a subtitle is two lines.
_VISION_MAX_TOKENS = 200
# Retries for a vision request refused by a busy server.
_VISION_RETRIES = 5
# Upper bound on concurrent batched requests for a hosted API (jobs == 0).
_CLOUD_JOBS = 8
# A picture this many times a one-line picture's height holds two lines, and
# one under _SLIVER pixels tall holds no text worth measuring by.
_TWO_LINES = 1.8
_SLIVER = 20

# What a vision model is asked of one subtitle, and of several. seconv's own
# prompt has it say how many lines it read, which a general model writes into
# the text; and a picture asked about alone that holds no text gets an
# apology instead of nothing, unless it is told.
_VISION_PROMPT = ("This image is one subtitle from a film, in {language}. "
                  "Write out its text exactly as shown, keeping its line "
                  "breaks, and nothing else: no numbering, labels or "
                  "comments. If it holds no text, answer with nothing.")
_VISION_GROUP_PROMPT = ("These {count} images are consecutive subtitles from "
                        "a film, in {language}, each after its number. Write "
                        "out the text of each "
                        "exactly as shown, keeping its line breaks as \\n. "
                        "Answer with a JSON array of exactly {count} strings, "
                        "one per image, in order, and nothing else; an image "
                        "that holds no text is an empty string.")

# The free VRAM, in MiB, paddle is tried on the GPU with: its detection and
# recognition models take about 1.5 GiB between them.
PADDLE_GPU_MIB = 2048

# What an engine needs beside seconv: the program seconv starts for it.
_ENGINE_TOOL = {"paddle": "paddleocr", "tesseract": "tesseract"}

# The extension mkvextract writes each picture codec to. A DVD's is two files,
# .idx and .sub, named for the .sub and read by the .idx. A broadcast's
# (S_DVBSUB) has no file mkvextract can write, and is not read.
_EXTRACTED = {"s_hdmv/pgs": ".sup", "s_vobsub": ".sub"}
_READ_FROM = {".sup": ".sup", ".sub": ".idx"}

# The languages tesseract has data for, and whether paddle's install has CUDA,
# asked once a run.
_tesseract_languages: set | None = None
_paddle_cuda: bool | None = None
# The engines already said to be unusable, so a run says it once each.
_said: set = set()


class Usage:
    """What the vision model was asked, on one subtitle or over a run: the
    requests it answered and the tokens their replies report
    (:class:`modelmeter.Tally`), and the stretches of time it was at work
    (:class:`modelmeter.Spans`). Safe to count into from many threads."""

    def __init__(self) -> None:
        self.tally = modelmeter.Tally()
        self.spans = modelmeter.Spans()

    @property
    def requests(self) -> int:
        return self.tally.requests

    @property
    def seconds(self) -> float:
        """The wall-clock the model was at work, each moment once."""
        return self.spans.seconds

    def merge(self, other: Usage) -> None:
        self.tally.merge(other.tally)
        self.spans.merge(other.spans)

    def line(self) -> str:
        """The usage said the way generate-chapters says a pass
        (:func:`modelmeter.rate_line`); "" when the model was never asked."""
        if not self.requests:
            return ""
        return modelmeter.rate_line("ocr", self.seconds, self.tally)


def group_size(context_tokens: int) -> int:
    """How many subtitle pictures fit in one request for a model with a
    ``context_tokens``-token window: :data:`VISION_GROUP` (16) at 4,096
    tokens or above, scaled down below that so a smaller window never
    overflows."""
    if context_tokens <= 0:
        return 1
    return max(1, min(VISION_GROUP,
                      context_tokens * VISION_GROUP // _VISION_CONTEXT))


def _workers(llm: llmclient.Llm, groups: int) -> int:
    """How many batched requests run in parallel: held to ``llm.jobs`` (the
    roster's concurrency for that model) since batching carries the speed."""
    limit = llm.jobs if llm.jobs > 0 else _CLOUD_JOBS
    return max(1, min(groups, limit))


def available() -> bool:
    return tooldeps.tool_present(TOOL)


def engines(environ=None) -> tuple:
    """The engines to try, in order, as (engine, device): device is "cpu"
    for an engine to keep off the GPU, "" for one left to choose.

    The ``subtitleOcrEngines`` setting's names, the ones this step knows, in
    its order; or, without it, the order the module says. ``vision`` only
    where the roster has a model for :data:`llmroster.SUBTITLE_OCR`."""
    environ = os.environ if environ is None else environ
    has_vision = bool(llmroster.optional(llmroster.SUBTITLE_OCR, environ)[0])
    named = (environ.get("subtitleOcrEngines") or "").replace(",", " ").split()
    if named:
        chosen = [enums.shell_lower(name) for name in named]
        return tuple((name, "") for name in dict.fromkeys(chosen)
                     if name in ENGINES and (name != "vision" or has_vision))
    order = [("vision", "")] if has_vision else []
    if paddle_on_gpu():
        order.append(("paddle", ""))
    order.append(("tesseract", ""))
    order.append(("paddle", "cpu"))
    return tuple(order)


def paddle_on_gpu() -> bool:
    """Whether paddle can read on the GPU now: its install has CUDA, and the
    card has :data:`PADDLE_GPU_MIB` free."""
    return _paddle_has_cuda() and modelserver.free_vram() >= PADDLE_GPU_MIB


def _paddle_has_cuda() -> bool:
    """Whether the Python paddleocr runs in has a paddle built with CUDA:
    asked of the interpreter its script names."""
    global _paddle_cuda
    if _paddle_cuda is None:
        _paddle_cuda = False
        script = shutil.which("paddleocr")
        try:
            with open(script or "", "rb") as handle:
                first = handle.readline().decode("utf-8", "replace")
            if first.startswith("#!"):
                done = subprocess.run(
                    [first[2:].strip(), "-c",
                     "import paddle; "
                     "print(paddle.device.is_compiled_with_cuda())"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, timeout=120)
                _paddle_cuda = done.stdout.strip().endswith("True")
        except (OSError, subprocess.TimeoutExpired):
            pass
    return _paddle_cuda


def readable(codec: str) -> bool:
    """Whether a picture subtitle of this mkvmerge codec can be read."""
    return enums.shell_lower(codec) in _EXTRACTED


def extract(film: str, tracks: Iterable, scratch: str) -> dict:
    """The picture subtitles taken out of ``film`` into ``scratch``, in one
    pass: {track id: the file seconv reads}. ``tracks`` are (id, codec)
    pairs. A track that did not come out is left out."""
    wanted = [(str(number), _EXTRACTED[enums.shell_lower(codec)])
              for number, codec in tracks if readable(codec)]
    if not wanted:
        return {}
    targets = ["%s:%s" % (number, os.path.join(scratch, "track%s%s"
                                               % (number, extension)))
               for number, extension in wanted]
    try:
        toolcapture.watched(
            ["mkvextract", os.path.abspath(film), "tracks", *targets],
            announce=False, skipped="its picture subtitles were not read",
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except OSError:
        return {}
    found = {}
    for number, extension in wanted:
        path = os.path.join(scratch, "track%s%s" % (number,
                                                    _READ_FROM[extension]))
        if os.path.isfile(path) and os.path.getsize(path):
            found[number] = path
    return found


def _ocr_language(engine: str, row) -> str:
    """The language as each engine is told it: tesseract by its data file's
    ISO 639-2 name, paddle by the two-letter code, a vision model by name."""
    if engine == "tesseract":
        return row.code3
    return row.code2


def _ready(engine: str, row, log: Callable[[str], None]) -> bool:
    """Whether the engine can read this language here; an engine that
    cannot is said once a run."""
    tool = _ENGINE_TOOL.get(engine)
    if tool and not tooldeps.tool_present(tool):
        _say_once(engine, log, "%s not installed, %s OCR is not tried"
                  % (tool, engine))
        return False
    if engine == "tesseract" and row.code3 not in _tesseract_has():
        _say_once("tesseract " + row.code3, log,
                  "tesseract has no %s data (%s), it is not tried for %s"
                  % (row.sub_word, "apt install tesseract-ocr-" + row.code3,
                     row.sub_word))
        return False
    return True


def _say_once(key: str, log: Callable[[str], None], line: str) -> None:
    if key not in _said:
        _said.add(key)
        log("  WARNING: " + line)


def _tesseract_has() -> set:
    global _tesseract_languages
    if _tesseract_languages is None:
        try:
            done = subprocess.run(["tesseract", "--list-langs"],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  errors="replace")
            _tesseract_languages = {line.strip()
                                    for line in done.stdout.splitlines()[1:]}
        except OSError:
            _tesseract_languages = set()
    return _tesseract_languages


def _command(image: str, engine: str, row, out: str) -> list:
    return [TOOL, image, "subrip", "--ocr-engine:" + engine,
            "--ocr-language:" + _ocr_language(engine, row),
            "--output-folder:" + out, "--overwrite", "--json",
            "--fix-common-errors"]


def _pictures(image: str, out: str) -> list:
    """Each line of a picture subtitle as (start, end, png, height), in
    order: the pictures and their heights from seconv's BDN export, the times
    from its time-codes-only SubRip, which keeps their milliseconds where BDN
    rounds to frames. [] when either cannot be had, or they do not pair up."""
    times = os.path.join(out, "times")
    pictures = os.path.join(out, "pictures")
    for folder, arguments in ((times, ["subrip", "--time-codes-only"]),
                              (pictures, ["BDNXml"])):
        os.makedirs(folder)
        done = toolcapture.watched(
            [TOOL, image, *arguments, "--output-folder:" + folder,
             "--overwrite", "--json"], announce=False,
            skipped="the next OCR engine is tried",
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        if done.returncode != 0:
            return []
    spans = _spans(next((os.path.join(times, name)
                         for name in sorted(os.listdir(times))
                         if name.endswith(".srt")), ""))
    index = next((os.path.join(root, name)
                  for root, _dirs, names in os.walk(pictures)
                  for name in names if name == "index.xml"), "")
    try:
        with open(index, encoding="utf-8-sig", errors="replace") as handle:
            graphics = re.findall(r"<Graphic([^>]*)>([^<]+)</Graphic>",
                                  handle.read())
    except OSError:
        return []
    if not graphics or len(graphics) != len(spans):
        return []
    folder = os.path.dirname(index)
    lines = []
    for (start, end), (attributes, name) in zip(spans, graphics, strict=True):
        height = re.search(r'Height="(\d+)"', attributes)
        lines.append((start, end, os.path.join(folder, name.strip()),
                      int(height.group(1)) if height else 0))
    return lines


def _spans(srt: str) -> list:
    """The (start, end) time lines of a SubRip file, as written."""
    try:
        with open(srt, encoding="utf-8-sig", errors="replace") as handle:
            return [tuple(part.strip() for part in line.split("-->"))
                    for line in handle if "-->" in line]
    except OSError:
        return []


def _tidy(text: str) -> str:
    """One subtitle's text: its lines trimmed, the empty ones dropped."""
    return "\n".join(line.strip() for line in (text or "").splitlines()
                     if line.strip())


def _short_of_lines(text: str, height: int, line_height: int) -> bool:
    """Whether an answer is one line where its picture is tall enough for
    two: a model reading several pictures at once now and then leaves out a
    line. Two lines set far apart - a dialogue's - are taller still, so a
    picture is not asked for more than two."""
    if not line_height or not text:
        return False
    return "\n" not in text and height >= _TWO_LINES * line_height


def _line_height(lines: list) -> int:
    """How tall a one-line picture of the subtitle is: the lower quartile of
    the pictures' heights, leaving out the slivers that hold no text."""
    heights = sorted(line[3] for line in lines if line[3] >= _SLIVER)
    return heights[len(heights) // 4] if heights else 0


def _vision_options(llm: llmclient.Llm) -> dict | None:
    """Per-request options for subtitle OCR on the openai protocol:
    deterministic and without thinking on local servers, with any option
    configured on ``llm`` taking precedence."""
    if llm.api != "openai":
        return None
    options: dict = {}
    if llm.local:
        options = {
            "temperature": 0,
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
        }
    options.update(llm.options)
    return options or None


def _read_group(llm: llmclient.Llm, row, group: list, line_height: int,
                options: dict | None, usage: Usage) -> list[str | None]:
    """The text of each picture of ``group``, in order - None for one the
    model would not read. Asked in one numbered request; a group answered
    with no usable array, and a picture whose answer is short of its lines,
    are asked again a picture at a time."""
    # A sliver holds no text; sent with the rest it would only make the model
    # lose count of which answer is which.
    texts: list[str | None] = ["" if line[3] < _SLIVER else None
                               for line in group]
    asked = [at for at, text in enumerate(texts) if text is None]
    if not asked:
        return texts
    batch_prompt = _VISION_GROUP_PROMPT.format(count=len(asked),
                                               language=row.sub_word)
    alone_prompt = _VISION_PROMPT.format(language=row.sub_word)
    found = llm.ask_pictures(
        "", batch_prompt, alone_prompt, [group[at][2] for at in asked],
        usage.tally, options=options, max_tokens_each=_VISION_MAX_TOKENS,
        allow_empty=True, retries=_VISION_RETRIES,
        reask=lambda index, text: _short_of_lines(
            _tidy(text.replace("\\n", "\n")),
            group[asked[index]][3], line_height),
    )
    for at, text in zip(asked, found, strict=True):
        if text is not None:
            texts[at] = _tidy(text.replace("\\n", "\n"))
    return texts


def _read_by_vision(image: str, row, out: str, environ,
                    log: Callable[[str], None],
                    usage: Usage | None = None,
                    models: Sequence[llmclient.Llm] | None = None) -> str:
    """The picture subtitle read into a SubRip in ``out`` by the first vision
    model of the roster's ``subtitle-ocr`` ladder that can be had now; ""
    when none could read every line, so the next engine reads the whole
    subtitle. A picture that holds no text is no line."""
    if models is None:
        models, why = llmroster.optional(llmroster.SUBTITLE_OCR, environ)
        if not models:
            if why:
                _say_once("vision:" + why, log,
                          "%s, the vision model is not asked" % why)
            return ""
    lines: list | None = None
    tried_any = False
    for llm in models:
        try:
            with llm.serving(log, announce=False) as served:
                if not served.sees_images():
                    continue
                if lines is None:
                    lines = _pictures(image, out)
                    if not lines:
                        return ""
                tried_any = True
                written = _read_with(served, lines, row, out, log, usage)
                if written:
                    return written
        except llmclient.LlmError as error:
            log("  vision OCR with %s could not start: %s"
                % (llm.name, error))
    if not tried_any:
        log("  no vision OCR server is answering, nor can one be started "
            "now, so the next engine reads")
    return ""


def _read_with(llm: llmclient.Llm, lines: list, row, out: str,
               log: Callable[[str], None],
               usage: Usage | None = None) -> str:
    """The picture subtitle read by ``llm``: see :func:`_read_by_vision`.
    Its requests are timed into ``usage`` as one stretch, from the first
    one's start to the last one's end, however many ran at once."""
    if not lines:
        return ""
    line_height = _line_height(lines)
    per_request = group_size(llm.context_tokens)
    groups = [lines[at:at + per_request]
              for at in range(0, len(lines), per_request)]
    options = _vision_options(llm)
    usage = Usage() if usage is None else usage
    with usage.spans.timing(), \
            ThreadPoolExecutor(max_workers=_workers(llm, len(groups))) as pool:
        texts = [text for found in pool.map(
                     lambda group: _read_group(llm, row, group, line_height,
                                               options, usage),
                     groups)
                 for text in found]
    missing = sum(1 for text in texts if text is None)
    if missing:
        where = llm.url or llm.name
        log("  vision OCR at %s could not read %d of the %d lines of the %s "
            "subtitle" % (where, missing, len(lines), row.sub_word))
        return ""
    srt = os.path.join(out, "vision.srt")
    with open(srt, "w", encoding="utf-8") as handle:
        number = 0
        for (start, end, _png, _height), text in zip(lines, texts,
                                                      strict=True):
            if text:
                number += 1
                handle.write("%d\n%s --> %s\n%s\n\n" % (number, start, end,
                                                         text))
    toolcapture.watched(
        [TOOL, srt, "subrip", "--fix-common-errors", "--overwrite",
         "--output-folder:" + out, "--json"], announce=False,
        skipped="its common errors were left as read",
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    return srt


def _written(stdout: str, out: str) -> str:
    """The SubRip seconv says it wrote, or the one it left in ``out``."""
    try:
        for entry in json.loads(stdout or "{}").get("files") or []:
            if entry.get("success") and entry.get("output") \
                    and os.path.isfile(entry["output"]):
                return entry["output"]
    except (ValueError, AttributeError):
        pass
    for name in sorted(os.listdir(out)):
        if name.endswith(".srt"):
            return os.path.join(out, name)
    return ""


def recognise(image: str, row, srt: str,
              log: Callable[[str], None], environ=None,
              usage: Usage | None = None) -> str:
    """The picture subtitle ``image``, in the language of ``row`` (one of
    :func:`languages.recognised`), read into ``srt`` by the first engine
    that manages it; the engine's name, or "" when none did. An engine that
    exits 0 having written no line is a failure too. What a vision model was
    asked is counted into ``usage``, whether or not it read the subtitle."""
    environ = os.environ if environ is None else environ
    for engine, device in engines(environ):
        if not _ready(engine, row, log):
            continue
        out = tempfile.mkdtemp(dir=os.path.dirname(image))
        try:
            try:
                if engine == "vision":
                    written = _read_by_vision(image, row, out, environ, log,
                                              usage)
                else:
                    done = toolcapture.watched(
                        _command(image, engine, row, out),
                        announce=False,
                        skipped="the next OCR engine is tried",
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        text=True, errors="replace",
                        env=dict(os.environ, CUDA_VISIBLE_DEVICES="")
                        if device == "cpu" else None)
                    written = (_written(done.stdout, out)
                               if done.returncode == 0 else "")
            except OSError:
                return ""
            if written and subtitletracks.cue_texts(written):
                shutil.move(written, srt)
                return _engine_said(engine, device)
            log("  %s OCR read nothing of the %s subtitle, trying the next "
                "engine" % (_engine_said(engine, device), row.sub_word))
        finally:
            shutil.rmtree(out, ignore_errors=True)
    return ""


def _engine_said(engine: str, device: str) -> str:
    return engine + " (CPU)" if device == "cpu" else engine
