"""Ask a language model that can search the web for an audiobook's metadata tags.

A joined book otherwise carries only whatever tags survived the join (usually
just the title written from the cleaned folder name). When ``bookTagsLlm`` is
on and a model that can search the web is available (the ``book-tags`` task in
:mod:`llmroster`), this module asks it for the recording's canonical metadata:
``title``, ``author``, ``narrator``, ``series``, ``series_index``, ``year``,
``language``, and ``abridged``.

Only tags that are currently empty on the joined output file are filled; any
non-empty tag already on the file is left untouched. The output file is never
renamed. When cover search (:mod:`aicover`) already ran for the same book, any
metadata it returned (``title``, ``language``, ``edition``) is passed as hints
and merged for any field the tag lookup did not return, while books that
already had a local cover are still tagged independently.

A joined music album is asked for an album's tags instead
(:data:`mutagentags.MUSIC_TAG_FIELDS`): title, album, artist, album artist,
year, genre and label - and a classical one
(:data:`mutagentags.CLASSICAL_TAG_FIELDS`) for its composer, work, soloist,
conductor and orchestra as tags of their own, never run together into the
artist.
"""

import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from medialib.lib import (
    formatting,
    languages,
    llmclient,
    llmroster,
    modelmeter,
    mutagentags,
    onlinecover,
)
from medialib.lib.enums import shell_lower
from medialib.lib.runlog import detail as _detail
from medialib.lib.runlog import log as _log

__all__ = [
    "BookTags",
    "MAX_SERIES_INDEX",
    "MAX_TEXT_LENGTH",
    "MAX_YEAR",
    "MIN_YEAR",
    "PURPOSE",
    "SWITCH_VARIABLE",
    "SYSTEM",
    "enabled",
    "merge_hints",
    "parse_music_tags",
    "parse_tags",
    "prompt",
    "tag_book",
]

# Whether a joined book's empty metadata tags are looked up by a model.
SWITCH_VARIABLE = "bookTagsLlm"

# What a call to the model is logged and billed under.
PURPOSE = "book tags"

# Caps for validating model-supplied fields before they touch audio tags.
MAX_TEXT_LENGTH = 250
MIN_YEAR = 1000
MAX_YEAR = 2100
MAX_SERIES_INDEX = 9999

# Caps for what goes into the prompt so a giant folder stays bounded.
_MAX_FILES = 60
_MAX_TAG_TRACKS = 3
_MAX_TAG_CHARS = 300
_YEAR_RE = re.compile(r"^\d{4}$")
_INDEX_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

_CONTENT = {
    "audiobook": "an audiobook",
    "drama": "a full-cast audio drama or radio play",
    "podcast": "a podcast episode or series",
    "youtube": "a YouTube video or podcast",
    "music": "a music album",
    "classical": "a classical music recording",
}

SYSTEM = (
    "You identify audiobooks, audio dramas and podcasts from a folder's file "
    "names and audio tags, verify the recording on the web, and return clean "
    "metadata tags for it.\n\n"
    "Search the web to confirm the recording (Audible, Goodreads, Open "
    "Library, publisher pages, or podcast directories). Prefer details of the "
    "specific audio edition shown in the files (its language, narrator, "
    "abridgement and release year) when the files point to one; otherwise use "
    "the work's standard metadata.\n\n"
    "Reply with one JSON object and nothing else, with exactly these keys:\n"
    "{\n"
    '  "title": string or null,\n'
    '  "author": string or null,\n'
    '  "narrator": string or null,\n'
    '  "series": string or null,\n'
    '  "series_index": number or string or null,\n'
    '  "year": 4-digit integer or null,\n'
    '  "language": ISO 639-3 code (e.g. "eng", "deu", "fra") or language name or null,\n'
    '  "abridged": true, false, or null\n'
    "}\n\n"
    "Rules:\n"
    "- Use null for any field you cannot verify with confidence. Never guess.\n"
    "- Every string must be a single line with no surrounding quotes or notes.\n"
    "- Only set series_index when series is also known (positive number, e.g. 1 or 2.5).\n"
    "- year must be a 4-digit year (e.g. 2019).\n"
    "- abridged must be a JSON boolean (true or false) or null.\n"
)


