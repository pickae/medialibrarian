"""Tests for medialib.lib.tmdblookup - TMDb-backed IMDb id lookup, and
Plex/Jellyfin id tagging.

What is pinned here: the exact certainty rule of the id lookup over the jq
null/empty edge cases (a missing primary title is the
literal word "null", an empty-string title is dropped, an empty alternative is
skipped while a null one is not), the curl argv the lookup hands the network, the
safety skips a rename records, and the no-key warning.

What the lookup does with a widened match is here - which candidate it settles
on, and which of the catalogue's spellings the folder is then renamed onto. What
COUNTS as the same title is not.
"""

import itertools
import json
import os
import pathlib
import re
import subprocess

import pytest

from medialib.lib import titlematch, tmdblookup
from medialib.lib.safety import SkipLog

pytestmark = pytest.mark.stubbed

_BASE = tmdblookup._BASE


# --- the one way out to the network -------------------------------------------


class TestTheCallItselfKeepsTheKeyOffArgv:
    """argv is not private: /proc/<pid>/cmdline is readable by every account on
    the machine, and ingest-movies opens that window once per candidate folder.
    The config goes to curl on stdin, where nothing else can read it."""

    def _record(self, monkeypatch, rc=0, out=b"{}"):
        seen = {}

        def fake_run(argv, input=None, **_kwargs):
            seen["argv"] = argv
            seen["input"] = input
            return subprocess.CompletedProcess(argv, rc, out, b"")

        monkeypatch.setattr(tmdblookup.subprocess, "run", fake_run)
        return seen

    def test_no_argument_carries_the_key_or_the_query(self, monkeypatch):
        seen = self._record(monkeypatch)
        tmdblookup._curl("https://example.test/search",
                         [("api_key", "SECRET"), ("query", "Some Movie")])
        assert seen["argv"] == ["curl", "--config", "-"]
        assert not any("SECRET" in argument for argument in seen["argv"])
        assert b"SECRET" in seen["input"]

    def test_the_config_asks_for_what_the_flags_did(self, monkeypatch):
        seen = self._record(monkeypatch)
        tmdblookup._curl("https://example.test/search", [("year", "2001")])
        lines = seen["input"].decode().splitlines()
        assert lines[0] == 'url = "https://example.test/search"'
        # -G -f -s -S, by their long names
        assert {"get", "fail", "silent", "show-error"} <= set(lines)
        assert 'data-urlencode = "year=2001"' in lines

    def test_a_failing_curl_is_no_answer(self, monkeypatch):
        self._record(monkeypatch, rc=22)
        assert tmdblookup._curl("https://example.test/x", []) is None

    @pytest.mark.parametrize("raw,quoted", [
        ("plain", '"plain"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("two\nlines", '"two\\nlines"'),
        ("a\tb", '"a\\tb"'),
    ])
    def test_a_value_is_quoted_the_way_the_config_syntax_reads_it(
            self, raw, quoted):
        """A title carries whatever the folder was named, and an unescaped quote
        in one would end the value early and turn the rest into directives."""
        assert tmdblookup._config_value(raw) == quoted


class TestTheIdInAUrlPath:
    """The value comes out of the API's own JSON and is spliced into a URL PATH,
    where a "/" or a "#" asks for something else entirely."""

    def test_a_number_is_itself(self):
        assert tmdblookup._id_token(550) == "550"

    def test_a_missing_id_is_the_word_jq_prints(self):
        assert tmdblookup._id_token(None) == "null"

    @pytest.mark.parametrize("value", ["../../other", "1/2", "1#x", "1 2", ""])
    def test_anything_that_is_not_a_number_is_not_an_id(self, value):
        assert tmdblookup._id_token(value) == "null"


# --- the id lookup's certainty rule -------------------------------------------


def _row(rid, title, original=None, date="1999-06-23"):
    return {"id": rid, "title": title, "original_title": original,
            "release_date": date}


def _detail(rid, alt_titles, ext_ids, runtimes, years):
    """One film document the way the lookup asks for it: its alternative
    titles, its external ids and every country's release date, appended to the
    film itself."""
    return json.dumps({
        "runtime": (runtimes or {}).get(rid),
        "alternative_titles": {"titles": [{"title": title}
                                          for title in alt_titles.get(rid, [])]},
        "external_ids": {"imdb_id": ext_ids.get(rid)},
        "release_dates": {"results": [
            {"iso_3166_1": "XX",
             "release_dates": [{"release_date": year + "-01-01T00:00:00.000Z"}]}
            for year in (years or {}).get(rid, [])]},
    })


def _install(monkeypatch, search, alt_titles, ext_ids, key="apikey",
             runtimes=None, years=None, second=None):
    """Stand the network in with canned answers and record the calls.

    ``search`` is the raw search body (or None for a curl that fails), and
    ``second`` the body of the ask that follows it when the first found nothing
    (the same body again when it is not given). ``alt_titles`` maps a candidate
    id to the alternative titles its document carries, ``ext_ids`` to its
    imdb_id, ``runtimes`` to the minutes TMDb states for it, and ``years`` to
    the release years its release_dates hold besides its primary one.
    """
    monkeypatch.setenv("tmdbApiKey", key)
    calls = []

    def fake_curl(url, params):
        calls.append((url, list(params)))
        if url == _BASE + "/search/movie":
            if second is not None and not any(k == "year" for k, _v in params):
                return second
            return search
        match = re.match(r"^" + re.escape(_BASE) + r"/movie/(\d+)$", url)
        if match:
            return _detail(int(match.group(1)), alt_titles, ext_ids, runtimes,
                           years)
        return None

    monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
    return calls


def _detail_url(rid) -> str:
    return _BASE + "/movie/%d" % rid


class TestTmdbImdbId:
    def test_no_key_is_no_answer(self, monkeypatch):
        calls = _install(monkeypatch, None, {}, {}, key="")
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""
        assert calls == []

    def test_an_unnormalisable_title_is_no_answer(self, monkeypatch):
        calls = _install(monkeypatch, None, {}, {})
        assert tmdblookup.tmdb_imdb_id("!!!", "1999") == ""
        assert calls == []

    def test_a_failed_search_is_no_answer(self, monkeypatch):
        calls = _install(monkeypatch, None, {}, {})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""
        assert len(calls) == 1 and calls[0][0] == _BASE + "/search/movie"

    def test_the_search_argv(self, monkeypatch):
        calls = _install(monkeypatch, json.dumps({"results": []}), {}, {})
        tmdblookup.tmdb_imdb_id("Some Movie", "2001")
        url, params = calls[0]
        assert url == _BASE + "/search/movie"
        assert params == [("api_key", "apikey"), ("query", "Some Movie"),
                          ("year", "2001"), ("include_adult", "false")]

    def test_nothing_at_all_for_that_year_is_asked_again_without_one(
            self, monkeypatch):
        """The search that finds a film whose folder carries a year no release
        of it happened in. What the year is then worth is the rule's business,
        not the search's."""
        calls = _install(monkeypatch, json.dumps({"results": []}), {}, {})
        assert tmdblookup.tmdb_imdb_id("Some Movie", "2001") == ""
        assert [params for _url, params in calls] == [
            [("api_key", "apikey"), ("query", "Some Movie"), ("year", "2001"),
             ("include_adult", "false")],
            [("api_key", "apikey"), ("query", "Some Movie"),
             ("include_adult", "false")]]

    def test_a_year_nothing_was_released_near_is_no_answer(self, monkeypatch):
        # both carry the title, so both are worth a document - and neither
        # document names a year anywhere near the one on the folder
        search = json.dumps({"results": [
            _row(1, "Nighthawk", date="1966-06-23"),
            _row(2, "Nighthawk", date="2005-06-23")]})
        calls = _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1"})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1989") == ""
        assert [url for url, _params in calls] == [
            _BASE + "/search/movie", _detail_url(1), _detail_url(2)]

    def test_zero_matching_titles_is_no_answer(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Not Nighthawk")]})
        calls = _install(monkeypatch, search, {1: []}, {})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""
        # one candidate in the year, so its document was read
        assert [url for url, _params in calls] == [
            _BASE + "/search/movie", _detail_url(1)]

    def test_the_document_is_asked_for_in_one_call(self, monkeypatch):
        calls = _install(monkeypatch, json.dumps({"results": [_row(1, "Nighthawk")]}),
                         {1: []}, {1: "tt0120737"})
        tmdblookup.tmdb_imdb_id("Nighthawk", "1999")
        url, params = calls[1]
        assert url == _detail_url(1)
        assert params == [("api_key", "apikey"),
                          ("append_to_response",
                           "alternative_titles,external_ids,release_dates")]

    def test_a_single_match_returns_the_id(self, monkeypatch):
        search = json.dumps({"results": [
            _row(1, "Nighthawk"), _row(2, "Other", date="1998-01-01")]})
        calls = _install(monkeypatch, search, {1: [], 2: []}, {1: "tt0120737"})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == "tt0120737"
        # the other release is near enough the year to be worth reading, and
        # turns out to carry a different title
        assert [url for url, _params in calls] == [
            _BASE + "/search/movie", _detail_url(1), _detail_url(2)]

    def test_two_matching_candidates_is_no_answer(self, monkeypatch):
        # the second carries the title only on its original title - the whole
        # point of the alternative/original scan
        search = json.dumps({"results": [
            _row(1, "Nighthawk"), _row(2, "The Grey Rider", original="Nighthawk")]})
        # Both carry an id, so both are answers this could settle on and the
        # pair of them is what makes it uncertain.
        calls = _install(monkeypatch, search, {1: [], 2: []},
                         {1: "tt1", 2: "tt2"})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""
        assert [url for url, _params in calls] == [
            _BASE + "/search/movie", _detail_url(1), _detail_url(2)]

    def test_an_alternative_title_is_a_match(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "The Movie")]})
        _install(monkeypatch, search, {1: ["Nighthawk"]}, {1: "tt0000001"})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == "tt0000001"

    def test_an_original_title_is_a_match(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "The Grey Rider",
                                              original="Nighthawk")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000002"})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == "tt0000002"

    def test_a_missing_primary_is_the_word_null_not_a_match(self, monkeypatch):
        # jq -r prints the literal "null" for a missing title; it is compared
        # (and missed) like any other title, not skipped as if it were empty
        search = json.dumps({"results": [_row(1, None, original="Nighthawk")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000003"})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == "tt0000003"

    def test_a_null_alternative_is_skipped_not_matched(self, monkeypatch):
        # `.title // empty`: a null/missing alternative is skipped, whereas the
        # search's missing primary above is not
        monkeypatch.setenv("tmdbApiKey", "apikey")

        def fake_curl(url, _params):
            if url == _BASE + "/search/movie":
                return json.dumps({"results": [_row(1, "The Movie")]})
            if url == _detail_url(1):
                # an alternative with no title key at all
                return json.dumps({"alternative_titles": {"titles": [{}]},
                                   "external_ids": {"imdb_id": "tt0000004"}})
            return None
        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        # the null alternative is skipped; the primary "The Movie" does not match
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""

    def test_a_non_tt_imdb_id_is_no_answer(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Nighthawk")]})
        _install(monkeypatch, search, {1: []}, {1: "not-an-id"})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""

    def test_a_missing_imdb_id_is_no_answer(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Nighthawk")]})
        _install(monkeypatch, search, {1: []}, {})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""

    def test_a_document_that_cannot_be_read_is_no_answer(self, monkeypatch):
        """The one call carries the titles, the id and the dates together, so a
        candidate whose document fails is a candidate nothing is known about -
        and nothing is what it matches on."""
        calls = []

        def fake_curl(url, _params):
            calls.append(url)
            if url == _BASE + "/search/movie":
                return json.dumps({"results": [_row(1, "Nighthawk")]})
            return None  # the document call fails
        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""
        assert calls[-1] == _detail_url(1)


class TestWhatTheLengthSettles:
    """The rule past the plain one: what a release year elsewhere answers for,
    and what the film's own length is - and is not - allowed to decide."""

    def test_a_release_year_elsewhere_answers_for_the_folder(self, monkeypatch):
        """The primary date is the premiere and the folder was named from the
        release a year later. One film, two true years, and no length needed to
        say so."""
        search = json.dumps({"results": [_row(1, "Nighthawk", date="1998-09-01")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000001"},
                 years={1: ["1999"]})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == "tt0000001"

    def test_two_films_of_one_title_and_year_are_told_apart_by_length(
            self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Nighthawk"), _row(2, "Nighthawk")]})
        _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1", 2: "tt2"},
                 runtimes={1: 90, 2: 150})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999",
                                       lambda: 150 * 60.0) == "tt2"

    def test_a_candidate_nobody_can_measure_keeps_it_uncertain(self, monkeypatch):
        """The film on the disk fits one of them, and the other states no
        runtime at all - so nothing rules it out, and one of two is still not
        an answer."""
        search = json.dumps({"results": [_row(1, "Nighthawk"), _row(2, "Nighthawk")]})
        _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1", 2: "tt2"},
                 runtimes={1: 150})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999",
                                       lambda: 150 * 60.0) == ""

    def test_and_so_does_a_film_nothing_on_disk_can_measure(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Nighthawk"), _row(2, "Nighthawk")]})
        _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1", 2: "tt2"},
                 runtimes={1: 90, 2: 150})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999", lambda: 0.0) == ""

    def test_a_length_that_fits_neither_is_no_answer(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Nighthawk"), _row(2, "Nighthawk")]})
        _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1", 2: "tt2"},
                 runtimes={1: 90, 2: 150})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999",
                                       lambda: 200 * 60.0) == ""

    def test_a_year_off_by_one_is_named_when_the_length_agrees(self, monkeypatch):
        """No release of it happened in the year on the folder, so the year has
        been given up on - and the length is what stands in its place."""
        second = json.dumps({"results": [_row(1, "Nighthawk", date="2000-06-23")]})
        _install(monkeypatch, json.dumps({"results": []}), {1: []},
                 {1: "tt0000009"}, runtimes={1: 120}, second=second)
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999",
                                       lambda: 121 * 60.0) == "tt0000009"

    def test_and_is_not_named_without_a_length_to_agree(self, monkeypatch):
        second = json.dumps({"results": [_row(1, "Nighthawk", date="2000-06-23")]})
        _install(monkeypatch, json.dumps({"results": []}), {1: []},
                 {1: "tt0000009"}, runtimes={1: 120}, second=second)
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""

    def test_nor_when_the_length_disagrees(self, monkeypatch):
        second = json.dumps({"results": [_row(1, "Nighthawk", date="2000-06-23")]})
        _install(monkeypatch, json.dumps({"results": []}), {1: []},
                 {1: "tt0000009"}, runtimes={1: 120}, second=second)
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999",
                                       lambda: 90 * 60.0) == ""

    def test_a_year_further_off_than_one_is_not_reached_by_a_length(
            self, monkeypatch):
        second = json.dumps({"results": [_row(1, "Nighthawk", date="2003-06-23")]})
        _install(monkeypatch, json.dumps({"results": []}), {1: []},
                 {1: "tt0000009"}, runtimes={1: 120}, second=second)
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999",
                                       lambda: 120 * 60.0) == ""

    def test_the_disk_is_not_read_when_the_year_settled_it(self, monkeypatch):
        """The probe costs an ffprobe of every film in the library, so it is
        asked for only by the ones the plain rule could not name."""
        search = json.dumps({"results": [_row(1, "Nighthawk")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000001"})
        asked = []

        def measure():
            asked.append(True)
            return 7200.0
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999", measure) == "tt0000001"
        assert asked == []

    def test_nor_when_there_is_nothing_left_to_settle(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Not Nighthawk")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000001"})
        asked = []

        def measure():
            asked.append(True)
            return 7200.0
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999", measure) == ""
        assert asked == []

    def test_only_the_first_handful_of_results_cost_a_request(self, monkeypatch):
        """Twenty answers to one title is TMDb saying it does not know which;
        every one of them would be a request, and the ones past the first few
        are not what a certainty rule turns on."""
        search = json.dumps({"results": [_row(n, "Nighthawk")
                                         for n in range(1, 21)]})
        calls = _install(monkeypatch, search, {}, {})
        tmdblookup.tmdb_imdb_id("Nighthawk", "1999")
        documents = [url for url, _params in calls
                     if url != _BASE + "/search/movie"]
        assert len(documents) == tmdblookup.MAX_CANDIDATES

    @pytest.mark.parametrize("seconds,minutes,verdict", [
        (100 * 60.0, 100, True),          # exactly what it says
        (100 * 60.0 + 420, 100, True),    # seven percent of a long film
        (100 * 60.0 + 421, 100, False),
        (30 * 60.0 + 300, 30, True),      # five minutes, where the fraction is less
        (30 * 60.0 + 301, 30, False),
        (0.0, 100, None),                 # nothing on disk could say
        (100 * 60.0, 0, None),            # and nothing at TMDb does either
    ])
    def test_the_tolerance_is_five_minutes_or_seven_percent(
            self, seconds, minutes, verdict):
        assert tmdblookup._runtime_agrees(seconds, minutes) is verdict


class TestTheLengthOfAFolder:
    """Which of a folder's files the length is taken from, and when none is."""

    def _measured(self, monkeypatch):
        seen = []
        monkeypatch.setattr(tmdblookup.durationcheck, "total_duration",
                            lambda paths: seen.append(list(paths)) or 7200.0)
        return seen

    def test_one_film_is_the_film(self, monkeypatch, tmp_path):
        seen = self._measured(monkeypatch)
        assert tmdblookup._folder_runtime(
            str(tmp_path), "The Movie (1999)",
            ["The Movie (1999).mkv", "The Movie (1999).en.srt"]) == 7200.0
        assert seen == [[str(tmp_path / "The Movie (1999).mkv")]]

    def test_a_split_film_is_its_parts_added_up(self, monkeypatch, tmp_path):
        seen = self._measured(monkeypatch)
        assert tmdblookup._folder_runtime(
            str(tmp_path), "The Movie (1999)",
            ["The Movie (1999) Part1.mkv",
             "The Movie (1999) Part2.mkv"]) == 7200.0
        assert seen == [[str(tmp_path / "The Movie (1999) Part1.mkv"),
                         str(tmp_path / "The Movie (1999) Part2.mkv")]]

    def test_a_folder_of_named_editions_says_nothing(self, monkeypatch, tmp_path):
        """Which of a theatrical and an extended cut the one stated runtime is
        for is exactly what is not known, so the folder abstains rather than
        offering a length that may be the other one's."""
        seen = self._measured(monkeypatch)
        assert tmdblookup._folder_runtime(
            str(tmp_path), "The Movie (1999)",
            ["The Movie (1999).mkv",
             "The Movie (1999) {edition-Extended}.mkv"]) == 0.0
        assert seen == []

    def test_and_neither_does_a_folder_with_no_film_in_it(self, monkeypatch,
                                                          tmp_path):
        seen = self._measured(monkeypatch)
        assert tmdblookup._folder_runtime(
            str(tmp_path), "The Movie (1999)",
            ["The Movie (1999).en.srt"]) == 0.0
        assert seen == []


# --- tagging a folder tree ----------------------------------------------------


def _tree(root, *folders):
    """A library of movie folders: each (base, fileNames...) becomes a folder
    holding the named files."""
    for spec in folders:
        base, files = spec
        (root / base).mkdir(parents=True)
        for name in files:
            (root / base / name).touch()


class TestTheIdList:
    """The file someone fills in for the films TMDb cannot name on its own."""

    @pytest.mark.parametrize("written,expected", [
        ("tt0000002", "{imdb-tt0000002}"),
        ("imdb-tt0000002", "{imdb-tt0000002}"),
        ("{imdb-tt0000002}", "{imdb-tt0000002}"),
        ("  tt0000002  ", "{imdb-tt0000002}"),
        ("TT0000002", "{imdb-tt0000002}"),
        ("12345", "{tmdb-12345}"),
        ("tmdb-12345", "{tmdb-12345}"),
    ])
    def test_an_id_is_read_the_way_someone_has_it_to_hand(self, written,
                                                          expected):
        assert tmdblookup.id_tag_for(written) == expected

    @pytest.mark.parametrize("written", ["", "   ", "nonsense", "tt", "-",
                                         "{imdb-}", "tt12ab34"])
    def test_and_anything_else_is_not_an_id(self, written):
        assert tmdblookup.id_tag_for(written) == ""

    def test_a_line_still_blank_is_one_still_to_do(self, tmp_path):
        path = tmp_path / "ids.tsv"
        path.write_text("# a comment\n\nDone (1999)\ttt0000001\n"
                        "Not Yet (2001)\t\n", encoding="utf-8")
        assert tmdblookup.read_id_list(str(path)) == {
            "Done (1999)": "{imdb-tt0000001}"}

    def test_a_missing_file_is_simply_no_ids(self, tmp_path):
        assert tmdblookup.read_id_list(str(tmp_path / "nothing.tsv")) == {}

    def test_what_was_filled_in_survives_being_rewritten(self, tmp_path):
        """The ids someone looked up by hand are the only record of them
        anywhere: dropping them would un-identify the film on the next run."""
        path = str(tmp_path / "ids.tsv")
        tmdblookup.write_id_list(path, ["Still Unknown (2001)"],
                                 {"Done (1999)": "{imdb-tt0000001}"})
        assert tmdblookup.read_id_list(path) == {
            "Done (1999)": "{imdb-tt0000001}"}
        with open(path, encoding="utf-8") as handle:
            written = handle.read()
        body = [line for line in written.splitlines()
                if line and not line.startswith("#")]
        assert body == ["Done (1999)\t{imdb-tt0000001}", "Still Unknown (2001)\t"]

    def test_a_film_named_from_the_list_never_reaches_the_network(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Unknown Film (2010)", ["Unknown Film (2010).mkv"]))
        monkeypatch.setenv("tmdbApiKey", "apikey")
        calls = []
        monkeypatch.setattr(tmdblookup, "_curl",
                            lambda url, params: calls.append(url))
        logs = []
        unmatched: list = []
        tmdblookup.tag_plex_ids(
            ".", logs.append, SkipLog(),
            ids={"Unknown Film (2010)": "{imdb-tt0000002}"},
            unmatched=unmatched)
        assert calls == []
        assert unmatched == []
        assert (tmp_path / "Unknown Film (2010) {imdb-tt0000002}").is_dir()
        assert logs == ['  from the id list: "Unknown Film (2010)" -> '
                        "{imdb-tt0000002}"]


class TestTheRateLimit:
    """TMDb asks for no more than about ten requests a second, and one film
    costs several - the search, an alternative-titles call per candidate, and
    the external-ids call."""

    def test_requests_are_spaced_at_the_limit(self, monkeypatch):
        tmdblookup.reset_rate_limit()
        ticks = [0.0]
        slept = []
        monkeypatch.setattr(tmdblookup.time, "monotonic", lambda: ticks[0])

        def fake_sleep(seconds):
            slept.append(seconds)
            ticks[0] += seconds
        monkeypatch.setattr(tmdblookup.time, "sleep", fake_sleep)
        monkeypatch.setattr(tmdblookup.subprocess, "run",
                            lambda *a, **k: _Done())
        for _call in range(3):
            tmdblookup._curl("https://example.invalid", [])
        # the first goes straight out, each one after it waits its slot
        assert slept == [1.0 / tmdblookup.MAX_REQUESTS_PER_SECOND] * 2

    def test_a_caller_that_took_its_time_waits_for_nothing(self, monkeypatch):
        tmdblookup.reset_rate_limit()
        ticks = [0.0]
        slept = []
        monkeypatch.setattr(tmdblookup.time, "monotonic", lambda: ticks[0])
        monkeypatch.setattr(tmdblookup.time, "sleep", slept.append)
        monkeypatch.setattr(tmdblookup.subprocess, "run",
                            lambda *a, **k: _Done())
        tmdblookup._curl("https://example.invalid", [])
        ticks[0] = 5.0
        tmdblookup._curl("https://example.invalid", [])
        assert slept == []


class _Done:
    returncode = 0
    stdout = b"{}"


class TestTagPlexIds:
    def _env(self, monkeypatch, matches, base="1999-06-23"):
        """Stand the network so that, for each title in ``matches`` that
        resolves to an id, its search returns one same-year same-title result
        whose document carries that id; every other title is a miss."""
        monkeypatch.setenv("tmdbApiKey", "apikey")
        by_title = {}
        for index, (title, imdb) in enumerate(matches.items()):
            if imdb:
                by_title[title] = (index + 1, imdb)
        calls = []

        def fake_curl(url, params):
            calls.append(url)
            kv = dict(params)
            if url == _BASE + "/search/movie":
                hit = by_title.get(kv["query"])
                if hit is None:
                    return json.dumps({"results": []})
                rid, _ = hit
                return json.dumps({"results": [
                    _row(rid, kv["query"], date=base)]})
            match = re.match(r"^" + re.escape(_BASE) + r"/movie/(\d+)$", url)
            if match:
                rid = int(match.group(1))
                for _t, (r2, imdb) in by_title.items():
                    if r2 == rid:
                        return json.dumps({"external_ids": {"imdb_id": imdb}})
            return None
        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        return calls

    def test_without_a_key_it_warns_and_touches_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("tmdbApiKey", "")
        _tree(tmp_path, ("The Movie (1999)", ["The Movie (1999).mkv"]))
        logs = []
        skip = SkipLog()
        tmdblookup.tag_plex_ids(".", logs.append, skip)
        assert logs == ["WARNING: tmdbApiKey not set, skipping IMDb id tagging"]
        assert skip.skips == []
        assert (tmp_path / "The Movie (1999)/The Movie (1999).mkv").is_file()

    def test_a_matched_folder_its_file_and_sidecars_are_tagged(self, monkeypatch,
                                                                tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv", "The Movie (1999).en.srt"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        skip = SkipLog()
        tmdblookup.tag_plex_ids(".", logs.append, skip)
        tagged = tmp_path / "The Movie (1999) {imdb-tt0120737}"
        assert tagged.is_dir()
        assert (tagged / "The Movie (1999) {imdb-tt0120737}.mkv").is_file()
        assert (tagged / "The Movie (1999) {imdb-tt0120737}.en.srt").is_file()
        assert not (tmp_path / "The Movie (1999)").exists()
        assert logs == ['  match: "The Movie (1999)" -> {imdb-tt0120737}']
        assert skip.skips == []

    def test_the_library_is_tagged_where_it_is_and_not_where_the_run_started(
            self, monkeypatch, tmp_path):
        """The library is an argument, so a run started from anywhere else
        renames each film beside itself rather than moving it to the caller."""
        library = tmp_path / "library"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        _tree(library, ("The Movie (1999)", ["The Movie (1999).mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        tmdblookup.tag_plex_ids(str(library), [].append, SkipLog())
        tagged = library / "The Movie (1999) {imdb-tt0120737}"
        assert (tagged / "The Movie (1999) {imdb-tt0120737}.mkv").is_file()
        assert list(elsewhere.iterdir()) == []

    def test_the_old_copy_an_improved_remux_left_keeps_its_name(self,
                                                                monkeypatch,
                                                                tmp_path):
        """Tagging runs after the improved copies are made; the original kept
        beside one is not renamed with it."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv",
                          "The Movie (1999) (old).mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        skip = SkipLog()
        tmdblookup.tag_plex_ids(".", [].append, skip)
        tagged = tmp_path / "The Movie (1999) {imdb-tt0120737}"
        assert (tagged / "The Movie (1999) {imdb-tt0120737}.mkv").is_file()
        assert (tagged / "The Movie (1999) (old).mkv").is_file()
        assert skip.skips == []

    def test_several_versions_of_one_film_become_named_editions(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999) (Original Mono Track).mkv",
                          "The Movie (1999) (Original Mono Track).en.srt",
                          "The Movie (1999) (Original Mono Track) 2 "
                          "Commentary.srt",
                          "The Movie (1999) colorized.mkv",
                          "The Movie (1999) colorized.nl.srt"]))
        self._env(monkeypatch, {"The Movie": "tt0000001"})
        tmdblookup.tag_plex_ids(".", [].append, SkipLog())
        tagged = tmp_path / "The Movie (1999) {imdb-tt0000001}"
        assert sorted(p.name for p in tagged.iterdir()) == [
            "The Movie (1999) {imdb-tt0000001} {edition-Colorized}.mkv",
            "The Movie (1999) {imdb-tt0000001} {edition-Colorized}.nl.srt",
            "The Movie (1999) {imdb-tt0000001} "
            "{edition-Original Mono Track} 2 Commentary.srt",
            "The Movie (1999) {imdb-tt0000001} "
            "{edition-Original Mono Track}.en.srt",
            "The Movie (1999) {imdb-tt0000001} "
            "{edition-Original Mono Track}.mkv"]

    def test_a_split_film_keeps_its_stacking_token_last(self, monkeypatch,
                                                         tmp_path):
        """Plex reads the stacking suffix at the end of the name, so the id goes
        in front of it and not after."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1968)",
                         ["The Movie (1968) Part1.mkv", "The Movie (1968) Part2.mkv",
                          "The Movie (1968) Part1.en.srt"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"}, base="1968-01-01")
        tmdblookup.tag_plex_ids(".", [].append, SkipLog())
        tagged = tmp_path / "The Movie (1968) {imdb-tt0120737}"
        assert sorted(p.name for p in tagged.iterdir()) == [
            "The Movie (1968) {imdb-tt0120737} Part1.en.srt",
            "The Movie (1968) {imdb-tt0120737} Part1.mkv",
            "The Movie (1968) {imdb-tt0120737} Part2.mkv"]

    def test_a_tagged_folder_whose_files_were_missed_is_put_right(
            self, monkeypatch, tmp_path):
        """The library an earlier version left half-renamed: the folder carries
        the id, the files do not, and no second lookup is needed to finish it."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999) {imdb-tt0120737}",
                         ["The Movie (1999).mkv", "The Movie (1999).en.srt"]))
        calls = self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        tagged = tmp_path / "The Movie (1999) {imdb-tt0120737}"
        assert sorted(p.name for p in tagged.iterdir()) == [
            "The Movie (1999) {imdb-tt0120737}.en.srt",
            "The Movie (1999) {imdb-tt0120737}.mkv"]
        assert calls == []
        assert logs == []

    def test_a_second_run_over_a_tagged_library_changes_nothing(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999) colorized.mkv",
                          "The Movie (1999) colorized.en.srt",
                          "The Movie (1999) (old).mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0000001"})
        tmdblookup.tag_plex_ids(".", [].append, SkipLog())
        before = sorted(str(p.relative_to(tmp_path))
                        for p in tmp_path.rglob("*"))
        skip = SkipLog()
        tmdblookup.tag_plex_ids(".", [].append, skip)
        assert sorted(str(p.relative_to(tmp_path))
                      for p in tmp_path.rglob("*")) == before
        assert skip.skips == []

    def test_a_folder_whose_tagged_name_is_taken_is_left_whole(self, monkeypatch,
                                                               tmp_path):
        """A library holding both the film and an already-tagged copy of it.

        Renaming the files anyway would leave them spelled for a folder they
        are not in, beside the folder they are spelled for - which reads, to
        anyone looking, as the film having left the place it was.
        """
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path,
              ("The Movie (1999)",
               ["The Movie (1999).mkv", "The Movie (1999).en.srt"]),
              ("The Movie (1999) {imdb-tt0120737}",
               ["The Movie (1999) {imdb-tt0120737}.mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        skip = SkipLog()
        tmdblookup.tag_plex_ids(".", logs.append, skip)
        assert sorted(p.name for p in (tmp_path / "The Movie (1999)").iterdir()) \
            == ["The Movie (1999).en.srt", "The Movie (1999).mkv"]
        assert skip.skips == [
            ("./The Movie (1999)",
             "./The Movie (1999) {imdb-tt0120737}")]
        assert logs[-1] == ('  "The Movie (1999) {imdb-tt0120737}" is already '
                            'there, left "The Movie (1999)" untouched')

    def test_a_film_is_never_tagged_into_a_hidden_name(self, monkeypatch,
                                                        tmp_path):
        """A folder already hidden keeps its films where they are: tagging it
        would rename every file to a name starting with a dot, and a film that
        drops out of the listing is worse than one left untagged."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, (".The Movie (1999)", [".The Movie (1999).mkv"]))
        self._env(monkeypatch, {".The Movie": "tt0120737"})
        tmdblookup.tag_plex_ids(".", [].append, SkipLog())
        assert (tmp_path / ".The Movie (1999)/.The Movie (1999).mkv").is_file()
        assert not (tmp_path / ".The Movie (1999) {imdb-tt0120737}").exists()

    def test_a_dry_run_hands_back_every_rename_it_would_make(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv", "The Movie (1999).en.srt"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        planned: list = []
        tmdblookup.tag_plex_ids(".", [].append, SkipLog(), dry_run=True,
                                planned=planned)
        assert planned == [
            ("./The Movie (1999)/The Movie (1999).en.srt",
             "./The Movie (1999)/The Movie (1999) {imdb-tt0120737}.en.srt"),
            ("./The Movie (1999)/The Movie (1999).mkv",
             "./The Movie (1999)/The Movie (1999) {imdb-tt0120737}.mkv"),
            ("./The Movie (1999)",
             "./The Movie (1999) {imdb-tt0120737}")]

    def test_a_real_run_collects_no_such_list(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)", ["The Movie (1999).mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        planned: list = []
        tmdblookup.tag_plex_ids(".", [].append, SkipLog(), planned=planned)
        assert planned == []
        assert (tmp_path / "The Movie (1999) {imdb-tt0120737}").is_dir()

    def test_a_refused_rename_is_not_one_it_would_make(self, monkeypatch,
                                                        tmp_path):
        """It is recorded as a safety skip, and the preview says only what
        would really happen."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv",
                          "The Movie (1999) {imdb-tt0120737}.mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        planned: list = []
        skip = SkipLog()
        tmdblookup.tag_plex_ids(".", [].append, skip, dry_run=True,
                                planned=planned)
        assert [t for _s, t in planned] == ["./The Movie (1999) {imdb-tt0120737}"]
        assert len(skip.skips) == 1

    def test_a_dry_run_says_what_it_would_do_and_does_none_of_it(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999) colorized.mkv",
                          "The Movie (1999) colorized.en.srt"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        before = sorted(str(p.relative_to(tmp_path))
                        for p in tmp_path.rglob("*"))
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog(), dry_run=True)
        assert sorted(str(p.relative_to(tmp_path))
                      for p in tmp_path.rglob("*")) == before
        assert logs == [
            '  match: "The Movie (1999)" -> {imdb-tt0120737}, '
            "editions: Colorized",
            '    would rename: "The Movie (1999) colorized.en.srt" -> '
            '"The Movie (1999) {imdb-tt0120737} {edition-Colorized}.en.srt"',
            '    would rename: "The Movie (1999) colorized.mkv" -> '
            '"The Movie (1999) {imdb-tt0120737} {edition-Colorized}.mkv"',
            '    would rename: "The Movie (1999)" -> '
            '"The Movie (1999) {imdb-tt0120737}"']

    def test_files_that_do_not_share_the_base_are_untouched(self, monkeypatch,
                                                             tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv", "cover.jpg"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        tagged = tmp_path / "The Movie (1999) {imdb-tt0120737}"
        assert (tagged / "The Movie (1999) {imdb-tt0120737}.mkv").is_file()
        assert (tagged / "cover.jpg").is_file()

    def test_a_folder_already_tagged_is_left_alone(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999) {imdb-tt0000000}",
                         ["The Movie (1999) {imdb-tt0000000}.mkv"]))
        self._env(monkeypatch, {"The Movie (1999)": "tt0120737"})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert logs == []
        assert (tmp_path / "The Movie (1999) {imdb-tt0000000}").is_dir()

    def test_a_folder_without_a_year_is_left_alone(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Just A Movie", ["Just A Movie.mkv"]))
        self._env(monkeypatch, {"Just A Movie": "tt0120737"})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert logs == []
        assert (tmp_path / "Just A Movie/Just A Movie.mkv").is_file()

    def test_the_films_it_could_not_name_are_collected(self, monkeypatch,
                                                        tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Unknown Film (2010)", ["Unknown Film (2010).mkv"]),
              ("The Movie (1999)", ["The Movie (1999).mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737", "Unknown Film": None})
        unmatched: list = []
        tmdblookup.tag_plex_ids(".", [].append, SkipLog(), unmatched=unmatched)
        assert unmatched == ["Unknown Film (2010)"]

    def test_a_film_the_year_could_not_name_is_named_by_its_length(
            self, monkeypatch, tmp_path):
        """The folder carries a year no release of the film happened in, and
        the film on the disk is as long as the one TMDb offers a year later -
        which is what the tagging hands the lookup to settle it with."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)", ["The Movie (1999).mkv"]))
        monkeypatch.setenv("tmdbApiKey", "apikey")

        def fake_curl(url, params):
            if url == _BASE + "/search/movie":
                if any(key == "year" for key, _value in params):
                    return json.dumps({"results": []})
                return json.dumps({"results": [
                    _row(1, "The Movie", date="2000-06-23")]})
            if url == _detail_url(1):
                return json.dumps({"runtime": 120,
                                   "external_ids": {"imdb_id": "tt0000123"}})
            return None
        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        monkeypatch.setattr(tmdblookup.durationcheck, "total_duration",
                            lambda _paths: 120 * 60.0)
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert (tmp_path / "The Movie (1999) {imdb-tt0000123}").is_dir()
        assert logs == ['  match: "The Movie (1999)" -> {imdb-tt0000123}']

    def test_a_folder_tmdB_rejects_is_left_alone_and_says_so(self, monkeypatch,
                                                              tmp_path):
        """Said rather than passed over in silence: a film that came back
        untagged is the thing people go looking for in the log."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Unknown Film (2010)", ["Unknown Film (2010).mkv"]))
        self._env(monkeypatch, {"Unknown Film": None})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert logs == ['  no confident TMDb match: "Unknown Film (2010)" '
                        "- left as it is"]
        assert (tmp_path / "Unknown Film (2010)/Unknown Film (2010).mkv").is_file()

    def test_a_film_tmdB_rejects_is_left_whole(self, monkeypatch, tmp_path):
        """Nothing is renamed for a film nothing could name: the editions would
        be guesses about a film that has not been identified, and the folder is
        reported instead so someone can give it an id."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Unknown Film (2010)",
                         ["Unknown Film (2010) colorized.mkv",
                          "Unknown Film (2010) (Uncut).mkv"]))
        self._env(monkeypatch, {"Unknown Film": None})
        logs = []
        unmatched: list = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog(),
                                unmatched=unmatched)
        folder = tmp_path / "Unknown Film (2010)"
        assert sorted(p.name for p in folder.iterdir()) == [
            "Unknown Film (2010) (Uncut).mkv",
            "Unknown Film (2010) colorized.mkv"]
        assert unmatched == ["Unknown Film (2010)"]
        assert logs == ['  no confident TMDb match: "Unknown Film (2010)" '
                        "- left as it is"]

    def test_a_folder_holding_a_film_that_is_not_its_own_is_reported(
            self, monkeypatch, tmp_path):
        """A name that EXTENDS the folder's own is one of this film's releases.
        One that does not is something else, and which film it is is not
        something a tag may guess."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv", "Some Other Film.mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        ambiguous: list = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog(),
                                ambiguous=ambiguous)
        assert (tmp_path / "The Movie (1999)/The Movie (1999).mkv").is_file()
        assert [(reason, names) for _path, reason, names in ambiguous] == [
            ("holds a film that is not its own", ["Some Other Film.mkv"])]
        assert logs == ['  "The Movie (1999)" holds 1 file(s) that are not '
                        "this film - left as it is"]

    def test_a_film_in_parts_that_will_not_stack_is_left_alone(
            self, monkeypatch, tmp_path):
        """Plex reads a part from the END of the name, and these have a title
        after the token. Tagged as editions they would read as three separate
        releases of the film rather than one film in three files."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1934)",
                         ["The Movie (1934) Part 1 - The First Half.mkv",
                          "The Movie (1934) Part 1 - The First Half.en.srt",
                          "The Movie (1934) Part 2 - The Others.mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        ambiguous: list = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog(),
                                ambiguous=ambiguous)
        # Left exactly as it was. It IS looked up - the catalogue is what
        # settles a folder holding one film under several of its titles, and
        # that cannot be known before asking - but an id this run found is
        # never put on a name it cannot account for.
        assert sorted(p.name for p in (tmp_path / "The Movie (1934)").iterdir()) \
            == ["The Movie (1934) Part 1 - The First Half.en.srt",
                "The Movie (1934) Part 1 - The First Half.mkv",
                "The Movie (1934) Part 2 - The Others.mkv"]
        assert [(reason, names) for _path, reason, names in ambiguous] == [
            ("is one film in parts that do not stack",
             ["The Movie (1934) Part 1 - The First Half.mkv",
              "The Movie (1934) Part 2 - The Others.mkv"])]
        assert logs == ['  "The Movie (1934)" is in parts that Plex will not '
                        "stack - left as it is"]

    def test_a_part_written_the_way_plex_reads_it_still_stacks(
            self, monkeypatch, tmp_path):
        """The token last and its number against it: that one Plex stacks, so
        it is named rather than reported."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1934)",
                         ["The Movie (1934) Part1.mkv",
                          "The Movie (1934) Part2.mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"},
                  base="1934-02-01")
        ambiguous: list = []
        tmdblookup.tag_plex_ids(".", [].append, SkipLog(),
                                ambiguous=ambiguous)
        assert ambiguous == []
        assert (tmp_path / "The Movie (1934) {imdb-tt0120737}"
                / "The Movie (1934) {imdb-tt0120737} Part1.mkv").is_file()

    def test_several_releases_of_the_one_film_are_not_ambiguous(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv",
                          "The Movie (1999) Theatrical Cut.mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        ambiguous: list = []
        tmdblookup.tag_plex_ids(".", [].append, SkipLog(),
                                ambiguous=ambiguous)
        assert ambiguous == []
        assert (tmp_path / "The Movie (1999) {imdb-tt0120737}"
                / "The Movie (1999) {imdb-tt0120737} "
                  "{edition-Theatrical Cut}.mkv").is_file()

    def test_a_match_with_editions_names_both(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999) colorized.mkv",
                          "The Movie (1999) (Uncut).mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert logs == ['  match: "The Movie (1999)" -> {imdb-tt0120737}, '
                        "editions: Colorized, Uncut"]

    def test_a_title_spanning_a_year_keeps_the_last_year(self, monkeypatch,
                                                          tmp_path):
        # "Nighthawk (1999) (2005)": the greedy title takes up to the LAST year, so
        # the film is "Nighthawk (1999)" of 2005, and that is what is searched
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Nighthawk (1999) (2005)", ["Nighthawk (1999) (2005).mkv"]))
        self._env(monkeypatch, {"Nighthawk (1999)": "tt0000009"}, base="2005-05-01")
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert (tmp_path / "Nighthawk (1999) (2005) {imdb-tt0000009}").is_dir()
        assert logs == ['  match: "Nighthawk (1999) (2005)" -> {imdb-tt0000009}']

    def test_a_collision_is_skipped_and_recorded(self, monkeypatch, tmp_path):
        # the target file name is already taken, so the file rename is refused
        # and recorded rather than overwriting
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv",
                          "The Movie (1999) {imdb-tt0120737}.mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        skip = SkipLog()
        tmdblookup.tag_plex_ids(".", logs.append, skip)
        assert skip.skips == [
            ("./The Movie (1999)/The Movie (1999).mkv",
             "./The Movie (1999)/The Movie (1999) {imdb-tt0120737}.mkv")]

    def test_a_symlinked_folder_is_not_descended_through(self, monkeypatch,
                                                          tmp_path):
        """find -P -type d: a link to a folder is a link, not a folder. The
        film below the real one is tagged once, through the path that is
        really there, and the link is left as a link rather than becoming a
        second way to reach the same film."""
        monkeypatch.chdir(tmp_path)
        real = tmp_path / "Real"
        _tree(real, ("The Movie (1999)", ["The Movie (1999).mkv"]))
        (tmp_path / "Link").symlink_to("Real")
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog(), recursive=True)
        assert logs == ['  match: "The Movie (1999)" -> {imdb-tt0120737}']
        assert (real / "The Movie (1999) {imdb-tt0120737}").is_dir()
        assert (tmp_path / "Link").is_symlink()

    def test_a_library_that_keeps_its_films_a_level_down_is_walked(
            self, monkeypatch, tmp_path):
        """An "Unsorted" or a box set between the root and the films: the
        folders in between are not films, so they are walked through."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path / "Unsorted", ("The Movie (1999)",
                                      ["The Movie (1999).mkv"]))
        _tree(tmp_path / "Box Set" / "Deeper", ("Another Film (2014)",
                                                ["Another Film (2014).mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737",
                                "Another Film": "tt0000003"},
                  base="1999-06-23")
        tmdblookup.tag_plex_ids(".", [].append, SkipLog(), recursive=True)
        assert (tmp_path / "Unsorted/The Movie (1999) {imdb-tt0120737}"
                "/The Movie (1999) {imdb-tt0120737}.mkv").is_file()

    def test_and_a_full_ingest_reads_one_level_as_it_always_did(
            self, monkeypatch, tmp_path):
        """The phases around this one inside a full run read that same one
        level, and the caller is pointed at the folder holding the films."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path / "Unsorted", ("The Movie (1999)",
                                      ["The Movie (1999).mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert logs == []
        assert (tmp_path / "Unsorted/The Movie (1999)").is_dir()

    def test_what_sits_inside_a_film_is_not_another_film(self, monkeypatch,
                                                          tmp_path):
        """A film folder is not descended into: its extras are its own
        material, and a bonus folder that happens to carry a year is not a
        second feature to look up."""
        monkeypatch.chdir(tmp_path)
        folder = tmp_path / "The Movie (1999)" / "Deleted Scenes (1999)"
        folder.mkdir(parents=True)
        (folder / "A scene.mkv").touch()
        (tmp_path / "The Movie (1999)" / "The Movie (1999).mkv").touch()
        self._env(monkeypatch, {"The Movie": "tt0120737",
                                "Deleted Scenes": "tt0000009"})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert logs == ['  match: "The Movie (1999)" -> {imdb-tt0120737}']
        assert (tmp_path / "The Movie (1999) {imdb-tt0120737}"
                / "Deleted Scenes (1999)").is_dir()

    def test_the_report_lists_the_refused_renames(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv",
                          "The Movie (1999) {imdb-tt0120737}.mkv"]))
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        skip = SkipLog()
        tmdblookup.tag_plex_ids(".", logs.append, skip)
        assert skip.report() == [
            "Safety: skipped 1 rename(s) to avoid overwrite",
            "Safety skip details:",
            "  ./The Movie (1999)/The Movie (1999).mkv -> "
            "./The Movie (1999)/The Movie (1999) {imdb-tt0120737}.mkv"]

class TestWhatASpellingSettles:
    """The folders a dry run over a real library reported, and what the module
    does with them now.

    Each of these was "holds a film that is not its own", "is one film in parts
    that do not stack" or "holds a duplicate marked only by a number" - and each
    of them was one film all along, written by two hands.
    """

    def _network(self, monkeypatch, title, imdb, spelled=None, year="1961"):
        """A catalogue that knows one film, under ``spelled`` (its own writing
        of the title) whatever it is asked."""
        monkeypatch.setenv("tmdbApiKey", "apikey")
        queries = []

        def fake_curl(url, params):
            kv = dict(params)
            if url == _BASE + "/search/movie":
                queries.append(kv["query"])
                # Exactly the fold and no reading beyond it: what is being
                # exercised is which SPELLINGS the lookup goes asking under, so
                # a catalogue that answered to all of them would prove nothing.
                if titlematch.normalize_title(kv["query"]) \
                        != titlematch.normalize_title(title):
                    return json.dumps({"results": []})
                return json.dumps({"results": [
                    _row(1, spelled or title, date=year + "-06-23")]})
            if url == _BASE + "/movie/1":
                return json.dumps({"external_ids": {"imdb_id": imdb}})
            return None

        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        return queries

    def _run(self, monkeypatch, tmp_path, folder, files, **network):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, (folder, files))
        queries = self._network(monkeypatch, **network)
        logs, ambiguous = [], []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog(),
                                ambiguous=ambiguous)
        return logs, ambiguous, queries

    def _listing(self, tmp_path):
        return sorted(str(path.relative_to(tmp_path))
                      for path in tmp_path.rglob("*"))

    def test_a_film_written_two_ways_is_one_film(self, monkeypatch, tmp_path):
        """The file said the same title as its folder, in another hand - and
        the folder was reported as holding a film that is not its own."""
        _logs, ambiguous, _q = self._run(
            monkeypatch, tmp_path, "Le seigneur de Val-Mont (1961)",
            ["le seigneur de val mont (1961).mkv"],
            title="Le seigneur de Val-Mont", imdb="tt0054824")
        assert ambiguous == []
        assert self._listing(tmp_path) == [
            "Le seigneur de Val-Mont (1961) {imdb-tt0054824}",
            "Le seigneur de Val-Mont (1961) {imdb-tt0054824}/"
            "Le seigneur de Val-Mont (1961) {imdb-tt0054824}.mkv"]

    def test_a_film_in_parts_that_now_stack(self, monkeypatch, tmp_path):
        """"Part 1" is the token Plex stacks on, written with the number held
        off."""
        _logs, ambiguous, _q = self._run(
            monkeypatch, tmp_path, "Le seigneur de Val-Mont (1961)",
            ["Le seigneur de Val-Mont (1961) Part 1.mkv",
             "Le seigneur de Val-Mont (1961) Part 2.mkv"],
            title="Le seigneur de Val-Mont", imdb="tt0054824")
        assert ambiguous == []
        tagged = "Le seigneur de Val-Mont (1961) {imdb-tt0054824}"
        assert self._listing(tmp_path) == [
            tagged, tagged + "/" + tagged + " Part1.mkv",
            tagged + "/" + tagged + " Part2.mkv"]

    def test_a_lone_copy_loses_its_marker(self, monkeypatch, tmp_path):
        _logs, ambiguous, _q = self._run(
            monkeypatch, tmp_path, "The Movie (1999)",
            ["The Movie (1999) (1).mkv"],
            title="The Movie", imdb="tt0120737", year="1999")
        assert ambiguous == []
        tagged = "The Movie (1999) {imdb-tt0120737}"
        assert self._listing(tmp_path) == [tagged, tagged + "/" + tagged + ".mkv"]

    def test_but_two_copies_are_still_two_files(self, monkeypatch, tmp_path):
        """Which of them to keep is not a naming question."""
        _logs, ambiguous, _q = self._run(
            monkeypatch, tmp_path, "The Movie (1999)",
            ["The Movie (1999).mkv", "The Movie (1999) (1).mkv"],
            title="The Movie", imdb="tt0120737", year="1999")
        assert [reason for _p, reason, _n in ambiguous] \
            == ["holds a duplicate marked only by a number"]

    def test_a_year_missing_half_its_brackets_is_repaired(self, monkeypatch,
                                                           tmp_path):
        _logs, ambiguous, _q = self._run(
            monkeypatch, tmp_path, "The Movie (1999",
            ["The Movie (1999.mkv"],
            title="The Movie", imdb="tt0120737", year="1999")
        assert ambiguous == []
        tagged = "The Movie (1999) {imdb-tt0120737}"
        assert self._listing(tmp_path) == [tagged, tagged + "/" + tagged + ".mkv"]

    def test_the_catalogues_own_spelling_is_what_everything_is_written_under(
            self, monkeypatch, tmp_path):
        """The folder matched through an equivalence, so the spelling that is
        known to be right is the catalogue's and not the disk's."""
        logs, _a, _q = self._run(
            monkeypatch, tmp_path, "LE SEIGNEUR DE VAL MONT (1961)",
            ["LE SEIGNEUR DE VAL MONT (1961).mkv"],
            title="Le seigneur de Val-Mont", imdb="tt0054824",
            spelled="Le seigneur de Val-Mont")
        tagged = "Le seigneur de Val-Mont (1961) {imdb-tt0054824}"
        assert self._listing(tmp_path) == [tagged, tagged + "/" + tagged + ".mkv"]
        assert '  TMDb spells it "Le seigneur de Val-Mont" - renaming ' \
            '"LE SEIGNEUR DE VAL MONT (1961)" onto it' in logs

    def test_a_film_found_under_its_own_language_keeps_it(self, monkeypatch,
                                                           tmp_path):
        """The catalogue's primary title is English and the folder's is not, so
        the English one folds to keys this folder never had - it is not a
        spelling the film was found under, and not one it is renamed to."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Le seigneur de Val-Mont (1961)",
                         ["Le seigneur de Val-Mont (1961).mkv"]))
        monkeypatch.setenv("tmdbApiKey", "apikey")

        def fake_curl(url, params):
            if url == _BASE + "/search/movie":
                return json.dumps({"results": [
                    _row(1, "The Lord of Val-Mont",
                         original="Le seigneur de Val-Mont",
                         date="1961-06-23")]})
            if url == _BASE + "/movie/1":
                return json.dumps({"external_ids": {"imdb_id": "tt0054824"}})
            return None

        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog())
        assert (tmp_path / "Le seigneur de Val-Mont (1961) "
                           "{imdb-tt0054824}").is_dir()

    def test_a_spelling_no_file_can_be_named_after_is_not_used(self):
        """Trade/Off is a real film and not a real folder, and the id is still
        worth having."""
        candidate = tmdblookup._Candidate(
            years=frozenset({"1997"}), titles=tmdblookup.title_keys("Trade/Off"),
            spellings=("Trade/Off",), runtime=0.0, imdb="tt0119094")
        settled = tmdblookup._matched(candidate,
                                      tmdblookup.title_keys("Trade Off"))
        assert (settled.imdb, settled.title) == ("tt0119094", "")

    def test_the_filler_a_library_wrote_in_front_is_asked_around(
            self, monkeypatch, tmp_path):
        _logs, ambiguous, queries = self._run(
            monkeypatch, tmp_path, "Movie - The Movie (1999)",
            ["Movie - The Movie (1999).mkv"],
            title="The Movie", imdb="tt0120737", year="1999")
        assert ambiguous == []
        assert queries[0] == "Movie - The Movie"
        assert "The Movie" in queries
        # And the filler goes with it: the spelling the film was FOUND under is
        # the catalogue's, and that is what the folder is written under.
        assert (tmp_path / "The Movie (1999) {imdb-tt0120737}").is_dir()


