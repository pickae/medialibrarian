"""The language models a user has, and which of them each use of a model asks.

A use of a model is a :class:`Task`: what it needs a model to be, what it
would rather the model be, and which way it leans between a small, cheap and
fast model and a large, slow, costly and more precise one. Searching the web
for a cover cannot be done without web search; chaptering a recording can,
but goes better with reasoning, and with the user's own instructions better
still with web search too, to check what they ask to have checked. Looking
at a few pictures is light work.

The models are the user's roster: the file :data:`MODELS_VARIABLE` names, a
TOML table per model -

    [models.qwen-local]
    url = "http://localhost:8081/v1"
    model = "qwen3.6-35b-a3b"
    harness = "opencode"
    harness_model = "ninfer/qwen3.6-35b-a3b"

    [models.claude]
    api = "anthropic"
    model = "claude-opus-5-5"
    traits = { cost_in = 5, cost_out = 25 }

    [tasks.chapters]
    lean = "light"

- each with the fields of :func:`medialib.lib.llmclient.build` - and the model
the single-model settings (``llmUrl`` and the rest) describe, called ``llm``,
after the file's. What each model is - :mod:`medialib.lib.llmtraits` - is
found out, or declared under ``traits``. A model's ``tasks`` keeps it to
those; a task's ``models`` gives its ladder outright, and its ``lean``
overrides the task's own, as :data:`LEAN_VARIABLE` does for every task.

:meth:`Roster.ladder` is a task's models, best first: those without what it
needs left out; then those with more of what it prefers first; then by its
lean - the cheapest, fastest, smallest first for a light one, the largest,
reasoning ones first for a precise one; and in the roster's order where all
that is even, so the order the user wrote is the last word.
"""

from __future__ import annotations

import os
import sys
import threading
import tomllib
from dataclasses import dataclass

from medialib.lib import llmclient, llmtraits, runlog, settings

__all__ = ["MODELS_VARIABLE", "LEAN_VARIABLE", "LEANS", "Task", "TASKS", "CHAPTERS",
           "CHAPTERS_INSTRUCTED", "COVER_SEARCH", "COVER_LOOK", "CONTENTS_SEARCH",
           "SUBTITLE_OCR", "VIDEO_PROFILE", "MUSIC_SEARCH", "PODCAST_ADS",
           "FRAGMENT_CLASSIFY", "GAPS_CHECK", "LETTERBOXD_MATCH", "BOOK_LANGUAGE",
           "BOOK_IMAGE_CHECK", "ERROR_TRIAGE", "COMIC_PAGE_CHECK",
           "COMIC_PDF_JUDGE", "BOOK_TAGS", "AUDIO_CONTENT", "FILM_YEAR",
           "FILM_IDENTIFY", "AMBIGUOUS_EDITIONS", "COMMENTARY_NAMES",
           "BONUS_CATEGORY", "TRACK_ROLE", "COMMENTARY_JUDGE", "SUBTITLE_ADS",
           "Roster", "RosterError", "load", "require", "optional",
           "chapters_task"]

MODELS_VARIABLE = "llmModels"
LEAN_VARIABLE = "llmLean"
MODELS_NAME = "models.toml"

LEANS = ("light", "balanced", "precise")

# The traits a task may need or prefer: llmtraits' yes-or-no ones, and web.
TRAITS = ("tools", "web", "vision", "reasoning", "images_out", "local", "cloud",
          "moe", "small", "medium", "large", "fast", "slow", "cheap")


class RosterError(llmclient.LlmError):
    """The roster cannot be read, or holds what is not a model."""


@dataclass(frozen=True)
class Task:
    """One use of a model: its name (as the roster file's ``[tasks.*]``
    calls it), what it is, the traits it cannot do without and those it
    would rather have, and its lean (:data:`LEANS`)."""

    name: str
    about: str
    requires: tuple[str, ...] = ()
    prefers: tuple[str, ...] = ()
    lean: str = "balanced"


CHAPTERS = Task("chapters", "chapter a recording from its transcript",
                prefers=("reasoning",))
CHAPTERS_INSTRUCTED = Task(
    "chapters-instructed", "chapter a recording following the user's own "
    "instructions, which may ask for something to be looked up",
    prefers=("web", "reasoning"), lean="precise")
COVER_SEARCH = Task("cover-search", "search the web for a recording's cover",
                    requires=("web",), prefers=("reasoning",))
COVER_LOOK = Task("cover-look", "look at the covers found and pick the "
                  "recording's own", requires=("vision",), lean="light")
