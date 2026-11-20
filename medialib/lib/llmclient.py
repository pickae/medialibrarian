"""One language model, behind one call: a system prompt and a user prompt in
- and pictures, for a model that sees - the model's text out.

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

An :class:`Llm` is one model the user has: which protocol, where, which
model, with what key. Which of them a use of a model asks - and so which
model chapters a recording or looks for a cover - is
:mod:`medialib.lib.llmroster`'s to decide, by what each one is
(:attr:`Llm.traits`, :mod:`medialib.lib.llmtraits`). The single-model
settings (:data:`API_VARIABLE` and the ones after it) describe one model
(:func:`from_environment`); the roster file describes any number.

Whether the model is local or in the cloud is its ``local`` trait
(:mod:`medialib.lib.llmtraits`): read off its address (:func:`is_local`)
unless the user declared it. A local server has one GPU to share, so its
requests are held to :data:`JOBS_VARIABLE` at a time; a cloud API is
serverless and scales with what it is sent, so every request goes the moment
it is ready, unless that setting says otherwise.

:data:`URL_VARIABLE` may name several servers, the preferred first, for a
machine whose one card holds one model at a time: each request goes to the
first that answers (:meth:`Llm.serving`). The first, not running, is started
with :data:`SERVE_VARIABLE` where the GPU has room for it, and stopped again
once idle (:mod:`medialib.lib.modelserver`); further down, the model a server
lists is asked when it does not list :data:`MODEL_VARIABLE`, and the window
is the one it lists when that is smaller than :data:`CONTEXT_VARIABLE`.

A model behind the openai protocol cannot look anything up, so where the user
has an agent harness (:data:`HARNESS_VARIABLE`, :mod:`medialib.lib.llmharness`)
a request that is to search the web (:meth:`Llm.searching`) is asked through
it instead. :mod:`medialib.lib.modelmeter` counts what each use asked.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace

from medialib.lib import llmharness, llmtraits, modelserver, tmdbclient, tooldeps
from medialib.lib.modelmeter import Tally

__all__ = ["API_VARIABLE", "URL_VARIABLE", "MODEL_VARIABLE", "KEY_VARIABLE",
           "CONTEXT_VARIABLE", "OPTIONS_VARIABLE", "JOBS_VARIABLE",
           "SERVE_VARIABLE", "SERVE_MIB_VARIABLE",
           "HARNESS_VARIABLE", "HARNESS_MODEL_VARIABLE", "TRAITS_VARIABLE",
           "SERVE_MIB", "APIS", "DEFAULT_API",
           "DEFAULT_ANTHROPIC_MODEL", "DEFAULT_CONTEXT_TOKENS", "DEFAULT_JOBS",
           "THOROUGH_EFFORT", "LlmError", "Tally", "Llm", "from_environment", "build",
           "preflight", "usable", "is_local", "first_json"]

# The settings, by the names a user gives them.
API_VARIABLE = "llmApi"
URL_VARIABLE = "llmUrl"
MODEL_VARIABLE = "llmModel"
KEY_VARIABLE = "llmKey"
CONTEXT_VARIABLE = "llmContext"
OPTIONS_VARIABLE = "llmOptions"
JOBS_VARIABLE = "llmJobs"
SERVE_VARIABLE = "llmServe"
SERVE_MIB_VARIABLE = "llmServeMib"
HARNESS_VARIABLE = llmharness.HARNESS_VARIABLE
HARNESS_MODEL_VARIABLE = llmharness.MODEL_VARIABLE
TRAITS_VARIABLE = "llmTraits"

# What a picture is counted as against the window, in tokens: about what a
# vision model makes of one picture scaled to a thousand pixels.
PICTURE_TOKENS = 1500

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
    # None is as its address says (:func:`is_local`).
    local: bool | None = None
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
    # What the model is asked for - "chapter", "cover" - which names a
    # harness's sessions (:func:`llmharness.session_title`).
    purpose: str = "chapter"
    # What the user declared it is (:func:`llmtraits.declared`), over what
    # is found out; and the name the roster knows it by.
    said: dict = field(default_factory=dict, compare=False)
    name: str = "llm"
    _slots: threading.BoundedSemaphore | None = field(
        default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.local is None:
            self.local = is_local(self.url)
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
        return self._thorough(instructions=instructions)

    def searching(self, purpose: str = "") -> Llm | None:
        """This model as it can look things up online, reasoning at
        :data:`THOROUGH_EFFORT`: Claude with its web search, a model behind
        the openai protocol through the first of :attr:`harnesses` that is
        installed. None where it cannot search - an openai server and no
        harness. ``purpose`` names what it is asked for (:attr:`purpose`).

        The copy shares this model's request slots, as :meth:`following`'s
        does."""
        copy = self._thorough(purpose=purpose or self.purpose)
        return copy if copy.search or copy.harness else None

    def _thorough(self, **changes) -> Llm:
        copy = replace(self, effort=THOROUGH_EFFORT, search=self.api == "anthropic",
                       harness=llmharness.installed(self.harnesses)
                       if self.api == "openai" else None, **changes)
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
        raise LlmError("no model server answers at %s, nor can one be "
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

    @property
    def traits(self) -> llmtraits.Traits:
        """What this model is (:mod:`medialib.lib.llmtraits`): what the user
        declared (:attr:`said`), else what a local server says, else what
        its name says. Found out once per model. Of the model :meth:`serving`
        gave, it is the model the server really holds."""
        return llmtraits.detect(self.api, self.url, self.model, self.key,
                                bool(self.local), self.said)

    def sees_images(self) -> bool:
        """Whether this model can be shown pictures (:meth:`ask`'s ``images``)."""
        return bool(self.traits.vision)

    def can_search(self) -> bool:
        """Whether :meth:`searching` has a way to search the web: Claude's
        own, or an installed harness for a model behind the openai protocol."""
        return self.api == "anthropic" or (
            self.api == "openai" and llmharness.installed(self.harnesses) is not None)

    def has(self, trait: str) -> bool:
        """Whether this model has ``trait`` - one of :mod:`llmtraits`'s, or
        ``web``, which is the route's (:meth:`can_search`)."""
        if trait == "web":
            return self.can_search()
        return self.traits.has(trait)

    def ask(self, system: str, prompt: str, tally: Tally | None = None,
            images: Sequence[str] = ()) -> str:
        """The model's text answer, its thinking taken out. LlmError when no
        answer came after the retries. Safe to call from many threads: the
        requests in flight never pass :attr:`jobs`. Every answer, a retried
        one too, is counted into ``tally``, timed from when its request held
        a slot.

        ``images`` are pictures - JPEG, PNG, GIF or WebP files - shown to the
        model before the prompt, in their order; for a model that
        :meth:`sees_images`, and never through a harness."""
        if images and self.harness:
            raise LlmError("pictures are not sent through %s" % self.harness.program)
        pictures = [_picture(path) for path in images]
        last = ""
        for attempt in range(RETRIES + 1):
            if attempt:
                time.sleep(RETRY_WAIT_SECONDS * 2 ** (attempt - 1))
            try:
                with self._slots or contextlib.nullcontext():
                    started = time.monotonic()
                    text, tokens = self._ask_once(system, prompt, pictures)
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

    def _ask_once(self, system: str, prompt: str,
                  pictures: Sequence[tuple[str, str]] = ()) -> tuple[str, int | None]:
        """The answer and the tokens the model wrote for it, None where they
        were not reported. ``pictures`` are (media type, base64) pairs."""
        if self.harness:
            try:
                return llmharness.ask(
                    self.harness, self.harness_model, system, prompt,
                    REQUEST_SECONDS, llmharness.session_title(self.purpose)), None
            except llmharness.HarnessError as error:
                raise LlmError(str(error)) from error
        # Pictures are passed only where there are any, so a request of text
        # is the call it always was.
        more = (pictures,) if pictures else ()
        if self.api == "anthropic":
            return _ask_anthropic(self, system, prompt, *more)
        return _ask_openai(self, system, prompt, *more)


def from_environment(environ=None) -> Llm:
    """The model the single-model settings describe. LlmError naming what is
    missing or wrong, so a run can refuse before it transcribes anything."""
    environ = os.environ if environ is None else environ
    entry = {field: environ.get(name) for field, name in SETTING_FIELDS.items()
             if (environ.get(name) or "").strip()}
    return build(entry, "llm", settings=True)


# Which of an entry's fields (:func:`build`) each single-model setting gives.
SETTING_FIELDS = {"api": API_VARIABLE, "url": URL_VARIABLE, "model": MODEL_VARIABLE,
                  "key": KEY_VARIABLE, "context": CONTEXT_VARIABLE,
                  "options": OPTIONS_VARIABLE, "jobs": JOBS_VARIABLE,
                  "serve": SERVE_VARIABLE, "serve_mib": SERVE_MIB_VARIABLE,
                  "harness": HARNESS_VARIABLE, "harness_model": HARNESS_MODEL_VARIABLE,
                  "traits": TRAITS_VARIABLE}

# The fields an entry may hold beyond those: the roster's own.
ROSTER_FIELDS = ("key_env", "tasks")


def build(entry: dict, name: str, settings: bool = False) -> Llm:
    """The model one entry describes: a table of :data:`SETTING_FIELDS`'s
    fields, as the roster file holds them (typed: a list of addresses, a
    table of options or traits) or as the settings do (all text). ``name``
    is what the roster calls it; with ``settings`` a problem is named by the
    setting it is in, otherwise by the field. LlmError for an entry that
    cannot be used."""
    def said(field: str) -> str:
        return SETTING_FIELDS[field] if settings else "%s's %s" % (name, field)

    unknown = sorted(set(entry) - set(SETTING_FIELDS) - set(ROSTER_FIELDS))
    if unknown:
        raise LlmError("%s has no field %s; it takes %s" % (
            name, ", ".join(unknown),
            ", ".join(list(SETTING_FIELDS) + list(ROSTER_FIELDS))))
    api = str(entry.get("api") or DEFAULT_API).strip().lower()
    if api not in APIS:
        raise LlmError("%s is %r; it takes %s" % (said("api"), api, " or ".join(APIS)))
    addresses = entry.get("url") or ""
    if isinstance(addresses, str):
        addresses = addresses.split(",")
    urls = [str(address).strip().rstrip("/") for address in addresses
            if str(address).strip()]
    url = urls[0] if urls else ""
    if urls[1:] and api != "openai":
        raise LlmError("%s names one address for the anthropic protocol" % said("url"))
    model = str(entry.get("model") or "").strip()
    key = str(entry.get("key") or "")
    if not key and entry.get("key_env"):
        key = os.environ.get(str(entry["key_env"]), "")
    if api == "openai":
        if not url:
            raise LlmError("%s is not set: give the address of an OpenAI-compatible "
                           "server, e.g. http://localhost:11434/v1 for Ollama or "
                           "https://api.openai.com/v1" % said("url"))
        if not model and not is_local(url):
            raise LlmError("%s is not set: give the model that API is to be "
                           "asked for" % said("model"))
    else:
        model = model or DEFAULT_ANTHROPIC_MODEL
    extra = entry.get("options") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra) if extra.strip() else {}
        except ValueError as error:
            raise LlmError("%s is not JSON: %s" % (said("options"), error)) from error
    if not isinstance(extra, dict):
        raise LlmError("%s has to be a JSON object" % said("options"))
    if extra and api != "openai":
        raise LlmError("%s applies to the openai protocol only" % said("options"))
    serve = str(entry.get("serve") or "").strip()
    if serve and api != "openai":
        raise LlmError("%s applies to the openai protocol only" % said("serve"))
    harness_names = entry.get("harness") or ""
    if not isinstance(harness_names, str):
        harness_names = ",".join(str(each) for each in harness_names)
    try:
        harnesses = llmharness.parse(harness_names)
    except llmharness.HarnessError as error:
        raise LlmError(str(error)) from error
    harness_model = str(entry.get("harness_model") or "").strip()
    if harnesses and not harness_model:
        # Not the harness's own default: that can be a hosted model, and the
        # transcript would be sent to it.
        raise LlmError("%s is not set: name the model %s is to ask, as it "
                       "names models (e.g. provider/model)"
                       % (said("harness_model"), harnesses[0].program))
    traits = entry.get("traits") or {}
    try:
        if isinstance(traits, str):
            declared = llmtraits.parse_traits(traits)
        elif isinstance(traits, dict):
            declared = llmtraits.declared(traits)
        else:
            raise llmtraits.TraitsError("a table or a comma list, not %r" % (traits,))
    except llmtraits.TraitsError as error:
        raise LlmError("%s: %s" % (said("traits"), error)) from error
    # The local trait as declared, else as the address says: it decides
    # whether requests queue for a GPU or all go at once.
    local = declared.get("local", is_local(url))
    return Llm(api, url, model, key,
               _positive(entry, "context", DEFAULT_CONTEXT_TOKENS, said),
               _positive(entry, "jobs", DEFAULT_JOBS if local else 0, said),
               extra, local, fallbacks=tuple(urls[1:]), serve=serve,
               serve_mib=_positive(entry, "serve_mib", SERVE_MIB, said),
               context_given=bool(str(entry.get("context") or "").strip()),
               harnesses=harnesses, harness_model=harness_model,
               said=declared, name=name)


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


