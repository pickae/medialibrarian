"""Asking a light language model which Plex bonus folder an unmatched extra
belongs in: the optional fallback after ``ingest-movies``'s keyword rules find
no match.

The deterministic keyword table handles the clear cases first. Only when no
keyword matches - names like "Photo Gallery", "Storyboard Comparison" or
"Making the Music" - is a light model (:data:`llmroster.BONUS_CATEGORY`) asked
to pick an exact category name from the Plex bonus folders, or ``"none"`` to
leave the file in ``Featurettes``.

The model is given the extra's file name, the film's title and year, and - when
already cached on disk under ``data/dvdcompare`` - the disc's extras listing
from :mod:`medialib.lib.dvdcompare`. It never forces a network scrape. Only an
exact known category returned with high confidence moves the file; anything
else (``"none"``, low confidence, an unknown name, bad JSON, an error, the
setting off, or no model set up) leaves the file where it is.

What the model was asked is counted (:mod:`medialib.lib.modelmeter`), logged,
and recorded for the run's total.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from medialib.lib import (
    dvdcompare,
    llmclient,
    llmroster,
    modelmeter,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = [
    "SWITCH_VARIABLE",
    "PURPOSE",
    "CATEGORIES",
    "SYSTEM",
    "enabled",
    "prompt",
    "parse_verdict",
    "classify",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "bonusCategoryLlm"

# What the model's requests are counted under in modelmeter.
PURPOSE = "bonus"

# The Plex bonus subfolders an extra may be moved into from Featurettes.
CATEGORIES = (
    "Behind The Scenes",
    "Deleted Scenes",
    "Interviews",
    "Shorts",
    "Trailers",
    "Scenes",
    "Other",
)

# Cap on cached dvdcompare lines handed to the model so large multi-disc sets
# stay compact.
MAX_EXTRAS_LINES = 80
MAX_EXTRA_LINE_CHARS = 200

SYSTEM = """You sort movie bonus extras into the Plex bonus folder they \
belong in.

The extra currently sits in the film's Featurettes folder. Pick one of the \
allowed Plex bonus folders ONLY when you are clearly sure it belongs there; \
otherwise answer "none" so it stays in Featurettes. A wrong folder is worse \
than leaving it in Featurettes.

What each folder holds:
- Behind The Scenes: making-of documentaries, production/set/VFX/music/score/\
costume/storyboard breakdowns and comparisons
- Deleted Scenes: deleted, extended, unused, or alternate scenes or endings
- Interviews: interviews, Q&As, cast/crew conversations, panels, press \
conferences
- Shorts: standalone short films or animated shorts
- Trailers: trailers, teasers, previews, TV spots, promos
- Scenes: bloopers, gag reels, outtakes, screen tests, selected scenes
- Other: photo or still galleries, trivia, reviews, essays, miscellaneous \
extras that fit none of the folders above
- none: a general featurette or anything you are unsure about (stays in \
Featurettes)

Answer with JSON only, in this form:
{"category": "<exact folder name from the list above, or none>", \
"confidence": "high" | "medium" | "low", \
"why": "<one short sentence>"}"""

_log = runlog.log
_detail = runlog.detail


def enabled() -> bool:
    """Whether a run asks the model to sort unmatched extras: unless the
    setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def prompt(extra_name: str, film_title: str = "", film_year: str = "",
           disc_extras: Sequence[str] = (),
           categories: Sequence[str] = CATEGORIES) -> str:
    """What the model is told about one unmatched extra."""
    stem = os.path.splitext(extra_name)[0]
    film = film_title.strip()
    if film and film_year.strip():
        film = "%s (%s)" % (film, film_year.strip())
    elif not film:
        film = "(unknown)"

    sections = [
        "Sort this bonus extra into one of the allowed Plex bonus folders, "
        'or "none" to leave it in Featurettes.',
        "Film: %s\nExtra file: %s\nExtra title: %s" % (film, extra_name, stem),
        "Allowed categories: %s" % ", ".join(categories),
    ]
    cleaned = [" ".join(line.split())[:MAX_EXTRA_LINE_CHARS]
               for line in disc_extras if line and line.strip()]
    if cleaned:
        shown = cleaned[:MAX_EXTRAS_LINES]
        listing = "\n".join("- " + line for line in shown)
        if len(cleaned) > len(shown):
            listing += "\n- ... and %d more line(s)" % (len(cleaned) - len(shown))
        sections.append("Cached disc extras listing (dvdcompare):\n" + listing)
    return "\n\n".join(sections) + "\n"


def parse_verdict(answer: str,
                  categories: Sequence[str] = CATEGORIES) -> tuple[str, str]:
    """The chosen category and reason from the model's answer, or ("", why).

    Only an exact match in ``categories`` with ``confidence == "high"`` is
    accepted. ``"none"``, ``"unsure"``, ``"Featurettes"``, lower confidence,
    unknown categories, and malformed replies all return ``""``.
    """
    verdict = llmclient.first_json(answer)
    if not isinstance(verdict, dict):
        return "", ""
    why = " ".join(str(verdict.get("why") or "").split())
    raw = verdict.get("category")
    if not isinstance(raw, str):
        return "", why
    choice = raw.strip()
    if shell_lower(choice) in ("", "none", "unsure", "featurettes"):
        return "", why
    if choice not in categories:
        return "", why
    confidence = shell_lower(str(verdict.get("confidence") or "").strip())
    if confidence != "high":
        return "", why
    return choice, why


def classify(extra_name: str, *, film_title: str = "", film_year: str = "",
             disc_extras: Sequence[str] = (), dvdcompare_dir: str = "",
             categories: Sequence[str] = CATEGORIES,
             models: Sequence[llmclient.Llm] | None = None,
             meter_file: str = "") -> str:
    """Which Plex bonus folder a light model places ``extra_name`` in, or ""
    to leave it in Featurettes.

    ``models`` is the roster's ladder for :data:`llmroster.BONUS_CATEGORY`
    unless given. ``disc_extras`` is used as given when non-empty; otherwise,
    when ``dvdcompare_dir`` is set, cached extras are read from disk there
    without contacting the network. Every decision is logged, and usage is
    recorded into ``meter_file`` when given.
    """
    if not enabled():
        _detail("  No model asked for bonus category of %s: the %s setting is off"
                % (extra_name, SWITCH_VARIABLE))
        return ""
    if models is None:
        models, why_none = llmroster.optional(llmroster.BONUS_CATEGORY)
        if not models:
            _detail("  No model asked for bonus category of %s: %s"
                    % (extra_name, why_none))
            return ""
    elif not models:
        return ""

    extras = list(disc_extras)
    if not extras and dvdcompare_dir and film_title and film_year:
        extras = dvdcompare.cached_extras(dvdcompare_dir, film_title, film_year)

    request = prompt(extra_name, film_title, film_year, extras, categories)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    answered_by = ""
    try:
        for llm in models:
            try:
                with llm.serving(_detail) as bound, spans.timing():
                    answer = bound.ask(SYSTEM, request, tally)
                answered_by = llm.name
                break
            except llmclient.LlmError as error:
                _log("  No bonus category from %s for %s: %s"
                     % (llm.name, extra_name, error))
    finally:
        if tally.requests:
            _detail("  " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)

    if not answer:
        return ""

    category, why = parse_verdict(answer, categories)
    reason = " (%s)" % why if why else ""
    if category:
        _log("  Bonus category (%s): %s -> %s%s"
             % (answered_by, extra_name, category, reason))
        return category

    _log("  Bonus category (%s): %s -> left in Featurettes%s"
         % (answered_by, extra_name, reason))
    return ""