CONTENTS_SEARCH = Task("contents-search", "search the web for a book's table "
                       "of contents, to name the chapters its files only number",
                       requires=("web",), prefers=("reasoning",))
SUBTITLE_OCR = Task("subtitle-ocr", "read a film's picture subtitles into text",
                    requires=("vision",), prefers=("local", "cheap"), lean="light")
VIDEO_PROFILE = Task(
    "video-profile", "classify a video from sampled frames to choose its "
    "encoding profile", requires=("vision",), lean="light")
MUSIC_SEARCH = Task("music-search",
                    "search the web for the MusicBrainz release of an album "
                    "beets could not match",
                    requires=("web",), prefers=("reasoning",))
PODCAST_ADS = Task("podcast-ads", "find sponsor reads, ads and self-promotion "
                   "in a podcast episode's transcript", prefers=("reasoning",),
                   lean="balanced")
FRAGMENT_CLASSIFY = Task(
    "fragment-classify",
    "classify recurring filename fragments and propose additions to data/fragments.txt",
    prefers=("reasoning", "web"),
    lean="balanced",
)
GAPS_CHECK = Task("gaps-check", "check a tree's numbered and dated runs "
                  "against real episode lists and release schedules",
                  requires=("web",), prefers=("reasoning",), lean="balanced")
LETTERBOXD_MATCH = Task(
    "letterboxd-match", "suggest matches for unmatched or ambiguous Letterboxd "
    "entries for the sync report", prefers=("reasoning", "web"), lean="balanced")
BOOK_LANGUAGE = Task("book-language", "identify a book's language from a short "
                     "excerpt", requires=(), prefers=(), lean="light")
BOOK_IMAGE_CHECK = Task("book-image-check", "tell whether an image inside an "
                        "e-book is part of the book or advertising",
                        requires=("vision",), lean="light")
ERROR_TRIAGE = Task("error-triage", "suggest a likely cause and next step for "
                    "unexplained errors in the error log",
                    prefers=("reasoning", "local"), lean="balanced")
COMIC_PAGE_CHECK = Task("comic-page-check", "check whether a small comic page "
                        "has real content worth keeping or should be dropped",
                        requires=("vision",), lean="light")
COMIC_PDF_JUDGE = Task("comic-pdf-judge", "judge whether a borderline PDF is "
                       "a comic book", requires=("vision",), lean="light")
BOOK_TAGS = Task("book-tags", "look up an audiobook's metadata tags on the web",
                 requires=("web",), prefers=("reasoning",))
AUDIO_CONTENT = Task(
    "audio-content", "classify whether a recording is a podcast, an audio "
    "drama, an audiobook or other", lean="light")
FILM_YEAR = Task("film-year", "look up the release year of an undated film, "
                 "concert, special or documentary",
                 requires=("web",), prefers=("reasoning",))
FILM_IDENTIFY = Task(
    "film-identify", "pick which offered film matches an unmatched folder",
    prefers=("web", "reasoning"), lean="precise")
AMBIGUOUS_EDITIONS = Task(
    "ambiguous-editions", "classify stray movie files and identify film "
    "cuts and editions", requires=("web",), prefers=("reasoning",))
COMMENTARY_NAMES = Task(
    "commentary-names",
    "match commentary tracks to a disc listing, name generic commentaries "
    "from opening introductions, and tell apart fitting disc pages",
    prefers=("reasoning", "web"))
BONUS_CATEGORY = Task("bonus-category", "sort an unmatched extra into its "
                      "Plex bonus folder", lean="light")
TRACK_ROLE = Task("track-role", "classify a subtitle or audio track's role "
                  "where tags and counts are borderline", lean="light")
COMMENTARY_JUDGE = Task(
    "commentary-judge",
    "judge an ambiguous audio track from its sampled transcript",
    prefers=("reasoning",))
SUBTITLE_ADS = Task("subtitle-ads", "spot advert cues in a subtitle",
                    lean="light")

TASKS = {task.name: task for task in (CHAPTERS, CHAPTERS_INSTRUCTED,
                                      COVER_SEARCH, COVER_LOOK, CONTENTS_SEARCH,
                                      SUBTITLE_OCR, VIDEO_PROFILE, MUSIC_SEARCH,
                                      PODCAST_ADS, FRAGMENT_CLASSIFY, GAPS_CHECK,
                                      LETTERBOXD_MATCH, BOOK_LANGUAGE,
                                      BOOK_IMAGE_CHECK, ERROR_TRIAGE,
                                      COMIC_PAGE_CHECK, COMIC_PDF_JUDGE,
                                      BOOK_TAGS, AUDIO_CONTENT, FILM_YEAR,
                                      FILM_IDENTIFY, AMBIGUOUS_EDITIONS,
                                      COMMENTARY_NAMES, BONUS_CATEGORY,
                                      TRACK_ROLE, COMMENTARY_JUDGE,
                                      SUBTITLE_ADS)}