class TestTheFolderNothingCanRename:
    """Which file is which release cannot be guessed. Which FILM they are is not
    a guess at all when every id already in the folder is the same id."""

    def _run(self, monkeypatch, tmp_path, folder, files):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        # The lookup runs before the folder is judged now, so the network is
        # reached; what these cases are about is that it settles nothing here.
        monkeypatch.setattr(tmdblookup, "_curl",
                            lambda *_a, **_k: json.dumps({"results": []}))
        _tree(tmp_path, (folder, files))
        logs, ambiguous = [], []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog(),
                                ambiguous=ambiguous)
        return logs, ambiguous

    def _reasons(self, ambiguous):
        return [reason for _p, reason, _n in ambiguous]

    def test_a_second_film_in_the_folder_still_gets_the_agreed_id(
            self, monkeypatch, tmp_path):
        tag = "{imdb-tt0054824}"
        _logs, ambiguous = self._run(
            monkeypatch, tmp_path, "Le seigneur de Val-Mont (1961) " + tag,
            ["Le seigneur de Val-Mont (1961) " + tag + ".mkv",
             "La fuite.mkv"])
        assert (tmp_path / ("Le seigneur de Val-Mont (1961) " + tag)
                / ("La fuite " + tag + ".mkv")).is_file()
        # And it is STILL on the list: the id is no longer missing, the names
        # still are, and whether they really could not be settled is the thing
        # somebody has to look at.
        assert self._reasons(ambiguous) == [
            "holds a film that is not its own" + tmdblookup.TAGGED_ANYWAY]

    def test_a_part_with_a_title_after_it_gets_it_too(self, monkeypatch,
                                                       tmp_path):
        tag = "{imdb-tt0054824}"
        folder = "The Film (1961) " + tag
        _logs, ambiguous = self._run(
            monkeypatch, tmp_path, folder,
            ["The Film (1961) " + tag + " Part 1 - The Escape.mkv",
             "The Film (1961) Part 2 - The Revenge.mkv"])
        assert self._reasons(ambiguous) == [
            "is one film in parts that do not stack" + tmdblookup.TAGGED_ANYWAY]
        assert (tmp_path / folder
                / ("The Film (1961) " + tag + " Part 2 - The Revenge.mkv")
                ).is_file()

    def test_two_ids_that_disagree_stop_it_dead(self, monkeypatch, tmp_path):
        """Two ids in a folder is how a sequel ends up filed under the film
        before it."""
        folder = "The Film (1961) {imdb-tt0000001}"
        _logs, ambiguous = self._run(
            monkeypatch, tmp_path, folder,
            ["The Film (1961) {imdb-tt0000001}.mkv",
             "Another Film {imdb-tt0000002}.mkv"])
        assert [reason for _p, reason, _n in ambiguous] \
            == ["holds a film that is not its own"]
        assert (tmp_path / folder / "Another Film {imdb-tt0000002}.mkv").is_file()

    def test_and_so_does_having_no_id_to_agree_on(self, monkeypatch, tmp_path):
        _logs, ambiguous = self._run(
            monkeypatch, tmp_path, "The Film (1961)",
            ["The Film (1961).mkv", "Another Film.mkv"])
        assert [reason for _p, reason, _n in ambiguous] \
            == ["holds a film that is not its own"]

    def test_a_folder_already_tagged_throughout_is_no_work(self, monkeypatch,
                                                            tmp_path):
        tag = "{imdb-tt0054824}"
        folder = "The Film (1961) " + tag
        logs, ambiguous = self._run(
            monkeypatch, tmp_path, folder,
            ["The Film (1961) " + tag + ".mkv",
             "Another Film " + tag + ".mkv"])
        assert logs == []
        assert (tmp_path / folder / ("Another Film " + tag + ".mkv")).is_file()
        # No work to do, and still reported: a run that says nothing about it
        # would be a folder that quietly stopped being on anyone's list.
        assert self._reasons(ambiguous) == [
            "holds a film that is not its own" + tmdblookup.TAGGED_ANYWAY]


