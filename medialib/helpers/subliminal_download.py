# /// script
# requires-python = ">=3.10"
# dependencies = ["subliminal>=2.6,<3"]
# ///
"""Download one language's subtitle for one film from OpenSubtitles.

    subliminal_download.py <language> <imdb id or ""> <video>

What ``subliminal download -p opensubtitles -l <language> <video>`` does, with
one thing its command line has no way to say: WHICH film this is. The name a
film carries its IMDb id in - ``{imdb-tt0000001}`` - reads to subliminal's
guesser as a release group, so the id is dropped and the search is by title,
which a remake, a same-named film of another year or a title the catalogue
spells differently all answer. Given the id, the search asks for that film,
and a subtitle the catalogue files under any other film is not downloaded at
all: none is better than the wrong one.

The credentials come from the environment (``openSubtitlesUser``,
``openSubtitlesPassword``) rather than from argv, which every account on the
machine can read. The subtitle is saved beside the video as
``<video stem>.<language>.srt``; a language the video already has, beside it or
inside it, is not downloaded again.
"""

import os
import re
import sys

from babelfish import Language
from subliminal import ProviderPool, check_video, refine, region, save_subtitles, scan_video
from subliminal.core import search_external_subtitles

PROVIDER = "opensubtitles"

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


def main(argv: list[str]) -> int:
    code, imdb, path = argv
    region.configure("dogpile.cache.memory")
    language = Language.fromietf(code)
    video = _scan(path)
    video.subtitles.extend(search_external_subtitles(video.name).values())
    if not check_video(video, languages={language}):
        return 0
    refine(video, embedded_subtitles=True, providers=[PROVIDER], languages={language})
    wanted = {language} - video.subtitle_languages
    if not wanted:
        return 0
    number = _imdb_number(imdb)
    if number is not None:
        # After the refiners, which search by title and would put their own
        # answer here.
        video.imdb_id = "tt%07d" % number
    config = {"username": os.environ.get("openSubtitlesUser", ""),
              "password": os.environ.get("openSubtitlesPassword", "")}
    with ProviderPool(providers=[PROVIDER], provider_configs={PROVIDER: config}) as pool:
        found = pool.list_subtitles(video, wanted)
        if number is not None:
            found = [s for s in found
                     if _imdb_number(getattr(s, "movie_imdb_id", None)) == number]
        best = pool.download_best_subtitles(found, video, wanted)
    save_subtitles(video, best, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
