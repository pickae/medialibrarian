"""The white box for the subliminal helper's first try of several languages:
one search for all of them, one line each for what became of it.

The helper runs in subliminal's own environment, so subliminal, babelfish and
requests are stood in for here by just enough of each for the helper to load
and for the one search to be watched.
"""

import importlib.util
import sys
import types

import pytest

from medialib import helpers

pytestmark = pytest.mark.pure


class _Language:
    def __init__(self, code: str) -> None:
        self.alpha2 = code

    @classmethod
    def fromietf(cls, code: str) -> "_Language":
        return cls(code)

    def __eq__(self, other) -> bool:
        return isinstance(other, _Language) and other.alpha2 == self.alpha2

    def __hash__(self) -> int:
        return hash(self.alpha2)


class _Subtitle:
    def __init__(self, language, subtitle_id) -> None:
        self.language = language
        self.id = subtitle_id
        self.movie_imdb_id = "tt0000007"


def _load(monkeypatch, has: set, offered: dict):
    """The helper, loaded against stand-ins: the video already ``has`` those
    languages, and the provider ``offered`` {code: subtitle id}."""
    searches: list = []

    class Video:
        def __init__(self, path):
            self.name = path
            self.subtitles: list = []
            self.subtitle_languages = {_Language(code) for code in has}
            self.imdb_id = None

    class Pool:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def list_subtitles(self, _video, languages):
            searches.append({language.alpha2 for language in languages})
            return [_Subtitle(_Language(code), offered[code])
                    for code in sorted(offered)
                    if _Language(code) in languages]

        def download_best_subtitles(self, found, _video, languages):
            return [subtitle for subtitle in found
                    if subtitle.language in languages]

    subliminal = types.ModuleType("subliminal")
    subliminal.ProviderPool = Pool
    subliminal.check_video = lambda video, languages: bool(
        languages - video.subtitle_languages)
    subliminal.refine = lambda *_a, **_k: None
    subliminal.region = types.SimpleNamespace(configure=lambda *_a: None)
    subliminal.save_subtitles = lambda _video, subtitles, encoding: subtitles
    subliminal.scan_video = lambda path, name=None: Video(path)
    core = types.ModuleType("subliminal.core")
    core.search_external_subtitles = lambda _name: {}
    subtitle = types.ModuleType("subliminal.subtitle")
    subtitle.Subtitle = _Subtitle
    babelfish = types.ModuleType("babelfish")
    babelfish.Language = _Language
    requests = types.ModuleType("requests")
    requests.RequestException = Exception
    for name, module in (("subliminal", subliminal),
                         ("subliminal.core", core),
                         ("subliminal.subtitle", subtitle),
                         ("babelfish", babelfish), ("requests", requests)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "subliminal_download_under_test",
        helpers.path_of("subliminal_download.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper, searches


def test_every_language_is_asked_in_one_search(monkeypatch, capsys):
    helper, searches = _load(monkeypatch, has={"it"}, offered={"en": "41"})
    assert helper.main(["en,de,it", "tt0000007", "Movie.mkv",
                        "opensubtitles"]) == 0
    assert searches == [{"en", "de"}]
    assert capsys.readouterr().out == ("en\tsaved\t41\n"
                                       "de\tnone\n"
                                       "it\talready\n")


def test_a_film_that_has_them_all_asks_no_one(monkeypatch, capsys):
    helper, searches = _load(monkeypatch, has={"en", "de"}, offered={})
    assert helper.main(["en,de", "", "Movie.mkv", "opensubtitles"]) == 0
    assert searches == []
    assert capsys.readouterr().out == "en\talready\nde\talready\n"


def test_one_language_is_still_the_call_it_always_was(monkeypatch, capsys):
    helper, searches = _load(monkeypatch, has=set(), offered={"en": "41"})
    assert helper.main(["en", "tt0000007", "Movie.mkv", "opensubtitles"]) == 0
    assert searches == [{"en"}]
    assert capsys.readouterr().out == "41\n"