class TestTheNamesAReportAndAProbeSee:
    """Nothing has been renamed while the folder is still being read, so both
    of them have to name the file that is actually there."""

    def test_a_report_names_the_file_on_disk_and_not_the_one_it_would_be(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        monkeypatch.setattr(tmdblookup, "_curl",
                            lambda *_a, **_k: json.dumps({"results": []}))
        _tree(tmp_path, ("The Film (1961)",
                         ["the film (1961) part 1 - the escape.mkv",
                          "the film (1961) part 2 - the revenge.mkv"]))
        ambiguous = []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                ambiguous=ambiguous)
        _path, reason, listed = ambiguous[0]
        assert reason == "is one film in parts that do not stack"
        for name in listed:
            assert (tmp_path / "The Film (1961)" / name).is_file(), name

    def test_the_length_is_read_off_the_file_that_is_there(self, monkeypatch,
                                                            tmp_path):
        """The parts are respelled onto the folder's own name, and the probe
        still has to open them under the names they arrived with."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        _tree(tmp_path, ("The Film (1961)",
                         ["the film (1961) part 1.mkv",
                          "the film (1961) part 2.mkv"]))
        opened = []

        def fake_duration(paths):
            # Asked while the folder still has its old name, which is the only
            # moment the answer means anything.
            opened.extend((name, pathlib.Path(name).is_file()) for name in paths)
            return 7200.0

        monkeypatch.setattr(tmdblookup.durationcheck, "total_duration",
                            fake_duration)
        search = json.dumps({"results": [_row(1, "The Film", date="1962-01-01"),
                                         _row(2, "The Film", date="1962-01-01")]})
        _install(monkeypatch, search, {}, {1: "tt0000001", 2: "tt0000002"},
                 runtimes={1: 120, 2: 30})
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog())
        assert opened == [
            ("./The Film (1961)/the film (1961) part 1.mkv", True),
            ("./The Film (1961)/the film (1961) part 2.mkv", True)]
        assert (tmp_path / "The Film (1961) {imdb-tt0000001}").is_dir()


class TestHowCloseItCame:
    """The list a dry run leaves so that "no confident match" can be read
    rather than taken on trust."""

    def _asked(self, monkeypatch, tmp_path, folder, rows, runtimes=None):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, (folder, [folder + ".mkv"]))
        search = json.dumps({"results": rows})
        _install(monkeypatch, search, {},
                 {row["id"]: "tt000000%d" % row["id"] for row in rows},
                 runtimes=runtimes)
        near = []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                dry_run=True, near_misses=near)
        return near

    def test_a_candidate_refused_for_its_year_says_so(self, monkeypatch,
                                                       tmp_path):
        near = self._asked(monkeypatch, tmp_path, "Some Film (1988)",
                           [_row(1, "Some Film", date="1991-01-01"),
                            _row(2, "Some Film", date="1992-01-01")])
        _path, reason, notes = near[0]
        assert reason == "no confident TMDb match"
        assert '    "Some Film" (1991) tt0000001 - carries the title, but ' \
            "came out 1991, not 1988" in notes

    def test_a_candidate_refused_for_its_title_says_which_titles_it_has(
            self, monkeypatch, tmp_path):
        """The line that says a reading is missing."""
        near = self._asked(monkeypatch, tmp_path, "Some Film (1991)",
                           [_row(1, "A Wholly Other Film", date="1991-01-01")])
        _path, _reason, notes = near[0]
        assert any("no title of its meets this folder's: A Wholly Other Film"
                   in note for note in notes)

    def test_a_candidate_with_no_title_is_not_called_null(self, monkeypatch,
                                                           tmp_path):
        """jq's word for a missing title is not a name to list films under."""
        near = self._asked(monkeypatch, tmp_path, "Some Film (1988)",
                           [{"id": 1, "title": None, "original_title": None,
                             "release_date": "1991-01-01"}])
        _path, _reason, notes = near[0]
        assert not any('"null"' in note for note in notes)

    def test_a_folder_another_list_accounts_for_is_not_in_here(
            self, monkeypatch, tmp_path):
        """One folder, one list. A folder read twice is a folder looked at
        twice, and the ambiguous list is where this one belongs."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        monkeypatch.setattr(tmdblookup, "_curl", lambda *_a, **_k: None)
        _tree(tmp_path, ("Box (1999)", ["Box (1999).mkv", "Other Film.mkv"]))
        near, ambiguous = [], []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                dry_run=True, near_misses=near,
                                ambiguous=ambiguous)
        assert near == []
        assert [reason for _p, reason, _n in ambiguous] \
            == ["holds a film that is not its own"]

    def test_and_the_folds_are_shown_where_it_IS_listed(self, tmp_path):
        """The comparison that says whether it was really unfixable goes with
        the folder, in the one list that has it."""
        listing = tmp_path / "ambiguous.txt"
        tmdblookup.write_ambiguous_list(
            str(listing),
            [(str(tmp_path / "Box (1999)"), "holds a film that is not its own",
              ["Other Film.mkv"])],
            str(tmp_path))
        body = [line for line in listing.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        assert body == ["Box (1999)  -  holds a film that is not its own",
                        "    the folder reads as: box 1999",
                        "    Other Film.mkv",
                        "        reads as: other film"]

    def test_nothing_is_collected_when_no_list_was_passed(self, monkeypatch,
                                                           tmp_path):
        """A real run pays none of it."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Some Film (1988)", ["Some Film (1988).mkv"]))
        calls = _install(monkeypatch, json.dumps({"results": []}), {}, {})
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog())
        assert calls


