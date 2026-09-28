"""What dvdcompare.net lists of a film's releases: the commentaries on each disc.

dvdcompare keeps one page per film per format - "<Title> (Blu-ray) (1974)",
"<Title> (Blu-ray 4K) (1979)", and the DVD page with neither - and on it every
release it has compared, each with its extras. A release says what its
commentaries are in one of two ways: a "Commentaries:" block, or among its
extras, under the disc they are on ("DISC TWO (Blu-ray)") and the cut of the
film they go with ("* The Film (Director's Cut)").

What is read is exactly that and nothing more: for each release, each disc's
commentaries on each cut of THIS film, as :class:`commentarynames.Release`. A box
set's other films, and a featurette's "with commentary", are not the film's
commentary tracks and are not read.

The site is asked the way its own search box asks it, one page at a time and
:data:`MIN_INTERVAL` apart give or take :data:`JITTER`, and every page is kept
under the checkout's ``data/dvdcompare`` so a second run over a library asks it
nothing.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
import unicodedata
from collections.abc import Callable
from html.parser import HTMLParser
from typing import NamedTuple

from medialib.lib import politepacing, titlematch
from medialib.lib.commentarynames import Disc, Release

__all__ = [
    "BASE",
    "JITTER",
    "MIN_INTERVAL",
    "PACER",
    "SearchEntry",
    "directory",
    "parse_film_page",
    "parse_search",
    "pick",
    "search_query",
    "Site",
]

# http and not https: the site's own links are http, and its https answers
# slowly or not at all.
BASE = "http://www.dvdcompare.net/comparisons/"

# Where the site is asked instead, when set: the suite points it at a port
# nothing listens on, so a CLI case that runs the whole ingest cannot reach the
# real one.
SITE_VARIABLE = "dvdCompareSite"

# The time between two requests, give or take the jitter. A library is
# thousands of films, and the site is one volunteer-run server.
MIN_INTERVAL = 5.0
JITTER = 2.0

# Every request to the site, from whichever thread of the run asks it.
PACER = politepacing.Pacer(MIN_INTERVAL, JITTER)

# How long a kept page is believed. A film's page grows as releases are added;
# a search is only a way of reaching it.
FILM_PAGE_DAYS = 90
SEARCH_PAGE_DAYS = 30

# Said on every request, so whoever runs the site can see what is asking.
USER_AGENT = ("medialibrarian/0 (a personal media library's commentary "
              "naming; one request at a time, cached)")


def directory(script_dir: str) -> str:
    """Where the pages are kept: the checkout's ``data/dvdcompare``."""
    return os.path.join(script_dir, "data", "dvdcompare")


# --- reading a film's page ----------------------------------------------------

# A line of a release's extras that is one of the film's commentary tracks:
# "Audio commentary by ...", "Commentary with ...", "Selected Scene Audio
# Commentary by ...", "NEW audio commentary ...". Up to three words may come in
# front, so a featurette "with optional commentary" - which says far more
# first - is not one.
_COMMENTARY_LINE = re.compile(
    r"^(?:[\w'’\"-]+\s+){0,3}?(?:audio\s+)?commentar(?:y|ies)\b",
    re.IGNORECASE)

# A disc's heading: "DISC TWO (Blu-ray)", "DISC ONE (Blu-ray 4K Copy)",
# "DISC ONE SIDE A". The part in brackets says what kind of disc it is.
_DISC_HEADING = re.compile(r"^DISC\b", re.IGNORECASE)

# The formats a page title or a disc heading names, most specific first.
_KINDS = (("blu-ray 4k", "uhd"), ("uhd", "uhd"), ("4k", "uhd"),
          ("hd dvd", "hddvd"), ("blu-ray", "bluray"), ("dvd", "dvd"))

# The articles a title is filed under at its end: "Lighthouse (The)".
_FILED_ARTICLE = re.compile(
    r"^(?P<head>[^:]*?) \((?P<article>The|A|An|Der|Die|Das|Le|La|Les|L'|El|"
    r"Il|Lo|De|Het|Een)\)(?P<rest>.*)$")

