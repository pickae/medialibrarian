"""Looking online for a cover nothing local supplied.

The last rung of the cover ladder, and a rare one: it is reached only when the
folder's images, its booklet PDF and the art embedded in its tracks all came up
empty.

WHERE to ask is a table (``medialib/config/coverproviders.tsv``, or the copy the
``coverSearchTable`` setting names), because the sites change and their order
is a matter of taste; HOW to read each site's answer is here, one reader per
site. A row says which content it is for, which reader reads it, how sure an
answer has to be, how far apart its requests go and the address to ask.

What the audio probably IS decides which rows are asked:

* a name ending in a bracketed YouTube id - yt-dlp's default - is a YouTube
  podcast for sure, and its own thumbnail is asked for by that id;
* a name starting with a yyyymmdd date is a podcast episode - it is how the
  podcast downloads name them, and nothing else is named that way;
* 30 to 200 minutes is most likely a podcast - unless it is encoded in
  stereo, which makes it most likely a full-cast drama;
* anything longer is most likely an audiobook;
* anything shorter is "other";
* whatever its length, music says so in its tags or where it is kept: a
  genre that is not speech, a conductor, a catalogue number such as "Op. 27"
  or "K. 999", or a folder called Music or Klassik on the way to it. Music
  with the marks of classical music - a classical genre, a conductor, a
  catalogue number, a Klassik folder - is "classical", which is looked for by
  its performer and its work rather than by an album title, the way classical
  releases are sold.

An answer is taken only when it is the same title, and nothing about it says
otherwise - an author or channel that is somebody else, a running time that is
another length. Where none is sure enough, the audio keeps no cover: a wrong
cover is worse than none, because nobody goes looking for a cover that is there.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import threading
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from medialib import commands
from medialib.lib import (
    cleannamesindividually,
    enums,
    formatting,
    imagecodecs,
    imagemagick,
    podcastfeeds,
    politepacing,
    runcontext,
    runlog,
    titlematch,
)
from medialib.lib.enums import lower_extension_of, shell_lower

__all__ = [
    "CONTENTS",
    "MATCHES",
    "PROVIDERS",
    "Candidate",
    "CoverSearch",
    "Row",
    "Subject",
    "book_cover",
    "book_subject",
    "enabled",
    "providers_for",
    "probable_content",
    "read_table",
    "track_cover",
    "track_subject",
    "track_wants_search",
]

# Whether the search runs at all: on unless set to off.
SWITCH_VARIABLE = "coverSearch"
# The provider table to read instead of the shipped one.
TABLE_VARIABLE = "coverSearchTable"
TABLE_NAME = "coverproviders.tsv"

# The content boundaries, in seconds.
PODCAST_MIN_SECONDS = 30 * 60
AUDIOBOOK_MIN_SECONDS = 200 * 60

CONTENTS = ("podcast", "drama", "audiobook", "music", "classical", "other")
MATCHES = ("id", "strict", "loose", "show", "performer")

# Where convert-and-concat's intermediate tree came from: "<tree>\t<source>".
# The tree holds the transcoded audio alone, under the source's own folder
# names; the source still has the folders it sits in, which say what it is.
SOURCE_TREE_VARIABLE = "SOURCE_TREE"

# Words a genre tag or a folder name says music with. A genre is music unless
# it is one of the speech ones or says nothing; a folder only by these words.
_MUSIC_FOLDER_WORDS = frozenset((
    "music", "musik", "muziek", "musique", "musica", "música", "musikk",
    "muzyka", "muzika", "musiikki"))
_CLASSICAL_FOLDER_WORDS = frozenset((
    "classical", "klassik", "klassiek", "classique", "clasica", "clásica",
    "classica", "klassisk", "klasyczna", "klassiikki", "opera", "opern"))
_CLASSICAL_GENRE_WORDS = _CLASSICAL_FOLDER_WORDS | frozenset((
    "baroque", "barock", "barok", "symphony", "symphonie", "sinfonie",
    "symfonie", "concerto", "sonata", "sonate", "orchestral", "chamber",
    "kammermusik", "requiem", "cantata", "kantate", "oratorio", "oper",
    "romantic", "romantik", "renaissance", "medieval", "early"))
_SPEECH_GENRE_WORDS = frozenset((
    "audiobook", "audiobooks", "hörbuch", "hörbücher", "hoerbuch", "horbuch",
    "luisterboek", "audiolibro", "audiolivre", "book", "books", "hörspiel",
    "hoerspiel", "horspiel", "hörspiele", "podcast", "podcasts", "speech",
    "spoken", "talk", "lecture", "lesung", "drama", "radio", "comedy",
    "kabarett", "cabaret", "audio drama", "story", "stories", "kinder"))
_EMPTY_GENRES = frozenset(("", "other", "others", "unknown", "misc", "genre",
                           "none", "various", "default"))
# A classical catalogue number in a title: "Op. 27", "BWV 9999", "K. 999",
# "Hob. XVI:99", "D 999", "RV 999", "WoO 999".
_CATALOGUE = re.compile(
    r"\b(?:op|opus|bwv|kv|k|hob|d|rv|woo|hwv|sz|bb|l|s|twv)\.?\s*"
    r"(?:[ivxl]+:)?\d+", re.IGNORECASE)

# How far a catalogue's running time may be from the audio's and still be the
# same recording: a tenth, or two minutes for a short one. An abridged edition
# is another length, and so another cover as often as not.
RUNTIME_SHARE = 0.10
RUNTIME_SECONDS = 120

# A title of fewer words than this is not trusted alone under a strict row:
# "Episode 12" and "Prologue" are everybody's.
SHORT_TITLE_WORDS = 3

# An image smaller than this is a site's "no image" placeholder.
MIN_IMAGE_BYTES = 2000

# Said on every request, so a site can see what is asking. Honest on purpose:
# a site that refuses it is a site that is not asked.
USER_AGENT = ("medialibrarian/0 (a personal media library's cover lookup; "
              "one request at a time)")

_log = runlog.log

Fetch = Callable[[str], "str | None"]
Download = Callable[[str, str], bool]


# --- the table ------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    """One search: for which content, read by which reader, how sure an answer
    has to be, the seconds between two requests, and the address."""

    content: str
    provider: str
    match: str
    pace: float
    address: str


def table_path() -> str:
    """The table a run reads: the setting's, else the shipped one."""
    return (os.environ.get(TABLE_VARIABLE, "")
            or os.path.join(commands.config_dir(), TABLE_NAME))