class TestTheFolderAboveAsTheFirstHalfOfTheTitle:
    """A library that keeps a franchise in a folder of its own writes half the
    title on each, and neither half is the title a catalogue has."""

    def _network(self, monkeypatch, known):
        """A catalogue that answers only to the titles in ``known``, folded
        exactly - so what is being exercised is which spellings are asked."""
        monkeypatch.setenv("tmdbApiKey", "apikey")
        asked = []

        def fake_curl(url, params):
            kv = dict(params)
            if url == _BASE + "/search/movie":
                asked.append(kv["query"])
                for wanted, (rid, _imdb, year) in known.items():
                    if titlematch.normalize_title(kv["query"]) \
                            == titlematch.normalize_title(wanted):
                        return json.dumps({"results": [
                            _row(rid, wanted, date=year + "-05-01")]})
                return json.dumps({"results": []})
            match = re.match(r"^" + re.escape(_BASE) + r"/movie/(\d+)$", url)
            if match:
                for _t, (rid, imdb, _y) in known.items():
                    if rid == int(match.group(1)):
                        return json.dumps({"external_ids": {"imdb_id": imdb}})
            return None

        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        return asked

    def test_the_two_halves_together_name_the_film(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Nordwind" / "Episode IV - Der Sturm (1977)").mkdir(
            parents=True)
        (tmp_path / "Nordwind" / "Episode IV - Der Sturm (1977)"
         / "Episode IV - Der Sturm (1977).mkv").touch()
        asked = self._network(monkeypatch, {
            "Nordwind: Episode IV - Der Sturm": (1, "tt0076759", "1977")})
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                recursive=True)
        # Asked under its own name first, and only then under the two halves.
        assert asked[0] == "Episode IV - Der Sturm"
        assert "Nordwind Episode IV - Der Sturm" in asked
        # And written under the spelling that answered.
        assert (tmp_path / "Nordwind"
                / "Nordwind: Episode IV - Der Sturm (1977) {imdb-tt0076759}"
                ).is_dir()

    def test_and_the_films_own_name_still_comes_first(self, monkeypatch,
                                                      tmp_path):
        """A film findable under its own name never pays for the reading."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Nordwind" / "Der Sturm (1977)").mkdir(parents=True)
        (tmp_path / "Nordwind" / "Der Sturm (1977)"
         / "Der Sturm (1977).mkv").touch()
        asked = self._network(monkeypatch, {"Der Sturm": (1, "tt0076759",
                                                           "1977")})
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                recursive=True)
        assert asked == ["Der Sturm"]
        assert (tmp_path / "Nordwind"
                / "Der Sturm (1977) {imdb-tt0076759}").is_dir()

    def test_the_folder_the_run_was_pointed_at_is_not_a_franchise(
            self, monkeypatch, tmp_path):
        """It is the library, named by whoever typed it, and gluing it to every
        title underneath would ask about "Films Rivertown" once per film."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Der Sturm (1977)", ["Der Sturm (1977).mkv"]))
        asked = self._network(monkeypatch, {})
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                recursive=True)
        assert not any(query.startswith(os.path.basename(str(tmp_path)))
                       for query in asked)

    def test_a_tag_an_earlier_run_left_on_the_folder_above_is_not_a_title(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        above = tmp_path / "Nordwind {imdb-tt0076759}" / "Der Sturm (1977)"
        above.mkdir(parents=True)
        (above / "Der Sturm (1977).mkv").touch()
        asked = self._network(monkeypatch, {})
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                recursive=True)
        assert "Nordwind Der Sturm" in asked
        assert not any("imdb" in query for query in asked)

    def test_the_reading_is_offered_to_the_matching_and_not_only_the_search(
            self, monkeypatch, tmp_path):
        """A candidate found under the film's own name still has to carry a
        title this folder answers to - and with the folder above read in, the
        full title is one of them."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Seewolf" / "Wiederkehr (1997)").mkdir(parents=True)
        (tmp_path / "Seewolf" / "Wiederkehr (1997)"
         / "Wiederkehr (1997).mkv").touch()
        self._network(monkeypatch, {"Seewolf Wiederkehr": (1, "tt0118583",
                                                           "1997")})
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                recursive=True)
        assert (tmp_path / "Seewolf"
                / "Seewolf Wiederkehr (1997) {imdb-tt0118583}").is_dir()


class TestTheFoldsAReportShows:
    def test_a_tag_is_not_folded_into_what_a_name_says(self, monkeypatch,
                                                        tmp_path):
        """Two names that differ only in that one already carries its id would
        otherwise read as two different films, with the id the loudest thing on
        the line."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        monkeypatch.setattr(tmdblookup, "_curl", lambda *_a, **_k: None)
        tag = "{imdb-tt0054824}"
        _tree(tmp_path, ("The Film (1961) " + tag,
                         ["The Film (1961) " + tag + " Part 1 - The Escape.mkv",
                          "The Film (1961) Part 2 - The Revenge.mkv"]))
        ambiguous = []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                dry_run=True, ambiguous=ambiguous)
        listing = tmp_path / "ambiguous.txt"
        tmdblookup.write_ambiguous_list(str(listing), ambiguous, str(tmp_path))
        written = listing.read_text(encoding="utf-8")
        # The name on disk is quoted verbatim, tag and all - it is what the
        # file is called. What must not carry the tag is the FOLD after it.
        folds = [line.split("reads as: ", 1)[1]
                 for line in written.splitlines() if "reads as: " in line]
        assert not any("imdb" in fold for fold in folds)
        assert sorted(folds) == ["the film 1961",
                                 "the film 1961 part 1 the escape",
                                 "the film 1961 part 2 the revenge"]


