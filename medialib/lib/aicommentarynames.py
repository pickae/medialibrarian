"""Asking a language model when deterministic commentary naming cannot be sure.

Two places in ``ingest-movies`` refuse whenever their strict rules leave more
than one possibility:

1. :func:`medialib.lib.commentarynames.decide` matches each commentary track's
   opening minutes against a disc's listed commentaries using capitalised
   words and English introduction phrases, and requires one unique pairing.
   Non-English introductions, informal openings, role-only listings ("Cast
   commentary" vs "Crew commentary"), or disagreeing regional releases leave
   tracks numbered. When that happens and every track has a transcript, a
   model is shown each track's opening ~10 minutes and the candidate listings
   from dvdcompare.net and picks which candidate listing and which permutation
   of its entries belongs to the tracks, or answers unsure.
2. :func:`medialib.lib.dvdcompare.pick` refuses whenever two or more database
   pages fit a film's title, year and disc kind. A model is given the film's
   title, year, disc format, TMDb id and alternative titles along with those
   fitting pages, and chooses the ``fid`` of one of them, or answers unsure.

Both choices are strictly guarded: the model may only select among the
candidate listings and track permutations offered, or among the fitting page
ids offered. It never invents names or pages. Any unsure, malformed or
out-of-range answer leaves the existing fallback in place.

The task in the roster (:mod:`medialib.lib.llmroster`) is
:data:`llmroster.COMMENTARY_NAMES`, which prefers reasoning and web search.
The :data:`SWITCH_VARIABLE` setting turns it off. What the model was asked is
counted (:mod:`medialib.lib.modelmeter`), said in the run's output and report
file, and accumulated for the run's total.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

from medialib.lib import (
    commentarynames,
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
    "MAX_TRANSCRIPT_CHARS",
    "MAX_ALIASES",
    "TRACKS_SYSTEM",
    "PAGE_SYSTEM",
    "INTRO_SYSTEM",
    "Usage",
    "enabled",
    "available",
    "tracks_prompt",
    "parse_tracks_answer",
    "match_tracks",
    "page_prompt",
    "parse_page_answer",
    "pick_page",
    "intro_prompt",
    "parse_intro_answer",
    "name_from_intro",
]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "commentaryNamesLlm"

# What the model's requests are for, which names harness sessions and meter lines.
PURPOSE = "commentary"

# How many characters of each track's opening transcript are sent to the model.
MAX_TRANSCRIPT_CHARS = 12000

# How many alternative titles are listed when disambiguating a disc page.
MAX_ALIASES = 30

TRACKS_SYSTEM = """You match numbered audio commentary tracks of a film in a \
personal movie library to the disc's listed commentaries from dvdcompare.net.

You are given the film's title, one or more candidate listings from disc \
releases (each with numbered entries), and the opening ~10 minutes of \
transcript from each commentary audio track (which may be in any language, \
not only English). A disc's audio streams do not necessarily match the menu \
order shown in a listing.

Your job is to choose which candidate listing matches the disc and assign \
each commentary track to a distinct entry in that listing. Use everything \
the transcripts reveal: speakers introducing themselves in any language, \
names mentioned by co-hosts, roles they speak from (director, actors, \
cinematographer, historian, critic, etc.), and process of elimination when \
only one listing is offered and one track is silent or anonymous.

A wrong name on a commentary track is worse than keeping its number. You \
MUST choose from the offered candidate listings and assign every entry 1..N \
exactly once - never invent new titles or listings. If you are not sure \
which listing or which track is which, answer unsure.

Answer with JSON only, in one of these two forms:
{"listing": <listing number 1..M>, \
"tracks": [<entry number for track 1>, <entry number for track 2>, ...], \
"why": "<one sentence explaining the match>"}
or, when not sure:
{"unsure": true, "why": "<one sentence explaining why>"}"""

PAGE_SYSTEM = """You choose which dvdcompare.net database page belongs to a \
specific film when two or more pages fit the film's title, year and disc \
format.

You are given what is known about the film (its title, year, disc format, \
and when known its TMDb id, IMDb id and alternative titles) and the fitting \
dvdcompare.net candidate pages (each with its page id fid, listed names/AKAs, \
disc format and year). If you have web search, you may verify which page \
corresponds to this film.

A wrong disc page is worse than none. You MUST choose one of the offered fid \
values - never invent a page id. If you are not sure which page is this \
film, answer with null.

