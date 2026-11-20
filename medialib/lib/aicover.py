"""Asking a language model that can search the web for a book's cover: the
last rung of concat-audio's cover ladder, below the catalogue search of
:mod:`medialib.lib.onlinecover`.

The catalogue search asks fixed sites with a fixed query and takes only an
answer that is plainly the same title. What it misses - a title spelled
another way, an edition in another language, a show whose artwork lives on
its own site - a model that can search and read pages can still find. So
where that search came up empty, the model is told everything the run knows
about the book: the folder as it lies, every file's name, the tags of its
tracks, the name the book is written out under, what the book probably is and
why; and what a good cover is. It answers with addresses of images, the best
first, which are downloaded here and taken only when they are images.

Two tasks of the roster (:mod:`medialib.lib.llmroster`) do it. The search,
:data:`llmroster.COVER_SEARCH`, needs a model that can search the web -
Claude with its own search, any other model through an agent harness - since
one that cannot could only guess an address; its ladder is tried in order
until one answers. The look, :data:`llmroster.COVER_LOOK`, needs a model that
sees pictures, and leans light: the first few images the search named are
shown to it, with the same account of the book, and it says which - if any -
is plausibly this recording's cover. The search names pages it read, the look
checks the picture itself; the two may well be different models. Where no
model sees, the search's own first is taken. The ``coverSearchLlm`` setting
turns this rung off alone; ``coverSearch`` off turns off every online rung,
this one too.

What the model was asked is counted (:mod:`medialib.lib.modelmeter`), said
under the book, and recorded for the run's total.
"""

from __future__ import annotations

import os
import subprocess
import urllib.parse
from collections.abc import Callable, Sequence

from medialib.lib import (
    formatting,
    imagecodecs,
    imagemagick,
    llmclient,
    llmroster,
    modelmeter,
    onlinecover,
    runlog,
)
from medialib.lib.enums import shell_lower

__all__ = ["SWITCH_VARIABLE", "PURPOSE", "enabled", "prompt", "look_prompt",
           "book_cover", "candidates"]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "coverSearchLlm"

# What the model's requests are for, which names a harness's sessions.
PURPOSE = "cover"

# How much of a folder is listed for the model, and how many tracks' tags.
MAX_LISTED_FILES = 200
MAX_TAGGED_TRACKS = 6
MAX_TAG_CHARS = 300

# How many of the addresses it answers with are tried.
MAX_IMAGES = 6

# How many of those that download are candidates a model that sees pictures
# is shown, and how large each is shown: enough to read a cover's title.
MAX_LOOKED_AT = 4
LOOK_GEOMETRY = "1024x1024"

# What the look at the candidates is counted as.
LOOK_PURPOSE = "look"

LOOK_SYSTEM = ("You judge cover images for a personal audio library. You are "
               "shown pictures found on the web for one recording and say "
               "which, if any, is that recording's cover. A wrong cover is "
               "worse than none.")

_log = runlog.log
_detail = runlog.detail

Download = Callable[[str, str], bool]

_CONTENT = {
    "podcast": "probably a podcast or a podcast episode",
    "drama": "probably an audio drama with a full cast",
    "audiobook": "probably an audiobook",
    "other": "unclear - perhaps music, a short story or a lecture",
}