def _picture(path: str) -> tuple[str, str]:
    """The picture at ``path`` as (media type, base64). LlmError for a file
    that is none of the kinds every vision API takes."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise LlmError("the picture %s cannot be read: %s" % (path, error)) from error
    if data.startswith(b"\xff\xd8\xff"):
        kind = "image/jpeg"
    elif data.startswith(b"\x89PNG"):
        kind = "image/png"
    elif data[:4] == b"GIF8":
        kind = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = "image/webp"
    else:
        raise LlmError("%s is not a JPEG, PNG, GIF or WebP picture" % path)
    return kind, base64.b64encode(data).decode("ascii")


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


def preflight(llm: Llm, program: str, purpose: str = "") -> bool:
    """Whether what asking ``llm`` goes through is here - the ``anthropic``
    package, or curl - after saying what is missing where it is not. With
    ``purpose``, a harness listed and none installed is warned about: what
    it was for (``purpose``) is asked without web search."""
    skip = bool(os.environ.get("SKIP_TOOL_PREFLIGHT", ""))
    if purpose and llm.harnesses and llm.api == "openai" \
            and not llmharness.installed(llm.harnesses):
        sys.stderr.write("\nWarning: %s lists %s, and none is installed: "
                         "%s without web search.\n"
                         % (llm.name, ", ".join(each.program for each in llm.harnesses),
                            purpose))
    if llm.api == "anthropic":
        return not tooldeps.require_python_module(
            "anthropic", program, "asks Claude (pip install anthropic)",
            skip_preflight=skip)
    return not tooldeps.require_tools(program, ["curl"], skip_preflight=skip)


def usable(llm: Llm) -> bool:
    """Whether ``llm`` can be asked without a word: its protocol's package
    is here. For a model a step only uses where it can."""
    return llm.api != "anthropic" or importlib.util.find_spec("anthropic") is not None


def _positive(entry: dict, field: str, default: int,
              said: Callable[[str], str]) -> int:
    value = str(entry.get(field) or "").strip()
    if not value:
        return default
    if not value.isdigit() or int(value) < 1:
        raise LlmError("%s is %r; it takes a whole number above 0"
                       % (said(field), value))
    return int(value)


def _ask_openai(llm: Llm, system: str, prompt: str,
                pictures: Sequence[tuple[str, str]] = ()) -> tuple[str, int | None]:
    """One Chat Completions request through curl, with ``pictures`` as
    ``image_url`` parts of the user's message where there are any.

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
            if pictures:
                return _post_openai(llm, system, prompt, tried, pictures)
            return _post_openai(llm, system, prompt, tried)
        except _LimitRefused:
            # Requests running side by side can all be refused the same name;
            # each moves the run one past the name it sent, not one further.
            llm.limit_field = max(llm.limit_field, tried + 1)