# A search result's title: its names, then what the site adds - "(TV)", the
# format, and the year.
_ENTRY = re.compile(r"^(?P<names>.*?)(?P<tv> \(TV\))?"
                    r"(?: \((?P<format>Blu-ray 4K|Blu-ray|HD DVD|UMD|"
                    r"Blu-ray 3D)\))? \((?P<year>\d{4})(?:-\d{4})?\)$")


class _Page(HTMLParser):
    """The page as its release blocks: each a list of (label, description)
    pairs, the description as lines, with a disc heading's line marked."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks: list = []
        self._in_title = False
        self._depth = 0
        self._current: list | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "ul" and attributes.get("class") == "dvd":
            self._depth += 1
            self.blocks.append({"release": False, "items": []})
            return
        if not self._depth:
            return
        if tag == "a" and (attributes.get("name") or "").isdigit():
            self.blocks[-1]["release"] = True
        if tag == "div" and attributes.get("class") in ("label", "description"):
            self._current = [attributes["class"], ""]
            self.blocks[-1]["items"].append(self._current)
        elif tag == "br" and self._current is not None:
            self._current[1] += "\n"
        elif tag == "b" and self._current is not None:
            self._current[1] += "\x02"

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "ul" and self._depth:
            self._depth -= 1
            self._current = None
        elif tag == "div":
            self._current = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._current is not None:
            self._current[1] += data


def _kind_of(text: str) -> str:
    """The kind of disc a title, heading or picture format names, or ""."""
    folded = text.lower()
    for word, kind in _KINDS:
        if word in folded:
            return kind
    return ""


def _picture_kind(text: str) -> str:
    """The kind of disc a release's "Picture Format" says it is."""
    if "2160" in text:
        return "uhd"
    if "1080" in text or "720p" in text:
        return "bluray"
    if text.strip():
        return "dvd"
    return ""


def _unfiled(title: str) -> str:
    """A title filed the site's way put back the way it is said:
    "Lighthouse (The)" is "The Lighthouse", and "Lighthouse (The): The Last
    Keeper" is "The Lighthouse: The Last Keeper"."""
    match = _FILED_ARTICLE.match(title)
    if not match:
        return title
    return "%s %s%s" % (match.group("article"), match.group("head"),
                        match.group("rest"))


def _names_of(listed: str) -> list[str]:
    """Every name a listing gives a film: "Nightfall Harbour AKA Hafen der
    Nacht" is both."""
    return [_unfiled(name.strip()) for name in listed.split(" AKA ")
            if name.strip()]