Answer with JSON only:
{"fid": "<chosen fid>", "why": "<one sentence>"}
or, when not sure:
{"fid": null, "why": "<one sentence>"}"""

INTRO_SYSTEM = """You name a generically labelled film commentary track \
(such as "Commentary" or "Commentary 1") from the speaker introductions \
heard in its first 5 minutes.

You are given the film's title and the first ~5 minutes of text from the \
commentary track (taken directly from its subtitles or transcribed from its \
audio, in any language). Look for typical speaker introductions ("Hi, I'm \
director ...", "Welcome to the commentary with ...", "This is ... joined \
by ...", "Hallo, ich bin ...", etc.).

Rules:
- Name the track strictly after who introduces themselves in the opening \
text. Never guess people who are not introduced or named there.
- Keep the name concise and clean, in the standard form "Commentary by ..." \
or "Commentary with ..." (for example "Commentary with director John \
Carpenter and Kurt Russell" or "Commentary by film historian Jane Doe").
- Set "confidence" to "high" ONLY when one or more speakers clearly \
introduce themselves by name (or explicit role such as director, writer, \
producer, cast, historian, moderated by ...). If nobody clearly introduces \
themselves, set "name" to null and "confidence" to "low".
- Never return a generic label like "Commentary" or "Commentary 1".

Answer with JSON only:
{"name": "<concise commentary title or null>", \
"confidence": "high|medium|low", \
"why": "<one sentence citing the speaker introduction>"}"""

_log = runlog.log
_detail = runlog.detail


class Usage:
    """What the commentary-naming model was asked, over one film or a whole
    run: the requests it answered and the tokens their replies report
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
        return modelmeter.rate_line(PURPOSE, self.seconds, self.tally)


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def available(models: Sequence[llmclient.Llm] | None = None) -> bool:
    """Whether the commentary-naming model is enabled and configured.

    Callers check this before doing expensive work (such as extracting
    embedded subtitles or transcribing the first 5 minutes of audio) that is
    only used to ask the model.
    """
    if not enabled():
        return False
    if models is not None:
        return bool(models)
    resolved, _why = llmroster.optional(llmroster.COMMENTARY_NAMES)
    return bool(resolved)