class TestOneFilmUnderSeveralOfItsTitles:
    """A folder whose files are the same film named in other languages. Nothing
    about the strings can say so - "Die Blaue Stunde" and "L'Ora Blu" share not
    a syllable with each other or with "The Blue Hour" - and the catalogue's own
    alternative titles can."""

    TITLES = ("The Blue Hour", "Die Blaue Stunde", "L'Ora Blu")

    def _catalogue(self, monkeypatch, titles, imdb="tt0079068", year="1979"):
        monkeypatch.setenv("tmdbApiKey", "apikey")

        def fake_curl(url, params):
            kv = dict(params)
            if url == _BASE + "/search/movie":
                if not any(titlematch.equivalent(kv["query"], t)
                           for t in titles):
                    return json.dumps({"results": []})
                return json.dumps({"results": [
                    _row(1, titles[0], date=year + "-04-01")]})
            if url == _BASE + "/movie/1":
                return json.dumps({
                    "external_ids": {"imdb_id": imdb},
                    "alternative_titles": {
                        "titles": [{"title": t} for t in titles[1:]]},
                    "release_dates": {"results": []}})
            return None

        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)

    def test_the_folder_is_tagged_and_every_file_keeps_its_own_language(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Blue Hour (1979)",
                         ["Die Blaue Stunde (1979).mkv",
                          "L'Ora Blu (1979).mkv"]))
        self._catalogue(monkeypatch, self.TITLES)
        aliases, ambiguous = [], []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                aliases=aliases, ambiguous=ambiguous)
        tagged = tmp_path / "The Blue Hour (1979) {imdb-tt0079068}"
        assert tagged.is_dir()
        assert ambiguous == []
        # Each keeps the language it was named in, and gains only the id.
        assert sorted(path.name for path in tagged.iterdir()) == [
            "Die Blaue Stunde (1979) {imdb-tt0079068}.mkv",
            "L'Ora Blu (1979) {imdb-tt0079068}.mkv"]

    def test_and_it_is_announced(self, monkeypatch, tmp_path):
        """Nothing was renamed away, so no other list would mention it."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Blue Hour (1979)",
                         ["Die Blaue Stunde (1979).mkv",
                          "L'Ora Blu (1979).mkv"]))
        self._catalogue(monkeypatch, self.TITLES)
        aliases = []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                aliases=aliases)
        (_path, base, tag, known), = aliases
        assert (base, tag) == ("The Blue Hour (1979)",
                               "{imdb-tt0079068}")
        assert sorted(known) == ["Die Blaue Stunde (1979).mkv",
                                 "L'Ora Blu (1979).mkv"]

    def test_a_file_the_catalogue_does_not_know_keeps_the_folder_ambiguous(
            self, monkeypatch, tmp_path):
        """One name the catalogue accounts for does not vouch for another it
        does not: a second feature is still a second feature."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Blue Hour (1979)",
                         ["Die Blaue Stunde (1979).mkv",
                          "A Wholly Different Film.mkv"]))
        self._catalogue(monkeypatch, self.TITLES)
        aliases, ambiguous = [], []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                aliases=aliases, ambiguous=ambiguous)
        assert aliases == []
        assert [reason for _p, reason, _n in ambiguous] \
            == ["holds a film that is not its own"]
        # And nothing was renamed, so neither file was tagged as the other.
        assert (tmp_path / "The Blue Hour (1979)"
                / "A Wholly Different Film.mkv").is_file()

    def test_an_id_this_run_found_is_never_put_on_a_name_it_cannot_account_for(
            self, monkeypatch, tmp_path):
        """The whole safety of the alias path: only names the CATALOGUE
        vouched for get the folder's id."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("The Blue Hour (1979)",
                         ["The Blue Hour (1979).mkv",
                          "A Wholly Different Film.mkv"]))
        self._catalogue(monkeypatch, self.TITLES)
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog())
        held = sorted(path.name for path in
                      (tmp_path / "The Blue Hour (1979)").iterdir())
        assert held == ["A Wholly Different Film.mkv",
                        "The Blue Hour (1979).mkv"]


class TestAYearWithADigitWrong:
    def test_it_is_reported_as_a_date_and_not_as_a_duplicate(self, monkeypatch,
                                                              tmp_path):
        """One is a second file nobody meant to keep, the other is one file
        with a digit wrong, and reporting the second as the first sends
        somebody looking for a duplicate that was never there."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        monkeypatch.setattr(tmdblookup, "_curl",
                            lambda *_a, **_k: json.dumps({"results": []}))
        _tree(tmp_path, ("Falcons Forever (1988)",
                         ["Falcons Forever (1998).mkv"]))
        ambiguous = []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                ambiguous=ambiguous)
        assert [reason for _p, reason, _n in ambiguous] \
            == ["holds the same film under another year"]

    def test_and_a_real_copy_marker_still_reads_as_one(self, monkeypatch,
                                                        tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        monkeypatch.setattr(tmdblookup, "_curl",
                            lambda *_a, **_k: json.dumps({"results": []}))
        _tree(tmp_path, ("The Movie (1999)",
                         ["The Movie (1999).mkv", "The Movie (1999) (1).mkv"]))
        ambiguous = []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                ambiguous=ambiguous)
        assert [reason for _p, reason, _n in ambiguous] \
            == ["holds a duplicate marked only by a number"]

    def test_a_homoglyph_is_not_a_film_of_its_own(self, monkeypatch, tmp_path):
        """The file's name is the folder's name on the screen; only one letter
        came from another keyboard."""
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Tempest - Rising (2017)",
                         ["Тempest - Rising (2017) IMAX.mkv"]))
        self._knows_the_film(monkeypatch)
        ambiguous = []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                ambiguous=ambiguous)
        assert ambiguous == []
        tagged = tmp_path / "Tempest - Rising (2017) {imdb-tt3501632}"
        assert tagged.is_dir()
        assert [path.name for path in tagged.iterdir()] == [
            "Tempest - Rising (2017) {imdb-tt3501632} {edition-IMAX}.mkv"]

    def _knows_the_film(self, monkeypatch):
        monkeypatch.setenv("tmdbApiKey", "apikey")

        def fake_curl(url, params):
            kv = dict(params)
            if url == _BASE + "/search/movie":
                if not titlematch.equivalent(kv["query"], "Tempest: Rising"):
                    return json.dumps({"results": []})
                return json.dumps({"results": [
                    _row(1, "Tempest - Rising", date="2017-10-25")]})
            return json.dumps({"external_ids": {"imdb_id": "tt3501632"}})

        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)