class _LimitRefused(Exception):
    """The server refused the name the output limit was sent under."""


def _post_openai(llm: Llm, system: str, prompt: str, tried: int,
                 pictures: Sequence[tuple[str, str]] = ()) -> tuple[str, int | None]:
    request: dict = dict(llm.options)
    if llm.effort:
        request["reasoning_effort"] = llm.effort
    asked: str | list = prompt
    if pictures:
        asked = [*({"type": "image_url",
                      "image_url": {"url": "data:%s;base64,%s" % picture}}
                     for picture in pictures),
                   {"type": "text", "text": prompt}]
    request.update({"model": llm.model, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": asked}]})
    limit_field = _LIMIT_FIELDS[min(tried, len(_LIMIT_FIELDS) - 1)]
    if limit_field and not any(name in request for name in _LIMIT_FIELDS if name):
        left = llm.context_tokens - (len(system) + len(prompt)) // 3 \
            - PICTURE_TOKENS * len(pictures)
        request[limit_field] = max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, left))
    body = json.dumps(request, ensure_ascii=False)
    descriptor, body_file = tempfile.mkstemp(prefix="llmclient.", suffix=".json")
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


def _ask_anthropic(llm: Llm, system: str, prompt: str,
                   pictures: Sequence[tuple[str, str]] = ()) -> tuple[str, int | None]:
    """One Messages request through the official SDK, streamed, because a long
    transcript is a long request; ``pictures`` go before the prompt as image
    blocks.

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
    content: str | list = prompt
    if pictures:
        content = [*({"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": data}}
                     for media_type, data in pictures),
                   {"type": "text", "text": prompt}]
    messages: list = [{"role": "user", "content": content}]
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
        messages = [{"role": "user", "content": content},
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