def read_table(path: str) -> list[Row]:
    """The rows of the table at ``path``, in order.

    A row that cannot be read is said and skipped rather than stopping a run:
    the table only decides where a missing cover is looked for, and the rest of
    it still can be asked.
    """
    rows: list[Row] = []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as error:
        _log("    WARNING: the cover search table cannot be read (%s)" % error)
        return rows
    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        problem = ""
        if len(fields) != 5:
            problem = "%d columns, not 5" % len(fields)
        else:
            content, provider, match, pace, address = (f.strip() for f in fields)
            if content not in CONTENTS:
                problem = "content %r is not one of %s" % (content, ", ".join(CONTENTS))
            elif provider not in PROVIDERS:
                problem = "provider %r is not one of %s" % (
                    provider, ", ".join(sorted(PROVIDERS)))
            elif match not in MATCHES:
                problem = "match %r is not one of %s" % (match, ", ".join(MATCHES))
            else:
                try:
                    seconds = float(pace)
                except ValueError:
                    seconds = -1.0
                if seconds < 0:
                    problem = "pace %r is not a number of seconds" % pace
                elif not address:
                    problem = "no address"
        if problem:
            _log("    WARNING: %s line %d skipped: %s" % (path, number, problem))
            continue
        rows.append(Row(content, provider, match, seconds, address))
    return rows


def enabled() -> bool:
    """Whether a run looks online at all: unless the setting says off."""
    value = shell_lower(os.environ.get(SWITCH_VARIABLE, "on").strip())
    return value not in ("off", "0", "no", "false")


# --- what the audio is ------------------------------------------------------------

_DATED = re.compile(r"^(\d{4})(\d{2})(\d{2})(?!\d)")


def _dated(name: str) -> bool:
    """Whether ``name`` starts with a plausible yyyymmdd date."""
    found = _DATED.match(name)
    if not found:
        return False
    year, month, day = (int(part) for part in found.groups())
    return 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


# The " [dQw4w9WgXcQ]" yt-dlp writes at the end of a name by default: a YouTube
# video's id, eleven characters of its URL-safe alphabet.
_YOUTUBE_ID = re.compile(r"\[([A-Za-z0-9_-]{11})\]\s*$")


def youtube_id(name: str) -> str:
    """The YouTube id ``name`` ends in (before any extension), or ""."""
    found = _YOUTUBE_ID.search(name) or _YOUTUBE_ID.search(
        os.path.splitext(name)[0])
    return found.group(1) if found else ""


def probable_content(name: str, seconds: float, stereo: bool = False) -> tuple:
    """(What it probably is, what it is on), for audio called ``name`` that
    runs ``seconds`` (0 when unknown).

    ``stereo`` is a run that encodes it in stereo rather than mono: a talk is
    kept mono, so podcast length in stereo is most likely a full-cast drama -
    which the audiobook catalogues sell, and the podcast searches miss. A
    dated or YouTube-named file is a podcast whatever it is encoded in.
    """
    if youtube_id(name):
        return "podcast", "the name ends in a YouTube id"
    if _dated(name):
        return "podcast", "the name starts with a date"
    if seconds > AUDIOBOOK_MIN_SECONDS:
        return "audiobook", "a length of %s" % formatting.fmt_clock(seconds)
    if seconds >= PODCAST_MIN_SECONDS:
        if stereo:
            return "drama", "a stereo recording of %s" \
                % formatting.fmt_clock(seconds)
        return "podcast", "a length of %s" % formatting.fmt_clock(seconds)
    if seconds > 0:
        return "other", "a length of %s" % formatting.fmt_clock(seconds)
    return "other", "no length"


def _words_of(text: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]+", shell_lower(text)))


def source_folder(path: str) -> str:
    """The folder ``path`` was transcoded from, where it lies in
    convert-and-concat's intermediate tree and the source folder is still
    there; ``path`` itself otherwise."""
    tree, _, source = str(runcontext.get(SOURCE_TREE_VARIABLE, "")).partition("\t")
    if not tree or not source:
        return path
    norm, tree = os.path.normpath(path), os.path.normpath(tree)
    if norm != tree and not norm.startswith(tree + os.sep):
        return path
    mapped = os.path.normpath(os.path.join(source, os.path.relpath(norm, tree)))
    return mapped if os.path.isdir(mapped) else path


