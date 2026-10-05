# /// script
# requires-python = ">=3.10"
# dependencies = ["subliminal>=2.6,<3"]
# ///
"""Download one language's subtitle for one film from one provider.

    subliminal_download.py <language> <imdb id or ""> <video> <provider> [<id>...]

What ``subliminal download -p <provider> -l <language> <video>`` does, with
two things its command line has no way to say. The first is WHICH film this
is. The name a film carries its IMDb id in - ``{imdb-tt0000001}`` - reads to
subliminal's guesser as a release group, so the id is dropped and the search
is by title, which a remake, a same-named film of another year or a title the
catalogue spells differently all answer. Given the id, a subtitle the provider
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

# The tags a Plex name carries - the id, an edition - which are nothing a
# guesser can read, so it is handed the name without them.
_TAG = re.compile(r"\s*\{[^{}]*\}")


def _imdb_number(value) -> int | None:
    """An IMDb id as a number, so "tt0000001" and "tt1" are the same film."""
    digits = str(value or "").lower().removeprefix("tt")
    return int(digits) if digits.isdigit() else None


def _scan(path: str):
    name = _TAG.sub("", path)
    try:
        return scan_video(path, name=name)
    except ValueError:
        # The command line's own fallback: a relative name the guesser cannot
        # read is tried again spelled from the root.
        return scan_video(os.path.abspath(path), name=os.path.abspath(name))


def _subliminal(provider: str, video, language, number: int | None,
                thrown: list[str]) -> list:
    """The best subtitle one of subliminal's providers has, downloaded."""
    config: dict[str, dict] = {provider: dict(OPTIONS.get(provider, {}))}
    if provider in LOGINS:
        user, password = LOGINS[provider]
        config[provider].update(username=os.environ.get(user, ""),
                                password=os.environ.get(password, ""))
    with ProviderPool(providers=[provider], provider_configs=config) as pool:
        found = [s for s in pool.list_subtitles(video, {language})
                 if str(s.id) not in thrown]
        if number is not None:
            found = [s for s in found
                     if _imdb_number(getattr(s, "movie_imdb_id", None)) == number]
        return pool.download_best_subtitles(found, video, {language})


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


def _subdl(language, number: int | None, thrown: list[str]) -> list:
    """The first subtitle SubDL files under the film, in the order it lists
    them, downloaded - leaving out the machine translations."""
    if number is None:
        return []
    code = language.alpha2.upper()
    try:
        answer = requests.get(_SUBDL_SEARCH, timeout=_TIMEOUT, params={
            "api_key": os.environ.get(SUBDL_KEY, ""),
            "imdb_id": "tt%07d" % number, "type": "movie",
            "languages": code, "subs_per_page": 30}).json()
    except (requests.RequestException, ValueError):
        return []
    if not answer.get("status"):
        return []
    for item in answer.get("subtitles") or []:
        name = str(item.get("name") or "")
        if (not name or name in thrown or item.get("ai_translated")
                or str(item.get("language", "")).upper() != code):
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
        video.imdb_id = "tt%07d" % number
    if provider == "subdl":
        best = _subdl(language, number, thrown)
    else:
        best = _subliminal(provider, video, language, number, thrown)
    saved = save_subtitles(video, best, encoding="utf-8")
    if saved:
        print(saved[0].id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