MUSIC_SYSTEM = (
    "You identify music albums from a folder's file names and audio tags, "
    "verify the release on the web, and return clean metadata tags for it.\n\n"
    "Search the web to confirm the release (MusicBrainz, Discogs, the label's "
    "or the artist's pages, store listings). Prefer the details of the very "
    "release the files point to - its label and year - over another "
    "edition's.\n\n"
    "Reply with one JSON object and nothing else, with exactly these keys:\n"
    "{\n"
    '  "title": string or null,\n'
    '  "album": string or null,\n'
    '  "artist": string or null,\n'
    '  "album_artist": string or null,\n'
    '  "year": 4-digit integer or null,\n'
    '  "genre": string or null,\n'
    '  "label": string or null\n'
    "}\n\n"
    "Rules:\n"
    "- Use null for any field you cannot verify with confidence. Never guess.\n"
    "- Every string must be a single line with no surrounding quotes or notes.\n"
    "- title is the album's title, which the joined file is named for.\n"
    "- year is the release's year (e.g. 1987).\n"
)

CLASSICAL_SYSTEM = (
    "You identify classical music recordings from a folder's file names and "
    "audio tags, verify the release on the web, and return clean metadata "
    "tags for it.\n\n"
    "A classical recording is one performance of a work: who composed it, "
    "what the work is, and who performs it - a soloist, a conductor, an "
    "orchestra or ensemble - are separate facts, each in its own tag. Search "
    "the web to confirm THIS performance (MusicBrainz, Discogs, Presto "
    "Music, the label's catalogue, store listings): the same performers, "
    "and the label and year the folder names give where they give them.\n\n"
    "Reply with one JSON object and nothing else, with exactly these keys:\n"
    "{\n"
    '  "title": string or null,\n'
    '  "album": string or null,\n'
    '  "composer": string or null,\n'
    '  "work": string or null,\n'
    '  "artist": string or null,\n'
    '  "album_artist": string or null,\n'
    '  "performer": string or null,\n'
    '  "conductor": string or null,\n'
    '  "ensemble": string or null,\n'
    '  "year": 4-digit integer or null,\n'
    '  "genre": string or null,\n'
    '  "label": string or null\n'
    "}\n\n"
    "Rules:\n"
    "- Use null for any field you cannot verify with confidence. Never guess.\n"
    "- Every string must be a single line with no surrounding quotes or notes.\n"
    "- composer: the composer's full name (e.g. Anton Velder); work: the work's "
    "usual title (e.g. Harbour Suite).\n"
    "- performer: the soloist(s) or ensemble leader; conductor: the conductor "
    "only; ensemble: the orchestra, choir or ensemble. null where there is "
    "none - a solo piano recital has no conductor.\n"
    "- artist and album_artist: the performers as the release credits them, "
    "never the composer.\n"
    "- title: the album's title as released, which the joined file is named "
    "for; year: the year this performance was recorded or first released.\n"
)

_SYSTEMS = {"music": MUSIC_SYSTEM, "classical": CLASSICAL_SYSTEM}
_MUSIC_FIELDS = {"music": mutagentags.MUSIC_TAG_FIELDS,
                 "classical": mutagentags.CLASSICAL_TAG_FIELDS}


@dataclass(frozen=True)
class BookTags:
    """Validated metadata tags returned for a joined book."""

    title: str = ""
    author: str = ""
    narrator: str = ""
    series: str = ""
    series_index: str = ""
    year: str = ""
    language: str = ""
    abridged: bool | None = None

    def to_tag_map(self) -> dict[str, str]:
        """Non-empty canonical fields mapped to the strings written into tags."""
        result: dict[str, str] = {}
        for field in ("title", "author", "narrator", "series",
                      "series_index", "year", "language"):
            value = getattr(self, field)
            if value:
                result[field] = value
        if self.abridged is not None:
            result["abridged"] = "true" if self.abridged else "false"
        return result

    def populated_fields(self) -> tuple[str, ...]:
        """Canonical field names carrying a validated value, in canonical order."""
        tag_map = self.to_tag_map()
        return tuple(field for field in mutagentags.BOOK_TAG_FIELDS
                     if field in tag_map)