def music_content(tag_sets: Sequence[dict], folder: str = "") -> tuple | None:
    """("music" or "classical", what it is on) when the tracks' tags or the
    folders ``folder`` lies in say music; None when nothing does.

    A genre decides when there is one: a speech genre is not music, a
    classical one is classical, any other is music. A conductor or a
    catalogue number in a title is classical. Failing those, a folder named
    for classical music, then one named for music, anywhere on the way."""
    genres = [str(tags.get("genre", "")).strip() for tags in tag_sets]
    genres = [g for g in genres if shell_lower(g) not in _EMPTY_GENRES]
    for genre in genres:
        words = _words_of(genre)
        if words & _SPEECH_GENRE_WORDS or shell_lower(genre) in _SPEECH_GENRE_WORDS:
            return None
    for genre in genres:
        if _words_of(genre) & _CLASSICAL_GENRE_WORDS:
            return "classical", "the genre %s" % genre
    for tags in tag_sets:
        if str(tags.get("conductor", "")).strip():
            return "classical", "a conductor in the tags"
        for key in ("title", "album", "work"):
            if _CATALOGUE.search(str(tags.get(key, ""))):
                return "classical", "a catalogue number in the %s" % key
    if genres:
        return "music", "the genre %s" % genres[0]
    parts = [part for part in os.path.normpath(folder).split(os.sep) if part]
    for part in parts:
        if _words_of(part) & _CLASSICAL_FOLDER_WORDS:
            return "classical", "the folder %s" % part
    for part in parts:
        if _words_of(part) & _MUSIC_FOLDER_WORDS:
            return "music", "the folder %s" % part
    return None


# What a name carries that is not its title: the "(Unabridged)" and
# "[B0ABC12345]" behind.
_BRACKETED = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*")

# The dash between a name's parts, however its spaces are written: " - ",
# ".-." or "_-_". A hyphen inside a word is not one.
_PART_BREAK = re.compile(r"[\s._]+-[\s._]+")


def clean_name(name: str) -> str:
    """``name`` with what is not its title taken off.

    Each part is cleaned the way concat-audio cleans a folder name - the date
    in front off, dots and underscores to spaces - and the parts are joined
    with " - " again, which that cleaning would lose: the break is what says
    where the series ends and the title starts.
    """
    parts = []
    for part in _PART_BREAK.split(name):
        _prefix, text = cleannamesindividually.clean_names_individually(part)
        text = " ".join(_BRACKETED.sub(" ", text).split()).strip(" -")
        if text:
            parts.append(text)
    return " - ".join(parts)


def _unique(values: Sequence[str]) -> list[str]:
    seen, kept = set(), []
    for value in values:
        value = (value or "").strip()
        if value and shell_lower(value) not in seen:
            seen.add(shell_lower(value))
            kept.append(value)
    return kept


@dataclass
class Subject:
    """What is being looked for: what it is called, best first; who made it,
    the trusted (tagged) names apart from the guessed ones; how long it runs;
    what it probably is, and the figure that read as it; and the title after
    the last dash of a name in parts - an episode's own."""

    titles: list[str]
    creators: list[str]
    trusted_creators: list[str]
    seconds: float
    content: str
    youtube_id: str = ""
    why: str = ""
    episode: str = ""
    # For music: the work or album, its performer and its composer, as the
    # classical catalogues are searched by them.
    work: str = ""
    performer: str = ""
    composer: str = ""
    # The label a folder names its performance by - "(Nordlabel, 1972)".
    label: str = ""

    @property
    def title(self) -> str:
        return self.titles[0] if self.titles else ""

    @property
    def creator(self) -> str:
        return self.creators[0] if self.creators else ""


def subject_for(name: str, seconds: float, tag_titles: Sequence[str] = (),
                tag_creators: Sequence[str] = (),
                guessed_creators: Sequence[str] = (),
                stereo: bool = False) -> Subject:
    """The subject for audio called ``name``.

    ``tag_titles`` and ``tag_creators`` are what its tags say, best first;
    ``guessed_creators`` names that may be its maker - a parent folder. A name
    written "Author - Title" offers both halves as titles and as creators,
    because which half is which is not written down. A name of three parts -
    "Series - Episode 12 - Its Title" - offers the last on its own as well:
    a catalogue may sell the episode under its own title alone.
    """
    cleaned = clean_name(name)
    head, tail = titlematch.leading_segment(cleaned)
    episode = titlematch.last_segment(cleaned)
    titles = _unique([*tag_titles, cleaned, tail, episode, head])
    creators = _unique([*tag_creators, *guessed_creators, head, tail])
    content, why = probable_content(name, seconds, stereo)
    return Subject(titles, creators, _unique(tag_creators), seconds,
                   content, youtube_id(name), why, episode)


# --- what a site answered -----------------------------------------------------------

@dataclass
class Candidate:
    """One answer: its title, its makers, its running time (0 unknown), its
    images best first. ``creators_certain`` is False where the makers were read
    out of loose page text, which can agree but cannot contradict."""

    title: str
    creators: tuple[str, ...] = ()
    seconds: float = 0.0
    images: tuple[str, ...] = ()
    creators_certain: bool = True
    # The label or copyright line, where the site gives one ("" where not).
    label: str = ""


def _json(text: str | None):
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _itunes_large(url: str) -> str:
    """An iTunes artwork address asked at 1400 pixels instead of 100 or 600."""
    return re.sub(r"/\d+x\d+bb\.", "/1400x1400bb.", url)


def read_itunes(text: str | None) -> list[Candidate]:
    found = []
    for item in _json(text).get("results") or ():
        if not isinstance(item, dict):
            continue
        art = item.get("artworkUrl600") or item.get("artworkUrl100") or ""
        if not art:
            continue
        millis = item.get("trackTimeMillis") or 0
        found.append(Candidate(
            item.get("collectionName") or item.get("trackName") or "",
            tuple(n for n in (item.get("artistName"),) if n),
            float(millis) / 1000 if isinstance(millis, (int, float)) else 0.0,
            tuple(_unique([_itunes_large(art), art])),
            label=str(item.get("copyright") or "")))
    return found