def chapters_task(instructions: str) -> Task:
    """The chapter task, with the user's instructions or without."""
    return CHAPTERS_INSTRUCTED if (instructions or "").strip() else CHAPTERS


def _lean_key(llm: llmclient.Llm, lean: str) -> tuple:
    traits = llm.traits
    size = llmtraits.SIZES.index(traits.size)
    if lean == "light":
        return (not traits.cheap, traits.speed != "fast", size)
    if lean == "precise":
        return (-size, not traits.reasoning)
    return ()


class Roster:
    """The models, in the roster's order; each one's tasks, where it is kept
    to some; and the tasks' own settings."""

    def __init__(self, models: list[llmclient.Llm],
                 kept_to: dict[str, tuple[str, ...]] | None = None,
                 tasks: dict[str, dict] | None = None, lean: str = "") -> None:
        self.models = models
        self.kept_to = kept_to or {}
        self.tasks = tasks or {}
        self.lean = lean

    def named(self, name: str) -> llmclient.Llm | None:
        return next((llm for llm in self.models if llm.name == name), None)

    def lean_of(self, task: Task) -> str:
        return self.tasks.get(task.name, {}).get("lean") or self.lean or task.lean

    def ladder(self, task: Task) -> list[llmclient.Llm]:
        """The models that can do ``task``, best first (see the module)."""
        given = self.tasks.get(task.name, {}).get("models")
        if given:
            models = [llm for llm in map(self.named, given) if llm is not None]
        else:
            models = [llm for llm in self.models
                      if task.name in self.kept_to.get(llm.name, (task.name,))]
        able = [llm for llm in models if all(llm.has(trait) for trait in task.requires)]
        if given:
            return able
        lean = self.lean_of(task)
        order = {llm.name: at for at, llm in enumerate(self.models)}
        return sorted(able, key=lambda llm: (
            -sum(llm.has(trait) for trait in task.prefers),
            _lean_key(llm, lean), order[llm.name]))

    def why_none(self, task: Task) -> str:
        """Why ``task`` has no model: none set up, or none with what it needs."""
        if not self.models:
            return ("no language model is set up: %s is not set - give the "
                    "address of an OpenAI-compatible server, e.g. "
                    "http://localhost:11434/v1 for Ollama, or %s=anthropic for "
                    "Claude - and there is no roster file (%s)"
                    % (llmclient.URL_VARIABLE, llmclient.API_VARIABLE,
                       MODELS_VARIABLE))
        return "none of %s can %s - that takes %s" % (
            ", ".join(llm.name for llm in self.models), task.about,
            " and ".join(_TRAIT_WORDS.get(trait, trait) for trait in task.requires))


_TRAIT_WORDS = {"web": "web search (Claude, or a model behind llmHarness)",
                "vision": "seeing pictures", "tools": "tool calling"}


def models_path(environ=None) -> str:
    if environ is None:
        environ = os.environ
    if MODELS_VARIABLE in environ:
        return environ[MODELS_VARIABLE]
    if "XDG_CONFIG_HOME" in environ:
        return os.path.join(environ["XDG_CONFIG_HOME"], "medialib", MODELS_NAME)
    return os.environ.get(MODELS_VARIABLE) or os.path.join(
        settings.config_home(), MODELS_NAME)