class TestEvidenceOfDifferentStrength:
    """Two candidates are not equally likely just because both survived the
    rules. Weighing them as though they were is what left obvious films
    unnamed."""

    def test_a_title_as_written_beats_one_reached_through_a_reading(
            self, monkeypatch):
        """The folder "1815" was offered the film "1815" - which folds to
        exactly what the folder does - alongside a "Bramble '15" that only
        reached the list through a reading, and both their lengths fit."""
        search = json.dumps({"results": [
            _row(1, "1815", date="2019-01-10"),
            _row(2, "Bramble '15", date="2019-01-10")]})
        # The second reaches the list only through the article reading, which
        # is the whole distinction: its titles fold to "the 1815", never to the
        # "1815" the folder is written as.
        _install(monkeypatch, search, {2: ["The 1815"]},
                 {1: "tt8579674", 2: "tt7520286"},
                 runtimes={1: 119, 2: 124})
        assert tmdblookup.tmdb_imdb_id("1815", "2019",
                                       lambda: 119 * 60.0) == "tt8579674"

    def test_but_a_second_that_also_carries_it_as_written_still_stops_it(
            self, monkeypatch):
        """If a catalogue really does hold both films under one title, two
        candidates whose lengths both fit are two candidates."""
        search = json.dumps({"results": [
            _row(1, "1815", date="2019-01-10"),
            _row(2, "Bramble '15", date="2019-01-10")]})
        _install(monkeypatch, search, {2: ["1815"]},
                 {1: "tt8579674", 2: "tt7520286"},
                 runtimes={1: 119, 2: 124})
        assert tmdblookup.tmdb_imdb_id("1815", "2019",
                                       lambda: 119 * 60.0) == ""

    def test_two_that_both_carry_it_as_written_are_still_two(self,
                                                             monkeypatch):
        """It narrows and never widens."""
        search = json.dumps({"results": [_row(1, "Nighthawk", date="1999-01-10"),
                                         _row(2, "Nighthawk", date="1999-01-10")]})
        _install(monkeypatch, search, {}, {1: "tt0000001", 2: "tt0000002"})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == ""

    def test_and_where_none_carries_it_as_written_they_all_stand(self,
                                                                 monkeypatch):
        search = json.dumps({"results": [_row(1, "The Nighthawk",
                                              date="1999-01-10")]})
        _install(monkeypatch, search, {}, {1: "tt0000001"})
        assert tmdblookup.tmdb_imdb_id("Nighthawk", "1999") == "tt0000001"

    def test_a_re_release_does_not_answer_for_the_film_it_was_timed_to(
            self, monkeypatch):
        """"A Light Is Lit" from 1976 went back into cinemas in 2018 because
        "A Light Is Lit" from 2018 came out."""
        search = json.dumps({"results": [
            _row(1, "A Light Is Lit", date="2018-10-03"),
            _row(2, "A Light Is Lit", date="1976-12-17")]})
        _install(monkeypatch, search, {}, {1: "tt1517451", 2: "tt0075265"},
                 runtimes={1: 136, 2: 140},
                 years={1: ["2018", "2019", "2021"],
                        2: ["1976", "1977", "1982", "2018"]})
        assert tmdblookup.tmdb_imdb_id("A Light Is Lit", "2018",
                                       lambda: 135.8 * 60.0) == "tt1517451"

    def test_a_premiere_a_year_before_the_release_is_not_a_re_release(
            self, monkeypatch):
        """Around the wanted year, not exactly on it: the festival premiere and
        the general release a year apart are the case the year rule exists
        for."""
        search = json.dumps({"results": [_row(1, "The Film", date="2017-09-01")]})
        _install(monkeypatch, search, {}, {1: "tt0000001"},
                 years={1: ["2017", "2018"]})
        assert tmdblookup.tmdb_imdb_id("The Film", "2018") == "tt0000001"

    def test_and_where_every_candidate_is_equally_old_they_all_stand(
            self, monkeypatch):
        search = json.dumps({"results": [
            _row(1, "The Film", date="1976-01-01"),
            _row(2, "The Film", date="1976-01-01")]})
        _install(monkeypatch, search, {}, {1: "tt0000001", 2: "tt0000002"},
                 years={1: ["1976", "2018"], 2: ["1976", "2018"]})
        assert tmdblookup.tmdb_imdb_id("The Film", "2018") == ""


