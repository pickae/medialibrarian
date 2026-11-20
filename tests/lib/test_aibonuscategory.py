"""Tests for sorting unmatched movie extras with a light language model -
medialib.lib.aibonuscategory (and cached dvdcompare extras lookup).

What is pinned:
- the prompt carries the extra name, film title/year, allowed categories, and
  cached dvdcompare extras when on disk (without touching the network);
- strict guard: only an exact category from BONUS_CATEGORIES with high
  confidence is accepted; "none", "unsure", "Featurettes", lower confidence,
  unknown names, malformed JSON, LlmError, switch off, and no model all leave
  the file unmatched ("");
- model decisions are logged and usage is recorded with modelmeter.
"""

import contextlib
import hashlib

import pytest

from medialib.lib import aibonuscategory, dvdcompare, llmclient, modelmeter
from tests import blackbox

pytestmark = pytest.mark.pure

_DVD_DATA = blackbox.DATA / "dvdcompare"


class _Model:
    """A stand-in light model recording prompts and returning ``answer``
    (or raising it, or cycling through a list of answers)."""

    def __init__(self, answer, name="stand-in"):
        self.answer = answer
        self.name = name
        self.model = name
        self.asked = []

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def ask(self, system, prompt, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(0.4, 25, 300)
        return self.answer


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aibonuscategory.SWITCH_VARIABLE, "on")