def _read_file(path: str) -> tuple[list, dict, dict]:
    try:
        with open(path, "rb") as handle:
            table = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise RosterError("%s is not TOML: %s" % (path, error)) from error
    except OSError as error:
        raise RosterError("%s cannot be read: %s" % (path, error)) from error
    unknown = sorted(set(table) - {"models", "tasks"})
    if unknown:
        raise RosterError("%s has %s; it takes [models.*] and [tasks.*]"
                          % (path, ", ".join(unknown)))
    entries = table.get("models", {})
    if any("key" in entry for entry in entries.values() if isinstance(entry, dict)) \
            and settings.readable_by_others(path):
        raise RosterError("%s holds a key and other accounts on this machine can "
                          "read it - make it private first (chmod 600), or name "
                          "the variable holding it with key_env" % path)
    models, kept_to = [], {}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            raise RosterError("%s: models.%s is not a table" % (path, name))
        tasks = entry.get("tasks")
        if tasks is not None:
            _known_tasks(path, "models.%s's tasks" % name, tasks)
            kept_to[name] = tuple(tasks)
        try:
            models.append(llmclient.build(entry, name))
        except llmclient.LlmError as error:
            raise RosterError("%s: %s" % (path, error)) from error
    tasks = table.get("tasks", {})
    for name, entry in tasks.items():
        _known_tasks(path, "tasks", [name])
        if not isinstance(entry, dict) or set(entry) - {"models", "lean"}:
            raise RosterError("%s: tasks.%s takes models and lean" % (path, name))
        if entry.get("lean", "balanced") not in LEANS:
            raise RosterError("%s: tasks.%s's lean is %s, not %r" % (
                path, name, ", ".join(LEANS), entry["lean"]))
    return models, kept_to, tasks


def _known_tasks(path: str, where: str, names) -> None:
    unknown = [name for name in names if name not in TASKS]
    if unknown:
        raise RosterError("%s: %s names %s; the tasks are %s" % (
            path, where, ", ".join(map(str, unknown)), ", ".join(TASKS)))


_loaded: dict[tuple, Roster] = {}
_loaded_lock = threading.Lock()


def load(environ=None) -> Roster:
    """The roster: the file's models, then the single-model settings' one.
    RosterError (an LlmError) for a file or a setting that cannot be used.
    Read once per file and settings."""
    environ = os.environ if environ is None else environ
    path = models_path(environ)
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        stamp = None
    single = tuple((name, environ.get(name) or "")
                   for name in llmclient.SETTING_FIELDS.values())
    lean = (environ.get(LEAN_VARIABLE) or "").strip().lower()
    index = (path, stamp, single, lean)
    with _loaded_lock:
        if index in _loaded:
            return _loaded[index]
    if lean and lean not in LEANS:
        raise RosterError("%s is %r; it takes %s" % (LEAN_VARIABLE, lean,
                                                     ", ".join(LEANS)))
    models, kept_to, tasks = _read_file(path) if stamp is not None else ([], {}, {})
    if any(value.strip() for name, value in single
           if name in (llmclient.API_VARIABLE, llmclient.URL_VARIABLE,
                       llmclient.HARNESS_VARIABLE)):
        if any(llm.name == "llm" for llm in models):
            raise RosterError("%s names a model llm, which is the single-model "
                              "settings' name - call it something else" % path)
        models.append(llmclient.from_environment(environ))
    for name, entry in tasks.items():
        missing = [each for each in entry.get("models", ())
                   if each not in {llm.name for llm in models}]
        if missing:
            raise RosterError("%s: tasks.%s names %s, which it does not hold"
                              % (path, name, ", ".join(map(str, missing))))
    roster = Roster(models, kept_to, tasks, lean)
    with _loaded_lock:
        _loaded[index] = roster
    return roster


def forget() -> None:
    """Forget every roster read. For a test."""
    with _loaded_lock:
        _loaded.clear()


def require(program: str, task: Task) -> llmclient.Llm | None:
    """The model ``task`` asks, for a command that cannot do without it - or
    None after saying why there is none, or what asking it goes through is
    not here. For every command, before it does any work."""
    try:
        roster = load()
    except llmclient.LlmError as error:
        _refuse(str(error))
        return None
    ladder = roster.ladder(task)
    if not ladder:
        _refuse(roster.why_none(task))
        return None
    llm = ladder[0]
    if len(roster.models) > 1:
        runlog.detail("%s: %s (%s)" % (task.name, llm.name, llm.traits.describe()))
    purpose = "instructions are asked" if "web" in task.prefers else ""
    return llm if llmclient.preflight(llm, program, purpose) else None


def _refuse(why: str) -> None:
    sys.stderr.write("\nError: no language model to ask: %s.\n"
                     "See the llm settings in docs/settings.md, and docs/models.md.\n"
                     "Nothing was changed.\n" % why)


def optional(task: Task, environ=None) -> tuple[list[llmclient.Llm], str]:
    """The models ``task`` may ask, best first, for a step that uses one only
    where there is one - and, where there is none, why. Says nothing: the
    step decides whether its not being there is worth a line."""
    try:
        roster = load(environ)
    except llmclient.LlmError as error:
        return [], str(error)
    ladder = [llm for llm in roster.ladder(task) if llmclient.usable(llm)]
    return ladder, "" if ladder else roster.why_none(task)
