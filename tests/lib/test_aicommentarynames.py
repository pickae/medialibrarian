"""Tests for the guarded LLM fallback for commentary track matching and
disc-page disambiguation - medialib.lib.aicommentarynames.

What is pinned: the switch (`commentaryNamesLlm`) gates both operations;
prompts include the opening ~10 minutes of transcript / film identifiers and
offered candidates; answers are strictly guarded so invented listings, bad
permutations, invented page fids, booleans and "unsure" all refuse safely;
usage is recorded through `Usage` and `modelmeter`; and web-capable models
use `searching` while direct models fall back to `serving`.
"""

import json
from contextlib import contextmanager

import pytest

from medialib.lib import (
    aicommentarynames,
    commentarynames,
    dvdcompare,
    llmclient,
)
from medialib.lib.commentarynames import FileCommentary

pytestmark = pytest.mark.pure


DIRECTOR = "Audio commentary by director Wenna Castellane"
CAST = "Audio commentary with actors Oriel Pask and Tamsin Voller"
CRITIC = "Audio commentary by film historian Maren Halvorsen"


def _srt(*cues: tuple[str, str, str]) -> str:
    return "\n".join(
        "%d\n%s --> %s\n%s\n" % (index, start, end, text)
        for index, (start, end, text) in enumerate(cues, start=1))


def _fake_model(name: str = "test-model", *, web: bool = False) -> llmclient.Llm:
    return llmclient.Llm(
        url="http://localhost:9/v1", model=name, name=name,
        api="anthropic" if web else "openai")


class _StubServer:
    """Fake ``llm.serving`` / ``llm.searching`` that records calls and returns
    scripted text replies (or raises scripted errors)."""

    def __init__(self, script, *, web: bool = False):
        self.script = list(script)
        self.web = web
        self.calls: list[dict] = []

    def install(self, monkeypatch, model: llmclient.Llm) -> None:
        @contextmanager
        def _serve(detail=None):
            self.calls.append({"mode": "serving", "model": model.name})
            yield model

        def _search(purpose):
            if not self.web:
                return None
            self.calls.append({"mode": "searching", "purpose": purpose,
                               "model": model.name})
            return model

        def _ask(system, prompt, tally=None, **_kwargs):
            self.calls.append({"mode": "ask", "model": model.name,
                               "system": system, "prompt": prompt})
            next_item = self.script.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            if tally is not None:
                tally.add(0.25, 42)
            return next_item

        monkeypatch.setattr(model, "serving", _serve)
        monkeypatch.setattr(model, "searching", _search)
        monkeypatch.setattr(model, "ask", _ask)


class TestTheSwitch:
    def test_on_by_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(aicommentarynames.SWITCH_VARIABLE, raising=False)
        assert aicommentarynames.enabled() is True

    def test_off_disables_it(self, monkeypatch):
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "off")
        assert aicommentarynames.enabled() is False

    def test_on_enables_it(self, monkeypatch):
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "on")
        assert aicommentarynames.enabled() is True


