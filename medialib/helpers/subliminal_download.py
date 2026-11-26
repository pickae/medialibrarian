# /// script
# requires-python = ">=3.10"
# dependencies = ["subliminal>=2.6,<3"]
# ///
"""Download one language's subtitle for one film from one provider.

    subliminal_download.py <language> <imdb id or ""> <video> <provider> [<id>...]
    subliminal_download.py <language>,<language>... <imdb id or ""> <video> <provider>

What ``subliminal download -p <provider> -l <language> <video>`` does, with
two things its command line has no way to say. The first is WHICH film this
is. The name a film carries its IMDb id in - ``{imdb-tt0000001}`` for Plex,
``[imdbid-tt0000001]`` for Jellyfin - reads to subliminal's guesser as a
release group, so the id is dropped and the search is by title, which a
remake, a same-named film of another year or a title the catalogue spells
differently all answer. Given the id, a subtitle the provider
files under any other film is not downloaded at all: none is better than the
wrong one.

The second is the subtitles already tried: each ``<id>`` after the provider is
one of its subtitles that was downloaded before and thrown out, and is not
offered again, so the next call answers with the next best.

SubDL is not one of subliminal's providers, and is asked here directly - and
only by id, so a film without one gets nothing from it.

The id of the subtitle downloaded is printed. Nothing is printed when the
provider has none to offer, and the exit status is ``ALREADY_THERE`` when the
video already has the language, beside it or inside it, so that no other
provider need be asked. The subtitle is saved beside the video as
``<video stem>.<language>.srt``.

Given several languages, comma separated, the provider is asked for all of them
in one search and the best of each is saved; one line is printed per language,
its code, a tab and what became of it: ``already``, ``none``, or ``saved``, a
tab and the subtitle's id. The exit status is 0 whatever each language's answer
was. It is the first try of each, so no ids are thrown out.

An episode is asked for by its SERIES' IMDb id with its season and episode,
``tt0000001:S01E02``: only a subtitle filed under that series, season and
episode is taken.

A provider's login comes from the environment rather than from argv, which
every account on the machine can read: ``openSubtitlesUser`` and
``openSubtitlesPassword`` for opensubtitles, ``openSubtitlesComUser`` and
``openSubtitlesComPassword`` for opensubtitlescom, ``subDlApiKey`` for subdl.
"""

import io
import os
import re
import sys
import zipfile
from urllib.parse import urljoin

import requests
from babelfish import Language
from subliminal import ProviderPool, check_video, refine, region, save_subtitles, scan_video
from subliminal.core import search_external_subtitles
from subliminal.subtitle import Subtitle

ALREADY_THERE = 3

# The environment variables each of subliminal's providers that takes a login
# reads it from.
LOGINS = {
    "opensubtitles": ("openSubtitlesUser", "openSubtitlesPassword"),
    "opensubtitlescom": ("openSubtitlesComUser", "openSubtitlesComPassword"),
}

# Options handed to a provider whatever the login. OpenSubtitles.com pages
# through a search by title alone as well, page after page until the API
# answers one with "Bad Request" - which throws away everything the searches by
# id had found.
OPTIONS = {
    "opensubtitlescom": {"max_result_pages": 3},
}

SUBDL_KEY = "subDlApiKey"
_SUBDL_SEARCH = "https://api.subdl.com/api/v1/subtitles"
_SUBDL_FILES = "https://dl.subdl.com"
_TIMEOUT = 30

# The tags a server name carries - the id, written the way either server reads
# it, and the edition - which are nothing a guesser can read, so the guesser is
# handed the name without them.
_CURLY_TAG = re.compile(r"\s*\{[^{}]*\}")
_BRACKET_ID = re.compile(r"\s*\[(?:imdbid|tmdbid|imdb|tmdb)-[^\[\]]*\]", re.I)
_PART_TAIL = r"(\s+(?:cd|dvd|part|pt|disk|disc)[\s._-]*[0-9]+)?(\.[A-Za-z0-9]+)?$"


