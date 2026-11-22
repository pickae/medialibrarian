"""Asking a language model that can search the web for a recording's cover:
the last rung of concat-audio's and convert-audio's cover ladders, below the
catalogue search of :mod:`medialib.lib.onlinecover`.

The catalogue search asks fixed sites with a fixed query and takes only an
answer that is plainly the same title. What it misses - a title spelled
another way, an edition in another language, a show whose artwork lives on
its own site - a model that can search and read pages can still find. So
where that search came up empty, the model is told everything the run knows
about the recording: for a book, the folder as it lies, every file's name,
the tags of its tracks, the name the book is written out under; for a track
(typically a podcast episode), the show, the episode title, the date from
its filename or tags, and its tags; and in both cases what it probably is
and why, and what a good cover is. It answers with addresses of images, the
best first, which are downloaded here and taken only when they are images.

Two tasks of the roster (:mod:`medialib.lib.llmroster`) do it. The search,
:data:`llmroster.COVER_SEARCH`, needs a model that can search the web -
Claude with its own search, any other model through an agent harness - since
one that cannot could only guess an address; its ladder is tried in order
until one answers. The look, :data:`llmroster.COVER_LOOK`, needs a model that
sees pictures, and leans light: it judges both the first few images the web
search named and, before the web is ever asked, a folder's own unnamed image
files when two or more sit there and none matched a cover name. For a book's
web search, where no model sees, the search's own first is taken; for a single
track, the look must accept an image (where no vision model is available or it
rejects every candidate, no cover is taken, since a wrong cover is worse than
none); for a folder's images, where none looks like a front cover or no model
sees, the first image found is taken. The ``coverSearchLlm`` setting turns the
web-search rung off alone (``coverSearch`` off turns off every online rung,
that one too), and ``coverLookLlm`` turns off looking at a folder's local
images.

What the model was asked is counted (:mod:`medialib.lib.modelmeter`), said
under the recording, and recorded for the run's total.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
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

__all__ = ["SWITCH_VARIABLE", "LOOK_SWITCH_VARIABLE", "PURPOSE", "enabled",
           "look_enabled", "account", "prompt", "track_prompt", "look_prompt",
           "folder_look_prompt", "book_cover", "track_cover",
           "choose_folder_cover",
           "candidates"]

# Whether the model is asked at all: on unless set to off.
SWITCH_VARIABLE = "coverSearchLlm"
LOOK_SWITCH_VARIABLE = "coverLookLlm"

# What the model's requests are for, which names a harness's sessions.
PURPOSE = "cover"

# How much of a folder is listed for the model, and how many tracks' tags.
MAX_LISTED_FILES = 200
MAX_TAGGED_TRACKS = 6
MAX_TAG_CHARS = 300

# How many of the addresses it answers with are tried.
MAX_IMAGES = 6

# How many of those that download are candidates a model that sees pictures
# is shown, how many unnamed local folder images are shown, and how large each
# is shown: enough to read a cover's title.
MAX_LOOKED_AT = 4
MAX_FOLDER_LOOKED_AT = 8
LOOK_GEOMETRY = "1024x1024"

# What the look at the candidates is counted as.
LOOK_PURPOSE = "look"

LOOK_SYSTEM = ("You judge cover images for a personal audio library. You are "
               "shown pictures found on the web for one recording and say "
               "which, if any, is that recording's cover. A wrong cover is "
               "worse than none.")

FOLDER_LOOK_SYSTEM = (
    "You pick the front cover among image files found in an audio recording's "
    "folder. You are shown the images with their file names and the folder's "
    "name, and say which one, if any, is the front cover - rather than a back "
    "cover, disc or CD scan, tray inlay, booklet page, artist photo or "
    "tracklist.")

_log = runlog.log
_detail = runlog.detail

Download = Callable[[str, str], bool]

_CONTENT = {
    "podcast": "probably a podcast or a podcast episode",
    "drama": "probably an audio drama with a full cast",
    "audiobook": "probably an audiobook",
    "music": "probably a music album",
    "classical": "probably a classical music recording, known by its "
                 "composer, work and performers",
    "other": "unclear - perhaps a short story or a lecture",
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
episode has none. For an audio drama, the release's own cover. For music, \
the album's own front cover. For classical music, the cover of THIS \
performance - the same soloist, conductor or orchestra, and the same label \
and year where the names give them - never another performance of the same \
work, whose cover is a different record.
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
"edition": "audiobook, book, podcast, episode, drama or album", \
"why": "one sentence on why it is this recording"}]}
Best first, at most %d. {"images": []} when you found nothing you are sure \
of.""" % MAX_IMAGES


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def look_enabled() -> bool:
    """Whether a run asks a vision model to pick among a folder's unnamed
    images: unless the setting says off."""
    value = shell_lower(os.environ.get(LOOK_SWITCH_VARIABLE, "on").strip())
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