class TestTracksPromptAndParsing:
    def test_prompt_includes_film_candidates_and_opening_transcripts(self):
        tracks = [FileCommentary("2", "Commentary 1"),
                  FileCommentary("3", "Commentary 2")]
        candidates = [
            ((DIRECTOR, CAST), ["Region A"]),
            ((DIRECTOR, CRITIC), ["Region B"]),
        ]
        openings = [
            commentarynames.opening_text(_srt(
                ("00:00:05,000", "00:00:10,000", "Welcome everyone."),
                ("00:15:00,000", "00:15:05,000", "Later past ten minutes."))),
            commentarynames.opening_text(_srt(
                ("00:00:12,000", "00:00:18,000", "Hallo zusammen."))),
        ]
        prompt = aicommentarynames.tracks_prompt(
            tracks, candidates, openings, "Nightfall Harbour",
            year="1987", disc_kind="bluray")

        assert "Nightfall Harbour (1987) [bluray]" in prompt
        assert "Listing 1 (Region A):" in prompt
        assert "Listing 2 (Region B):" in prompt
        assert DIRECTOR in prompt
        assert CRITIC in prompt
        assert "Welcome everyone." in prompt
        assert "Later past ten minutes." not in prompt
        assert "Hallo zusammen." in prompt

    def test_prompt_truncates_huge_transcripts(self):
        tracks = [FileCommentary("2", "Commentary 1")]
        huge = "word " * 5000
        prompt = aicommentarynames.tracks_prompt(
            tracks, [((DIRECTOR,), ["Region A"])], [huge], "Film")
        assert "[...]" in prompt

    def test_parse_accepts_valid_json_even_in_markdown_fence(self):
        # Listing 2, Track 1 -> entry 2, Track 2 -> entry 1 => order=(1, 0)
        text = 'Sure:\n```json\n{"listing": 2, "tracks": [2, 1], "why": "matched voices"}\n```'
        assert aicommentarynames.parse_tracks_answer(text, 2, 2) == (
            1, (1, 0), "matched voices", False)

    def test_parse_converts_track_mapping_to_entry_indexed_order(self):
        # 3 tracks: track 1 -> entry 3, track 2 -> entry 1, track 3 -> entry 2
        # entry 1 sits on track 1 (idx 1), entry 2 on track 2 (idx 2), entry 3 on track 0 (idx 0)
        text = '{"listing": 1, "tracks": [3, 1, 2], "why": "three speakers"}'
        assert aicommentarynames.parse_tracks_answer(text, 1, 3) == (
            0, (1, 2, 0), "three speakers", False)

    def test_parse_handles_unsure(self):
        text = '{"unsure": true, "why": "neither speaker gave a name"}'
        assert aicommentarynames.parse_tracks_answer(text, 2, 2) == (
            None, None, "neither speaker gave a name", True)

    @pytest.mark.parametrize("payload", [
        {"listing": 3, "tracks": [1, 2], "why": "listing out of range"},
        {"listing": 0, "tracks": [1, 2], "why": "zero listing"},
        {"listing": True, "tracks": [1, 2], "why": "bool listing"},
        {"listing": 1, "tracks": [1, 1], "why": "duplicate entry"},
        {"listing": 1, "tracks": [1], "why": "wrong length"},
        {"listing": 1, "tracks": [1, 3], "why": "entry out of range"},
        {"listing": 1, "tracks": [False, True], "why": "bools in tracks"},
        {"listing": "1", "tracks": [1, 2], "why": "string listing"},
        {"listing": 1, "tracks": "1,2", "why": "non-list tracks"},
    ])
    def test_parse_rejects_invalid_or_invented_mappings(self, payload):
        listing, order, _why, unsure = aicommentarynames.parse_tracks_answer(
            json.dumps(payload), 2, 2)
        assert listing is None and order is None and unsure is False

    def test_parse_rejects_non_json(self):
        listing, order, _why, unsure = aicommentarynames.parse_tracks_answer(
            "I think track 1 is 2", 2, 2)
        assert listing is None and order is None and unsure is False