SYSTEM = """You find cover images for a personal audio library. You can search \
the web and read pages. You are given what is known about one recording that \
has no cover yet, and you answer with the addresses of images that are its \
cover.

What makes a good cover, most important first:
1. It is the cover of THIS recording - the same title, the same author or \
show, the same part of a series. A wrong cover is worse than none: give no \
image rather than one you are not sure of.
2. The edition in the recording's own language, judged from its names, tags \
and words - not a translation's cover.
3. For an audiobook, the audiobook edition's cover (Audible, the publisher's \
audio label, a library's audiobook record) over the printed book's; the \
printed book's cover when no audiobook cover exists. For a podcast, the \
episode's own image over the show's artwork; the show's artwork when the \
episode has none. For an audio drama, the release's own cover.
4. High resolution: the largest version a site offers (at least 500 pixels \
on the short side where there is one), square or portrait, without \
watermarks, stickers, borders or a store's overlay.
5. Any image format: jpg, png, webp and avif are all converted later.

Each address must be the image file itself - one a plain HTTP GET downloads \
as an image, without a login or cookies - not the page it is shown on. \
Prefer the site's original over a thumbnail-sized copy (strip size tokens \
from the address where the site serves the original that way).

Answer with JSON only, in this form:
{"images": [{"url": "https://...", "page": "the page you found it on", \
"title": "the title as that page gives it", "language": "its language", \
"edition": "audiobook, book, podcast, episode or drama", \
"why": "one sentence on why it is this recording"}]}
Best first, at most %d. {"images": []} when you found nothing you are sure \
of.""" % MAX_IMAGES


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def _listing(input_path: str) -> list[str]:
    """Every file below ``input_path``, relative, with its size, folders
    first-come as they sort; cut at :data:`MAX_LISTED_FILES`."""
    lines: list[str] = []
    total = 0
    for parent, dirs, names in os.walk(input_path):
        dirs.sort()
        for name in sorted(names):
            total += 1
            if len(lines) >= MAX_LISTED_FILES:
                continue
            path = os.path.join(parent, name)
            try:
                size = formatting.fmt_bytes(os.path.getsize(path))
            except OSError:
                size = "?"
            lines.append("%s (%s)" % (
                os.path.relpath(path, input_path).replace(os.sep, "/"), size))
    if total > len(lines):
        lines.append("... and %d more file(s)" % (total - len(lines)))
    return lines


def _sampled(tracks: Sequence[str]) -> list[str]:
    """Up to :data:`MAX_TAGGED_TRACKS` of the tracks, spread over them: the
    first ones and the last, where a series' parts differ."""
    if len(tracks) <= MAX_TAGGED_TRACKS:
        return list(tracks)
    head = MAX_TAGGED_TRACKS - 1
    return [*tracks[:head], tracks[-1]]


def _tag_lines(input_path: str, tracks: Sequence[str],
               probe: Callable[[str], dict]) -> list[str]:
    lines: list[str] = []
    for track in _sampled(tracks):
        tags = {key: " ".join(str(value).split())[:MAX_TAG_CHARS]
                for key, value in probe(track).items() if str(value).strip()}
        name = os.path.relpath(track, input_path).replace(os.sep, "/")
        if not tags:
            lines.append("%s: no tags" % name)
            continue
        lines.append("%s:" % name)
        lines.extend("  %s: %s" % (key, value) for key, value in sorted(tags.items()))
    return lines


def prompt(input_path: str, output_name: str, tracks: Sequence[str],
           subject: onlinecover.Subject,
           probe: Callable[[str], dict] = onlinecover.probe_tags) -> str:
    """What the model is told about the book joined from ``input_path``."""
    folder = os.path.normpath(input_path)
    parents = [part for part in folder.replace(os.sep, "/").split("/")[-4:-1] if part]
    content = _CONTENT.get(subject.content, subject.content)
    if subject.why:
        content += ", because of %s" % subject.why
    tried = onlinecover.providers_for(subject.content)
    sections = [
        "Find the cover image for this recording.",
        "## The input folder\n\nIts name: %s\nThe folders it sits in, outermost "
        "first: %s\n\nIts files:\n%s" % (
            os.path.basename(folder), " / ".join(parents) or "(none)",
            "\n".join("- " + line for line in _listing(input_path))),
        "## The tags inside the audio files\n\n%s" % (
            "\n".join(_tag_lines(input_path, tracks, probe)) or "(no audio files)"),
        "## The output\n\nThe tracks are joined into one file named: %s\n"
        "(the folder's name, cleaned)" % output_name,
        "## What it probably is\n\nProvisionally, and not certainly: %s.%s\n"
        "Titles it may go by, likeliest first: %s\nWho may have made it: %s\n"
        "If what you find says otherwise, trust what you find." % (
            content,
            " Running time: %s." % formatting.fmt_clock(subject.seconds)
            if subject.seconds > 0 else "",
            "; ".join(subject.titles) or "(none)",
            "; ".join(subject.creators) or "(none)"),
        "## Already tried\n\nA search of %s by these titles found no answer "
        "sure enough. Look further: other spellings, the edition in the "
        "recording's own language, the publisher's or the show's own site." % (
            ", ".join(tried) or "the usual catalogues"),
    ]
    return "\n\n".join(sections) + "\n"