def _seed_dvdcompare_cache(cache_dir):
    """Write the test search and film HTML pages into ``cache_dir`` under the
    exact filenames dvdcompare.Site caches them as."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    query = dvdcompare.search_query("Nightfall Harbour")
    key = hashlib.sha1(query.lower().encode("utf-8")).hexdigest()[:16]
    (cache_dir / ("search-%s.html" % key)).write_text(
        (_DVD_DATA / "search.html").read_text(encoding="utf-8"),
        encoding="utf-8")
    (cache_dir / "film-12.html").write_text(
        (_DVD_DATA / "film.html").read_text(encoding="utf-8"),
        encoding="utf-8")


class TestPromptAndCache:
    def test_prompt_includes_film_extra_categories_and_cached_extras(self):
        text = aibonuscategory.prompt(
            "Making the Music.mkv",
            film_title="Nightfall Harbour",
            film_year="1987",
            disc_extras=['"Harbour Lights" featurette (8:39)', "Deleted scenes"],
        )
        assert "Film: Nightfall Harbour (1987)" in text
        assert "Extra file: Making the Music.mkv" in text
        assert "Extra title: Making the Music" in text
        assert "Behind The Scenes" in text
        assert "Cached disc extras listing (dvdcompare):" in text
        assert '"Harbour Lights" featurette (8:39)' in text

    def test_cached_extras_reads_disk_only_and_never_fetches(self, tmp_path,
                                                             monkeypatch):
        def _forbid(*args, **kwargs):
            raise AssertionError("cached_extras must never hit the network")

        monkeypatch.setattr(dvdcompare, "_curl", _forbid)
        assert dvdcompare.cached_extras(str(tmp_path), "Nightfall Harbour", "1987") == []

        _seed_dvdcompare_cache(tmp_path)
        extras = dvdcompare.cached_extras(str(tmp_path), "Nightfall Harbour", "1987")
        assert any("Harbour Lights" in line for line in extras)
        assert any("Theatrical trailer" in line for line in extras)
        assert not any(line.startswith("DISC ") for line in extras)

    def test_long_extras_listing_is_capped(self, monkeypatch):
        monkeypatch.setattr(aibonuscategory, "MAX_EXTRAS_LINES", 2)
        text = aibonuscategory.prompt(
            "Photo Gallery.mkv",
            disc_extras=["Line 1", "Line 2", "Line 3"],
        )
        assert "... and 1 more line(s)" in text


class TestStrictGuard:
    @pytest.mark.parametrize("answer,expected", [
        ('{"category": "Other", "confidence": "high", "why": "stills gallery"}',
         "Other"),
        ('{"category": "Behind The Scenes", "confidence": "high", '
         '"why": "score documentary"}', "Behind The Scenes"),
        ('```json\n{"category": "Behind The Scenes", "confidence": "high", '
         '"why": "storyboard breakdown"}\n```', "Behind The Scenes"),
    ])
    def test_exact_category_with_high_confidence_is_accepted(self, answer, expected):
        category, why = aibonuscategory.parse_verdict(answer)
        assert category == expected
        assert why

    @pytest.mark.parametrize("answer", [
        '{"category": "none", "confidence": "high", "why": "general featurette"}',
        '{"category": "unsure", "confidence": "low", "why": "ambiguous"}',
        '{"category": "Featurettes", "confidence": "high", "why": "stays put"}',
        '{"category": "Behind The Scenes", "confidence": "medium", "why": "maybe"}',
        '{"category": "Behind The Scenes", "confidence": "low", "why": "guess"}',
        '{"category": "Behind The Scenes", "why": "no confidence field"}',
        '{"category": "Documentaries", "confidence": "high", "why": "invented"}',
        '{"category": "behind the scenes", "confidence": "high", "why": "wrong case"}',
        '{"category": 123, "confidence": "high"}',
        'not json at all',
        '["Behind The Scenes"]',
        '',
    ])
    def test_none_low_confidence_unknown_or_bad_json_returns_empty(self, answer):
        category, _why = aibonuscategory.parse_verdict(answer)
        assert category == ""


class TestClassify:
    @pytest.mark.parametrize("filename,reply,expected", [
        ("Photo Gallery.mkv",
         '{"category": "Other", "confidence": "high", "why": "photo gallery"}',
         "Other"),
        ("Storyboard Comparison.mkv",
         '{"category": "Behind The Scenes", "confidence": "high", '
         '"why": "production storyboard breakdown"}',
         "Behind The Scenes"),
        ("Making the Music.mkv",
         '{"category": "Behind The Scenes", "confidence": "high", '
         '"why": "scoring featurette"}',
         "Behind The Scenes"),
        ("Featurette.mkv",
         '{"category": "none", "confidence": "high", "why": "generic featurette"}',
         ""),
    ])
    def test_unmatched_extras_classified_logged_and_metered(
            self, tmp_path, capsys, filename, reply, expected):
        cache = tmp_path / "dvdcompare"
        _seed_dvdcompare_cache(cache)
        model = _Model(reply)
        meter = tmp_path / "meter.jsonl"

        got = aibonuscategory.classify(
            filename,
            film_title="Nightfall Harbour",
            film_year="1987",
            dvdcompare_dir=str(cache),
            models=[model],
            meter_file=str(meter),
        )
        assert got == expected
        assert len(model.asked) == 1
        assert "Harbour Lights" in model.asked[0][1]

        err = capsys.readouterr().err
        if expected:
            assert "%s -> %s" % (filename, expected) in err
        else:
            assert "%s -> left in Featurettes" % filename in err

        _spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 25, 300)

    def test_switch_off_never_asks_the_model(self, monkeypatch):
        monkeypatch.setenv(aibonuscategory.SWITCH_VARIABLE, "off")
        model = _Model(
            '{"category": "Other", "confidence": "high", "why": "gallery"}')
        assert aibonuscategory.classify("Photo Gallery.mkv", models=[model]) == ""
        assert model.asked == []

    def test_no_model_available_returns_empty(self, monkeypatch):
        for name in (llmclient.API_VARIABLE, llmclient.URL_VARIABLE,
                     llmclient.HARNESS_VARIABLE):
            monkeypatch.delenv(name, raising=False)
        assert aibonuscategory.classify("Photo Gallery.mkv") == ""

    def test_llm_error_falls_through_ladder_or_returns_empty(self, tmp_path):
        broken = _Model(llmclient.LlmError("server down"), name="first")
        backup = _Model(
            '{"category": "Other", "confidence": "high", "why": "gallery"}',
            name="second")
        meter = tmp_path / "meter.jsonl"
        assert aibonuscategory.classify(
            "Photo Gallery.mkv", models=[broken, backup],
            meter_file=str(meter)) == "Other"
        assert len(broken.asked) == 1 and len(backup.asked) == 1
        assert modelmeter.gathered(str(meter))[1].requests == 1

        only_broken = _Model(llmclient.LlmError("timeout"), name="only")
        assert aibonuscategory.classify(
            "Photo Gallery.mkv", models=[only_broken]) == ""