class TestPagePromptAndParsing:
    FITTING = [
        dvdcompare.SearchEntry("101", ("Nightfall Harbour",), "bluray", "1987", False),
        dvdcompare.SearchEntry("202", ("Nightfall Harbour", "Nachthafen"), "bluray", "1987", False),
    ]

    def test_prompt_includes_film_ids_aliases_and_all_candidates(self):
        prompt = aicommentarynames.page_prompt(
            self.FITTING, "Nightfall Harbour", "1987", "bluray",
            tmdb_id="55123", imdb_id="tt0093123",
            aliases=("Nachthafen", "Port de nuit"))
        assert "Nightfall Harbour" in prompt
        assert "1987" in prompt
        assert "TMDb id: 55123" in prompt
        assert "IMDb id: tt0093123" in prompt
        assert "Nachthafen" in prompt
        assert "Port de nuit" in prompt
        assert "fid=101" in prompt and "fid=202" in prompt

    def test_parse_accepts_offered_fid_string_or_int(self):
        allowed = ["101", "202"]
        assert aicommentarynames.parse_page_answer(
            '{"fid": "202", "why": "matches TMDb entry"}', allowed) == (
            "202", "matches TMDb entry", False)
        assert aicommentarynames.parse_page_answer(
            '{"fid": 101, "why": "correct film"}', allowed) == (
            "101", "correct film", False)

    def test_parse_rejects_invented_fid_or_bool(self):
        allowed = ["101", "202"]
        fid, _why, unsure = aicommentarynames.parse_page_answer(
            '{"fid": "999", "why": "invented"}', allowed)
        assert fid == "" and unsure is False
        fid, _why, unsure = aicommentarynames.parse_page_answer(
            '{"fid": true, "why": "bool"}', allowed)
        assert fid == "" and unsure is False

    def test_parse_handles_null_or_unsure(self):
        allowed = ["101", "202"]
        assert aicommentarynames.parse_page_answer(
            '{"fid": null, "why": "both could fit"}', allowed) == (
            "", "both could fit", True)
        assert aicommentarynames.parse_page_answer(
            '{"unsure": true, "why": "ambiguous"}', allowed) == (
            "", "ambiguous", True)