def candidates(answer: str) -> list[dict]:
    """The images the model answered with, as it ranked them: each a dict
    with at least an http(s) ``url``."""
    found = llmclient.first_json(answer)
    if isinstance(found, dict):
        found = found.get("images")
    if not isinstance(found, list):
        return []
    kept = []
    for item in found:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            continue
        kept.append({**item, "url": url})
    return kept[:MAX_IMAGES]


def book_cover(input_path: str, output_name: str, tracks: Sequence[str],
               seconds: float, dest_base: str, *, stereo: bool = False,
               subject: onlinecover.Subject | None = None,
               searchers: Sequence[llmclient.Llm] | None = None,
               lookers: Sequence[llmclient.Llm] | None = None,
               meter_file: str = "",
               download: Download = onlinecover.curl_download,
               probe: Callable[[str], dict] = onlinecover.probe_tags,
               prepare: Callable[[str, str], str] | None = None) -> str:
    """An image a model found for the book joined from ``input_path`` and
    written out as ``output_name``, saved beside ``dest_base`` with its own
    extension; "" when none was asked or none found one.

    ``searchers`` are the models that may search, best first, and
    ``lookers`` those that may look - the roster's ladders for the two tasks
    unless given. The first :data:`MAX_LOOKED_AT` of the search's answers
    that download as images are the candidates; a model that sees is shown
    them and picks the one that is plausibly this recording's cover, or
    none; without one, the first is taken. ``prepare`` makes a picture fit
    to be shown (a JPEG at :data:`LOOK_GEOMETRY`), ImageMagick unless given.

    What the models were asked is said under the book and, with
    ``meter_file``, recorded there for the run (:func:`modelmeter.record`)."""
    prepare = prepare or _for_looking
    if not enabled():
        _detail("    No model asked for a cover: the %s setting is off"
                % SWITCH_VARIABLE)
        return ""
    if searchers is None:
        searchers, why = llmroster.optional(llmroster.COVER_SEARCH)
        if not searchers:
            _detail("    No model asked for a cover: %s" % why)
            return ""
    if subject is None:
        subject = onlinecover.book_subject(input_path, tracks, seconds, stereo,
                                           probe)
    request = prompt(input_path, output_name, tracks, subject, probe)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    try:
        for llm in searchers:
            searcher = llm.searching(PURPOSE)
            if searcher is None:
                continue
            try:
                with spans.timing():
                    answer = searcher.ask(SYSTEM, request, tally)
                break
            except llmclient.LlmError as error:
                _log("    No cover from %s: %s" % (llm.name, error))
    finally:
        if tally.requests:
            _log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    if not answer:
        return ""
    if not answer:
        return ""
    found = _downloaded(candidates(answer), dest_base, download)
    if not found:
        _log("    No cover found by the model either")
        return ""
    choice: int | None = 0
    why = ""
    try:
        choice, why = _look(lookers, found, output_name, subject, meter_file,
                            prepare)
    finally:
        for at, (path, _item) in enumerate(found):
            if at != choice:
                _remove(path)
    if choice is None:
        _log("    No cover from the model either: looking at the %d it found, "
             "none is this recording's%s" % (len(found), ": " + why if why else ""))
        return ""
    path, item = found[choice]
    image = "%s.%s" % (dest_base, onlinecover.image_kind(path) or "jpg")
    os.replace(path, image)
    title = str(item.get("title") or "").strip()
    _log("    Cover found by the model (%s): %s%s%s" % (
        subject.content, urllib.parse.urlsplit(item["url"]).netloc,
        ", " + title if title else "",
        "; picked on sight of %d: %s" % (len(found), why) if why else ""))
    return image