def _music_line(subject: onlinecover.Subject) -> str:
    """The work, performer and composer a music subject is known by, as a
    sentence to follow what it probably is; "" for anything else."""
    named = [(label, value) for label, value in (
        ("work or album", subject.work), ("performer", subject.performer),
        ("composer", subject.composer)) if value]
    if subject.content not in ("music", "classical") or not named:
        return ""
    return ". Probably: " + "; ".join("%s %s" % pair for pair in named)


def account(input_path: str, output_name: str, tracks: Sequence[str],
            subject: onlinecover.Subject,
            probe: Callable[[str], dict] = onlinecover.probe_tags) -> list[str]:
    """What a model is told about the book joined from ``input_path``, a
    section to an item: its folder and files, the tags inside its tracks, the
    name it is written out under, and what it probably is."""
    folder = os.path.normpath(input_path)
    # Where it came from rather than convert-and-concat's scratch tree, whose
    # folders say nothing about it.
    source = os.path.normpath(onlinecover.source_folder(input_path))
    parents = [part for part in source.replace(os.sep, "/").split("/")[-4:-1] if part]
    content = _CONTENT.get(subject.content, subject.content)
    if subject.why:
        content += ", because of %s" % subject.why
    content += _music_line(subject)
    return [
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
    ]


def prompt(input_path: str, output_name: str, tracks: Sequence[str],
           subject: onlinecover.Subject,
           probe: Callable[[str], dict] = onlinecover.probe_tags) -> str:
    """What the model is told about the book joined from ``input_path``."""
    tried = onlinecover.providers_for(subject.content)
    sections = [
        "Find the cover image for this recording.",
        *account(input_path, output_name, tracks, subject, probe),
        "## Already tried\n\nA search of %s by these titles found no answer "
        "sure enough. Look further: other spellings, the edition in the "
        "recording's own language, the publisher's or the show's own site." % (
            ", ".join(tried) or "the usual catalogues"),
    ]
    return "\n\n".join(sections) + "\n"


_DASHED_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?!\d)")
_COMPACT_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?!\d)")
_DATE_TAGS = ("date", "release_date", "origdate", "creation_time", "year")


def _filename_date(name: str) -> str:
    """A leading yyyy-mm-dd or yyyymmdd date in ``name`` as yyyy-mm-dd, or ""."""
    for pattern in (_DASHED_DATE, _COMPACT_DATE):
        found = pattern.match(name)
        if found:
            year, month, day = (int(part) for part in found.groups())
            if 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                return "%04d-%02d-%02d" % (year, month, day)
    return ""


def _track_date(name: str, tags: dict[str, str]) -> str:
    """The episode's date from its filename, else from its tags, or ""."""
    from_name = _filename_date(name)
    if from_name:
        return from_name
    for key in _DATE_TAGS:
        value = " ".join(str(tags.get(key) or "").split())
        if value:
            normalised = _filename_date(value)
            return normalised or value[:MAX_TAG_CHARS]
    return ""


def _clean_tags(raw: dict) -> dict[str, str]:
    return {shell_lower(str(key)): " ".join(str(value).split())[:MAX_TAG_CHARS]
            for key, value in raw.items() if str(value).strip()}