def _guess_name(path: str) -> str:
    """``path`` with the id tag and the edition off: the title the search is
    by.

    The id tag reads to a guesser as a release group - the Plex curlies and the
    Jellyfin brackets alike - so it is dropped, and the edition comes off with
    it: the Plex tag is among the curlies, and the version label a Jellyfin
    name writes after its id is the name's last " - ...", a part token where
    the name ends on one kept, the way the curlies keep it. A dash with no id
    tag on either side is the name's own, and stays.
    """
    last = None
    for tag in _BRACKET_ID.finditer(path):
        last = tag
    if last:
        title, tail = path[:last.start()], path[last.end():]
        match = re.match(r"(?:\s*-\s*)?(.*?)" + _PART_TAIL, tail, re.I | re.S)
        if match:
            tail = (match.group(2) or "") + (match.group(3) or "")
        path = title + tail
    return _CURLY_TAG.sub("", _BRACKET_ID.sub("", path))


_EPISODE_ID = re.compile(r"^(tt[0-9]+|[0-9]+):S([0-9]+)E([0-9]+)$", re.I)


def _episode_of(imdb: str) -> tuple:
    """(series IMDb id, season, episode) of an episode's id, or (imdb, None,
    None) for a film's."""
    match = _EPISODE_ID.match(imdb or "")
    if not match:
        return imdb, None, None
    return match.group(1), int(match.group(2)), int(match.group(3))


def _same_episode(subtitle, number: int, season: int, episode: int) -> bool:
    """Whether a provider files ``subtitle`` under this series' episode: the
    series' IMDb id where the provider says it, and the season and episode."""
    series = None
    for name in ("series_imdb_id", "imdb_id"):
        series = _imdb_number(getattr(subtitle, name, None))
        if series is not None:
            break
    if series is not None and series != number:
        return False
    for names, wanted in ((("season", "series_season"), season),
                          (("episode", "series_episode"), episode)):
        for name in names:
            value = getattr(subtitle, name, None)
            if value is not None:
                if str(value).isdigit() and int(value) != wanted:
                    return False
                break
    return True


def _imdb_number(value) -> int | None:
    """An IMDb id as a number, so "tt0000001" and "tt1" are the same film."""
    digits = str(value or "").lower().removeprefix("tt")
    return int(digits) if digits.isdigit() else None


def _scan(path: str):
    name = _guess_name(path)
    try:
        return scan_video(path, name=name)
    except ValueError:
        # The command line's own fallback: a relative name the guesser cannot
        # read is tried again spelled from the root.
        return scan_video(os.path.abspath(path), name=os.path.abspath(name))


def _subliminal(provider: str, video, languages: set, number: int | None,
                thrown: list[str], season=None, episode=None) -> list:
    """The best subtitle of each language one of subliminal's providers has,
    downloaded."""
    config: dict[str, dict] = {provider: dict(OPTIONS.get(provider, {}))}
    if provider in LOGINS:
        user, password = LOGINS[provider]
        config[provider].update(username=os.environ.get(user, ""),
                                password=os.environ.get(password, ""))
    with ProviderPool(providers=[provider], provider_configs=config) as pool:
        found = [s for s in pool.list_subtitles(video, set(languages))
                 if str(s.id) not in thrown]
        if number is not None and season is not None:
            found = [s for s in found
                     if _same_episode(s, number, season, episode)]
        elif number is not None:
            found = [s for s in found
                     if _imdb_number(getattr(s, "movie_imdb_id", None)) == number]
        return pool.download_best_subtitles(found, video, set(languages))


