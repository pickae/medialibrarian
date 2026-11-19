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

1. ``vision``, a vision model a server holds - any language. The servers in
   ``subtitleOcrUrl`` are tried in its order: one that answers is used; the
   first, not running, is started with ``subtitleOcrServe`` where the GPU has
   room for it, and stopped again once idle (:mod:`medialib.lib.modelserver`);
   and with the card taken, the next one that answers reads instead.
   seconv only takes the lines' pictures out and their times; the pictures
   are sent to the model here, ``subtitleOcrJobs`` at a time, since a server
   that batches reads several nearly as fast as one;
2. ``paddle`` (PP-OCR, a hundred-odd languages) on the GPU, where its install
   has CUDA and the card has :data:`PADDLE_GPU_MIB` free - asked afresh for
   each subtitle, since another program may hold the card;
3. ``tesseract`` on the CPU, fast and good, but only in the languages it has
   data installed for;
4. ``paddle`` on the CPU, with the GPU hidden from it: any language, but about
   a second a line.

A GPU too full for paddle after all is one more failure the next engine
answers, so the run never stops over it.

It is optional: without seconv a film keeps its picture subtitles as they are.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from medialib.lib import enums, modelserver, subtitletracks, toolcapture, tooldeps

__all__ = ["TOOL", "ENGINES", "available", "engines", "extract", "recognise"]

TOOL = "seconv"

# Every engine this step knows: ``vision`` is asked here, the rest by seconv.
ENGINES = ("vision", "paddle", "tesseract")

# How many requests are in flight to the vision model at once, unless
# ``subtitleOcrJobs`` says, and how many of a subtitle's pictures each carries.
# A server admits a request at a time, so a track goes as fast as it can be
# put in few requests: on a French Blu-ray's 1,342 lines with a 35B-A3B model,
# one picture a request took 210 s whatever the concurrency, 4 a request 64 s,
# 8 a request 40 s and 16 a request 20 s. Sixteen pictures and their answers
# fit a 4,096-token context; 32 overflowed it.
VISION_JOBS = 32
VISION_GROUP = 16
# The most a line's answer may run to, in tokens: a subtitle is two lines.
_VISION_MAX_TOKENS = 200
# A picture this many times a one-line picture's height holds two lines, and
# one under _SLIVER pixels tall holds no text worth measuring by.
_TWO_LINES = 1.8
_SLIVER = 20
# How often a request refused by a busy server is asked again, and how long
# to wait first, doubling each time.
_VISION_TRIES = 6
_VISION_BACKOFF = 2.0
# The statuses a server answers a request it has no room for with.
_BUSY = (429, 502, 503, 504)

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
# Where a reasoning model's thinking ends, when its server sends it on with the
# answer.
_THOUGHT_END = "</think>"

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


def available() -> bool:
    return tooldeps.tool_present(TOOL)


def engines(environ=None) -> tuple:
    """The engines to try, in order, as (engine, device): device is "cpu"
    for an engine to keep off the GPU, "" for one left to choose.

    The ``subtitleOcrEngines`` setting's names, the ones this step knows, in
    its order; or, without it, the order the module says. ``vision`` only
    with an address in ``subtitleOcrUrl`` to ask."""
    environ = os.environ if environ is None else environ
    url = (environ.get("subtitleOcrUrl") or "").strip()
    named = (environ.get("subtitleOcrEngines") or "").replace(",", " ").split()
    if named:
        chosen = [enums.shell_lower(name) for name in named]
        return tuple((name, "") for name in dict.fromkeys(chosen)
                     if name in ENGINES and (name != "vision" or url))
    order = [("vision", "")] if url else []
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


# The vision servers of this run, by address, kept from one subtitle to the
# next so a server started for one is used for the next and stopped once idle.
_servers: dict = {}
_servers_lock = threading.Lock()
# The free VRAM, in MiB, the preferred vision server is started with unless
# ``subtitleOcrServeMib`` says: a 35B-A3B's weights are 22 GiB, its lanes and
# picture encoder most of the rest of a 32 GiB card.
SERVE_MIB = 26000
# How long a vision server started here waits unused before it is stopped.
SERVE_IDLE = 300.0