def _unique_values(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        if text and shell_lower(text) not in seen:
            seen.add(shell_lower(text))
            kept.append(text)
    return kept


def track_prompt(source: str, subject: onlinecover.Subject,
                 probe: Callable[[str], dict] = onlinecover.probe_tags) -> str:
    """What the model is told about one track (typically a podcast episode)
    at ``source``."""
    norm = os.path.normpath(source)
    name = os.path.basename(norm)
    stem = os.path.splitext(name)[0]
    parent_dir = os.path.dirname(norm)
    parent = os.path.basename(parent_dir) if parent_dir else ""
    parents = [part for part in parent_dir.replace(os.sep, "/").split("/")[-3:]
               if part]
    tags = _clean_tags(probe(source))
    shows = _unique_values([
        tags.get("album", ""), tags.get("album_artist", ""),
        tags.get("artist", ""), parent,
    ])
    episode = (tags.get("title", "") or subject.episode or subject.title
               or onlinecover.clean_name(stem) or stem)
    date = _track_date(name, tags)
    content = _CONTENT.get(subject.content, subject.content)
    if subject.why:
        content += ", because of %s" % subject.why
    tried = onlinecover.providers_for(subject.content)
    tag_block = ("\n".join("  %s: %s" % (key, value)
                           for key, value in sorted(tags.items()))
                 or "(no tags)")
    sections = [
        "Find the cover image for this recording.",
        "## The episode\n\nShow: %s\nEpisode title: %s\nDate: %s\n"
        "File: %s\nParent folder: %s\nThe folders it sits in, outermost "
        "first: %s" % (
            "; ".join(shows) or "(none)",
            episode or "(none)",
            date or "(none)",
            name,
            parent or "(none)",
            " / ".join(parents) or "(none)"),
        "## The tags inside the audio file\n\n%s" % tag_block,
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
        "recording's own language, the episode's or the show's own site." % (
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


def _resolve_models(
    searchers: Sequence[llmclient.Llm] | None,
    lookers: Sequence[llmclient.Llm] | None,
    require_look: bool,
) -> tuple[Sequence[llmclient.Llm] | None, Sequence[llmclient.Llm] | None, bool]:
    """Check the switch and settle the search and look ladders. Returns False
    in the third slot when no model should be asked."""
    if not enabled():
        _detail("    No model asked for a cover: the %s setting is off"
                % SWITCH_VARIABLE)
        return None, None, False
    if require_look:
        if lookers is None:
            lookers, why_look = llmroster.optional(llmroster.COVER_LOOK)
            if not lookers:
                _detail("    No model asked for a cover: %s" % why_look)
                return None, None, False
        elif not lookers:
            _detail("    No model asked for a cover: no vision model is available")
            return None, None, False
    if searchers is None:
        searchers, why = llmroster.optional(llmroster.COVER_SEARCH)
        if not searchers:
            _detail("    No model asked for a cover: %s" % why)
            return None, None, False
    return searchers, lookers, True


def _search_and_look(
    request: str, output_name: str, dest_base: str,
    subject: onlinecover.Subject, *,
    searchers: Sequence[llmclient.Llm],
    lookers: Sequence[llmclient.Llm] | None,
    meter_file: str,
    download: Download,
    prepare: Callable[[str, str], str],
    require_look: bool,
    found_meta: dict[str, str] | None = None,
) -> str:
    """Ask ``searchers`` for candidate URLs, download them, and let
    ``lookers`` choose one (required when ``require_look`` is True)."""
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
    found = _downloaded(candidates(answer), dest_base, download)
    if not found:
        _log("    No cover found by the model either")
        return ""
    choice: int | None = None if require_look else 0
    why = ""
    try:
        choice, why = _look(lookers, found, output_name, subject, meter_file,
                            prepare, require_look=require_look)
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
    if found_meta is not None:
        for key in ("title", "language", "edition"):
            value = str(item.get(key) or "").strip()
            if value:
                found_meta[key] = value
    title = str(item.get("title") or "").strip()
    _log("    Cover found by the model (%s): %s%s%s" % (
        subject.content, urllib.parse.urlsplit(item["url"]).netloc,
        ", " + title if title else "",
        "; picked on sight of %d: %s" % (len(found), why) if why else ""))
    return image


def book_cover(input_path: str, output_name: str, tracks: Sequence[str],
               seconds: float, dest_base: str, *, stereo: bool = False,
               subject: onlinecover.Subject | None = None,
               searchers: Sequence[llmclient.Llm] | None = None,
               lookers: Sequence[llmclient.Llm] | None = None,
               meter_file: str = "",
               found_meta: dict[str, str] | None = None,
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
    searchers, lookers, ok = _resolve_models(searchers, lookers,
                                             require_look=False)
    if not ok or searchers is None:
        return ""
    if subject is None:
        subject = onlinecover.book_subject(input_path, tracks, seconds, stereo,
                                           probe)
    request = prompt(input_path, output_name, tracks, subject, probe)
    return _search_and_look(
        request, output_name, dest_base, subject,
        searchers=searchers, lookers=lookers, meter_file=meter_file,
        download=download, prepare=prepare or _for_looking,
        require_look=False, found_meta=found_meta)


def track_cover(source: str, seconds: float, dest_base: str, *,
                stereo: bool = False,
                subject: onlinecover.Subject | None = None,
                searchers: Sequence[llmclient.Llm] | None = None,
                lookers: Sequence[llmclient.Llm] | None = None,
                meter_file: str = "",
                download: Download = onlinecover.curl_download,
                probe: Callable[[str], dict] = onlinecover.probe_tags,
                prepare: Callable[[str, str], str] | None = None) -> str:
    """An image a model found for one track (typically a podcast episode) at
    ``source``, saved beside ``dest_base`` with its own extension; "" when
    none was asked, none found one, or no vision model accepted a candidate.

    Unlike :func:`book_cover`, the vision look (:data:`llmroster.COVER_LOOK`)
    is required: a wrong cover is worse than none, so where no vision model
    is available or the look rejects all candidates, no cover is returned."""
    searchers, lookers, ok = _resolve_models(searchers, lookers,
                                             require_look=True)
    if not ok or searchers is None:
        return ""
    tags = probe(source)
    if subject is None:
        subject = onlinecover.track_subject(source, seconds, stereo, tags=tags)
    request = track_prompt(source, subject, lambda _path: tags)
    stem = os.path.splitext(os.path.basename(source))[0]
    return _search_and_look(
        request, stem, dest_base, subject,
        searchers=searchers, lookers=lookers, meter_file=meter_file,
        download=download, prepare=prepare or _for_looking,
        require_look=True)


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
        "ones, prefer an audiobook cover over a book's for an audiobook, an "
        "episode's own image over the show's for a podcast, then the sharper "
        "and the larger.\n\n"
        'Answer with JSON only: {"choice": <its number>, "why": "<one '
        'sentence>"}, or {"choice": null, "why": "..."} when none is.' % (
            len(found), "\n".join(described), output_name, content,
            "; ".join(subject.titles) or "(none)",
            "; ".join(subject.creators) or "(none)"))


def _ask_vision(paths: Sequence[str], system: str, user_prompt: str,
                lookers: Sequence[llmclient.Llm] | None, meter_file: str,
                prepare: Callable[[str, str], str],
                scratch_dir: str = "", *,
                none_label: str = "The candidates not looked at",
                fail_label: str = "The candidates not looked at") -> str:
    """Show ``paths`` (prepared to :data:`LOOK_GEOMETRY` in a throwaway temp
    directory) to the first of ``lookers`` that sees pictures, returning its
    raw answer or "" when none could be asked."""
    if lookers is None:
        lookers, why_none = llmroster.optional(llmroster.COVER_LOOK)
        if not lookers:
            _detail("    %s: %s" % (none_label, why_none))
            return ""
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    parent = scratch_dir if scratch_dir and os.path.isdir(scratch_dir) else None
    try:
        with tempfile.TemporaryDirectory(prefix="coverLook.", dir=parent) as work:
            pictures = [prepare(path, os.path.join(work, "look%d.jpg" % at))
                        for at, path in enumerate(paths)]
            for llm in lookers:
                try:
                    with llm.serving(_detail) as bound:
                        # The server may hold another model than the roster says.
                        if not bound.sees_images():
                            _detail("    %s does not see pictures"
                                    % (bound.model or llm.name))
                            continue
                        with spans.timing():
                            answer = bound.ask(system, user_prompt, tally,
                                               images=pictures)
                        break
                except llmclient.LlmError as error:
                    _log("    %s by %s: %s" % (fail_label, llm.name, error))
    finally:
        if tally.requests:
            _log("    " + modelmeter.rate_line(LOOK_PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)
    return answer


def _look(lookers: Sequence[llmclient.Llm] | None,
          found: Sequence[tuple[str, dict]], output_name: str,
          subject: onlinecover.Subject, meter_file: str,
          prepare: Callable[[str, str], str], *,
          require_look: bool = False) -> tuple[int | None, str]:
    """Which of ``found`` to take - its index, or None for none - and why,
    asked of the first of ``lookers`` that can be had, with the pictures
    shown (the roster's ladder for the look unless given). Where there is
    none, or the look fails, the search's own first is taken unless
    ``require_look`` is True, in which case None is returned."""
    if lookers is not None and not lookers:
        if require_look:
            _detail("    The candidates not looked at: no vision model is available")
            return None, "no vision model is available"
        return 0, ""
    scratch_dir = os.path.dirname(found[0][0]) if found else ""
    answer = _ask_vision(
        [path for path, _item in found], LOOK_SYSTEM,
        look_prompt(found, output_name, subject), lookers, meter_file,
        prepare, scratch_dir)
    if not answer:
        return (None, "no vision model answered") if require_look else (0, "")
    verdict = llmclient.first_json(answer)
    if not isinstance(verdict, dict) or "choice" not in verdict:
        if require_look:
            _log("    The look at the candidates gave no answer: no cover taken")
            return None, "the look gave no answer"
        _log("    The look at the candidates gave no answer: the search's "
             "first is taken")
        return 0, ""
    why = " ".join(str(verdict.get("why") or "").split())
    choice = verdict.get("choice")
    if choice is None:
        return None, why
    if (isinstance(choice, int) and not isinstance(choice, bool)
            and 1 <= choice <= len(found)):
        return choice - 1, why
    if require_look:
        _log("    The look at the candidates chose %r, which is none of them: "
             "no cover taken" % (choice,))
        return None, why
    _log("    The look at the candidates chose %r, which is none of them: the "
         "search's first is taken" % (choice,))
    return 0, ""


def folder_look_prompt(input_path: str, images: Sequence[str]) -> str:
    """What a vision model is told about unnamed image files in a folder."""
    folder = os.path.basename(os.path.normpath(input_path))
    described = []
    for number, path in enumerate(images, start=1):
        rel = os.path.relpath(path, input_path).replace(os.sep, "/")
        try:
            size = formatting.fmt_bytes(os.path.getsize(path))
        except OSError:
            size = "?"
        described.append("%d. %s (%s)" % (number, rel, size))
    return (
        "The %d image file(s) above sit in the folder \"%s\", in this order:\n"
        "%s\n\n"
        "Look at each picture. Which one is the front cover of this "
        "recording, if any? Prefer the front cover over back covers, disc or "
        "CD labels, tray inlays, booklet pages, tracklists or author photos.\n\n"
        'Answer with JSON only: {"choice": <its number>, "why": "<one '
        'sentence>"}, or {"choice": null, "why": "..."} when none is.'
        % (len(images), folder, "\n".join(described)))


def choose_folder_cover(
        input_path: str, images: Sequence[str], *,
        lookers: Sequence[llmclient.Llm] | None = None,
        meter_file: str = "",
        scratch_dir: str = "",
        prepare: Callable[[str, str], str] | None = None) -> str:
    """Ask a vision model which of a folder's unnamed ``images`` is the front
    cover, returning its path or "" when none looks like one, the switch is
    off, no vision model is available, or the answer is unusable.

    Only called when no cover-name rung matched and at least two images are
    present. At most :data:`MAX_FOLDER_LOOKED_AT` images are shown, prepared
    into a temporary directory (under ``scratch_dir`` when given, never in
    ``input_path``) and cleaned up afterwards."""
    if len(images) < 2:
        return ""
    if not look_enabled():
        _detail("    Folder images not looked at: the %s setting is off"
                % LOOK_SWITCH_VARIABLE)
        return ""
    shown = list(images[:MAX_FOLDER_LOOKED_AT])
    answer = _ask_vision(
        shown, FOLDER_LOOK_SYSTEM, folder_look_prompt(input_path, shown),
        lookers, meter_file, prepare or _for_looking, scratch_dir,
        none_label="Folder images not looked at",
        fail_label="Folder images not looked at")
    if not answer:
        return ""
    fallback = os.path.relpath(shown[0], input_path).replace(os.sep, "/")
    verdict = llmclient.first_json(answer)
    if not isinstance(verdict, dict) or "choice" not in verdict:
        _log("    The look at the folder images gave no answer; falling back "
             "to %s" % fallback)
        return ""
    why = " ".join(str(verdict.get("why") or "").split())
    choice = verdict.get("choice")
    if choice is None or (isinstance(choice, int) and not isinstance(choice, bool)
                          and choice == 0) \
            or (isinstance(choice, str) and shell_lower(choice.strip()) == "none"):
        _log("    None of the %d folder images looked like a front cover%s; "
             "falling back to %s" % (
                 len(shown), " (" + why + ")" if why else "", fallback))
        return ""
    if isinstance(choice, int) and not isinstance(choice, bool) \
            and 1 <= choice <= len(shown):
        picked = shown[choice - 1]
        picked_rel = os.path.relpath(picked, input_path).replace(os.sep, "/")
        _log("    Cover picked on sight of %d folder images: %s%s" % (
            len(shown), picked_rel, " - " + why if why else ""))
        return picked
    _log("    The look at the folder images chose %r, which is none of them; "
         "falling back to %s" % (choice, fallback))
    return ""


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