def _srt_in(archive: bytes) -> bytes | None:
    """The subtitle a downloaded zip holds: its largest .srt."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as packed:
            names = [i for i in packed.infolist()
                     if i.filename.lower().endswith(".srt")]
            if not names:
                return None
            return packed.read(max(names, key=lambda i: i.file_size))
    except (zipfile.BadZipFile, OSError):
        return None


def _subdl(language, number: int | None, thrown: list[str], season=None,
           episode=None) -> list:
    """The first subtitle SubDL files under the film - or the series'
    episode - in the order it lists them, downloaded, leaving out the machine
    translations."""
    if number is None:
        return []
    code = language.alpha2.upper()
    params = {"api_key": os.environ.get(SUBDL_KEY, ""),
              "imdb_id": "tt%07d" % number, "type": "movie",
              "languages": code, "subs_per_page": 30}
    if season is not None:
        # SubDL's television search: the series' id, and the episode by its
        # season and number.
        params.update(type="tv", season_number=season, episode_number=episode)
    try:
        answer = requests.get(_SUBDL_SEARCH, timeout=_TIMEOUT,
                              params=params).json()
    except (requests.RequestException, ValueError):
        return []
    if not answer.get("status"):
        return []
    for item in answer.get("subtitles") or []:
        name = str(item.get("name") or "")
        if (not name or name in thrown or item.get("ai_translated")
                or str(item.get("language", "")).upper() != code):
            continue
        if season is not None and str(item.get("episode") or "").isdigit() \
                and int(item["episode"]) != episode:
            continue
        try:
            fetched = requests.get(urljoin(_SUBDL_FILES, item.get("url", "")),
                                   timeout=_TIMEOUT)
            fetched.raise_for_status()
        except requests.RequestException:
            continue
        content = _srt_in(fetched.content)
        if content:
            subtitle = Subtitle(language, name)
            subtitle.content = content
            return [subtitle]
    return []


def main(argv: list[str]) -> int:
    code, imdb, path, provider, *thrown = argv
    imdb, season, episode = _episode_of(imdb)
    if "," in code:
        return _main_many(code.split(","), imdb, path, provider, season,
                          episode)
    region.configure("dogpile.cache.memory")
    language = Language.fromietf(code)
    video = _scan(path)
    video.subtitles.extend(search_external_subtitles(video.name).values())
    if not check_video(video, languages={language}):
        return ALREADY_THERE
    # The hash refiner knows only subliminal's own providers.
    refine(video, embedded_subtitles=True,
           providers=[] if provider == "subdl" else [provider],
           languages={language})
    if language in video.subtitle_languages:
        return ALREADY_THERE
    number = _imdb_number(imdb)
    if number is not None:
        # After the refiners, which search by title and would put their own
        # answer here.
        _set_id(video, number, season)
    if provider == "subdl":
        best = _subdl(language, number, thrown, season, episode)
    else:
        best = _subliminal(provider, video, {language}, number, thrown,
                           season, episode)
    saved = save_subtitles(video, best, encoding="utf-8")
    if saved:
        print(saved[0].id)
    return 0


def _set_id(video, number: int, season) -> None:
    """The id a provider searches the video by: the film's, or for an
    episode its series'."""
    if season is None:
        video.imdb_id = "tt%07d" % number
    else:
        video.series_imdb_id = "tt%07d" % number


def _main_many(codes: list[str], imdb: str, path: str, provider: str,
               season=None, episode=None) -> int:
    """The first try of every language in ``codes``, in one search."""
    region.configure("dogpile.cache.memory")
    asked = [(code, Language.fromietf(code)) for code in codes if code]
    video = _scan(path)
    video.subtitles.extend(search_external_subtitles(video.name).values())
    wanted = {language for _code, language in asked
              if check_video(video, languages={language})}
    if wanted:
        refine(video, embedded_subtitles=True,
               providers=[] if provider == "subdl" else [provider],
               languages=wanted)
        wanted = {language for language in wanted
                  if language not in video.subtitle_languages}
    number = _imdb_number(imdb)
    if number is not None:
        _set_id(video, number, season)
    best: list = []
    if wanted:
        if provider == "subdl":
            for _code, language in asked:
                if language in wanted:
                    best += _subdl(language, number, [], season, episode)
        else:
            best = _subliminal(provider, video, wanted, number, [], season,
                               episode)
    saved = {subtitle.language: subtitle
             for subtitle in save_subtitles(video, best, encoding="utf-8")}
    for code, language in asked:
        if language not in wanted:
            print("%s\talready" % code)
        elif language in saved:
            print("%s\tsaved\t%s" % (code, saved[language].id))
        else:
            print("%s\tnone" % code)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