def _ask(models: Sequence[llmclient.Llm] | None, system: str, request: str,
         *, usage: Usage | None = None, meter_file: str = "",
         log: Callable[[str], None] = _log) -> tuple[str, str]:
    """Ask the first usable model on the ladder, using web search when the
    model supports it and direct serving otherwise. Returns
    ``(answer_text, model_name)``."""
    if not enabled():
        _detail("  No model asked for commentary naming: the %s setting is off"
                % SWITCH_VARIABLE)
        return "", ""
    if models is None:
        models, why = llmroster.optional(llmroster.COMMENTARY_NAMES)
        if not models:
            _detail("  No model asked for commentary naming: %s" % why)
            return "", ""
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    answered_by = ""
    try:
        for llm in models:
            try:
                searcher = llm.searching(PURPOSE)
                if searcher is not None:
                    with spans.timing():
                        answer = searcher.ask(system, request, tally)
                    answered_by = llm.name
                    break
                with llm.serving(_detail) as bound, spans.timing():
                    answer = bound.ask(system, request, tally)
                answered_by = llm.name
                break
            except llmclient.LlmError as error:
                log("  No commentary naming answer from %s: %s"
                    % (llm.name, error))
    finally:
        if tally.requests:
            log("  " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
            if usage is not None:
                usage.tally.merge(tally)
                usage.spans.merge(spans)
            if meter_file:
                modelmeter.record(meter_file, spans, tally)
    return answer, answered_by


# --- matching commentary tracks to candidate listings -------------------------

def tracks_prompt(
        tracks: Sequence[commentarynames.FileCommentary],
        candidate_lists: Sequence[tuple[tuple[str, ...], Sequence[str]]],
        openings: Sequence[str], title: str = "", *, year: str = "",
        disc_kind: str = "") -> str:
    """What the model is told about the film's numbered commentary tracks,
    their opening transcripts, and the candidate disc listings."""
    film = title or "(unknown title)"
    if year:
        film += " (%s)" % year
    if disc_kind:
        film += " [%s]" % disc_kind

    listing_blocks: list[str] = []
    for index, (entries, releases) in enumerate(candidate_lists, start=1):
        releases_note = (", ".join(releases) if releases else "unnamed release")
        lines = ["Listing %d (%s):" % (index, releases_note)]
        for entry_num, description in enumerate(entries, start=1):
            lines.append("  %d. %s" % (entry_num, description))
        listing_blocks.append("\n".join(lines))

    track_blocks: list[str] = []
    for number, (track, text) in enumerate(
            zip(tracks, openings, strict=True), start=1):
        snippet = text.strip()
        if len(snippet) > MAX_TRANSCRIPT_CHARS:
            snippet = snippet[:MAX_TRANSCRIPT_CHARS].rstrip() + "\n[...]"
        if not snippet:
            snippet = "(no speech in the first 10 minutes)"
        track_blocks.append(
            "Track %d (current name: %s):\n%s" % (number, track.name, snippet))

    sections = [
        "Match each commentary track of %s to one entry of a candidate listing."
        % film,
        "## Candidate listings\n\n" + "\n\n".join(listing_blocks),
        "## Commentary tracks (first ~10 minutes of each transcript)\n\n"
        + "\n\n".join(track_blocks),
        "Choose listing 1..%d and give the entry number (1..%d) for each of "
        "Track 1..%d in order, using every entry once - or answer unsure."
        % (len(candidate_lists), len(tracks), len(tracks)),
    ]
    return "\n\n".join(sections) + "\n"


def parse_tracks_answer(
        answer: str, num_listings: int,
        track_count: int) -> tuple[int | None, tuple[int, ...] | None, str, bool]:
    """Read and validate the model's track-matching answer.

    Returns ``(listing_index, order, why, unsure)`` where ``listing_index`` is
    0-based into the candidate listings and ``order`` is a permutation of
    ``range(track_count)`` in :func:`commentarynames._proved_order`'s shape
    (the i-th listed entry sits on track ``order[i]``). Returns
    ``(None, None, why, unsure)`` when the model answered unsure or its answer
    fails validation.
    """
    verdict = llmclient.first_json(answer)
    if not isinstance(verdict, dict):
        return None, None, "answer contained no JSON object", False
    why = " ".join(str(verdict.get("why") or "").split())
    if verdict.get("unsure"):
        return None, None, why, True

    raw_listing = verdict.get("listing")
    raw_tracks = verdict.get("tracks")
    if raw_listing == "unsure" or raw_tracks == "unsure":
        return None, None, why, True
    if raw_listing is None and raw_tracks is None:
        return None, None, why, True

    if raw_listing is None and num_listings == 1:
        listing_num = 1
    elif type(raw_listing) is int and 1 <= raw_listing <= num_listings:
        listing_num = raw_listing
    else:
        return None, None, "invalid listing choice %r" % (raw_listing,), False

    if raw_tracks is None and track_count == 1:
        raw_tracks = [1]
    if (not isinstance(raw_tracks, list) or len(raw_tracks) != track_count
            or not all(type(item) is int for item in raw_tracks)
            or sorted(raw_tracks) != list(range(1, track_count + 1))):
        return None, None, "invalid track mapping %r" % (raw_tracks,), False

    order_list = [0] * track_count
    for track_idx, entry_num in enumerate(raw_tracks):
        order_list[entry_num - 1] = track_idx
    return listing_num - 1, tuple(order_list), why, False


def match_tracks(
        tracks: Sequence[commentarynames.FileCommentary],
        candidate_lists: Sequence[tuple[tuple[str, ...], Sequence[str]]],
        transcripts: Sequence[str], title: str = "", *, year: str = "",
        disc_kind: str = "", models: Sequence[llmclient.Llm] | None = None,
        usage: Usage | None = None, meter_file: str = "",
        log: Callable[[str], None] = _log,
        ) -> tuple[int | None, tuple[int, ...] | None, str]:
    """Ask a model which candidate listing and track permutation match the
    commentary transcripts' first ~10 minutes.

    Returns ``(listing_index, order, note)``, or ``(None, None, note)`` when
    no model was asked, the model was unsure, or its answer did not pass the
    guard. ``note`` describes the model's decision for the run output and
    report file (empty when no model answered).
    """
    if not tracks or not candidate_lists or len(transcripts) != len(tracks):
        return None, None, ""
    openings = [commentarynames.opening_text(text) for text in transcripts]
    if not any(openings):
        return None, None, ""
    request = tracks_prompt(tracks, candidate_lists, openings, title,
                            year=year, disc_kind=disc_kind)
    answer, answered_by = _ask(models, TRACKS_SYSTEM, request,
                               usage=usage, meter_file=meter_file, log=log)
    if not answer:
        return None, None, ""
    listing_idx, order, why, unsure = parse_tracks_answer(
        answer, len(candidate_lists), len(tracks))
    if listing_idx is None or order is None:
        if unsure:
            note = "model %s was unsure%s" % (
                answered_by, ": " + why if why else "")
        else:
            note = "model %s gave an unusable track mapping%s" % (
                answered_by, ": " + why if why else "")
        return None, None, note
    note = "matched by model %s%s" % (
        answered_by, ": " + why if why else "")
    return listing_idx, order, note


# --- disambiguating fitting dvdcompare.net pages -----------------------------

def page_prompt(
        fitting: Sequence[dvdcompare.SearchEntry], title: str, year: str,
        kind: str, *, tmdb_id: str = "", imdb_id: str = "",
        aliases: Sequence[str] = ()) -> str:
    """What the model is told about the film and the dvdcompare.net pages
    that all fit its title, year and disc kind."""
    about = [
        "Title: %s" % title,
        "Year: %s" % (year or "(unknown)"),
        "Disc format: %s" % kind,
    ]
    if tmdb_id:
        about.append("TMDb id: %s" % tmdb_id)
    if imdb_id:
        about.append("IMDb id: %s" % imdb_id)
    cleaned_aliases = [name for name in aliases if name][:MAX_ALIASES]
    if cleaned_aliases:
        about.append("Alternative titles: %s" % "; ".join(cleaned_aliases))

    pages = []
    for entry in fitting:
        pages.append("- fid=%s: %s (%s, %s)" % (
            entry.fid, " AKA ".join(entry.names), entry.kind, entry.year))

    sections = [
        "Which dvdcompare.net page below is for this film?",
        "## The film\n\n" + "\n".join(about),
        "## Fitting dvdcompare.net pages\n\n" + "\n".join(pages),
        "Answer with the fid of the one page that is this film, or null if "
        "unsure.",
    ]
    return "\n\n".join(sections) + "\n"


def parse_page_answer(answer: str,
                      allowed_fids: Sequence[str]) -> tuple[str, str, bool]:
    """Read and validate the model's disc-page pick.

    Returns ``(fid, why, unsure)``. ``fid`` is guaranteed to be one of
    ``allowed_fids`` or ``""``.
    """
    verdict = llmclient.first_json(answer)
    if not isinstance(verdict, dict):
        return "", "answer contained no JSON object", False
    why = " ".join(str(verdict.get("why") or "").split())
    if verdict.get("unsure"):
        return "", why, True
    raw_fid = verdict.get("fid")
    if raw_fid is None:
        return "", why, True
    if type(raw_fid) is int:
        fid = str(raw_fid)
    elif isinstance(raw_fid, str):
        fid = raw_fid.strip()
    else:
        return "", "invalid page id %r" % (raw_fid,), False
    if not fid or fid.lower() == "unsure":
        return "", why, True
    if fid not in set(allowed_fids):
        return "", "chose fid %r, which is not among the fitting pages" % (fid,), False
    return fid, why, False


def pick_page(
        fitting: Sequence[dvdcompare.SearchEntry], title: str, year: str,
        kind: str, *, tmdb_id: str = "", imdb_id: str = "",
        aliases: Sequence[str] = (),
        models: Sequence[llmclient.Llm] | None = None,
        usage: Usage | None = None, meter_file: str = "",
        log: Callable[[str], None] = _log) -> tuple[str, str]:
    """Ask a model which of the ``fitting`` dvdcompare.net pages is this film.

    Returns ``(fid, note)`` where ``fid`` is one of the fitting pages' fids or
    ``""``, and ``note`` describes the model's decision for the run output and
    report file (empty when no model answered).
    """
    if len(fitting) < 2:
        return (fitting[0].fid if len(fitting) == 1 else ""), ""
    request = page_prompt(fitting, title, year, kind, tmdb_id=tmdb_id,
                          imdb_id=imdb_id, aliases=aliases)
    answer, answered_by = _ask(models, PAGE_SYSTEM, request,
                               usage=usage, meter_file=meter_file, log=log)
    if not answer:
        return "", ""
    fid, why, unsure = parse_page_answer(answer, [entry.fid for entry in fitting])
    if not fid:
        if unsure:
            note = "disc page pick: model %s was unsure%s" % (
                answered_by, ": " + why if why else "")
        else:
            note = "disc page pick: model %s gave an unusable answer%s" % (
                answered_by, ": " + why if why else "")
        return "", note
    chosen = next(entry for entry in fitting if entry.fid == fid)
    label = "%s (%s)" % (" / ".join(chosen.names), chosen.year)
    note = "disc page picked by model %s: %s (fid=%s)%s" % (
        answered_by, label, fid, ": " + why if why else "")
    return fid, note


# --- naming a generic commentary directly from its opening 5 minutes ---------

_MAX_INTRO_TITLE_CHARS = 140


def intro_prompt(opening: str, *, title: str = "", year: str = "",
                 track_name: str = "") -> str:
    """What the model is told about one generically named commentary track and
    its first 5 minutes of subtitles or transcribed speech."""
    film = title or "(unknown title)"
    if year:
        film += " (%s)" % year
    snippet = opening.strip()
    if len(snippet) > MAX_TRANSCRIPT_CHARS:
        snippet = snippet[:MAX_TRANSCRIPT_CHARS].rstrip() + "\n[...]"
    sections = [
        "Name this commentary track of %s from any speaker introductions "
        "in its first 5 minutes." % film,
        "Current track label: %s" % (track_name or "Commentary"),
        "## First ~5 minutes of commentary text\n\n" + snippet,
        "If speakers clearly introduce themselves, return a concise "
        'commentary title with confidence "high"; otherwise return null.',
    ]
    return "\n\n".join(sections) + "\n"


def parse_intro_answer(answer: str) -> tuple[str, str, bool]:
    """Read and validate the model's opening-introduction track name.

    Returns ``(title, why, unsure)``. Accepts only a ``"high"`` confidence
    answer whose normalized title is non-generic and within length bounds.
    """
    verdict = llmclient.first_json(answer)
    if not isinstance(verdict, dict):
        return "", "answer contained no JSON object", False
    why = " ".join(str(verdict.get("why") or "").split())
    if verdict.get("unsure"):
        return "", why, True
    raw_name = verdict.get("name")
    if raw_name is None:
        return "", why, True
    if not isinstance(raw_name, str):
        return "", "invalid commentary name %r" % (raw_name,), False
    cleaned = " ".join(raw_name.split()).strip()
    if not cleaned or cleaned.lower() == "unsure":
        return "", why, True
    confidence = shell_lower(str(verdict.get("confidence") or "").strip())
    if confidence != "high":
        return "", why or ("confidence was %s" % (confidence or "missing")), True
    titled = commentarynames.track_title(cleaned)
    if not titled or commentarynames.generic_number(titled) is not None:
        return "", "returned generic title %r" % (cleaned,), False
    if len(titled) > _MAX_INTRO_TITLE_CHARS:
        return "", "returned an overlong title (%d chars)" % len(titled), False
    return titled, why, False


def name_from_intro(
        text: str, *, title: str = "", year: str = "", track_name: str = "",
        models: Sequence[llmclient.Llm] | None = None,
        usage: Usage | None = None, meter_file: str = "",
        log: Callable[[str], None] = _log) -> tuple[str, str]:
    """Ask a model to name one generically named commentary track from speaker
    introductions in its first 5 minutes of subtitles or speech transcript.

    Returns ``(resolved_name, note)`` where ``resolved_name`` is ``""`` when
    no model was asked, no clear introduction was found, or the answer did not
    pass the guard.
    """
    opening = commentarynames.opening_subtitle_text(
        text, limit=commentarynames.INTRO_SAMPLE_SECONDS)
    if not opening:
        return "", ""
    request = intro_prompt(opening, title=title, year=year,
                           track_name=track_name)
    answer, answered_by = _ask(models, INTRO_SYSTEM, request,
                               usage=usage, meter_file=meter_file, log=log)
    if not answer:
        return "", ""
    titled, why, unsure = parse_intro_answer(answer)
    if not titled:
        if unsure:
            note = "opening intro: model %s found no clear introduction%s" % (
                answered_by, ": " + why if why else "")
        else:
            note = "opening intro: model %s gave an unusable name%s" % (
                answered_by, ": " + why if why else "")
        return "", note
    note = "named from opening 5 min by model %s%s" % (
        answered_by, ": " + why if why else "")
    return titled, note