def _downloaded(items: list[dict], dest_base: str,
                download: Download) -> list[tuple[str, dict]]:
    """The first :data:`MAX_LOOKED_AT` of ``items`` that download as images,
    each as (its file, the item)."""
    found: list[tuple[str, dict]] = []
    for item in items:
        if len(found) >= MAX_LOOKED_AT:
            break
        raw = "%s.candidate%d" % (dest_base, len(found) + 1)
        if download(item["url"], raw):
            found.append((raw, item))
    return found


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def look_prompt(found: Sequence[tuple[str, dict]], output_name: str,
                subject: onlinecover.Subject) -> str:
    """What a model that sees is told about the pictures it is shown."""
    content = _CONTENT.get(subject.content, subject.content)
    if subject.why:
        content += ", because of %s" % subject.why
    described = []
    for number, (_path, item) in enumerate(found, start=1):
        said = ", ".join("%s: %s" % (key, " ".join(str(item[key]).split())[:200])
                         for key in ("title", "edition", "language", "page")
                         if item.get(key))
        described.append("%d. from %s%s" % (
            number, urllib.parse.urlsplit(item["url"]).netloc,
            " - " + said if said else ""))
    return (
        "The %d picture(s) above were found on the web as the cover of this "
        "recording, in this order:\n%s\n\n"
        "The recording is written out as: %s\n"
        "It is, provisionally and not certainly: %s.\n"
        "Titles it may go by: %s\nWho may have made it: %s\n\n"
        "Look at each picture. Which one is plausibly this recording's cover "
        "art - a cover, not a banner, an author photo, a screenshot or a "
        "store page; the title and author printed on it, if any, this "
        "recording's; in its language where that shows? Among plausible "
        "ones, prefer an audiobook cover over a book's for an audiobook, then "
        "the sharper and the larger.\n\n"
        'Answer with JSON only: {"choice": <its number>, "why": "<one '
        'sentence>"}, or {"choice": null, "why": "..."} when none is.' % (
            len(found), "\n".join(described), output_name, content,
            "; ".join(subject.titles) or "(none)",
            "; ".join(subject.creators) or "(none)"))


def _look(lookers: Sequence[llmclient.Llm] | None,
          found: Sequence[tuple[str, dict]], output_name: str,
          subject: onlinecover.Subject, meter_file: str,
          prepare: Callable[[str, str], str]) -> tuple[int | None, str]:
    """Which of ``found`` to take - its index, or None for none - and why,
    asked of the first of ``lookers`` that can be had, with the pictures
    shown (the roster's ladder for the look unless given). Where there is
    none, or the look fails, the search's own first is taken, as it ranked
    them."""
    if lookers is None:
        lookers, why_none = llmroster.optional(llmroster.COVER_LOOK)
        if not lookers:
            _detail("    The candidates not looked at: %s" % why_none)
            return 0, ""
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    pictures: list[str] = []
    answer = ""
    try:
        pictures = [prepare(path, "%s.look%d.jpg" % (path, at))
                    for at, (path, _item) in enumerate(found)]
        for llm in lookers:
            try:
                with llm.serving(_detail) as bound:
                    # The server may hold another model than the roster says.
                    if not bound.sees_images():
                        _detail("    %s does not see pictures" % (bound.model or llm.name))
                        continue
                    with spans.timing():
                        answer = bound.ask(LOOK_SYSTEM,
                                           look_prompt(found, output_name, subject),
                                           tally, images=pictures)
                    break
            except llmclient.LlmError as error:
                _log("    The candidates not looked at by %s: %s" % (llm.name, error))
    finally:
        for picture in pictures:
            if picture not in (path for path, _item in found):
                _remove(picture)
        if tally.requests:
            _log("    " + modelmeter.rate_line(LOOK_PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    if not answer:
        return 0, ""
    verdict = llmclient.first_json(answer)
    if not isinstance(verdict, dict) or "choice" not in verdict:
        _log("    The look at the candidates gave no answer: the search's "
             "first is taken")
        return 0, ""
    why = " ".join(str(verdict.get("why") or "").split())
    choice = verdict.get("choice")
    if choice is None:
        return None, why
    if isinstance(choice, int) and 1 <= choice <= len(found):
        return choice - 1, why
    _log("    The look at the candidates chose %r, which is none of them: the "
         "search's first is taken" % (choice,))
    return 0, ""


def _for_looking(path: str, dest: str) -> str:
    """``path`` as a JPEG no larger than :data:`LOOK_GEOMETRY`, which every
    vision API takes - an AVIF or a 4000-pixel scan is neither - at ``dest``;
    ``path`` itself where it cannot be converted."""
    try:
        subprocess.run(imagemagick.convert_argv(
            ["-quiet", imagecodecs.coder_prefixed(path) + "[0]",
             "-resize", LOOK_GEOMETRY + ">", "-quality", "85", dest]),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return path
    return dest if onlinecover.image_kind(dest) == "jpg" else path