def _vision_servers(environ) -> list:
    """The vision servers to try, in ``subtitleOcrUrl``'s order; the first
    started with ``subtitleOcrServe`` where it is not running and the GPU has
    room for it."""
    urls = [url.strip() for url in
            (environ.get("subtitleOcrUrl") or "").split(",") if url.strip()]
    command = (environ.get("subtitleOcrServe") or "").strip()
    try:
        need = int(environ.get("subtitleOcrServeMib") or SERVE_MIB)
    except ValueError:
        need = SERVE_MIB
    found = []
    with _servers_lock:
        for at, url in enumerate(urls):
            key = (url, command if at == 0 else "", need)
            if key not in _servers:
                _servers[key] = modelserver.Server(
                    url, key[1], need, idle=SERVE_IDLE)
            found.append(_servers[key])
    return found


def _model_on(server_url: str, environ) -> str:
    """The model to ask the server for: ``subtitleOcrModel`` where the server
    lists it, else the first it lists - a server further down the ladder
    holds another model than the one named."""
    listed = modelserver.served_model(server_url) or []
    named = (environ.get("subtitleOcrModel") or "").strip()
    if named and (named in listed or not listed):
        return named
    return listed[0] if listed else ""


def _jobs(environ) -> int:
    try:
        return max(1, int(environ.get("subtitleOcrJobs") or VISION_JOBS))
    except ValueError:
        return VISION_JOBS


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


def _options(environ) -> dict | None:
    """The ``subtitleOcrOptions`` setting: more fields for every request, as a
    JSON object - None for one that is not."""
    text = (environ.get("subtitleOcrOptions") or "").strip()
    try:
        extra = json.loads(text) if text else {}
    except ValueError:
        return None
    return extra if isinstance(extra, dict) else None


def _picture_part(png: str) -> dict:
    with open(png, "rb") as handle:
        picture = base64.b64encode(handle.read()).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": "data:image/png;base64," + picture}}


def _numbered(pngs: list) -> list:
    """The pictures as message parts, each after its number where there are
    several: a model that is told which picture is which keeps their answers
    in their places."""
    if len(pngs) == 1:
        return [_picture_part(pngs[0])]
    parts = []
    for number, png in enumerate(pngs, 1):
        parts += [{"type": "text", "text": "Image %d:" % number},
                  _picture_part(png)]
    return parts