class TestWhichListAFolderBelongsTo:
    """Four reports, each folder in exactly one - except the hand-fill list,
    which is meant to hold the near misses so they can be looked up by hand."""

    def _library(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        _tree(tmp_path,
              ("Ordinary (1999)", ["Ordinary (1999).mkv"]),
              ("Box (1999)", ["Box (1999).mkv", "Other Film.mkv"]),
              ("Unknowable (1899)", ["Unknowable (1899).mkv"]),
              ("Bluehour (1979)", ["Blaue (1979).mkv"]))

        def fake_curl(url, params):
            kv = dict(params)
            if url == _BASE + "/search/movie":
                for rid, title, year in ((1, "Ordinary", "1999"),
                                         (2, "Box", "1999"),
                                         (4, "Bluehour", "1979")):
                    if titlematch.normalize_title(kv["query"]) \
                            == titlematch.normalize_title(title):
                        return json.dumps({"results": [
                            _row(rid, title, date=year + "-01-01")]})
                return json.dumps({"results": []})
            match = re.match(r"^" + re.escape(_BASE) + r"/movie/(\d+)$", url)
            rid = int(match.group(1)) if match else 0
            body = {"external_ids": {"imdb_id": "tt000000%d" % rid},
                    "release_dates": {"results": []},
                    "alternative_titles": {"titles": []}}
            if rid == 4:
                body["alternative_titles"] = {"titles": [{"title": "Blaue"}]}
            return json.dumps(body)

        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)

    def test_each_folder_is_named_by_exactly_one_report(self, monkeypatch,
                                                         tmp_path):
        self._library(monkeypatch, tmp_path)
        unmatched, ambiguous, planned, near, aliases = [], [], [], [], []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                dry_run=True, recursive=True,
                                unmatched=unmatched, ambiguous=ambiguous,
                                planned=planned, near_misses=near,
                                aliases=aliases)
        seen = {
            "ambiguous": {os.path.basename(path) for path, _r, _n in ambiguous},
            "nearmiss": {os.path.basename(path) for path, _r, _n in near},
            "othertitles": {os.path.basename(path)
                            for path, _b, _t, _k in aliases},
            # A rename is either of a film folder or of a file inside one, so
            # the folder it is about is one of those two parts.
            "renames": {part for source, _target in planned
                        for part in (os.path.basename(source),
                                     os.path.basename(os.path.dirname(source)))
                        if part in ("Ordinary (1999)", "Box (1999)",
                                    "Unknowable (1899)", "Bluehour (1979)")},
        }
        assert seen["ambiguous"] == {"Box (1999)"}
        assert seen["nearmiss"] == {"Unknowable (1899)"}
        assert seen["othertitles"] == {"Bluehour (1979)"}
        assert seen["renames"] == {"Ordinary (1999)"}
        for one, other in itertools.combinations(seen, 2):
            assert not seen[one] & seen[other], (one, other)

    def test_but_the_hand_fill_list_still_holds_the_near_misses(
            self, monkeypatch, tmp_path):
        """It is the file somebody types an id into, so the films to look up
        have to be in it."""
        self._library(monkeypatch, tmp_path)
        unmatched, near = [], []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                dry_run=True, recursive=True,
                                unmatched=unmatched, near_misses=near)
        assert unmatched == ["Unknowable (1899)"]
        assert [os.path.basename(path) for path, _r, _n in near] \
            == ["Unknowable (1899)"]