def _words(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", kept.casefold()))


def _is_this_film(feature: str, names: list[str]) -> bool:
    """Whether a "* ..." section of a release's extras is this film rather
    than another one in the same set: it is "The Film", or it is called by
    one of the film's own names, a cut in brackets after it."""
    feature = feature.strip()
    if feature.lower().startswith("the film"):
        return True
    bare = re.sub(r"\s*[(\[].*$", "", feature).strip(" \"'“”")
    folded = _words(bare)
    return any(folded == _words(name) for name in names)


def _page_title(title: str) -> tuple[list[str], str]:
    """The film's names and the page's kind of disc, from the page's title:
    "Rewind @ www.dvdcompare.net - Nightfall Harbour AKA Hafen der Nacht
    (Blu-ray) (1987)"."""
    title = " ".join(title.split())
    title = title.split(" - ", 1)[1] if " - " in title else title
    match = _ENTRY.match(title)
    if not match:
        return [title], ""
    kind = _kind_of(match.group("format") or "") or "dvd"
    return _names_of(match.group("names")), kind


def parse_film_page(text: str) -> list[Release]:
    """Every release a film's page lists, with the commentaries on each of its
    discs, one :class:`Disc` for each cut of this film a disc carries."""
    page = _Page()
    page.feed(text or "")
    names, page_kind = _page_title(page.title)

    releases = []
    for block in page.blocks:
        if not block["release"]:
            continue
        items: list = []
        label = ""
        for kind, content in block["items"]:
            if kind == "label":
                label = " ".join(content.split())
            else:
                items.append((label, content))
        if not items:
            continue
        heading = " ".join(items[0][1].replace("\x02", "").split())
        fields = dict(items[1:])
        default = _picture_kind(fields.get("Picture Format:", "")) or page_kind
        discs = _discs(fields.get("Commentaries:", ""),
                       fields.get("Extras:", ""), default, names)
        releases.append(Release(heading, tuple(discs)))
    return releases


def _lines(content: str) -> list[str]:
    return [" ".join(line.split()) for line in content.split("\n")
            if line.strip()]


def _discs(commentaries: str, extras: str, default: str,
           names: list[str]) -> list[Disc]:
    """A release's commentaries, disc by disc and cut by cut.

    A "Commentaries:" block is the release's film, on the release's own kind
    of disc. In the extras a disc heading starts a disc, of the kind its
    brackets name; a "* " line starts a cut, which counts only when it is this
    film; and a commentary before any cut is the disc's film.
    """
    found: list[Disc] = []
    listed = [line for line in _lines(commentaries)
              if _COMMENTARY_LINE.match(line)]
    if listed:
        found.append(Disc(default, tuple(listed)))

    kind = default
    buckets: list[list] = []
    current: list | None = None
    for line in _lines(extras):
        if line.startswith("\x02") or _DISC_HEADING.match(line):
            heading = line.replace("\x02", "")
            if _DISC_HEADING.match(heading):
                bracket = re.search(r"\(([^)]*)\)", heading)
                kind = _kind_of(bracket.group(1)) if bracket else ""
                kind = kind or default
                current = None
                continue
            line = heading
        if line.startswith("* "):
            current = [kind, []] if _is_this_film(line[2:], names) else ["", []]
            buckets.append(current)
            continue
        if line.startswith(("-", "–")) or not _COMMENTARY_LINE.match(line):
            continue
        if current is None:
            current = [kind, []]
            buckets.append(current)
        current[1].append(line)
    for bucket_kind, entries in buckets:
        if bucket_kind and entries:
            found.append(Disc(bucket_kind, tuple(entries)))
    return found


# --- finding the film's page ----------------------------------------------------

class SearchEntry(NamedTuple):
    """One film the site's search offered: its page, its names, the kind of
    disc its page is about, its year, and whether it is television."""

    fid: str
    names: tuple[str, ...]
    kind: str
    year: str
    tv: bool


_RESULT = re.compile(r'<a\s[^>]*href="[^"]*film\.php\?fid=(\d+)"[^>]*>(.*?)</a>',
                     re.IGNORECASE | re.DOTALL)


def parse_search(text: str) -> list[SearchEntry]:
    """Every film a search results page offers."""
    import html as html_module

    found = []
    seen = set()
    for fid, inner in _RESULT.findall(text or ""):
        label = " ".join(html_module.unescape(re.sub(r"<[^>]+>", "", inner))
                         .split())
        match = _ENTRY.match(label)
        if not match or fid in seen:
            continue
        seen.add(fid)
        found.append(SearchEntry(
            fid, tuple(_names_of(match.group("names"))),
            _kind_of(match.group("format") or "") or "dvd",
            match.group("year"), bool(match.group("tv"))))
    return found


def search_query(title: str) -> str:
    """What to type into the site's search for a film: its name without the
    article the site files at the end, and only up to a subtitle, which the
    site's own spelling of may differ from the library's."""
    head = re.split(r"\s*[:–—]\s+|\s+-\s+", title, maxsplit=1)[0]
    words = head.split()
    if len(words) > 1 and words[0].lower() in ("the", "a", "an"):
        words = words[1:]
    return " ".join(words)


def pick(entries: list[SearchEntry], title: str, year: str,
         kind: str) -> str:
    """The one page of this film for this kind of disc, or "".

    The page must be of the year the library dates the film to, or failing any
    such, one year either side of it - a festival premiere and a release a year
    later are the same film - and carry the title, under any of its names, the
    way :mod:`titlematch` reads two titles as one. Television is never a film.
    Two pages that both fit are no answer at all.
    """
    wanted = titlematch.title_keys(title)
    candidates = [entry for entry in entries
                  if entry.kind == kind and not entry.tv]
    for spread in (0, 1):
        fitting = []
        for entry in candidates:
            if not year.isdigit() or not entry.year.isdigit():
                continue
            if abs(int(entry.year) - int(year)) != spread:
                continue
            if any(titlematch.title_keys(name) & wanted
                   for name in entry.names):
                fitting.append(entry.fid)
        if fitting:
            return fitting[0] if len(set(fitting)) == 1 else ""
    return ""


# --- asking the site ---------------------------------------------------------------

class Site:
    """The site, asked politely and remembered.

    ``fetch`` is handed a URL and the form fields to post (or None) and
    answers with the page's text or None; the default is curl. ``pacer``
    spaces the requests out, :data:`PACER` unless a test brings a clock of its
    own. The first time the site does not answer, nothing more is asked of it
    this run - it is down, or it has started refusing - and only the pages
    already kept are read.
    """

    def __init__(self, where: str, log: Callable[[str], None],
                 fetch: Callable | None = None,
                 pacer: politepacing.Pacer | None = None) -> None:
        self.where = where
        self.log = log
        self.fetch = fetch or _curl
        self.pacer = pacer or PACER
        self.down = False
        self._searches: dict = {}

    def releases(self, title: str, year: str, kind: str) -> list | None:
        """What the site lists for this film on this kind of disc, or None
        when it cannot say which film this is. A Blu-ray is looked for on the
        4K page as well: a 4K set carries the Blu-ray too, and lists it."""
        kinds = {"uhd": ("uhd",), "bluray": ("bluray", "uhd"),
                 "dvd": ("dvd",)}.get(kind, ())
        query = search_query(title)
        if not kinds or not query:
            return None
        entries = self._search(query)
        if entries is None:
            return None
        found = []
        answered = False
        for page_kind in kinds:
            fid = pick(entries, title, year, page_kind)
            if not fid:
                continue
            text = self._page("film-%s.html" % fid, BASE + "film.php?fid=" + fid,
                              None, FILM_PAGE_DAYS)
            if text is None:
                return None
            answered = True
            found += parse_film_page(text)
        return found if answered else None

    def _search(self, query: str) -> list | None:
        if query not in self._searches:
            key = hashlib.sha1(query.lower().encode("utf-8")).hexdigest()[:16]
            text = self._page("search-%s.html" % key, BASE + "search.php",
                              {"param": query, "searchtype": "text"},
                              SEARCH_PAGE_DAYS)
            self._searches[query] = None if text is None else parse_search(text)
        return self._searches[query]

    def _page(self, name: str, url: str, form, days: int) -> str | None:
        """A page from the ones kept, or asked for and kept."""
        path = os.path.join(self.where, name)
        try:
            if time.time() - os.path.getmtime(path) < days * 86400:
                with open(path, encoding="utf-8") as handle:
                    return handle.read()
        except OSError:
            pass
        if self.down:
            return None
        self.pacer.wait()
        text = self.fetch(url, form)
        if text is None:
            self.down = True
            self.log("WARNING: dvdcompare did not answer for %s - nothing more "
                     "is asked of it this run" % url)
            return None
        try:
            os.makedirs(self.where, exist_ok=True)
            with open(path + ".part", "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(path + ".part", path)
        except OSError:
            pass
        return text


def _curl(url: str, form) -> str | None:
    """One request, the site's own search form's way: a POST with the form's
    fields when there are any, a plain GET otherwise."""
    site = os.environ.get(SITE_VARIABLE)
    if site and url.startswith(BASE):
        url = site + url[len(BASE):]
    argv = ["curl", "-fsS", "--max-time", "90", "-A", USER_AGENT]
    for field, value in (form or {}).items():
        argv += ["--data-urlencode", "%s=%s" % (field, value)]
    try:
        done = subprocess.run(argv + [url], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return done.stdout.decode("cp1252", "replace")
