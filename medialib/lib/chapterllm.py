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
"""

from __future__ import annotations

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
import urllib.parse
from dataclasses import dataclass, field, replace

from medialib.lib import tmdbclient, tooldeps

__all__ = ["API_VARIABLE", "URL_VARIABLE", "MODEL_VARIABLE", "KEY_VARIABLE",
           "CONTEXT_VARIABLE", "OPTIONS_VARIABLE", "JOBS_VARIABLE", "APIS", "DEFAULT_API",
           "DEFAULT_ANTHROPIC_MODEL", "DEFAULT_CONTEXT_TOKENS", "DEFAULT_JOBS",
           "THOROUGH_EFFORT", "LlmError", "Llm", "from_environment", "require",
           "is_local", "first_json"]

# The settings, by the names a user gives them.
API_VARIABLE = "chapterLlmApi"
URL_VARIABLE = "chapterLlmUrl"
MODEL_VARIABLE = "chapterLlmModel"
KEY_VARIABLE = "chapterLlmKey"
CONTEXT_VARIABLE = "chapterLlmContext"
OPTIONS_VARIABLE = "chapterLlmOptions"
JOBS_VARIABLE = "chapterLlmJobs"

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
    _slots: threading.BoundedSemaphore | None = field(
        default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.jobs > 0:
            self._slots = threading.BoundedSemaphore(self.jobs)

    def describe(self) -> str:
        """The model as a run reports it: which, and where, and how hard it
        thinks when that was raised."""
        where = self.url or ("the Claude API" if self.api == "anthropic" else "")
        text = "%s at %s (%s protocol, %s)" % (
            self.model, where, self.api, "local" if self.local else "hosted")
        if self.effort:
            text += ", reasoning at %s effort" % self.effort
        if self.search:
            text += ", with web search"
        return text

    def following(self, instructions: str) -> Llm:
        """This model with the user's own ``instructions`` for every chapter
        request (:mod:`medialib.lib.aichapters` puts them in), and asked to
        reason at :data:`THOROUGH_EFFORT` while it follows them - Claude
        allowed to search the web as well, which no server behind the openai
        protocol does for its caller. Itself when there are none.

        The copy shares this model's request slots, so the two together stay
        within :attr:`jobs`."""
        instructions = (instructions or "").strip()
        if not instructions:
            return self
        copy = replace(self, effort=THOROUGH_EFFORT, search=self.api == "anthropic",
                       instructions=instructions)
        copy._slots = self._slots
        return copy

    def ask(self, system: str, prompt: str) -> str:
        """The model's text answer, its thinking taken out. LlmError when no
        answer came after the retries. Safe to call from many threads: the
        requests in flight never pass :attr:`jobs`."""
        last = ""
        for attempt in range(RETRIES + 1):
            if attempt:
                time.sleep(RETRY_WAIT_SECONDS * 2 ** (attempt - 1))
            try:
                if self._slots is None:
                    text = self._ask_once(system, prompt)
                else:
                    with self._slots:
                        text = self._ask_once(system, prompt)
            except LlmError as error:
                last = str(error)
                continue
            text = _THINKING.sub("", text).strip()
            if text:
                return text
            last = "an empty answer"
        raise LlmError(last)

    def _ask_once(self, system: str, prompt: str) -> str:
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
    url = (environ.get(URL_VARIABLE) or "").strip().rstrip("/")
    model = (environ.get(MODEL_VARIABLE) or "").strip()
    key = environ.get(KEY_VARIABLE) or ""
    if api == "openai":
        if not url:
            raise LlmError("%s is not set: give the address of an OpenAI-compatible "
                           "server, e.g. http://localhost:11434/v1 for Ollama or "
                           "https://api.openai.com/v1" % URL_VARIABLE)
        if not model:
            raise LlmError("%s is not set: give the model name that server "
                           "serves" % MODEL_VARIABLE)
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
    local = is_local(url)
    return Llm(api, url, model, key,
               _positive(environ, CONTEXT_VARIABLE, DEFAULT_CONTEXT_TOKENS),
               _positive(environ, JOBS_VARIABLE, DEFAULT_JOBS if local else 0),
               extra, local)


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


def _ask_openai(llm: Llm, system: str, prompt: str) -> str:
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


def _post_openai(llm: Llm, system: str, prompt: str, tried: int) -> str:
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
        message = json.loads(text)["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise LlmError("%s: not a chat completion: %s" % (llm.url, text[:200])) from error
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content
                          if isinstance(part, dict))
    return content or ""


def _error_text(body: str) -> str:
    """What a server said went wrong, out of its error body when there is one."""
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return body.strip()[:300]
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or body)[:300]


def _ask_anthropic(llm: Llm, system: str, prompt: str) -> str:
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
        if message.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": message.content}]
    if message.stop_reason == "refusal":
        raise LlmError("the model declined to answer")
    return "".join(block.text for block in message.content if block.type == "text")


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
