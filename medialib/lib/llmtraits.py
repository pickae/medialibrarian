"""What a language model is: what it can do, and what it costs to ask.

A use of a model (:mod:`medialib.lib.llmroster`'s tasks) needs some of these
and prefers others, so every model the user has is described by the same
:class:`Traits`:

  tools       it calls functions (tool calling)
  web         it can search the web - a property of the ROUTE, not only the
              model: Claude searches on Anthropic's side, any other model
              through an agent harness (:mod:`medialib.lib.llmharness`).
              Answered by :class:`medialib.lib.llmclient.Llm`, not here
  vision      it can be shown pictures
  reasoning   it thinks before it answers
  images_out  it makes pictures (none of this package's uses asks yet)
  local       it runs on this machine or its network, rather than hosted
              (cloud). Read off its address, unless declared - a proxy on
              this machine in front of a hosted API is ``cloud``. A local
              model shares one GPU, so its requests queue; a cloud API
              scales with what it is sent, so they never wait
              (:mod:`medialib.lib.llmclient`)
  moe         a mixture of experts, which reads only a few of its weights a
              token (``active_b`` of ``params_b`` billion) - as fast as a
              small model, as knowing as a large one
  size        small, medium or large
  speed       fast or slow
  cheap       whether asking it costs next to nothing

The names follow the open catalogues, so an entry can be copied over:
models.dev (which opencode reads) calls them ``tool_call``, ``reasoning``,
``attachment``/``modalities``, ``open_weights`` and ``cost``; LiteLLM's model
map ``supports_function_calling``, ``supports_vision``, ``supports_reasoning``
and ``input_cost_per_token``; Ollama's ``/api/show`` lists ``tools``,
``vision`` and ``thinking`` among a model's capabilities.

Each trait is found the surest way there is: what the user declared; else
what a local server says of the model it holds (llama.cpp's ``/props``,
Ollama's ``/api/show``); else what the model's name says - ``Qwen3-VL-30B-A3B``
is a 30-billion-parameter mixture of experts with 3 billion active that sees
pictures and reasons. :func:`detect` answers once per model.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from dataclasses import dataclass, fields, replace

__all__ = ["Traits", "TraitsError", "SIZES", "detect", "declared", "parse_traits",
           "forget"]

SIZES = ("small", "medium", "large")
SPEEDS = ("fast", "slow")

# The total parameters, in billions, up to which a model is small, and up to
# which it is medium.
SMALL_B = 15.0
MEDIUM_B = 80.0

# The active parameters, in billions, up to which a local model is fast:
# what a token reads is what a card spends its time on.
FAST_ACTIVE_B = 15.0

# Tokens a second from which a model is fast, where that was measured or said.
FAST_TOK_S = 40.0

# The price of a million written tokens, in dollars, up to which a hosted
# model is cheap.
CHEAP_COST_OUT = 2.0


class TraitsError(ValueError):
    """A declaration that is not one."""


@dataclass(frozen=True)
class Traits:
    """What one model is. None is not known."""

    tools: bool | None = None
    vision: bool | None = None
    reasoning: bool | None = None
    images_out: bool | None = None
    local: bool = False
    moe: bool | None = None
    params_b: float | None = None
    active_b: float | None = None
    tok_s: float | None = None
    cost_in: float | None = None
    cost_out: float | None = None
    # Said outright, over what is worked out from the rest.
    size_said: str | None = None
    speed_said: str | None = None
    cheap_said: bool | None = None
    # A hosted model's family as small or large, from its name.
    family: str | None = None

    @property
    def size(self) -> str:
        if self.size_said:
            return self.size_said
        if self.params_b is not None:
            return ("small" if self.params_b <= SMALL_B
                    else "medium" if self.params_b <= MEDIUM_B else "large")
        if self.family:
            return self.family
        return "medium" if self.local else "large"

    @property
    def speed(self) -> str:
        if self.speed_said:
            return self.speed_said
        if self.tok_s is not None:
            return "fast" if self.tok_s >= FAST_TOK_S else "slow"
        if self.local:
            reads = self.active_b if self.active_b is not None else self.params_b
            if reads is not None:
                return "fast" if reads <= FAST_ACTIVE_B else "slow"
            return "fast" if self.moe else "slow"
        return "fast" if self.size == "small" else "slow"

    @property
    def cheap(self) -> bool:
        if self.cheap_said is not None:
            return self.cheap_said
        if self.local:
            return True
        if self.cost_out is not None:
            return self.cost_out <= CHEAP_COST_OUT
        return self.size == "small"

    def has(self, trait: str) -> bool:
        """Whether it has ``trait``, a yes-or-no one (see the module's list);
        what is not known is no."""
        value = {"fast": self.speed == "fast", "slow": self.speed == "slow",
                 "cheap": self.cheap, "cloud": not self.local,
                 "small": self.size == "small", "large": self.size == "large",
                 "medium": self.size == "medium"}.get(trait)
        if value is not None:
            return value
        return bool(getattr(self, trait, False))

    def describe(self) -> str:
        """The traits in a few words, as a run reports a model."""
        said = [name for name in ("tools", "vision", "reasoning", "images_out")
                if getattr(self, name)]
        said = [{"images_out": "makes images"}.get(name, name) for name in said]
        shape = "MoE" if self.moe else "dense" if self.moe is False else ""
        if self.params_b:
            shape = ("%s %gB" % (shape, self.params_b)).strip()
            if self.active_b:
                shape += "-A%gB" % self.active_b
        return ", ".join(filter(None, [
            "local" if self.local else "hosted", self.size, self.speed,
            "cheap" if self.cheap else "paid", shape, *said]))


# --- what a name says ------------------------------------------------------

_VISION = re.compile(
    r"(?:\bvl\b|[-_.]vl[-_.:]|vl$|vision|llava|bakllava|moondream|pixtral|"
    r"gemma-?[3-9]|mistral-small-3\.[1-9]|mistral-small-[4-9]|mistral-medium|"
    r"llama-?4|minicpm-?[o-v]|internvl|molmo|omni|gpt-4o|gpt-4\.1|gpt-[5-9]|"
    r"\bo[3-9]\b|gemini|claude)", re.IGNORECASE)

_TOOLS = re.compile(
    r"(?:qwen-?[2-9]|qwq|llama-?3\.[1-9]|llama-?[4-9]|mistral|mixtral|magistral|"
    r"devstral|ministral|command-r|command-a|gpt-|\bo[1-9]\b|claude|gemini|"
    r"deepseek-(?:v[3-9]|r1)|glm-?4|kimi|granite-?[3-9]|hermes|functionary|"
    r"phi-?4|gemma-?3|nemotron)", re.IGNORECASE)

_REASONING = re.compile(
    r"(?:qwq|qwen-?3(?!.*instruct)|deepseek-r1|r1-distill|gpt-oss|\bo[1-9]\b|"
    r"\bo[1-9]-|thinking|magistral|reasoning|claude-(?:opus|sonnet)-[4-9]|"
    r"claude-3-7|gemini-(?:2\.5|[3-9])|gpt-[5-9]|glm-4\.[5-9]|kimi-k2|"
    r"exaone-deep|nemotron.*super)", re.IGNORECASE)

_IMAGES_OUT = re.compile(
    r"(?:gpt-image|dall-?e|imagen|flux|stable-diffusion|sdxl|image-preview|"
    r"image-generation)", re.IGNORECASE)

_MOE = re.compile(
    r"(?:[-_.:]a\d+(?:\.\d+)?b\b|\bmoe\b|mixtral|gpt-oss|deepseek-v[3-9]|"
    r"deepseek-r1(?!-distill)|llama-?4|qwen-?3[-.:]?(?:30|235|480|next)|"
    r"glm-4\.[5-9]|kimi|ernie-4\.5|granite-.*-a\d)", re.IGNORECASE)

_PARAMS = re.compile(r"(?<![a-z\d.])(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)
_ACTIVE = re.compile(r"(?<![a-z\d])a(\d+(?:\.\d+)?)b\b", re.IGNORECASE)

# A hosted model's size, where its name gives no figure.
_SMALL_FAMILY = re.compile(r"(?:haiku|mini|nano|flash|lite|small|tiny)",
                           re.IGNORECASE)
_LARGE_FAMILY = re.compile(r"(?:opus|sonnet|gpt-[4-9]|\bo[1-9]\b|pro|ultra|"
                           r"large|max|grok)", re.IGNORECASE)

# The active parameters of the mixtures whose names do not carry them.
_KNOWN_ACTIVE = (
    (re.compile(r"gpt-oss-?120b", re.IGNORECASE), 117.0, 5.1),
    (re.compile(r"gpt-oss-?20b", re.IGNORECASE), 21.0, 3.6),
    (re.compile(r"mixtral-?8x7b", re.IGNORECASE), 47.0, 13.0),
    (re.compile(r"mixtral-?8x22b", re.IGNORECASE), 141.0, 39.0),
)


def _from_name(name: str, local: bool) -> Traits:
    name = name or ""
    params = active = None
    for pattern, total, reads in _KNOWN_ACTIVE:
        if pattern.search(name):
            params, active = total, reads
    if params is None:
        found = _PARAMS.search(name)
        params = float(found.group(1)) if found else None
        found = _ACTIVE.search(name)
        active = float(found.group(1)) if found else None
    family = None
    if params is None and not local:
        family = ("small" if _SMALL_FAMILY.search(name)
                  else "large" if _LARGE_FAMILY.search(name) else None)
    return Traits(tools=bool(_TOOLS.search(name)),
                  vision=bool(_VISION.search(name)),
                  reasoning=bool(_REASONING.search(name)),
                  images_out=bool(_IMAGES_OUT.search(name)),
                  local=local,
                  # A name says a mixture where it is one; that it is dense
                  # it never says.
                  moe=True if _MOE.search(name) or active is not None else None,
                  params_b=params, active_b=active, family=family)


# --- what a server says ----------------------------------------------------

def _server_json(url: str, key: str, body: dict | None = None):
    """What the server at ``url`` answers, as JSON - a GET, or a POST of
    ``body`` - or None when it does not answer with any."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return json.loads(reply.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


_BILLIONS = re.compile(r"(\d+(?:\.\d+)?)\s*([BM])", re.IGNORECASE)


def _from_server(url: str, model: str, key: str) -> dict:
    """What a local server says of ``model``: llama.cpp's ``/props`` its
    modalities and its template's tool support, Ollama's ``/api/show`` the
    model's capabilities, size and experts. Only what it said."""
    root = re.sub(r"/v1$", "", url.rstrip("/"))
    said: dict = {}
    props = _server_json(root + "/props", key)
    if isinstance(props, dict):
        modalities = props.get("modalities")
        if isinstance(modalities, dict) and "vision" in modalities:
            said["vision"] = bool(modalities["vision"])
        caps = props.get("chat_template_caps")
        if isinstance(caps, dict) and "supports_tools" in caps:
            said["tools"] = bool(caps["supports_tools"])
    if said or not model:
        return said
    # A POST naming the model is a request to load it on the ninfer switcher,
    # as on Ollama: noted like any other, so an interrupted run unloads it,
    # and not sent at all once the run is interrupted.
    from medialib.lib import modelserver, runcontext
    if runcontext.interrupted():
        return said
    modelserver.note_request(url, model)
    shown = _server_json(root + "/api/show", key, {"model": model})
    if not isinstance(shown, dict):
        return said
    capabilities = shown.get("capabilities")
    if isinstance(capabilities, list):
        said["vision"] = "vision" in capabilities
        said["tools"] = "tools" in capabilities
        said["reasoning"] = "thinking" in capabilities
    details = shown.get("details")
    size = details.get("parameter_size") if isinstance(details, dict) else None
    found = _BILLIONS.search(str(size or ""))
    if found:
        figure = float(found.group(1))
        said["params_b"] = figure if found.group(2).upper() == "B" else figure / 1000
    info = shown.get("model_info")
    if isinstance(info, dict):
        experts = [value for name, value in info.items()
                   if name.endswith(".expert_count") and isinstance(value, int)]
        if experts:
            said["moe"] = experts[0] > 1
    return said


# --- what the user said ----------------------------------------------------

_FLAGS = ("tools", "vision", "reasoning", "images_out", "moe", "cheap", "local")
_FIGURES = {"params": "params_b", "params_b": "params_b",
            "active": "active_b", "active_b": "active_b",
            "tok_s": "tok_s", "cost_in": "cost_in", "cost_out": "cost_out"}
# The catalogues' names for the same, so an entry copied from one reads.
_ALIASES = {"tool_call": "tools", "supports_function_calling": "tools",
            "attachment": "vision", "supports_vision": "vision",
            "supports_reasoning": "reasoning", "thinking": "reasoning",
            "image_output": "images_out", "dense": "-moe", "cloud": "-local",
            "hosted": "-local"}


def declared(values: dict) -> dict:
    """A model's traits as a table of the user's says them - booleans,
    figures, ``size`` and ``speed`` - as :class:`Traits` fields. TraitsError
    for a name or value that is none."""
    said: dict = {}
    for name, value in values.items():
        name = _ALIASES.get(name, name)
        negated = name.startswith("-")
        name = name.lstrip("-")
        if name in _FLAGS:
            if not isinstance(value, bool):
                raise TraitsError("%s is true or false, not %r" % (name, value))
            said["cheap_said" if name == "cheap" else name] = value != negated
        elif name in _FIGURES:
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or value < 0:
                raise TraitsError("%s is a number, not %r" % (name, value))
            said[_FIGURES[name]] = float(value)
        elif name == "size":
            if value not in SIZES:
                raise TraitsError("size is %s, not %r" % (", ".join(SIZES), value))
            said["size_said"] = value
        elif name == "speed":
            if value not in SPEEDS:
                raise TraitsError("speed is fast or slow, not %r" % (value,))
            said["speed_said"] = value
        else:
            raise TraitsError("%r is no trait" % name)
    return said


def parse_traits(text: str) -> dict:
    """The traits a one-line setting declares: a comma list of names, ``-``
    before one that the model lacks, ``name=value`` for a figure, a size or a
    speed - ``vision,tools,-reasoning,params=30,active=3,size=medium``."""
    values: dict = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        name = name.strip()
        if not sep:
            values[name.lstrip("-")] = not name.startswith("-")
            continue
        value = value.strip()
        try:
            values[name] = float(value)
        except ValueError:
            values[name] = value
    return declared(values)


# --- all three -------------------------------------------------------------

_seen: dict[tuple, Traits] = {}
_seen_lock = threading.Lock()


def detect(api: str, url: str, model: str, key: str, local: bool,
           said: dict | None = None) -> Traits:
    """The traits of ``model`` at ``url`` over ``api``: ``said`` over what
    a local server says over what its name says. Answered once per model."""
    said = dict(said or {})
    index = (api, url, model, tuple(sorted(said.items())))
    with _seen_lock:
        if index in _seen:
            return _seen[index]
    traits = _from_name(model, local)
    if api == "anthropic":
        # Every Claude model this package would ask sees, calls tools and
        # reasons.
        traits = replace(traits, tools=True, vision=True, reasoning=True)
    elif local and url:
        wanted = {"tools", "vision", "reasoning", "moe", "params_b"}
        if not wanted <= set(said):
            traits = replace(traits, **_from_server(url, model, key))
    known = {item.name for item in fields(Traits)}
    traits = replace(traits, **{name: value for name, value in said.items()
                                if name in known})
    with _seen_lock:
        _seen[index] = traits
    return traits


def forget() -> None:
    """Forget every model answered. For a test."""
    with _seen_lock:
        _seen.clear()