def _amazon_full(url: str) -> str:
    """An Amazon image address without its size token: ``._SL500_.jpg`` or
    ``._AC_UY218_.jpg`` asks for a thumbnail, and without it the original."""
    return re.sub(r"\._[^/]*?_\.(jpe?g|png)$", r".\1", url)


def read_audible(text: str | None) -> list[Candidate]:
    found = []
    for item in _json(text).get("products") or ():
        if not isinstance(item, dict):
            continue
        images = item.get("product_images") or {}
        urls = [images[key] for key in sorted(
            images, key=lambda k: int(k) if str(k).isdigit() else 0, reverse=True)]
        if not urls:
            continue
        minutes = item.get("runtime_length_min") or 0
        found.append(Candidate(
            item.get("title") or "",
            tuple(a.get("name", "") for a in item.get("authors") or ()
                  if isinstance(a, dict) and a.get("name")),
            float(minutes) * 60 if isinstance(minutes, (int, float)) else 0.0,
            tuple(_unique([_amazon_full(urls[0]), *urls]))))
    return found


_AMAZON_RESULT = 'data-component-type="s-search-result"'
_TAG_ATTRIBUTE = re.compile(r'([a-zA-Z-]+)="([^"]*)"')
_IMAGE_TAG = re.compile(r'<img[^>]*class="s-image"[^>]*>')
_H2_TEXT = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_ANY_TAG = re.compile(r"<[^>]+>")


def read_amazon(text: str | None) -> list[Candidate]:
    """A search results page. A page that is a robot check instead holds no
    results, and reads as no answer."""
    found = []
    for block in (text or "").split(_AMAZON_RESULT)[1:]:
        image = _IMAGE_TAG.search(block)
        if not image:
            continue
        attributes = dict(_TAG_ATTRIBUTE.findall(image.group(0)))
        src = html.unescape(attributes.get("src", ""))
        if not src:
            continue
        heading = _H2_TEXT.search(block)
        title = html.unescape(_ANY_TAG.sub(" ", heading.group(1))) if heading \
            else html.unescape(attributes.get("alt", ""))
        page_text = html.unescape(_ANY_TAG.sub(" ", block))
        found.append(Candidate(" ".join(title.split()),
                               (" ".join(page_text.split()),), 0.0,
                               tuple(_unique([_amazon_full(src), src])),
                               creators_certain=False))
    return found


def read_openlibrary(text: str | None) -> list[Candidate]:
    found = []
    for item in _json(text).get("docs") or ():
        if not isinstance(item, dict) or not item.get("cover_i"):
            continue
        found.append(Candidate(
            item.get("title") or "",
            tuple(n for n in item.get("author_name") or () if n),
            0.0,
            ("https://covers.openlibrary.org/b/id/%s-L.jpg" % item["cover_i"],)))
    return found


def read_youtube(text: str | None) -> list[Candidate]:
    """What ``yt-dlp --flat-playlist -J`` printed: a search's entries, or the
    one video a watch address names."""
    answer = _json(text)
    entries = answer.get("entries") or ([answer] if answer.get("id") else [])
    found = []
    for item in entries:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        duration = item.get("duration") or 0
        video = item["id"]
        found.append(Candidate(
            item.get("title") or "",
            tuple(n for n in (item.get("channel"), item.get("uploader")) if n),
            float(duration) if isinstance(duration, (int, float)) else 0.0,
            ("https://i.ytimg.com/vi/%s/maxresdefault.jpg" % video,
             "https://i.ytimg.com/vi/%s/hqdefault.jpg" % video)))
    return found


def read_deezer(text: str | None) -> list[Candidate]:
    """Deezer's album search: its albums, the largest cover first."""
    found = []
    for item in _json(text).get("data") or ():
        if not isinstance(item, dict):
            continue
        images = tuple(_unique([item.get("cover_xl") or "",
                                item.get("cover_big") or ""]))
        if not images:
            continue
        artist = item.get("artist")
        if not isinstance(artist, dict):
            artist = {}
        found.append(Candidate(
            item.get("title") or "",
            tuple(n for n in (artist.get("name"),) if n), 0.0, images))
    return found


def read_musicbrainz(text: str | None) -> list[Candidate]:
    """MusicBrainz's release-group search, each group's front cover asked of
    the Cover Art Archive - which answers 404 where it has none, and the next
    group is tried."""
    found = []
    for item in _json(text).get("release-groups") or ():
        if not isinstance(item, dict) or not item.get("id"):
            continue
        credits = [credit.get("name") or (credit.get("artist") or {}).get("name")
                   for credit in item.get("artist-credit") or ()
                   if isinstance(credit, dict)]
        found.append(Candidate(
            item.get("title") or "", tuple(n for n in credits if n), 0.0,
            ("https://coverartarchive.org/release-group/%s/front" % item["id"],)))
    return found


PROVIDERS: dict[str, Callable[[str | None], list[Candidate]]] = {
    "amazon": read_amazon,
    "audible": read_audible,
    "deezer": read_deezer,
    "itunes": read_itunes,
    "musicbrainz": read_musicbrainz,
    "openlibrary": read_openlibrary,
    "youtube": read_youtube,
}


# --- is it the same thing -------------------------------------------------------------

def _words(text: str) -> set[str]:
    return {word for word in titlematch.normalize_title(text).split()
            if len(word) >= 3}


def _names_agree(ours: Sequence[str], theirs: Sequence[str]) -> bool:
    """Whether a name of ours and one of theirs share a word: "Jane Doe" and
    "Doe, Jane", or a surname in a page's text."""
    mine = set().union(*(_words(name) for name in ours)) if ours else set()
    return any(mine & _words(name) for name in theirs)