def _ask(url: str, model: str, prompt: str, pngs: list, extra=None):
    """The model's answer to the prompt about the pictures, or None when the
    server would not give one. A server with no room is asked again, after a
    pause that doubles; any other refusal is final."""
    body = {"messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                *_numbered(pngs)]}],
            "max_tokens": _VISION_MAX_TOKENS * len(pngs), "temperature": 0,
            # Both spellings servers take for "answer, without thinking".
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False}}
    if model:
        body["model"] = model
    body.update(extra or {})
    request = urllib.request.Request(
        modelserver.root(url) + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    pause = _VISION_BACKOFF
    for _try in range(_VISION_TRIES):
        try:
            with urllib.request.urlopen(request, timeout=600) as reply:
                answer = json.loads(reply.read().decode("utf-8", "replace"))
            text = answer["choices"][0]["message"]["content"] or ""
            if _THOUGHT_END in text:
                text = text.rsplit(_THOUGHT_END, 1)[1]
            return text
        except urllib.error.HTTPError as refused:
            if refused.code not in _BUSY:
                return None
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            pass
        time.sleep(pause)
        pause *= 2
    return None


def _tidy(text: str) -> str:
    """One subtitle's text: its lines trimmed, the empty ones dropped."""
    return "\n".join(line.strip() for line in (text or "").splitlines()
                     if line.strip())


def _array(text: str, count: int):
    """The model's JSON array of ``count`` strings, or None for an answer
    that is not one. A fence around it is taken off, and a backslash that
    escapes nothing JSON knows is taken out: a slip, not a character."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    text = re.sub(r'\\(?!["\\/bfnrtu])', "", text)
    try:
        found = json.loads(text)
    except ValueError:
        return None
    if not isinstance(found, list) or len(found) != count \
            or not all(isinstance(one, str) for one in found):
        return None
    return [_tidy(one.replace("\\n", "\n")) for one in found]


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


def _read_group(url: str, model: str, row, group: list, line_height: int,
                extra) -> list:
    """The text of each picture of ``group``, in order - None for one the
    model would not read. Asked in one request; a group answered with no
    usable array, and a picture whose answer is short of its lines, are asked
    again a picture at a time."""
    # A sliver holds no text; sent with the rest it would only make the model
    # lose count of which answer is which.
    texts: list = ["" if line[3] < _SLIVER else None for line in group]
    asked = [at for at, text in enumerate(texts) if text is None]
    prompt = _VISION_GROUP_PROMPT.format(count=len(asked),
                                         language=row.sub_word)
    found = _array(_ask(url, model, prompt, [group[at][2] for at in asked],
                        extra), len(asked)) if len(asked) > 1 else None
    for at, text in zip(asked, found or [], strict=False):
        texts[at] = text
    alone = _VISION_PROMPT.format(language=row.sub_word)
    for at, line in enumerate(group):
        if at not in asked:
            continue
        if texts[at] is not None \
                and not _short_of_lines(texts[at], line[3], line_height):
            continue
        answer = _ask(url, model, alone, [line[2]], extra)
        if answer is not None:
            texts[at] = _tidy(answer)
    return texts


def _read_by_vision(image: str, row, out: str, environ,
                    log: Callable[[str], None]) -> str:
    """The picture subtitle read into a SubRip in ``out`` by the first vision
    server of the ladder that can be had now (:func:`_vision_servers`),
    :data:`VISION_GROUP` pictures a request and ``subtitleOcrJobs`` requests
    at a time; "" when none could read every line, so the next engine reads
    the whole subtitle. A picture that holds no text is no line."""
    extra = _options(environ)
    if extra is None:
        _say_once("options", log, "subtitleOcrOptions is not a JSON object, "
                  "the vision model is not asked")
        return ""
    for server in _vision_servers(environ):
        if not server.acquire(log):
            continue
        try:
            written = _read_with(server.url, image, row, out, environ, extra,
                                 log)
        finally:
            server.release()
        if written:
            return written
    log("  no vision OCR server is answering, nor can one be started now, so "
        "the next engine reads")
    return ""


def _read_with(url: str, image: str, row, out: str, environ, extra,
               log: Callable[[str], None]) -> str:
    """The picture subtitle read by the vision model at ``url``: see
    :func:`_read_by_vision`."""
    lines = _pictures(image, out)
    if not lines:
        return ""
    model = _model_on(url, environ)
    line_height = _line_height(lines)
    groups = [lines[at:at + VISION_GROUP]
              for at in range(0, len(lines), VISION_GROUP)]
    with ThreadPoolExecutor(max_workers=_jobs(environ)) as pool:
        texts = [text for found in pool.map(
                     lambda group: _read_group(url, model, row, group,
                                               line_height, extra), groups)
                 for text in found]
    missing = sum(1 for text in texts if text is None)
    if missing:
        log("  vision OCR at %s could not read %d of the %d lines of the %s "
            "subtitle" % (url, missing, len(lines), row.sub_word))
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
              log: Callable[[str], None], environ=None) -> str:
    """The picture subtitle ``image``, in the language of ``row`` (one of
    :data:`languages.LANGUAGES`), read into ``srt`` by the first engine
    that manages it; the engine's name, or "" when none did. An engine that
    exits 0 having written no line is a failure too."""
    environ = os.environ if environ is None else environ
    for engine, device in engines(environ):
        if not _ready(engine, row, log):
            continue
        out = tempfile.mkdtemp(dir=os.path.dirname(image))
        try:
            try:
                if engine == "vision":
                    written = _read_by_vision(image, row, out, environ, log)
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