class TestMatchTracksAndPickPage:
    def test_match_tracks_returns_empty_when_switch_off(self, monkeypatch):
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "off")
        model = _fake_model("m1")
        stub = _StubServer(['{"listing": 1, "tracks": [2, 1]}'])
        stub.install(monkeypatch, model)
        result = aicommentarynames.match_tracks(
            [FileCommentary("2", "Commentary 1"),
             FileCommentary("3", "Commentary 2")],
            [((DIRECTOR, CAST), ["Region A"])],
            [_srt(("00:00:05,000", "00:00:10,000", "Hello")),
             _srt(("00:00:05,000", "00:00:10,000", "Hi"))],
            "Nightfall Harbour", models=[model])
        assert result == (None, None, "")
        assert stub.calls == []

    def test_match_tracks_uses_searching_for_web_model_and_records_meter(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "on")
        meter = tmp_path / "meter.txt"
        model = _fake_model("claude-web", web=True)
        stub = _StubServer([
            '{"listing": 1, "tracks": [2, 1], "why": "German intro names actors on track 1"}',
        ], web=True)
        stub.install(monkeypatch, model)

        usage = aicommentarynames.Usage()
        logged: list[str] = []
        listing, order, note = aicommentarynames.match_tracks(
            [FileCommentary("2", "Commentary 1"),
             FileCommentary("3", "Commentary 2")],
            [((DIRECTOR, CAST), ["Region A"])],
            [_srt(("00:00:05,000", "00:00:10,000", "Guten Tag, hier sind Oriel und Tamsin.")),
             _srt(("00:00:05,000", "00:00:10,000", "Guten Tag, ich bin die Regisseurin."))],
            "Nightfall Harbour", year="1987", disc_kind="bluray",
            models=[model], usage=usage, meter_file=str(meter),
            log=logged.append)

        assert (listing, order) == (0, (1, 0))
        assert "claude-web" in note
        assert "German intro" in note
        assert any(c.get("mode") == "searching" for c in stub.calls)
        assert not any(c.get("mode") == "serving" for c in stub.calls)
        assert usage.requests == 1
        assert "commentary" in usage.line()
        assert meter.is_file() and meter.read_text(encoding="utf-8").strip()

    def test_match_tracks_uses_serving_when_no_web_and_falls_back_on_error(
            self, monkeypatch):
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "on")
        first = _fake_model("broken-model")
        second = _fake_model("backup-model")
        stub1 = _StubServer([llmclient.LlmError("timeout")], web=False)
        stub2 = _StubServer([
            '{"unsure": true, "why": "no names spoken"}',
        ], web=False)
        stub1.install(monkeypatch, first)
        stub2.install(monkeypatch, second)

        usage = aicommentarynames.Usage()
        logged: list[str] = []
        listing, order, note = aicommentarynames.match_tracks(
            [FileCommentary("2", "Commentary 1"),
             FileCommentary("3", "Commentary 2")],
            [((DIRECTOR, CAST), ["Region A"])],
            [_srt(("00:00:05,000", "00:00:10,000", "Music plays.")),
             _srt(("00:00:05,000", "00:00:10,000", "More music."))],
            "Nightfall Harbour", models=[first, second],
            usage=usage, log=logged.append)

        assert (listing, order) == (None, None)
        assert "unsure" in note and "backup-model" in note
        assert any(c.get("mode") == "serving" for c in stub2.calls)
        assert usage.requests == 1
        assert any("timeout" in line for line in logged)

    def test_match_tracks_refuses_invented_order(self, monkeypatch):
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "on")
        model = _fake_model("m1")
        _StubServer(['{"listing": 1, "tracks": [1, 1]}']).install(
            monkeypatch, model)
        listing, order, note = aicommentarynames.match_tracks(
            [FileCommentary("2", "Commentary 1"),
             FileCommentary("3", "Commentary 2")],
            [((DIRECTOR, CAST), ["Region A"])],
            [_srt(("00:00:05,000", "00:00:10,000", "A")),
             _srt(("00:00:05,000", "00:00:10,000", "B"))],
            "Nightfall Harbour", models=[model])
        assert (listing, order) == (None, None)
        assert "unusable" in note and "m1" in note

    def test_pick_page_picks_valid_fid_and_records_usage(self, monkeypatch, tmp_path):
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "on")
        meter = tmp_path / "meter.txt"
        model = _fake_model("claude-picker", web=True)
        _StubServer([
            '{"fid": "202", "why": "1987 UK thriller matching TMDb 55123"}',
        ], web=True).install(monkeypatch, model)

        fitting = [
            dvdcompare.SearchEntry("101", ("Nightfall Harbour",), "bluray", "1987", False),
            dvdcompare.SearchEntry("202", ("Nightfall Harbour",), "bluray", "1987", False),
        ]
        usage = aicommentarynames.Usage()
        logged: list[str] = []
        fid, note = aicommentarynames.pick_page(
            fitting, "Nightfall Harbour", "1987", "bluray",
            tmdb_id="55123", aliases=("Nachthafen",),
            models=[model], usage=usage, meter_file=str(meter),
            log=logged.append)

        assert fid == "202"
        assert "fid=202" in note and "claude-picker" in note
        assert usage.requests == 1
        assert meter.is_file()

    def test_pick_page_rejects_invented_fid(self, monkeypatch):
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "on")
        model = _fake_model("m1")
        _StubServer(['{"fid": "999", "why": "made up"}']).install(
            monkeypatch, model)
        fitting = [
            dvdcompare.SearchEntry("101", ("Nightfall Harbour",), "bluray", "1987", False),
            dvdcompare.SearchEntry("202", ("Nightfall Harbour",), "bluray", "1987", False),
        ]
        fid, note = aicommentarynames.pick_page(
            fitting, "Nightfall Harbour", "1987", "bluray", models=[model])
        assert fid == ""
        assert "unusable" in note and "m1" in note

    def test_pick_page_noop_when_switch_off_or_fewer_than_two(self, monkeypatch):
        one = [dvdcompare.SearchEntry("101", ("Nightfall Harbour",), "bluray", "1987", False)]
        two = one + [dvdcompare.SearchEntry("202", ("Nightfall Harbour",), "bluray", "1987", False)]
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "on")
        assert aicommentarynames.pick_page(
            [], "Nightfall Harbour", "1987", "bluray",
            models=[_fake_model()]) == ("", "")
        assert aicommentarynames.pick_page(
            one, "Nightfall Harbour", "1987", "bluray",
            models=[_fake_model()]) == ("101", "")
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "off")
        assert aicommentarynames.pick_page(
            two, "Nightfall Harbour", "1987", "bluray",
            models=[_fake_model()]) == ("", "")


