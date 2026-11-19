"""The language model the chapter generator asks, behind one call: a system
prompt and a user prompt in, the model's text out.

Two wire protocols cover nearly every model a user could point this at, local
or hosted, and neither is preferred:

  openai     the Chat Completions protocol, which is what local servers speak
             (Ollama, llama.cpp, vLLM, LM Studio, NInfer, ...) and what most
             hosted APIs offer beside their own (OpenAI, Groq, Mistral,
             OpenRouter, DeepSeek, Gemini's compatibility endpoint, ...). Sent
             with curl, the key on stdin and never on the argv, the way every
             other request this package makes is sent.
  anthropic  Claude, through the official ``anthropic`` package, imported only
             when this protocol is asked for - a run that never asks needs no
             package.

Which one, where, which model and with what key are settings
(:data:`API_VARIABLE` and the three after it), so one machine can keep a local
model and another a hosted one without anything here changing. Everything the
answer MEANS is :mod:`medialib.lib.aichapters`.

Whether the model is local or hosted is read off its address
(:func:`is_local`). A local server has one GPU to share, so its requests are
held to :data:`JOBS_VARIABLE` at a time; a hosted API scales with what it is
sent, so nothing waits for it unless that setting says otherwise.

:data:`URL_VARIABLE` may name several servers, the preferred first, for a
machine whose one card holds one model at a time: each recording is chaptered
by the first that answers (:meth:`Llm.serving`). The first, not running, is
started with :data:`SERVE_VARIABLE` where the GPU has room for it, and stopped
again once idle (:mod:`medialib.lib.modelserver`); further down, the model a
server lists is asked when it does not list :data:`MODEL_VARIABLE`, and the
window is the one it lists when that is smaller than :data:`CONTEXT_VARIABLE`.

A model behind the openai protocol cannot look anything up, so where the user
has an agent harness (:data:`HARNESS_VARIABLE`, :mod:`medialib.lib.llmharness`)
the requests that carry the user's own instructions are asked through it
instead, which can search the web for what they ask to have checked.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace

from medialib.lib import llmharness, modelserver, tmdbclient, tooldeps

__all__ = ["API_VARIABLE", "URL_VARIABLE", "MODEL_VARIABLE", "KEY_VARIABLE",
           "CONTEXT_VARIABLE", "OPTIONS_VARIABLE", "JOBS_VARIABLE",
           "SERVE_VARIABLE", "SERVE_MIB_VARIABLE",
           "HARNESS_VARIABLE", "HARNESS_MODEL_VARIABLE", "SERVE_MIB", "APIS", "DEFAULT_API",
           "DEFAULT_ANTHROPIC_MODEL", "DEFAULT_CONTEXT_TOKENS", "DEFAULT_JOBS",
           "THOROUGH_EFFORT", "LlmError", "Tally", "Llm", "from_environment", "require",
           "is_local", "first_json"]

# The settings, by the names a user gives them.
API_VARIABLE = "chapterLlmApi"
URL_VARIABLE = "chapterLlmUrl"
MODEL_VARIABLE = "chapterLlmModel"
KEY_VARIABLE = "chapterLlmKey"
CONTEXT_VARIABLE = "chapterLlmContext"
OPTIONS_VARIABLE = "chapterLlmOptions"
JOBS_VARIABLE = "chapterLlmJobs"
SERVE_VARIABLE = "chapterLlmServe"
SERVE_MIB_VARIABLE = "chapterLlmServeMib"
HARNESS_VARIABLE = llmharness.HARNESS_VARIABLE
HARNESS_MODEL_VARIABLE = llmharness.MODEL_VARIABLE

# The free GPU memory, in MiB, the first server is started with unless
# SERVE_MIB_VARIABLE says: a 35B-A3B's weights are 22 GiB, and a long
# transcript's cache wants most of the rest of a 32 GiB card.
SERVE_MIB = 26000

# How long a server started here waits unused before it is stopped.
SERVE_IDLE = 300.0

# The servers started here, one per address and command, so every recording
# and every thread holds the same one.
_servers: dict = {}
_servers_lock = threading.Lock()

APIS = ("openai", "anthropic")
DEFAULT_API = "openai"

# The Claude model asked when the setting names none.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"

# How many tokens of transcript one request may carry, when nothing says how
# large the model's window is. Small enough for most local servers as they are
# usually started, so a run on an unconfigured one degrades to more, smaller
# requests rather than to a server silently cutting the transcript short.
DEFAULT_CONTEXT_TOKENS = 32000

# How many requests are in flight at once on a local server. Two keeps a
# single GPU busy without queueing so deep that the server's own admission
# timeout fires. A hosted API gets every request the moment it is ready (0).
DEFAULT_JOBS = 2

# Host names that never leave this machine or its network: mDNS, home routers,
# Tailscale, and the names RFC 6761 keeps off the Internet.
_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal",
                   ".home.arpa", ".ts.net", ".test", ".invalid")

# Tailscale's addresses, which ipaddress does not count as private.
_SHARED_SPACE = ipaddress.ip_network("100.64.0.0/10")

# How hard the model thinks when it is asked to do more than divide the
# recording - follow the user's own instructions, look something up. Sent as
# ``reasoning_effort`` over the openai protocol, over whatever the options say
# there, and as the effort over the anthropic one.
THOROUGH_EFFORT = "medium"
DEFAULT_ANTHROPIC_EFFORT = "medium"

# How many web searches one Claude request may make, when it may search.
MAX_SEARCHES = 5

# How many times a Claude request whose searching ran past the API's own turn
# limit is resumed before what it has is taken.
MAX_CONTINUATIONS = 3

# How long one request may take. Long, because a local model reading a whole
# podcast transcript spends minutes on the prompt before the first word.
REQUEST_SECONDS = 900

# How many times a request that got no usable answer is sent again, and how
# long to wait before each, doubling.
RETRIES = 2
RETRY_WAIT_SECONDS = 5

# The most a reply may run to. An outline is a few kilobytes of JSON; the
# headroom is for a model that reasons before it answers, which can spend
# tens of thousands of tokens on a long transcript - and a server left to its
# own default (8192 is common) cuts the answer off before the JSON.
MAX_OUTPUT_TOKENS = 32768
MIN_OUTPUT_TOKENS = 4096

# The names the output limit goes by, in the order they are tried: the one
# nearly every server takes, the one OpenAI's reasoning models insist on, and
# none at all for a server that takes neither. A server refusing one moves the
# run on to the next for every later request.
_LIMIT_FIELDS = ("max_tokens", "max_completion_tokens", "")

# A reasoning model's thinking, where a server hands it back inside the text.
_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LlmError(Exception):
    """The model could not be asked, or did not answer."""


@dataclass
class Tally:
    """What the requests counted into it took: how many answered, the seconds
    each was under way summed, and the tokens the model wrote, from those
    whose server reported them (``counted`` of them, over ``counted_seconds``).
    A harness reports none. Safe to add to from many threads."""

    requests: int = 0
    seconds: float = 0.0
    tokens: int = 0
    counted: int = 0
    counted_seconds: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False,
                                  repr=False, compare=False)

    def add(self, seconds: float, tokens: int | None) -> None:
        with self._lock:
            self.requests += 1
            self.seconds += seconds
            if tokens is not None:
                self.tokens += tokens
                self.counted += 1
                self.counted_seconds += seconds

    def merge(self, other: Tally) -> None:
        with self._lock:
            self.requests += other.requests
            self.seconds += other.seconds
            self.tokens += other.tokens
            self.counted += other.counted
            self.counted_seconds += other.counted_seconds


@dataclass
class Llm:
    """One configured model. :meth:`ask` is all a caller uses."""

    api: str
    url: str
    model: str
    key: str = ""
    context_tokens: int = DEFAULT_CONTEXT_TOKENS
    # How many requests may be in flight at once, over the whole run; 0 is
    # no limit.
    jobs: int = DEFAULT_JOBS
    options: dict = field(default_factory=dict)
    # A server on this machine or its network, rather than a hosted API.
    local: bool = True
    limit_field: int = field(default=0, compare=False)
    effort: str = ""
    search: bool = False
    instructions: str = ""
    # The servers asked when :attr:`url`'s is not to be had, in order.
    fallbacks: tuple[str, ...] = ()
    # The command that starts :attr:`url`'s server, and the free GPU memory
    # in MiB it is started with.
    serve: str = ""
    serve_mib: int = SERVE_MIB
    # Whether :attr:`context_tokens` was set, rather than the default: a
    # server's own window replaces the default, and only narrows a setting.
    context_given: bool = True
    # The harnesses the user has, preferred first, and the model they are
    # asked for; :attr:`harness` is the one a request goes through, set by
    # :meth:`following`.
    harnesses: tuple[llmharness.Harness, ...] = ()
    harness_model: str = ""
    harness: llmharness.Harness | None = None
    _slots: threading.BoundedSemaphore | None = field(
        default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.jobs > 0:
            self._slots = threading.BoundedSemaphore(self.jobs)

    def describe(self) -> str:
        """The model as a run reports it: which, and where, and how hard it
        thinks when that was raised."""
        where = self.url or ("the Claude API" if self.api == "anthropic" else "")
        if self.serve:
            where += " (started when needed)"
        if self.fallbacks:
            where += ", else " + ", else ".join(self.fallbacks)
        text = "%s at %s (%s protocol, %s)" % (
            self.model or "the model listed", where, self.api,
            "local" if self.local else "hosted")
        if self.effort:
            text += ", reasoning at %s effort" % self.effort
        if self.search:
            text += ", with web search"
        if self.harness:
            text += ", through %s asking %s, with web search" % (
                self.harness.program, self.harness_model)
        return text

    def following(self, instructions: str) -> Llm:
        """This model with the user's own ``instructions`` for every chapter
        request (:mod:`medialib.lib.aichapters` puts them in), and asked to
        reason at :data:`THOROUGH_EFFORT` while it follows them - Claude
        allowed to search the web as well, which no server behind the openai
        protocol does for its caller - so a model behind that protocol is
        asked through the first of :attr:`harnesses` that is installed, when
        there is one. Itself when there are no instructions.

        The copy shares this model's request slots, so the two together stay
        within :attr:`jobs`."""
        instructions = (instructions or "").strip()
        if not instructions:
            return self
        copy = replace(self, effort=THOROUGH_EFFORT, search=self.api == "anthropic",
                       instructions=instructions,
                       harness=llmharness.installed(self.harnesses)
                       if self.api == "openai" else None)
        copy._slots = self._slots
        return copy

    @contextlib.contextmanager
    def serving(self, log: Callable[[str], None]) -> Iterator[Llm]:
        """This model on the first server of the ladder that can be had now,
        held until the block ends: the first one answering, or the first
        started where it is not running (:attr:`serve`) and the GPU has room.
        A server started here is stopped once it has been idle a while, and at
        the end of the run at the latest.

        The model is :attr:`model` where the server lists it, or names none,
        and otherwise the first it lists; the window is the one it lists
        where that is narrower. LlmError when no server can be had, or one
        names no model and lists none.

        A single address with a model and no command is used as it is,
        unasked - a hosted API need not list its models."""
        if self.api != "openai" or not (self.fallbacks or self.serve
                                        or not self.model):
            yield self
            return
        for at, url in enumerate((self.url,) + self.fallbacks):
            server = None
            if at == 0 and self.serve:
                server = _server(url, self.serve, self.serve_mib)
                if not server.acquire(log):
                    continue
            try:
                listing = _listing(url, self.key)
                if listing is None:
                    continue
                yield self._on(url, listing, log)
                return
            finally:
                if server is not None:
                    server.release()
        raise LlmError("no chapter model server answers at %s, nor can one be "
                       "started now" % ", ".join((self.url,) + self.fallbacks))

    def _on(self, url: str, listed: dict[str, int],
            log: Callable[[str], None]) -> Llm:
        """This model as the server at ``url``, listing ``listed``, serves it
        - see :meth:`serving`."""
        model = self.model
        if listed and model not in listed:
            model = next(iter(listed))
        if not model:
            raise LlmError("%s lists no model and %s names none"
                           % (url, MODEL_VARIABLE))
        window = listed.get(model, 0)
        context = self.context_tokens
        if window and (not self.context_given or window < context):
            context = window
        if url != self.url or model != self.model:
            log("    asking %s at %s" % (model, url))
        bound = replace(self, url=url, model=model, context_tokens=context,
                        fallbacks=(), serve="")
        bound._slots = self._slots
        return bound

    def ask(self, system: str, prompt: str, tally: Tally | None = None) -> str:
        """The model's text answer, its thinking taken out. LlmError when no
        answer came after the retries. Safe to call from many threads: the
        requests in flight never pass :attr:`jobs`. Every answer, a retried
        one too, is counted into ``tally``, timed from when its request held
        a slot."""
        last = ""
        for attempt in range(RETRIES + 1):
            if attempt:
                time.sleep(RETRY_WAIT_SECONDS * 2 ** (attempt - 1))
            try:
                with self._slots or contextlib.nullcontext():
                    started = time.monotonic()
                    text, tokens = self._ask_once(system, prompt)
            except LlmError as error:
                last = str(error)
                continue
            if tally is not None:
                tally.add(time.monotonic() - started, tokens)
            text = _THINKING.sub("", text).strip()
            if text:
                return text
            last = "an empty answer"
        raise LlmError(last)

    def _ask_once(self, system: str, prompt: str) -> tuple[str, int | None]:
        """The answer and the tokens the model wrote for it, None where they
        were not reported."""
        if self.harness:
            try:
                return llmharness.ask(self.harness, self.harness_model, system,
                                      prompt, REQUEST_SECONDS), None
            except llmharness.HarnessError as error:
                raise LlmError(str(error)) from error
        if self.api == "anthropic":
            return _ask_anthropic(self, system, prompt)
        return _ask_openai(self, system, prompt)


def from_environment(environ=None) -> Llm:
    """The model the settings describe. LlmError naming what is missing or
    wrong, so a run can refuse before it transcribes anything."""
    environ = os.environ if environ is None else environ
    api = (environ.get(API_VARIABLE) or DEFAULT_API).strip().lower()
    if api not in APIS:
        raise LlmError("%s is %r; it takes %s" % (API_VARIABLE, api, " or ".join(APIS)))
    urls = [address.strip().rstrip("/") for address in
            (environ.get(URL_VARIABLE) or "").split(",") if address.strip()]
    url = urls[0] if urls else ""
    if urls[1:] and api != "openai":
        raise LlmError("%s names one address for the anthropic protocol"
                       % URL_VARIABLE)
    model = (environ.get(MODEL_VARIABLE) or "").strip()
    key = environ.get(KEY_VARIABLE) or ""
    if api == "openai":
        if not url:
            raise LlmError("%s is not set: give the address of an OpenAI-compatible "
                           "server, e.g. http://localhost:11434/v1 for Ollama or "
                           "https://api.openai.com/v1" % URL_VARIABLE)
        if not model and not is_local(url):
            raise LlmError("%s is not set: give the model that API is to be "
                           "asked for" % MODEL_VARIABLE)
    else:
        model = model or DEFAULT_ANTHROPIC_MODEL
    options = (environ.get(OPTIONS_VARIABLE) or "").strip()
    try:
        extra = json.loads(options) if options else {}
    except ValueError as error:
        raise LlmError("%s is not JSON: %s" % (OPTIONS_VARIABLE, error)) from error
    if not isinstance(extra, dict):
        raise LlmError("%s has to be a JSON object" % OPTIONS_VARIABLE)
    if extra and api != "openai":
        raise LlmError("%s applies to the openai protocol only" % OPTIONS_VARIABLE)
    serve = (environ.get(SERVE_VARIABLE) or "").strip()
    if serve and api != "openai":
        raise LlmError("%s applies to the openai protocol only" % SERVE_VARIABLE)
    try:
        harnesses = llmharness.parse(environ.get(HARNESS_VARIABLE) or "")
    except llmharness.HarnessError as error:
        raise LlmError(str(error)) from error
    harness_model = (environ.get(HARNESS_MODEL_VARIABLE) or "").strip()
    if harnesses and not harness_model:
        # Not the harness's own default: that can be a hosted model, and the
        # transcript would be sent to it.
        raise LlmError("%s is not set: name the model %s is to ask, as it "
                       "names models (e.g. provider/model)"
                       % (HARNESS_MODEL_VARIABLE, harnesses[0].program))
    local = is_local(url)
    return Llm(api, url, model, key,
               _positive(environ, CONTEXT_VARIABLE, DEFAULT_CONTEXT_TOKENS),
               _positive(environ, JOBS_VARIABLE, DEFAULT_JOBS if local else 0),
               extra, local, fallbacks=tuple(urls[1:]), serve=serve,
               serve_mib=_positive(environ, SERVE_MIB_VARIABLE, SERVE_MIB),
               context_given=bool((environ.get(CONTEXT_VARIABLE) or "").strip()),
               harnesses=harnesses, harness_model=harness_model)


def _server(url: str, command: str, need_mib: int) -> modelserver.Server:
    """The one :class:`modelserver.Server` for ``url`` started by ``command``,
    shared by every recording and thread of the run."""
    key = (url, command, need_mib)
    with _servers_lock:
        if key not in _servers:
            _servers[key] = modelserver.Server(url, command, need_mib,
                                               idle=SERVE_IDLE)
        return _servers[key]


def _listing(url: str, key: str = "") -> dict[str, int] | None:
    """The models the server at ``url`` lists, in its order, each with the
    window it serves it with (``max_model_len``, 0 where it says none). None
    when it does not answer; nothing listed when it answers with an error, as
    a hosted API that keeps its list to itself does."""
    request = urllib.request.Request(url.rstrip("/") + "/models")
    if key:
        request.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            body = json.loads(reply.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError:
        return {}
    except (OSError, ValueError):
        return None
    entries = body.get("data") if isinstance(body, dict) else None
    listed = {}
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("id"):
            window = entry.get("max_model_len")
            listed[str(entry["id"])] = window if isinstance(window, int) \
                and window > 0 else 0
    return listed


def is_local(url: str) -> bool:
    """Whether the model at ``url`` runs on this machine or its network rather
    than as a hosted API. No address is the protocol's own hosted endpoint.

    Read off the host: a loopback, private or Tailscale address, a name
    without a dot (a machine on the LAN), one of :data:`_LOCAL_SUFFIXES`, or
    a name that resolves to local addresses only. A name that does not
    resolve is taken as local, the cautious side: a local server is only
    queued for, a hosted one is never waited on.
    """
    if not url:
        return False
    host = (urllib.parse.urlsplit(url if "//" in url else "//" + url).hostname
            or "").lower().rstrip(".")
    if not host or host == "localhost" or "." not in host and ":" not in host \
            or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        return _local_address(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        found = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return True
    addresses = {str(entry[4][0]).split("%", 1)[0] for entry in found}
    return bool(addresses) and all(_local_address(ipaddress.ip_address(address))
                                   for address in addresses)


def _local_address(address) -> bool:
    return (address.is_loopback or address.is_private or address.is_link_local
            or address.version == 4 and address in _SHARED_SPACE)


def require(program: str) -> Llm | None:
    """The model the settings describe, or None after saying why it cannot be
    used: a setting missing, or the tool or package its protocol goes through.
    For every command that chapters, before it does any work."""
    try:
        llm = from_environment()
    except LlmError as error:
        sys.stderr.write("\nError: no language model to ask: %s.\n"
                         "See the chapterLlm settings in docs/settings.md.\n"
                         "Nothing was changed.\n" % error)
        return None
    skip = bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))
    if llm.harnesses and llm.api == "openai" and not llmharness.installed(llm.harnesses):
        sys.stderr.write("\nWarning: %s lists %s, and none is installed: "
                         "instructions are asked without web search.\n"
                         % (HARNESS_VARIABLE, ", ".join(each.program
                                                        for each in llm.harnesses)))
    if llm.api == "anthropic":
        if tooldeps.require_python_module(
                "anthropic", program, "asks Claude for the chapters (pip install "
                "anthropic)", skip_preflight=skip):
            return None
    elif tooldeps.require_tools(program, ["curl"], skip_preflight=skip):
        return None
    return llm


def _positive(environ, name: str, default: int) -> int:
    value = (environ.get(name) or "").strip()
    if not value:
        return default
    if not value.isdigit() or int(value) < 1:
        raise LlmError("%s is %r; it takes a whole number above 0" % (name, value))
    return int(value)


def _ask_openai(llm: Llm, system: str, prompt: str) -> tuple[str, int | None]:
    """One Chat Completions request through curl.

    Nothing beyond the messages and an output limit is asked for - no
    temperature, no response format - because each of those is refused by
    some server this protocol is meant to reach, and the answer is read
    leniently instead (:func:`first_json`). What one server takes and another
    does not - how hard to think, a sampling setting - is the user's to add,
    in :data:`OPTIONS_VARIABLE`, except the ``reasoning_effort`` a model
    given instructions (:meth:`Llm.following`) sends over it. The limit is what the window has
    left after the prompt, up to :data:`MAX_OUTPUT_TOKENS`, under whichever of
    :data:`_LIMIT_FIELDS` the server takes.

    The body goes in a file and the key in the config on stdin: /proc shows
    every account an argv, and a transcript is too long for one anyway.
    """
    while True:
        tried = llm.limit_field
        try:
            return _post_openai(llm, system, prompt, tried)
        except _LimitRefused:
            # Requests running side by side can all be refused the same name;
            # each moves the run one past the name it sent, not one further.
            llm.limit_field = max(llm.limit_field, tried + 1)


class _LimitRefused(Exception):
    """The server refused the name the output limit was sent under."""


def _post_openai(llm: Llm, system: str, prompt: str, tried: int) -> tuple[str, int | None]:
    request: dict = dict(llm.options)
    if llm.effort:
        request["reasoning_effort"] = llm.effort
    request.update({"model": llm.model, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt}]})
    limit_field = _LIMIT_FIELDS[min(tried, len(_LIMIT_FIELDS) - 1)]
    if limit_field and not any(name in request for name in _LIMIT_FIELDS if name):
        left = llm.context_tokens - (len(system) + len(prompt)) // 3
        request[limit_field] = max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, left))
    body = json.dumps(request, ensure_ascii=False)
    descriptor, body_file = tempfile.mkstemp(prefix="chapterllm.", suffix=".json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
        value = tmdbclient._config_value
        lines = ["url = " + value(llm.url + "/chat/completions"),
                 "request = \"POST\"", "silent", "show-error",
                 "max-time = %d" % REQUEST_SECONDS,
                 'header = "Content-Type: application/json"',
                 "data-binary = " + value("@" + body_file),
                 'write-out = "\\n%{http_code}"']
        if llm.key:
            lines.append("header = " + value("Authorization: Bearer " + llm.key))
        try:
            proc = subprocess.run(["curl", "--config", "-"],
                                  input="\n".join(lines).encode("utf-8"),
                                  capture_output=True)
        except OSError as error:
            raise LlmError("curl could not be started: %s" % error) from error
    finally:
        try:
            os.remove(body_file)
        except OSError:
            pass
    if proc.returncode != 0:
        raise LlmError("%s: %s" % (llm.url, proc.stderr.decode("utf-8", "replace").strip()
                                   or "curl exited %d" % proc.returncode))
    text, _, status = proc.stdout.decode("utf-8", "replace").rpartition("\n")
    if status == "400" and limit_field and re.search(
            r"max_(completion_)?tokens|unsupported parameter|unrecognized",
            text, re.IGNORECASE):
        raise _LimitRefused()
    if not status.startswith("2"):
        raise LlmError("%s answered HTTP %s: %s" % (llm.url, status, _error_text(text)))
    try:
        reply = json.loads(text)
        message = reply["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise LlmError("%s: not a chat completion: %s" % (llm.url, text[:200])) from error
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content
                          if isinstance(part, dict))
    usage = reply.get("usage") if isinstance(reply, dict) else None
    tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    return content or "", tokens if isinstance(tokens, int) else None


def _error_text(body: str) -> str:
    """What a server said went wrong, out of its error body when there is one."""
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return body.strip()[:300]
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or body)[:300]


def _ask_anthropic(llm: Llm, system: str, prompt: str) -> tuple[str, int | None]:
    """One Messages request through the official SDK, streamed, because a long
    transcript is a long request.

    The key is the setting when there is one, and otherwise whatever the SDK
    finds itself (ANTHROPIC_API_KEY, a login profile). A refusal is retried on
    another model by the API itself (the fallbacks), and one that still stands
    is no answer.

    With :attr:`Llm.search` the model may search the web, which Anthropic runs
    on its side. A turn that searches past the API's own limit comes back
    paused and is sent again as it stands, which resumes it.
    """
    try:
        import anthropic
    except ImportError as error:
        raise LlmError("the Python \"anthropic\" package is not installed") from error
    options: dict = {}
    if llm.key:
        options["api_key"] = llm.key
    if llm.url:
        options["base_url"] = llm.url
    client = anthropic.Anthropic(timeout=REQUEST_SECONDS, **options)
    request: dict = {}
    if llm.search:
        request["tools"] = [{"type": "web_search_20260209", "name": "web_search",
                             "max_uses": MAX_SEARCHES}]
    messages: list = [{"role": "user", "content": prompt}]
    tokens: int | None = None
    for _ in range(MAX_CONTINUATIONS + 1):
        try:
            with client.beta.messages.stream(
                    model=llm.model, max_tokens=MAX_OUTPUT_TOKENS,
                    system=system, messages=messages,
                    output_config={"effort": llm.effort or DEFAULT_ANTHROPIC_EFFORT},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default", **request) as stream:
                message = stream.get_final_message()
        except anthropic.APIStatusError as error:
            raise LlmError("the Claude API answered HTTP %d: %s"
                           % (error.status_code, error.message)) from error
        except anthropic.APIConnectionError as error:
            raise LlmError("the Claude API could not be reached: %s" % error) from error
        written = getattr(getattr(message, "usage", None), "output_tokens", None)
        if isinstance(written, int):
            tokens = (tokens or 0) + written
        if message.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": message.content}]
    if message.stop_reason == "refusal":
        raise LlmError("the model declined to answer")
    return "".join(block.text for block in message.content if block.type == "text"), tokens


def first_json(text: str):
    """The first JSON object or array in a model's answer, wherever in the
    text it is - after a preamble, inside a fenced block. None when there is
    none.

    Read this way rather than asked for as a response format, which half the
    servers refuse: every model that can follow the prompt at all writes the
    JSON, and most of the rest is talk around it.
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        return value
    return None