class TestACandidateThatCouldNotBeTheAnswer:
    """What is being asked for is an IMDb id. A candidate with none was never a
    possible answer, and the only thing it can do is stand beside one that is
    and make the pair look uncertain."""

    def test_the_one_with_an_id_is_taken(self, monkeypatch):
        """A folder called "Re Made" was offered the film it is and an idol
        video with no id, both lengths fitting."""
        search = json.dumps({"results": [
            _row(1, "RE:MADE", date="2016-09-10"),
            _row(2, "忍野さら/Re-Made", date="2016-01-01")]})
        _install(monkeypatch, search, {}, {1: "tt5678110"},
                 runtimes={1: 101, 2: 102})
        assert tmdblookup.tmdb_imdb_id("Re Made", "2016",
                                       lambda: 100 * 60.0) == "tt5678110"

    def test_where_none_of_them_has_one_they_all_stand(self, monkeypatch):
        """There is nothing to answer with either way, and the reports should
        still say what was offered."""
        search = json.dumps({"results": [_row(1, "Re Made", date="2016-09-10"),
                                         _row(2, "Re Made", date="2016-01-01")]})
        _install(monkeypatch, search, {}, {}, runtimes={1: 101, 2: 102})
        near: list = []
        assert tmdblookup.identify("Re Made", "2016", lambda: 100 * 60.0,
                                   near).imdb == ""
        assert any("does not fit" in note or "fits" in note for note in near)

    def test_and_it_is_said_in_the_report(self, monkeypatch):
        search = json.dumps({"results": [
            _row(1, "Re Made", date="2016-09-10"),
            _row(2, "Re Made", date="2016-01-01"),
            _row(3, "Re Made", date="2016-02-01")]})
        _install(monkeypatch, search, {}, {1: "tt0000001"},
                 runtimes={1: 101, 2: 102, 3: 103})
        near: list = []
        tmdblookup.identify("Re Made", "2016", lambda: 0.0, near)
        assert sum("no IMDb id, so it could not be the answer" in note
                   for note in near) == 2

    def test_and_it_stops_blocking_as_an_unmeasurable_one(self, monkeypatch):
        """Worse than standing beside a fit: a candidate with no runtime is
        never ruled OUT by a length, so one that could not have been the answer
        kept the answer at "not certain" all by itself."""
        search = json.dumps({"results": [
            _row(1, "Mr. Blue", date="2015-04-01"),
            _row(2, "Mr. Blue", date="2015-01-01"),
            _row(3, "我的Mr. Blue", date="2015-01-01")]})
        _install(monkeypatch, search, {},
                 {1: "tt2091935", 2: "tt3812348"},
                 runtimes={1: 95, 2: 79})
        assert tmdblookup.tmdb_imdb_id("Mr. Blue", "2015",
                                       lambda: 95.5 * 60.0) == "tt2091935"

    def test_but_one_that_could_have_been_still_blocks(self, monkeypatch):
        """The rule this rests on is unchanged: a candidate nobody can measure
        is not ruled out by a length, so as long as one is standing the answer
        is still "not certain"."""
        search = json.dumps({"results": [
            _row(1, "Mr. Blue", date="2015-04-01"),
            _row(2, "Mr. Blue", date="2015-01-01")]})
        _install(monkeypatch, search, {},
                 {1: "tt2091935", 2: "tt3812348"}, runtimes={1: 95})
        assert tmdblookup.tmdb_imdb_id("Mr. Blue", "2015",
                                       lambda: 95.5 * 60.0) == ""


class TestAFolderNamedInAThirdLanguage:
    """A library that holds a film under two languages usually names the folder
    in a third, and that third is the one being looked up. The files are then
    the only names a catalogue has ever heard of."""

    TITLES = ("Die Blaue Stunde", "The Blue Hour", "L'Ora Blu")

    def _catalogue(self, monkeypatch, titles, imdb="tt0071877", year="1974"):
        monkeypatch.setenv("tmdbApiKey", "apikey")
        asked = []

        def fake_curl(url, params):
            kv = dict(params)
            if url == _BASE + "/search/movie":
                asked.append(kv["query"])
                for rid, group in enumerate(titles, start=1):
                    if any(titlematch.equivalent(kv["query"], t) for t in group):
                        return json.dumps({"results": [
                            _row(rid, group[0], date=year + "-03-01")]})
                return json.dumps({"results": []})
            match = re.match(r"^" + re.escape(_BASE) + r"/movie/(\d+)$", url)
            rid = int(match.group(1)) if match else 1
            group = titles[rid - 1]
            return json.dumps({
                "external_ids": {"imdb_id": imdb if rid == 1
                                 else "tt000000%d" % rid},
                "alternative_titles": {"titles": [{"title": t} for t in group]},
                "release_dates": {"results": []}})

        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        return asked

    def _run(self, monkeypatch, tmp_path, files, titles=None):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Twee Misdienaars (1974)", files))
        asked = self._catalogue(monkeypatch, titles or [self.TITLES])
        aliases, ambiguous, unmatched = [], [], []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                aliases=aliases, ambiguous=ambiguous,
                                unmatched=unmatched)
        return asked, aliases, ambiguous, unmatched

    def test_the_files_name_the_film_the_folder_could_not(self, monkeypatch,
                                                           tmp_path):
        asked, aliases, ambiguous, unmatched = self._run(
            monkeypatch, tmp_path,
            ["Die Blaue Stunde (1974).mkv", "L'Ora Blu (1974).mkv"])
        assert ambiguous == [] and unmatched == []
        # The folder's own name first, and only then the files' own.
        assert asked[0] == "Twee Misdienaars"
        assert "L'Ora Blu" in asked
        tagged = tmp_path / "Twee Misdienaars (1974) {imdb-tt0071877}"
        assert tagged.is_dir()
        # Every name is its own still; only the id is added.
        assert sorted(path.name for path in tagged.iterdir()) == [
            "Die Blaue Stunde (1974) {imdb-tt0071877}.mkv",
            "L'Ora Blu (1974) {imdb-tt0071877}.mkv"]

    def test_and_it_is_announced_with_what_each_answered_under(
            self, monkeypatch, tmp_path):
        _asked, aliases, _amb, _un = self._run(
            monkeypatch, tmp_path,
            ["Die Blaue Stunde (1974).mkv", "L'Ora Blu (1974).mkv"])
        (_path, base, tag, known), = aliases
        assert (base, tag) == ("Twee Misdienaars (1974)", "{imdb-tt0071877}")
        assert known["L'Ora Blu (1974).mkv"] == "L'Ora Blu"

    def test_two_films_that_answer_to_different_ids_settle_nothing(
            self, monkeypatch, tmp_path):
        """A folder whose films answer to two ids is a folder holding two
        films, which is exactly the thing an id must not be guessed over."""
        _asked, aliases, ambiguous, _un = self._run(
            monkeypatch, tmp_path,
            ["L'Ora Blu (1974).mkv", "Een Andere Film (1974).mkv"],
            titles=[self.TITLES, ("Een Andere Film",)])
        assert aliases == []
        assert [reason for _p, reason, _n in ambiguous] \
            == ["holds a film that is not its own"]

    def test_and_so_does_one_the_catalogue_cannot_name_at_all(
            self, monkeypatch, tmp_path):
        """Every one of them has to answer, or the folder is not settled."""
        _asked, aliases, ambiguous, unmatched = self._run(
            monkeypatch, tmp_path,
            ["L'Ora Blu (1974).mkv", "Nobody Knows This (1974).mkv"])
        assert aliases == []
        assert ambiguous or unmatched

    def test_a_folder_whose_own_name_answers_never_asks_its_files(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("L'Ora Blu (1974)",
                         ["L'Ora Blu (1974).mkv"]))
        asked = self._catalogue(monkeypatch, [self.TITLES])
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog())
        assert asked == ["L'Ora Blu"]


class TestALanguageWrittenAsAnEdition:
    """A file named in one language with the language written after the year,
    in a folder named in another. The suffix is an edition, and once it is
    written as one Plex collapses the files into one film with a picker."""

    TITLES = ("Le Seigneur de Val-Mont", "The Lord of Val-Mont")

    def _run(self, monkeypatch, tmp_path, files):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        _tree(tmp_path, ("Le Seigneur de Val-Mont (1961)", files))

        def fake_curl(url, params):
            kv = dict(params)
            if url == _BASE + "/search/movie":
                if not any(titlematch.equivalent(kv["query"], t)
                           for t in self.TITLES):
                    return json.dumps({"results": []})
                return json.dumps({"results": [
                    _row(1, self.TITLES[1], date="1961-06-23")]})
            return json.dumps({
                "external_ids": {"imdb_id": "tt0054824"},
                "alternative_titles": {"titles": [{"title": t}
                                                  for t in self.TITLES]},
                "release_dates": {"results": []}})

        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        ambiguous: list = []
        tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(),
                                ambiguous=ambiguous)
        return ambiguous

    def test_both_languages_become_editions_of_one_film(self, monkeypatch,
                                                         tmp_path):
        ambiguous = self._run(
            monkeypatch, tmp_path,
            ["The Lord of Val-Mont (1961) English.mkv",
             "Le Seigneur de Val-Mont (1961) French.mkv"])
        assert ambiguous == []
        tagged = tmp_path / "Le Seigneur de Val-Mont (1961) {imdb-tt0054824}"
        assert sorted(path.name for path in tagged.iterdir()) == [
            "Le Seigneur de Val-Mont (1961) {imdb-tt0054824} "
            "{edition-English}.mkv",
            "Le Seigneur de Val-Mont (1961) {imdb-tt0054824} "
            "{edition-French}.mkv"]

    def test_but_a_language_that_is_only_the_title_keeps_its_name(
            self, monkeypatch, tmp_path):
        """Nothing else distinguishes it, so rewriting the title would throw
        away which language the file is."""
        ambiguous = self._run(monkeypatch, tmp_path,
                              ["The Lord of Val-Mont (1961).mkv"])
        assert ambiguous == []
        tagged = tmp_path / "Le Seigneur de Val-Mont (1961) {imdb-tt0054824}"
        assert [path.name for path in tagged.iterdir()] == [
            "The Lord of Val-Mont (1961) {imdb-tt0054824}.mkv"]