class TestFilmInfo:
    def test_reads_id_from_tag_without_api_key(self, monkeypatch):
        from medialib.lib import tmdbidentify

        monkeypatch.delenv("tmdbApiKey", raising=False)
        assert tmdbidentify.film_info("Nightfall Harbour", "1987", "{tmdb-55123}") == (
            tmdbidentify.FilmInfo(tmdb="55123"))
        assert tmdbidentify.film_info("Nightfall Harbour", "1987", "{imdb-tt0093123}") == (
            tmdbidentify.FilmInfo(imdb="tt0093123"))

    def test_fetches_aliases_and_external_ids_when_key_set(self, monkeypatch):
        from medialib.lib import tmdbclient, tmdbidentify

        monkeypatch.setenv("tmdbApiKey", "test-key")
        detail = {
            "title": "Nightfall Harbour",
            "original_title": "Nachthafen",
            "alternative_titles": {"titles": [{"title": "Port de nuit"}]},
            "translations": {"translations": [{"data": {"title": "Nachthafen"}}]},
            "external_ids": {"imdb_id": "tt0093123"},
        }
        monkeypatch.setattr(tmdbclient, "_curl", lambda *_a, **_k: json.dumps(detail))
        info = tmdbidentify.film_info("Nightfall Harbour", "1987", "{tmdb-55123}")
        assert info.tmdb == "55123"
        assert info.imdb == "tt0093123"
        assert "Nachthafen" in info.aliases
        assert "Port de nuit" in info.aliases


class TestIntroNaming:
    def test_parse_intro_answer_accepts_high_confidence_and_guards_bad(self):
        title, why, unsure = aicommentarynames.parse_intro_answer(
            '{"name": "Audio commentary by director Wenna Castellane", '
            '"confidence": "high", "why": "Wenna introduces herself"}')
        assert title == "Commentary by director Wenna Castellane"
        assert "Wenna" in why and unsure is False

        # Rejects medium/low confidence, null, and generic labels.
        assert aicommentarynames.parse_intro_answer(
            '{"name": "Commentary by Wenna", "confidence": "medium", '
            '"why": "guessed"}')[0] == ""
        assert aicommentarynames.parse_intro_answer(
            '{"name": "Commentary 1", "confidence": "high", '
            '"why": "generic"}')[:3:2] == ("", False)
        assert aicommentarynames.parse_intro_answer(
            '{"name": null, "confidence": "low", "why": "no intro"}')[2] is True

    def test_name_from_intro_uses_first_five_minutes_and_respects_switch(
            self, monkeypatch):
        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "on")
        model = _fake_model("intro-model")
        stub = _StubServer([
            '{"name": "Commentary with director Wenna Castellane", '
            '"confidence": "high", "why": "She introduces herself at 0:15"}',
        ])
        stub.install(monkeypatch, model)

        srt = _srt(
            ("00:00:15,000", "00:00:20,000", "Hi, I'm director Wenna Castellane."),
            ("00:06:00,000", "00:06:05,000", "Late guest after 5 minutes."),
        )
        name, note = aicommentarynames.name_from_intro(
            srt, title="Nightfall Harbour", year="1987",
            track_name="Commentary", models=[model])
        assert name == "Commentary with director Wenna Castellane"
        assert "intro-model" in note
        asks = [c for c in stub.calls if c.get("mode") == "ask"]
        assert len(asks) == 1
        assert "Late guest" not in asks[0]["prompt"]
        assert "Wenna Castellane" in asks[0]["prompt"]

        monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "off")
        assert aicommentarynames.name_from_intro(
            srt, title="Nightfall Harbour", models=[model]) == ("", "")