def enabled() -> bool:
    """Whether a run asks the model at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


def _listing(input_path: str) -> str:
    """The folder's name and its contents, relative paths sorted, capped."""
    lines = [os.path.basename(input_path.rstrip("/\\")) + "/"]
    entries: list[str] = []
    for root, dirs, files in os.walk(input_path):
        dirs.sort()
        files.sort()
        rel_root = os.path.relpath(root, input_path)
        for name in files:
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            entries.append(rel.replace("\\", "/"))
    for rel in entries[:_MAX_FILES]:
        lines.append("  " + rel)
    if len(entries) > _MAX_FILES:
        lines.append("  ... and %d more files" % (len(entries) - _MAX_FILES))
    return "\n".join(lines)


def _tag_lines(tracks: Sequence[str],
               probe: Callable[[str], dict]) -> str:
    """Tags read from up to :data:`_MAX_TAG_TRACKS` tracks."""
    if not tracks:
        return "(none)"
    picks = [tracks[0]]
    if len(tracks) > 2:
        picks.append(tracks[len(tracks) // 2])
    if len(tracks) > 1:
        picks.append(tracks[-1])
    picks = picks[:_MAX_TAG_TRACKS]
    blocks = []
    for path in picks:
        tags = probe(path)
        if not tags:
            continue
        pairs = []
        for key in sorted(tags):
            value = " ".join(str(tags[key]).split())
            if value:
                pairs.append("  %s: %s" % (key, value[:_MAX_TAG_CHARS]))
        if pairs:
            blocks.append(os.path.basename(path) + "\n" + "\n".join(pairs))
    return "\n".join(blocks) if blocks else "(none)"


def prompt(input_path: str, output_name: str, tracks: Sequence[str],
           subject: onlinecover.Subject,
           existing_tags: Mapping[str, str] | None = None,
           cover_hints: Mapping[str, str] | None = None,
           probe: Callable[[str], dict] = onlinecover.probe_tags,
           fields: Sequence[str] = mutagentags.BOOK_TAG_FIELDS) -> str:
    """Everything the model is told about one book before it looks up tags:
    of ``fields``, those it already carries and those still needed."""
    content = _CONTENT.get(subject.content, subject.content)
    if subject.why:
        content += ", because of %s" % subject.why
    named = [(label, value) for label, value in (
        ("work or album", subject.work), ("performer", subject.performer),
        ("composer", subject.composer)) if value]
    if subject.content in _MUSIC_FIELDS and named:
        content += ". Probably: " + "; ".join("%s %s" % pair for pair in named)
    existing = {k: v for k, v in (existing_tags or {}).items()
                if k in fields and str(v).strip()}
    missing = [field for field in fields if field not in existing]
    sections = [
        "## Files\n\n" + _listing(input_path),
        "## Tags in the audio\n\n" + _tag_lines(tracks, probe),
        "## Written out as\n\n%s\n(the folder's name, cleaned)" % output_name,
        "## What it probably is\n\nProvisionally, and not certainly: %s.%s\n"
        "Titles it may go by, likeliest first: %s\nWho may have made it: %s\n"
        "If what you find says otherwise, trust what you find." % (
            content,
            " Running time: %s." % formatting.fmt_clock(subject.seconds)
            if subject.seconds > 0 else "",
            "; ".join(subject.titles) or "(none)",
            "; ".join(subject.creators) or "(none)"),
    ]
    known_lines: list[str] = []
    if existing:
        known_lines.append(
            "Tags already set on the output file (will not be overwritten): "
            + ", ".join("%s=%s" % (k, existing[k])
                        for k in fields if k in existing))
    hints = {k: str(v).strip() for k, v in (cover_hints or {}).items()
             if str(v).strip()}
    if hints:
        known_lines.append(
            "Hints from cover search (verify before trusting): "
            + ", ".join("%s=%s" % (k, hints[k]) for k in sorted(hints)))
    if missing:
        known_lines.append("Empty tags still needed: " + ", ".join(missing))
    if known_lines:
        sections.append("## Already known\n\n" + "\n".join(known_lines))
    return "\n\n".join(sections) + "\n"


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    if _CONTROL_RE.search(value):
        return ""
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > MAX_TEXT_LENGTH:
        return ""
    return cleaned


def _clean_year(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        year = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not _YEAR_RE.match(stripped):
            return ""
        year = int(stripped)
    else:
        return ""
    if MIN_YEAR <= year <= MAX_YEAR:
        return str(year)
    return ""


def _clean_series_index(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        if 1 <= value <= MAX_SERIES_INDEX:
            return str(value)
        return ""
    if isinstance(value, float):
        if not math.isfinite(value) or value <= 0 or value > MAX_SERIES_INDEX:
            return ""
        rounded = round(value, 2)
        if abs(value - rounded) > 1e-6:
            return ""
        formatted = ("%.2f" % rounded).rstrip("0").rstrip(".")
        return formatted if formatted and formatted != "0" else ""
    if isinstance(value, str):
        stripped = value.strip()
        if not _INDEX_RE.match(stripped):
            return ""
        num = float(stripped)
        if not math.isfinite(num) or num <= 0 or num > MAX_SERIES_INDEX:
            return ""
        formatted = ("%.2f" % num).rstrip("0").rstrip(".")
        return formatted if formatted and formatted != "0" else ""
    return ""


def _clean_language(value: object) -> str:
    if not isinstance(value, str) or _CONTROL_RE.search(value):
        return ""
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 60:
        return ""
    code = languages.code3_from_label(cleaned)
    if not code and ("-" in cleaned or "_" in cleaned):
        primary = re.split(r"[-_]", cleaned, maxsplit=1)[0]
        code = languages.code3_from_label(primary)
    if code and code != "und":
        return code
    return ""


def _clean_abridged(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _abridged_from_edition(edition: str) -> bool | None:
    lower = " ".join(edition.lower().split())
    if not lower:
        return None
    if "unabridged" in lower:
        return False
    if re.search(r"\babridged\b", lower):
        return True
    return None


def parse_tags(answer: str) -> BookTags:
    """Validate and extract a :class:`BookTags` from the model's answer.

    Returns an empty :class:`BookTags` when ``answer`` does not contain a JSON
    object; invalid individual fields are dropped rather than failing the rest.
    """
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return BookTags()
    return BookTags(
        title=_clean_text(found.get("title")),
        author=_clean_text(found.get("author")),
        narrator=_clean_text(found.get("narrator")),
        series=_clean_text(found.get("series")),
        series_index=_clean_series_index(found.get("series_index")),
        year=_clean_year(found.get("year")),
        language=_clean_language(found.get("language")),
        abridged=_clean_abridged(found.get("abridged")),
    )


def parse_music_tags(answer: str,
                     fields: Sequence[str] = mutagentags.MUSIC_TAG_FIELDS
                     ) -> dict[str, str]:
    """The validated tags among ``fields`` in the model's answer: a single
    line of text each, the year a plausible year. {} when ``answer`` holds no
    JSON object; an invalid field is dropped rather than failing the rest."""
    found = llmclient.first_json(answer)
    if not isinstance(found, dict):
        return {}
    tags: dict[str, str] = {}
    for field in fields:
        value = (_clean_year(found.get(field)) if field == "year"
                 else _clean_text(found.get(field)))
        if value:
            tags[field] = value
    return tags


def merge_hints(tags: BookTags,
                cover_hints: Mapping[str, str] | None) -> BookTags:
    """Fill any empty field in ``tags`` from validated cover-search metadata.

    Never overwrites a field already present in ``tags``. Recognizes ``title``,
    ``language``, and ``edition`` (for ``abridged``) as returned by
    :mod:`aicover`.
    """
    if not cover_hints:
        return tags
    title = tags.title or _clean_text(cover_hints.get("title"))
    language = tags.language or _clean_language(cover_hints.get("language"))
    abridged = tags.abridged
    if abridged is None:
        edition = _clean_text(cover_hints.get("edition"))
        if edition:
            abridged = _abridged_from_edition(edition)
    return BookTags(
        title=title,
        author=tags.author,
        narrator=tags.narrator,
        series=tags.series,
        series_index=tags.series_index,
        year=tags.year,
        language=language,
        abridged=abridged,
    )


def tag_book(joined: str, input_path: str, output_name: str,
             tracks: Sequence[str], seconds: float, *,
             stereo: bool = False,
             subject: onlinecover.Subject | None = None,
             cover_hints: Mapping[str, str] | None = None,
             searchers: Sequence[llmclient.Llm] | None = None,
             meter_file: str = "",
             probe: Callable[[str], dict] = onlinecover.probe_tags) -> list[str]:
    """Fill empty canonical metadata tags on ``joined`` using a web-searching
    model (and optional ``cover_hints`` from :mod:`aicover`).

    Existing non-empty tags on ``joined`` are never overwritten, and ``joined``
    is never renamed. Returns the list of canonical field names written.
    """
    if not enabled():
        _detail("    No model asked for book tags: the %s setting is off"
                % SWITCH_VARIABLE)
        return []
    if not os.path.isfile(joined):
        return []

    if subject is None:
        subject = onlinecover.book_subject(input_path, tracks, seconds, stereo,
                                           probe)
    fields = _MUSIC_FIELDS.get(subject.content, mutagentags.BOOK_TAG_FIELDS)
    music = subject.content in _MUSIC_FIELDS
    existing = mutagentags.read_book_tags(joined, fields)
    missing = [field for field in fields if not existing.get(field)]
    if not missing:
        _detail("    %s tags already filled; no model asked"
                % ("Music" if music else "Book"))
        return []

    if searchers is None:
        searchers, why = llmroster.optional(llmroster.BOOK_TAGS)
        if not searchers:
            _detail("    No model asked for %s tags: %s"
                    % ("music" if music else "book", why))
            return _apply_tags(joined, existing,
                               _hinted({}, cover_hints, music),
                               source="cover search", fields=fields)

    request = prompt(input_path, output_name, tracks, subject,
                     existing_tags=existing, cover_hints=cover_hints,
                     probe=probe, fields=fields)
    system = _SYSTEMS.get(subject.content, SYSTEM)
    tally, spans = modelmeter.Tally(), modelmeter.Spans()
    answer = ""
    model_used = ""
    try:
        for llm in searchers:
            searcher = llm.searching(PURPOSE)
            if searcher is None:
                continue
            try:
                with spans.timing():
                    answer = searcher.ask(system, request, tally)
                model_used = llm.name
                break
            except llmclient.LlmError as error:
                _log("    No %s tags from %s"
                     % ("music" if music else "book", llm.name) + ": %s" % error)
    finally:
        if tally.requests:
            _log("    " + modelmeter.rate_line(PURPOSE, spans.seconds, tally))
        if meter_file:
            modelmeter.record(meter_file, spans, tally)

    if music:
        found = parse_music_tags(answer, fields) if answer else {}
    else:
        found = (parse_tags(answer) if answer else BookTags()).to_tag_map()
    written = _apply_tags(joined, existing, _hinted(found, cover_hints, music),
                          source=model_used or "cover search", fields=fields)
    if not written and answer:
        _log("    No new %s tags found by %s"
             % ("music" if music else "book", model_used or "the model"))
    return written


def _hinted(found: Mapping[str, str], cover_hints: Mapping[str, str] | None,
            music: bool) -> dict[str, str]:
    """``found`` with its empty fields filled from cover-search metadata: a
    book's title, language and abridgement (:func:`merge_hints`), an album's
    title alone - a cover's edition and language say nothing about music."""
    if not music:
        book = BookTags(**{k: v for k, v in found.items()
                           if k != "abridged"},
                        abridged=None if "abridged" not in found
                        else found["abridged"] == "true")
        return merge_hints(book, cover_hints).to_tag_map()
    merged = dict(found)
    title = _clean_text((cover_hints or {}).get("title"))
    if title and not merged.get("title"):
        merged["title"] = title
    return merged


def _apply_tags(joined: str, existing: Mapping[str, str],
                tags: Mapping[str, str], source: str,
                fields: Sequence[str] = mutagentags.BOOK_TAG_FIELDS) -> list[str]:
    """Write validated empty-only tags from ``tags`` onto ``joined`` and log
    every decision."""
    tag_map = dict(tags)
    # Never write a dangling series_index without a series name (existing or new).
    if "series_index" in tag_map and not (
            existing.get("series") or tag_map.get("series")):
        tag_map.pop("series_index", None)
    candidate_map = {field: value for field, value in tag_map.items()
                     if not existing.get(field)}
    if not candidate_map:
        return []
    written: list[str] = []
    status = mutagentags.fill_book_tags(joined, candidate_map, written=written,
                                        fields=fields)
    if status != 0:
        _log("    Book tags could not be written to %s"
             % os.path.basename(joined))
        return []
    if written:
        summary = ", ".join("%s=%s" % (field, candidate_map[field])
                            for field in written)
        _log("    %s tags filled (%s): %s"
             % ("Book" if fields is mutagentags.BOOK_TAG_FIELDS else "Music",
                source, summary))
    return written