def _matched_title(subject: Subject, candidate: Candidate) -> str:
    """Which of the subject's titles the candidate is, or ""."""
    if not candidate.title:
        return ""
    # A catalogue writes the subtitle and the series into the title - "The
    # Title: A Novel", "The Title (Book 2)" - where a folder seldom does.
    bare = _BRACKETED.sub(" ", candidate.title).strip()
    theirs = _unique([candidate.title, bare,
                      titlematch.leading_segment(bare)[0]])
    for title in subject.titles:
        if any(titlematch.equivalent(title, other) for other in theirs):
            return title
    return ""


def _runtime(subject: Subject, candidate: Candidate) -> str:
    """"agrees", "conflicts" or "" when either length is unknown."""
    if subject.seconds <= 0 or candidate.seconds <= 0:
        return ""
    slack = max(subject.seconds * RUNTIME_SHARE, RUNTIME_SECONDS)
    return "agrees" if abs(subject.seconds - candidate.seconds) <= slack \
        else "conflicts"


# The number a series gives one of its parts: "Folge 6", "Episode 06", "Part 2".
_EPISODE = re.compile(
    r"\b(?:folge|episode|ep|teil|part|band|vol|volume|book|buch)\.?\s*0*(\d+)\b",
    re.IGNORECASE)


def _episode(text: str) -> str:
    found = _EPISODE.search(text)
    return found.group(1) if found else ""


def _another_episode(subject: Subject, candidate: Candidate) -> bool:
    """Whether the candidate is numbered as another part of the series than
    the subject: "Folge 13: The Glass Lighthouse" is not "Folge 06 - The
    Glass Lighthouse", although the titles past the number are the same."""
    ours = next((n for n in map(_episode, subject.titles) if n), "")
    theirs = _episode(candidate.title)
    return bool(ours and theirs and ours != theirs)


def accepts(match: str, subject: Subject, candidate: Candidate) -> bool:
    """Whether ``candidate`` is ``subject``, as sure as ``match`` asks."""
    if match == "id":
        # Asked for by the id the file itself carries: it IS the source.
        return True
    if match == "show":
        return any(titlematch.equivalent(name, candidate.title)
                   for name in subject.creators)
    if match == "performer":
        return _performer_and_work(subject, candidate)
    title = _matched_title(subject, candidate)
    if not title or _another_episode(subject, candidate):
        return False
    if match == "loose":
        return True
    creators_agree = _names_agree(subject.creators, candidate.creators)
    if (candidate.creators_certain and candidate.creators
            and subject.trusted_creators
            and not _names_agree(subject.trusted_creators, candidate.creators)):
        return False
    runtime = _runtime(subject, candidate)
    if runtime == "conflicts":
        return False
    return (creators_agree or runtime == "agrees"
            or len(title.split()) >= SHORT_TITLE_WORDS)


def _performer_and_work(subject: Subject, candidate: Candidate) -> bool:
    """A classical release is the subject's when its artist is the subject's
    performer and its title names the work: "Velder: Harbour Suite; Ostrand:
    Night Pieces" by the pianist is the pianist's "Harbour Suite", couplings
    and all - a title match would
    never take it, and a performer alone would take any of their records."""
    performers = _unique([subject.performer, *subject.trusted_creators])
    performers = [p for p in performers
                  if shell_lower(p) != shell_lower(subject.composer)]
    if not performers or not _names_agree(performers, candidate.creators):
        return False
    # A performer who recorded the work more than once: where the folder
    # names the label, the release must be that label's - and one that does
    # not say its label cannot be told from the others, so is not taken. A
    # label's own release may well be named for its recital rather than the
    # work, so it needs only a word of the work in its title.
    label_agrees = False
    if subject.label:
        if not _label_agrees(subject.label, candidate.label):
            return False
        label_agrees = True
    work = _words(subject.work or subject.title)
    if not work:
        return False
    shared = len(work & _words(candidate.title))
    return shared >= 1 if label_agrees else shared * 3 >= len(work) * 2


# What a folder abbreviates a label to, and how the label calls itself.
_LABEL_NAMES = {"dg": "deutsche grammophon", "hm": "harmonia mundi",
                "dhm": "deutsche harmonia mundi", "rca": "rca",
                "emi": "emi", "bbc": "bbc"}


def _label_agrees(ours: str, theirs: str) -> bool:
    """Whether a release's label or copyright line ``theirs`` is the label a
    folder named, ``ours``: every word of ours, or of its long name, in it."""
    if not theirs:
        return False
    full = _LABEL_NAMES.get(shell_lower(ours.strip()), ours)
    line = _words_of(theirs)
    return any(words and words <= line
               for words in (_words_of(ours), _words_of(full)))


# --- the network ---------------------------------------------------------------------

_PACERS: dict[str, politepacing.Pacer] = {}
_PACERS_LOCK = threading.Lock()


def _pacer(row: Row) -> politepacing.Pacer:
    """One pacer per provider, made at the first row that asks it."""
    with _PACERS_LOCK:
        pacer = _PACERS.get(row.provider)
        if pacer is None:
            pacer = politepacing.Pacer(row.pace, row.pace / 4)
            _PACERS[row.provider] = pacer
        return pacer


def curl_text(url: str) -> str | None:
    """One GET, its body, or None when it did not answer."""
    try:
        done = subprocess.run(
            ["curl", "-fsSL", "--max-time", "30", "-A", USER_AGENT, url],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")


def _ytdlp_command() -> list[str] | None:
    platform = "windows" if os.name == "nt" else "linux"
    return podcastfeeds.locate_ytdlp(platform, commands.script_dir())


def ytdlp_search(search: str) -> str | None:
    """yt-dlp's answer to ``ytsearchN:...``, as JSON text, or None."""
    command = _ytdlp_command()
    if not command:
        return None
    try:
        done = subprocess.run(
            [*command, "--flat-playlist", "-J", "--no-warnings", search],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")


def _fetch(url: str) -> str | None:
    """A site's answer: through yt-dlp for YouTube, a search or a watch
    address alike, and curl for everything else."""
    if url.startswith("ytsearch") or "youtube.com/" in url or "youtu.be/" in url:
        return ytdlp_search(url)
    return curl_text(url)


def image_kind(path: str) -> str:
    """jpg, png or webp by the file's first bytes, or "" for anything else -
    a page, a robot check, a placeholder."""
    try:
        if os.path.getsize(path) < MIN_IMAGE_BYTES:
            return ""
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return ""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return ""


def curl_download(url: str, dest: str) -> bool:
    """The image at ``url`` into ``dest``; True when an image arrived."""
    try:
        done = subprocess.run(
            ["curl", "-fsSL", "--max-time", "60", "-A", USER_AGENT,
             "-o", dest, url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError:
        return False
    if done.returncode == 0 and image_kind(dest):
        return True
    try:
        os.remove(dest)
    except OSError:
        pass
    return False


# --- the search ------------------------------------------------------------------------

def _address(row: Row, subject: Subject) -> str:
    """The row's address for this subject, or "" when it needs a name the
    subject does not have."""
    values = {"title": subject.title, "creator": subject.creator,
              "query": " ".join(v for v in (subject.title, subject.creator) if v),
              "youtube_id": subject.youtube_id, "episode": subject.episode,
              "work": subject.work, "performer": subject.performer,
              "composer": subject.composer}
    for key, value in values.items():
        placeholder = "{%s}" % key
        if placeholder not in row.address:
            continue
        if not value:
            return ""
        spelled = value if row.provider == "youtube" \
            else urllib.parse.quote_plus(value)
        values[key] = spelled
    address = row.address
    for key, value in values.items():
        address = address.replace("{%s}" % key, value)
    return address


@dataclass
class CoverSearch:
    """The table's rows asked for one subject, in order, until an image
    arrives. ``fetch`` and ``download`` are the network, for a test to stand
    in for."""

    rows: list[Row]
    fetch: Fetch = _fetch
    download: Download = curl_download
    pace: bool = True
    asked: list[str] = field(default_factory=list)

    def find(self, subject: Subject, dest_base: str) -> tuple[str, str]:
        """The image found for ``subject``, saved beside ``dest_base`` with
        its own extension, and a line saying where it came from; ("", "")
        when no row found one."""
        tried = set()
        for row in self.rows:
            if row.content != subject.content:
                continue
            address = _address(row, subject)
            if not address or address in tried:
                continue
            tried.add(address)
            if self.pace:
                _pacer(row).wait()
            self.asked.append(address)
            for candidate in PROVIDERS[row.provider](self.fetch(address)):
                if not accepts(row.match, subject, candidate):
                    continue
                for url in candidate.images:
                    if self.pace:
                        _pacer(row).wait()
                    raw = dest_base + ".download"
                    if not self.download(url, raw):
                        continue
                    kind = image_kind(raw) or "jpg"
                    image = "%s.%s" % (dest_base, kind)
                    os.replace(raw, image)
                    return image, "%s, %s match: %s" % (
                        row.provider, row.match, candidate.title)
        return "", ""


def _search(subject: Subject, dest_base: str,
            search: CoverSearch | None) -> str:
    if search is None:
        search = CoverSearch(read_table(table_path()))
    image, source = search.find(subject, dest_base)
    # The content and the figure it was taken on, the way the table's rows
    # were chosen: a hand-built subject without one says the content alone.
    what = subject.content
    if subject.why:
        what = "%s - %s" % (subject.content, subject.why)
    if image:
        _log("    Cover found online (%s): %s" % (what, source))
    else:
        _log("    No cover found online either (%s, looked for \"%s\")"
             % (what, subject.title))
    return image


# --- what a file says about itself -------------------------------------------------------

def probe_tags(path: str) -> dict[str, str]:
    """The container's tags, lower-cased keys; {} when ffprobe will not say."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format_tags",
             "-of", "json", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    except OSError:
        return {}
    tags = (_json(done.stdout.decode("utf-8", "replace")).get("format")
            or {}).get("tags") or {}
    return {shell_lower(str(k)): str(v) for k, v in tags.items()}


def has_embedded_cover(path: str) -> bool:
    """Whether ``path`` carries a picture: an attached-picture stream, or a
    Matroska attachment."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    except OSError:
        return False
    kinds = done.stdout.decode("utf-8", "replace").split()
    return "video" in kinds or "attachment" in kinds


def media_seconds(path: str) -> float:
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        return float(done.stdout.decode().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def media_channels(path: str) -> int:
    """The channel count of ``path``'s first audio stream; 0 when unknown."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
             "-show_entries", "stream=channels",
             "-of", "default=nk=1:nw=1", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        return int(done.stdout.decode().split("\n")[0].strip() or 0)
    except (OSError, ValueError):
        return 0


# --- concat-audio ----------------------------------------------------------------------

def _refine_content(subject: Subject, name: str, *,
                    folder_path: str, tracks: Sequence[str],
                    seconds: float, stereo: bool,
                    list_folder: bool,
                    classifiers: Sequence[Any] | None,
                    meter_file: str,
                    probe: Callable[[str], dict]) -> None:
    """Refine ``subject.content`` and ``subject.why`` with the light content
    classifier when enabled and available; leave the deterministic
    :func:`probable_content` heuristic in place on any fallback."""
    from medialib.lib import aicontentkind
    classified = aicontentkind.classify(
        name, folder_path=folder_path, tracks=tracks,
        seconds=seconds, stereo=stereo,
        heuristic=(subject.content, subject.why),
        list_folder=list_folder, classifiers=classifiers,
        meter_file=meter_file, probe=probe)
    if classified is not None:
        subject.content, subject.why = classified


def book_cover(input_path: str, tracks: Sequence[str], seconds: float,
               dest_base: str, search: CoverSearch | None = None,
               stereo: bool = False, *,
               subject: Subject | None = None,
               meter_file: str = "",
               classifiers: Sequence[Any] | None = None) -> str:
    """An image for the book joined from ``input_path``, or "".

    Always asked when the local rungs failed: a book is one file, so one
    cover is never wasted on it. ``stereo`` is whether the book was encoded
    in stereo - see :func:`probable_content`.
    """
    if not enabled():
        return ""
    if subject is None:
        subject = book_subject(input_path, tracks, seconds, stereo,
                               meter_file=meter_file, classifiers=classifiers)
    return _search(subject, dest_base, search)


def book_subject(input_path: str, tracks: Sequence[str], seconds: float,
                 stereo: bool = False,
                 probe: Callable[[str], dict] | None = None, *,
                 meter_file: str = "",
                 classifiers: Sequence[Any] | None = None) -> Subject:
    """What the book joined from ``input_path`` is looked for as: its folder's
    name, the first track's tags (read by ``probe``, ffprobe unless given),
    the folder above as a possible author. When content classification by a
    language model is enabled and available, its verdict refines the content
    kind before the catalogue search runs (except for a YouTube id, which
    fetches that video's own thumbnail directly)."""
    folder = os.path.basename(os.path.normpath(input_path))
    source = source_folder(input_path)
    tag_reader = probe or probe_tags
    tags = tag_reader(tracks[0]) if tracks else {}
    parent = os.path.basename(os.path.dirname(os.path.normpath(source)))
    subject = subject_for(
        folder, seconds,
        tag_titles=[tags.get("album", "")],
        tag_creators=[tags.get("album_artist", ""), tags.get("artist", ""),
                      tags.get("composer", "")],
        guessed_creators=[parent],
        stereo=stereo)
    if subject.youtube_id or _dated(folder):
        return subject
    sampled = [tags]
    if len(tracks) > 1:
        sampled.append(tag_reader(tracks[-1]))
    music = music_content(sampled, source)
    if music:
        subject.content, subject.why = music
    _refine_content(
        subject, folder, folder_path=source, tracks=tracks,
        seconds=seconds, stereo=stereo, list_folder=True,
        classifiers=classifiers, meter_file=meter_file, probe=tag_reader)
    if subject.content in ("music", "classical"):
        _music_names(subject, tags, folder, parent)
    return subject


# Anything in round, square or curly brackets, anywhere in a tag.
_BRACKETED_ANY = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")

# The label and date a performance folder ends in: "(Nordlabel, 1972)",
# "(Live Label, ~1985)", "(Nordlabel, 10.2.1969)".
_FOLDER_LABEL = re.compile(r"\(([^(),]*[^\W\d_][^(),]*),\s*[~\d][^()]*\)\s*$")


def _folder_label(folder: str) -> str:
    """The label a performance folder names in its closing brackets, or ""."""
    found = _FOLDER_LABEL.search(folder)
    return found.group(1).strip() if found else ""


def _music_names(subject: Subject, tags: dict, folder: str,
                 parent: str) -> None:
    """The work, performer and composer of a music subject, from its tags,
    else from where it lies: a classical collection is kept as one folder
    per work holding one folder per performance - "Harbour Suite / 35
    Lindqvist (Nordlabel, 1972)" - so an untagged recording's
    own folder names the performer and the one above it the work."""
    cleaned = clean_name(folder)
    composer = tags.get("composer", "").strip()
    performer = next((name.strip() for name in (
        tags.get("album_artist", ""), tags.get("artist", ""),
        tags.get("performer", ""), tags.get("conductor", ""))
        if name.strip() and shell_lower(name.strip()) != shell_lower(composer)),
        "")
    subject.composer = composer
    subject.label = _folder_label(folder)
    # Without what a tag carries in brackets - "(piano)", "[FLAC]", "(Nordlabel
    # Series 1000, NL-70530)" - which only narrows a catalogue's search to
    # nothing.
    work = _BRACKETED_ANY.sub(" ", tags.get("work", "") or tags.get("album", ""))
    subject.work = " ".join(work.split()) \
        or (clean_name(parent) if subject.content == "classical" else cleaned)
    performer = " ".join(_BRACKETED_ANY.sub(" ", performer).split())
    subject.performer = performer or (
        cleaned if subject.content == "classical" else subject.creator)
    if subject.content == "classical" and subject.performer:
        subject.creators = _unique([subject.performer, *subject.creators])


def providers_for(content: str) -> list[str]:
    """The providers the table asks for ``content``, in its order."""
    return _unique([row.provider for row in read_table(table_path())
                    if row.content == content])


# --- convert-audio -----------------------------------------------------------------------

_IMAGE_SUFFIXES = frozenset(
    "." + shell_lower(e)
    for e in (*enums.IMAGE_EXTENSIONS, *enums.COVER_IMAGE_EXTENSIONS))
_AUDIO_SUFFIXES = frozenset(
    "." + shell_lower(e)
    for e in (*enums.AUDIO_EXTENSIONS, *enums.LOSSLESS_AUDIO_EXTENSIONS))

# How many of a folder's other tracks are asked whether they carry a cover.
MAX_SIBLINGS_PROBED = 20

_FOLDERS: dict[str, bool] = {}
_FOLDERS_LOCK = threading.Lock()


def _siblings_mostly_covered(folder: str, track: str,
                             has_cover: Callable[[str], bool]) -> bool:
    """Whether most of the other tracks in ``folder`` carry a cover - or
    True when there are none. Answered once per folder."""
    with _FOLDERS_LOCK:
        if folder in _FOLDERS:
            return _FOLDERS[folder]
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    others = [os.path.join(folder, n) for n in names
              if n != os.path.basename(track)
              and "." + lower_extension_of(n) in _AUDIO_SUFFIXES
              and os.path.isfile(os.path.join(folder, n))][:MAX_SIBLINGS_PROBED]
    answer = not others or 2 * sum(1 for o in others if has_cover(o)) > len(others)
    with _FOLDERS_LOCK:
        _FOLDERS[folder] = answer
    return answer


def track_wants_search(source: str, seconds: float,
                       has_cover: Callable[[str], bool] = has_embedded_cover
                       ) -> bool:
    """Whether a converted track with no cover of its own is worth a search.

    The album case is left alone on purpose: a folder of an album's tracks,
    short, none of them carrying a cover, with the cover beside them as a
    folder image. Embedding one copy per track wastes as much as it would for a
    downloaded cover, and ingest-music's beets writes those folder images
    better than a search would. What IS searched is the odd one out - a long
    or dated track in a folder holding no image, among tracks that mostly
    carry their own: the podcast episode whose thumbnail download failed.
    """
    name = os.path.basename(source)
    if (seconds < PODCAST_MIN_SECONDS and not _dated(name)
            and not youtube_id(name)):
        return False
    folder = os.path.dirname(source) or "."
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    if any("." + lower_extension_of(n) in _IMAGE_SUFFIXES for n in names):
        return False
    return _siblings_mostly_covered(folder, source, has_cover)


def track_subject(source: str, seconds: float, stereo: bool = False,
                  probe: Callable[[str], dict] | None = None,
                  tags: dict[str, str] | None = None, *,
                  meter_file: str = "",
                  classifiers: Sequence[Any] | None = None) -> Subject:
    """What a single track at ``source`` is looked for as: its stem, its tags
    (read by ``probe``, ffprobe unless given, or taken from ``tags``), and its
    parent folder. When content classification by a language model is enabled
    and available, its verdict refines the content kind before the catalogue
    search runs (except for a YouTube id)."""
    stem = os.path.splitext(os.path.basename(source))[0]
    tag_reader: Callable[[str], dict]
    if tags is None:
        tag_reader = probe or probe_tags
        tags = tag_reader(source)
    else:
        known_tags = tags

        def tag_reader(_path: str) -> dict:
            return known_tags
    subject = subject_for(
        stem, seconds,
        tag_titles=[tags.get("title", "")],
        tag_creators=[tags.get("album_artist", ""), tags.get("artist", "")],
        guessed_creators=[tags.get("album", ""),
                          os.path.basename(os.path.dirname(source))],
        stereo=stereo)
    if not subject.youtube_id:
        _refine_content(
            subject, stem, folder_path=os.path.dirname(source),
            tracks=(source,), seconds=seconds, stereo=stereo,
            list_folder=False, classifiers=classifiers,
            meter_file=meter_file, probe=tag_reader)
    return subject


def track_cover(source: str, seconds: float, tmp_dir: str,
                search: CoverSearch | None = None,
                has_cover: Callable[[str], bool] = has_embedded_cover,
                stereo: bool = False,
                meter_file: str = "", *,
                classifiers: Sequence[Any] | None = None) -> None:
    """Fill ``<tmp_dir>/tempCover.jpg`` from online, where the source left it
    empty and the track is worth a search. ``apply_cover`` embeds it from
    there the way it embeds a cover pulled out of the source. ``stereo`` is
    whether the run encodes it in stereo - see :func:`probable_content`.

    Falls back to :func:`medialib.lib.aicover.track_cover` when the catalogue
    search comes up empty; ``meter_file`` records what the model was asked."""
    temp_cover = os.path.join(tmp_dir, "tempCover.jpg")
    if os.path.isfile(temp_cover) or not enabled():
        return
    if not track_wants_search(source, seconds, has_cover):
        return
    tags = probe_tags(source)
    subject = track_subject(source, seconds, stereo=stereo, tags=tags,
                            meter_file=meter_file, classifiers=classifiers)
    image = _search(subject, os.path.join(tmp_dir, "online"), search)
    if not image:
        from medialib.lib import aicover
        image = aicover.track_cover(
            source, seconds, os.path.join(tmp_dir, "model"),
            stereo=stereo, subject=subject, meter_file=meter_file,
            probe=lambda _path: tags)
    if not image:
        return
    if image.endswith(".jpg"):
        os.replace(image, temp_cover)
        return
    subprocess.run(imagemagick.convert_argv(
        ["-quiet", imagecodecs.coder_prefixed(image), temp_cover]),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    try:
        os.remove(image)
    except OSError:
        pass


def forget() -> None:
    """Forget every folder answered and every provider's pace. For a test."""
    with _FOLDERS_LOCK:
        _FOLDERS.clear()
    with _PACERS_LOCK:
        _PACERS.clear()
